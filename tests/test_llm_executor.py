# tests/test_llm_executor.py
"""
Tests del LLMExecutor: troceado de prompts grandes, fusión de respuestas y
detección de respuestas inutilizables.

No llaman a la API: se sustituye ``obtener_llm_client_compartido`` por un
doble de ``LLMClient`` que implementa ``completar()``.
"""

from types import SimpleNamespace

import pytest

from core.agent import Agente, TipoAgente
from core.executors.llm_executor import LLMExecutor, fusionar_json, json_util
from core.llm_client import LLMResultado


class _LLMFalso:
    """Doble de ``LLMClient`` con la misma interfaz que usa el ejecutor.

    B1 (v3.3.0): el ejecutor no crea un ``OpenAI()`` por llamada; pide el
    cliente compartido y llama a ``LLMClient.completar()`` pasando
    ``reasoning_effort``/``thinking_enabled`` del agente. El doble se engancha
    a ese seam y registra mensajes y llamadas completas.
    """

    def __init__(self, estado):
        self.estado = estado
        self.api_key = "clave-falsa"
        self.base_url = "http://falso"
        self.reasoning_effort = "high"
        self.thinking_enabled = True

    def completar(self, mensajes, **kwargs):
        self.estado.setdefault("mensajes", []).append(mensajes)
        self.estado.setdefault("llamadas", []).append(kwargs)
        idx = len(self.estado["mensajes"]) - 1
        respuestas = self.estado["respuestas"]
        contenido = respuestas[min(idx, len(respuestas) - 1)]
        return LLMResultado(
            contenido=contenido,
            razonamiento=None,
            finish_reason=self.estado.get("finish_reason", "stop"),
            uso=SimpleNamespace(
                prompt_tokens=10, completion_tokens=5, total_tokens=15
            ),
            modelo=kwargs.get("model"),
        )


@pytest.fixture
def api_falsa(monkeypatch):
    estado = {"respuestas": [""], "finish_reason": "stop"}
    from core import llm_client as modulo_cliente

    monkeypatch.setattr(
        modulo_cliente, "obtener_llm_client_compartido",
        lambda *a, **kw: _LLMFalso(estado),
    )
    return estado


def _agente(prompt: str, **kwargs) -> Agente:
    base = dict(nombre="Traducir", tipo=TipoAgente.LLM, prompt_llm=prompt,
                modelo_llm="deepseek-v4-pro", max_tokens_llm=8000)
    base.update(kwargs)
    return Agente(**base)


def test_prompt_pequeno_una_sola_llamada(api_falsa):
    api_falsa["respuestas"] = ['{"resultado": "ok"}']

    ok, _, resultado = LLMExecutor.ejecutar(_agente("Traduce: {Dep.texto}"), {"Dep": {"texto": "hola"}})

    assert ok is True
    assert len(api_falsa["mensajes"]) == 1
    assert resultado["json"] == {"resultado": "ok"}
    assert "chunks" not in resultado


def test_prompt_grande_se_trocea_y_fusiona(api_falsa):
    api_falsa["respuestas"] = [
        '{"ucraniano": [{"url": "u1", "texto": "привіт"}], "farsi": "Sin contenido"}',
        '{"ucraniano": "Sin contenido", "farsi": [{"url": "f1", "texto": "سلام"}]}',
    ]
    contexto = {"A": {"datos": "x" * 15000}, "B": {"datos": "y" * 15000}}
    agente = _agente('Devuelve JSON. Ucraniano: {A.datos}. Farsi: {B.datos}.')

    ok, _, resultado = LLMExecutor.ejecutar(agente, contexto)

    assert ok is True
    assert len(api_falsa["mensajes"]) == 2
    assert resultado["chunks"] == 2
    assert resultado["json"]["ucraniano"] == [{"url": "u1", "texto": "привіт"}]
    assert resultado["json"]["farsi"] == [{"url": "f1", "texto": "سلام"}]
    # Cada llamada mantiene la instrucción completa
    for mensaje in api_falsa["mensajes"]:
        assert "Devuelve JSON" in mensaje[1]["content"]
    # Y en cada trozo se omite la otra dependencia
    assert "omitido" in api_falsa["mensajes"][0][1]["content"]
    assert resultado["tokens_uso"]["total"] == 30


def test_respuesta_plantilla_falla(api_falsa):
    """El modelo devuelve el ejemplo ecoado: no se puede usar."""
    api_falsa["respuestas"] = ['{"datos": [{"url": "...", "texto": "..."}]}']

    ok, mensaje, resultado = LLMExecutor.ejecutar(_agente("Devuelve JSON con los datos"), {})

    assert ok is False
    assert resultado["error"] == "json_placeholder"
    assert "plantilla" in mensaje


def test_sin_json_cuando_se_pide_falla(api_falsa):
    api_falsa["respuestas"] = ["We need answer in Spanish. Let's plan translation for each."]

    ok, mensaje, resultado = LLMExecutor.ejecutar(_agente("Devuelve un JSON con la traduccion"), {})

    assert ok is False
    assert resultado["error"] == "json_no_parseable"


def test_texto_libre_sin_json_no_falla(api_falsa):
    """Un paso que NO pide JSON puede devolver texto normal."""
    api_falsa["respuestas"] = ["Había una vez un dragón que perdió su fuego."]

    ok, _, resultado = LLMExecutor.ejecutar(_agente("Escribe un cuento corto"), {})

    assert ok is True
    assert resultado["json"] is None
    assert "dragón" in resultado["respuesta"]


def test_respuesta_cortada_por_max_tokens(api_falsa):
    api_falsa["respuestas"] = ["We need translate. Let's plan"]
    api_falsa["finish_reason"] = "length"

    ok, mensaje, resultado = LLMExecutor.ejecutar(_agente("Devuelve un JSON traducido"), {})

    assert ok is False
    assert resultado["error"] == "truncated_response"
    assert "max_tokens" in mensaje


def test_fusionar_json_prefiere_el_contenido_real():
    a = {"u": [{"url": "u1"}], "f": "Sin contenido"}
    b = {"u": "Sin contenido", "f": [{"url": "f1"}]}

    fusion = fusionar_json(a, b)

    assert fusion["u"] == [{"url": "u1"}]
    assert fusion["f"] == [{"url": "f1"}]


def test_fusionar_json_une_listas_sin_duplicados():
    fusion = fusionar_json([{"url": "a"}], [{"url": "a"}, {"url": "b"}])

    assert fusion == [{"url": "a"}, {"url": "b"}]


def test_json_util_detecta_plantillas():
    assert json_util(None) is False
    assert json_util({"a": "..."}) is False
    assert json_util({"a": []}) is False
    assert json_util({"a": [{"url": "http://x"}]}) is True


def test_prompt_grande_con_una_lista_se_trocea_por_items(api_falsa):
    api_falsa["respuestas"] = [
        '{"traducciones": [{"url": "u1", "texto": "uno"}]}',
        '{"traducciones": [{"url": "u2", "texto": "dos"}]}',
        '{"traducciones": [{"url": "u3", "texto": "tres"}]}',
    ]
    contexto = {"Dep": {"resultados": [
        {"url": f"u{i}", "texto": "x" * 9000} for i in range(3)
    ]}}
    agente = _agente("Traduce y devuelve JSON: {Dep.resultados}")

    ok, _, resultado = LLMExecutor.ejecutar(agente, contexto)

    assert ok is True
    assert len(api_falsa["mensajes"]) == 3
    assert resultado["chunks"] == 3
    assert [t["url"] for t in resultado["json"]["traducciones"]] == ["u1", "u2", "u3"]


def test_timeout_crece_con_el_prompt():
    from core.executors.llm_executor import timeout_para_prompt

    assert timeout_para_prompt("x" * 1000) == 60
    assert timeout_para_prompt("x" * 32000) > 60
    assert timeout_para_prompt("x" * 32000, SimpleNamespace(timeout_llm=200)) == 200


# ---------------------------------------------------------------------------
# B1 (v3.3.0): propagación de thinking/reasoning y detección de la TAREA
# ---------------------------------------------------------------------------

def test_b1_propaga_reasoning_y_thinking_del_agente(api_falsa):
    """La petición lleva los valores del AGENTE, no los del cliente (B1)."""
    api_falsa["respuestas"] = ["Había una vez..."]
    agente = _agente(
        "Escribe un cuento corto",
        reasoning_effort_llm="low",
        thinking_enabled_llm=False,
    )

    ok, _, _ = LLMExecutor.ejecutar(agente, {})

    assert ok is True
    llamada = api_falsa["llamadas"][0]
    assert llamada["reasoning_effort"] == "low"
    assert llamada["thinking_enabled"] is False


def test_b1_tarea_texto_plano_ignora_el_json_del_preambulo(api_falsa):
    """El preámbulo del builder menciona JSON; la TAREA es texto plano (B1).

    Antes, ``"json" in prompt`` forzaba el parseo JSON y la ejecución fallaba
    con "El LLM no devolvió JSON válido en la parte 1/1".
    """
    api_falsa["respuestas"] = ["hola"]

    agente = _agente(
        "INSTRUCCIONES CRÍTICAS:\n"
        "5. Devuelve la respuesta como JSON válido y COMPLETO (sin truncar).\n"
        "\n"
        "TAREA:\n"
        "Responde únicamente con la palabra 'hola'."
    )

    ok, _, resultado = LLMExecutor.ejecutar(agente, {})

    assert ok is True
    assert resultado["respuesta"] == "hola"
    assert resultado["json"] is None


def test_b1_tarea_pide_json():
    from core.executors.llm_executor import tarea_pide_json

    preambulo = (
        "INSTRUCCIONES CRÍTICAS:\n"
        "5. Devuelve la respuesta como JSON válido y COMPLETO (sin truncar).\n"
        "\n"
        "TAREA:\n"
    )
    assert tarea_pide_json(preambulo + "Responde solo con 'hola'.") is False
    assert tarea_pide_json(preambulo + "Devuelve un JSON con la lista.") is True
    # Sin marcador TAREA se analiza el prompt completo (sin cambios).
    assert tarea_pide_json("Devuelve un JSON") is True
    assert tarea_pide_json("Escribe un cuento") is False
    assert tarea_pide_json("") is False
