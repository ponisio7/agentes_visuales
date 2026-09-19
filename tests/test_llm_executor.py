# tests/test_llm_executor.py
"""
Tests del LLMExecutor: troceado de prompts grandes, fusión de respuestas y
detección de respuestas inutilizables.

No llaman a la API: se sustituyen `openai.OpenAI` y `LLMClient` por dobles.
"""

from types import SimpleNamespace

import pytest

from core.agent import Agente, TipoAgente
from core.executors import llm_executor
from core.executors.llm_executor import LLMExecutor, fusionar_json, json_util


class _Respuesta:
    def __init__(self, contenido, finish_reason="stop"):
        self.choices = [SimpleNamespace(
            message=SimpleNamespace(content=contenido, reasoning_content=None),
            finish_reason=finish_reason,
        )]
        self.usage = SimpleNamespace(prompt_tokens=10, completion_tokens=5, total_tokens=15)


class _Completions:
    def __init__(self, estado):
        self.estado = estado

    def create(self, **kwargs):
        self.estado.setdefault("mensajes", []).append(kwargs.get("messages"))
        idx = len(self.estado["mensajes"]) - 1
        respuestas = self.estado["respuestas"]
        contenido = respuestas[min(idx, len(respuestas) - 1)]
        return _Respuesta(contenido, self.estado.get("finish_reason", "stop"))


class _OpenAI:
    def __init__(self, estado):
        self.estado = estado

    @property
    def chat(self):
        return SimpleNamespace(completions=_Completions(self.estado))


@pytest.fixture
def api_falsa(monkeypatch):
    estado = {"respuestas": [""], "finish_reason": "stop"}
    import openai
    from core import llm_client as modulo_cliente

    monkeypatch.setattr(openai, "OpenAI", lambda **kw: _OpenAI(estado))
    monkeypatch.setattr(
        modulo_cliente, "LLMClient",
        lambda: SimpleNamespace(api_key="clave-falsa", base_url="http://falso"),
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
