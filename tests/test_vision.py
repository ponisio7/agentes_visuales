# tests/test_vision.py
"""Visión de página (H11, fase 1): mensajes multimodales y decisión validada.

No se llama a ningún modelo real: el LLM se sustituye por un doble. No se
ejecuta ninguna acción de Playwright: la decisión se valida y se devuelve.
"""
import json

import pytest

from core.vision import (
    ACCIONES_PERMITIDAS,
    codificar_imagen,
    construir_mensajes_multimodales,
    decidir_accion_desde_captura,
    vision_habilitada,
)

PNG_1x1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


class _LLMFalso:
    disponible = True

    def __init__(self, respuesta: str):
        self._respuesta = respuesta
        self.llamadas = []

    def completar(self, mensajes, **kwargs):
        self.llamadas.append((mensajes, kwargs))

        class _R:
            contenido = self._respuesta

        return _R()


@pytest.fixture(autouse=True)
def _vision_activa(monkeypatch):
    monkeypatch.setenv("AGENTES_VISION_HABILITADA", "1")


# ============================================================
# KILL SWITCH Y CODIFICACIÓN
# ============================================================

def test_vision_deshabilitada_por_defecto(monkeypatch):
    monkeypatch.delenv("AGENTES_VISION_HABILITADA", raising=False)
    assert vision_habilitada() is False

    llm = _LLMFalso('{"accion": null}')
    assert decidir_accion_desde_captura(llm, PNG_1x1, "pulsa el botón") is None
    assert llm.llamadas == []  # ni se consulta al modelo


def test_codificar_imagen_bytes_y_data_url():
    url = codificar_imagen(PNG_1x1)
    assert url.startswith("data:image/png;base64,")
    # Un data URL ya construido se respeta tal cual.
    assert codificar_imagen("data:image/png;base64,AAAA") == "data:image/png;base64,AAAA"


def test_construir_mensajes_multimodales():
    mensajes = construir_mensajes_multimodales(
        "describe", [PNG_1x1], system_prompt="eres un agente"
    )
    assert mensajes[0]["role"] == "system"
    contenido = mensajes[1]["content"]
    assert contenido[0]["type"] == "text"
    assert contenido[1]["type"] == "image_url"
    assert contenido[1]["image_url"]["url"].startswith("data:image/png;base64,")


# ============================================================
# DECISIÓN VALIDADA
# ============================================================

def test_decision_valida_se_devuelve():
    llm = _LLMFalso(json.dumps({
        "razon": "hay un botón de aceptar",
        "accion": {"tipo": "click", "selector": "#aceptar"},
    }))
    decision = decidir_accion_desde_captura(llm, PNG_1x1, "acepta las cookies")

    assert decision is not None
    assert decision["accion"] == {"tipo": "click", "selector": "#aceptar"}
    assert "botón" in decision["razon"]


def test_decision_null_cuando_ya_esta_resuelto():
    llm = _LLMFalso('{"razon": "ya se ve la información", "accion": null}')
    decision = decidir_accion_desde_captura(llm, PNG_1x1, "lee el precio")
    assert decision == {"razon": "ya se ve la información", "accion": None}


def test_accion_fuera_del_allowlist_se_rechaza():
    llm = _LLMFalso(json.dumps({
        "razon": "quiero borrar todo",
        "accion": {"tipo": "borrar_ficheros"},
    }))
    assert decidir_accion_desde_captura(llm, PNG_1x1, "x") is None


def test_accion_invalida_no_es_dict():
    llm = _LLMFalso('{"razon": "x", "accion": "click"}')
    assert decidir_accion_desde_captura(llm, PNG_1x1, "x") is None


def test_json_invalido_devuelve_none():
    llm = _LLMFalso("esto no es json")
    assert decidir_accion_desde_captura(llm, PNG_1x1, "x") is None


def test_error_del_modelo_no_rompe():
    class _LLMRoto:
        disponible = True

        def completar(self, *a, **k):
            raise RuntimeError("modelo caído")

    assert decidir_accion_desde_captura(_LLMRoto(), PNG_1x1, "x") is None


def test_allowlist_por_defecto():
    assert "click" in ACCIONES_PERMITIDAS
    assert "navegar" in ACCIONES_PERMITIDAS


# ============================================================
# CLIENTE MULTIMODAL
# ============================================================

def test_llm_client_completar_multimodal(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-no-real")
    from core.llm_client import LLMClient

    capturado = {}

    class _Completions:
        def create(self, **kwargs):
            capturado.update(kwargs)

            class _Msg:
                content = "ok"
                reasoning_content = None

            class _Choice:
                message = _Msg()
                finish_reason = "stop"

            class _Resp:
                choices = [_Choice()]
                usage = None

            return _Resp()

    class _Chat:
        completions = _Completions()

    class _Cliente:
        chat = _Chat()

    cliente = LLMClient(api_key="sk-test-no-real")
    cliente._client = _Cliente()

    cliente.completar_multimodal("mira esto", [PNG_1x1], max_tokens=100)

    mensajes = capturado["messages"]
    contenido = mensajes[-1]["content"]
    assert any(parte["type"] == "image_url" for parte in contenido)
    assert capturado["max_tokens"] == 100
