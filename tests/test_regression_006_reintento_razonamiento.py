"""Regresión 006 — reintento correctivo cuando el LLM solo devuelve razonamiento.

Bug 1.9 del ROADMAP (punto 3.9):

    ``deepseek-v4-flash`` gastaba el presupuesto de tokens "pensando" y
    devolvía ``finish_reason`` sin contenido final. El executor lo **detectaba**
    (``logger.warning`` en ``_llamar_una_vez``), pero no hacía nada: el paso
    fallaba y bloqueaba en cascada a sus dependientes.

El arreglo reintenta **una vez** y no repitiendo lo mismo: sube ``max_tokens``,
desactiva ``thinking`` y añade una instrucción explícita de responder ya.
"""
from __future__ import annotations

import logging

import pytest

from core.executors.llm_executor import MIN_TOKENS_SEGUROS, LLMExecutor


class _AgenteFalso:
    nombre = "Resumir"
    progreso = 0
    mensaje = ""


SALIDA_SOLO_RAZONAMIENTO = (
    False,
    "⚠️ LLM solo devolvió razonamiento (no respuesta final).",
    {"error": "only_reasoning", "razonamiento_preview": "We need to..."},
)
SALIDA_OK = (True, "", {"respuesta": "informe listo", "tokens": {"total": 10}})


@pytest.fixture
def llamadas(monkeypatch):
    """Registra los kwargs de cada llamada y devuelve las respuestas dadas."""
    registro: list[dict] = []

    def _instalar(respuestas):
        pendientes = list(respuestas)

        def _falsa(**kwargs):
            registro.append(kwargs)
            if pendientes:
                return pendientes.pop(0)
            return SALIDA_OK

        monkeypatch.setattr(LLMExecutor, "_llamar_una_vez", staticmethod(_falsa))
        return registro

    return _instalar


def _kwargs_base(**extra):
    base = dict(
        agente=_AgenteFalso(),
        llm=object(),
        modelo="deepseek-v4-flash",
        contenido_usuario="resume esto",
        contexto={},
        se_sustituyo_algo=False,
        temperatura=0.0,
        max_tokens=500,
        reasoning_effort="high",
        thinking_enabled=True,
        cancellation_token=None,
    )
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# 1. El reintento ocurre y cambia los parámetros
# ---------------------------------------------------------------------------

def test_reintenta_una_vez_tras_solo_razonamiento(llamadas, caplog):
    registro = llamadas([SALIDA_SOLO_RAZONAMIENTO, SALIDA_OK])
    caplog.set_level(logging.WARNING, logger="core.executors.llm_executor")

    ok, mensaje, datos = LLMExecutor._llamar_con_reintento_correctivo(**_kwargs_base())

    assert ok is True
    assert datos["respuesta"] == "informe listo"
    assert len(registro) == 2, "debe reintentar exactamente una vez"
    assert "solo devolvió razonamiento" in caplog.text


def test_el_reintento_desactiva_thinking_y_sube_max_tokens(llamadas):
    registro = llamadas([SALIDA_SOLO_RAZONAMIENTO, SALIDA_OK])

    LLMExecutor._llamar_con_reintento_correctivo(**_kwargs_base(max_tokens=500))

    primero, segundo = registro
    assert primero["thinking_enabled"] is True
    assert segundo["thinking_enabled"] is False
    assert segundo["max_tokens"] > primero["max_tokens"]
    assert segundo["max_tokens"] >= MIN_TOKENS_SEGUROS * 2
    assert segundo["reasoning_effort"] == "low"


def test_el_reintento_añade_instruccion_correctiva(llamadas):
    registro = llamadas([SALIDA_SOLO_RAZONAMIENTO, SALIDA_OK])

    LLMExecutor._llamar_con_reintento_correctivo(**_kwargs_base())

    original, reintento = registro
    assert original["contenido_usuario"] == "resume esto"
    assert "resume esto" in reintento["contenido_usuario"]
    assert "DIRECTAMENTE" in reintento["contenido_usuario"]


def test_marca_que_hubo_reintento(llamadas):
    llamadas([SALIDA_SOLO_RAZONAMIENTO, SALIDA_OK])

    _, _, datos = LLMExecutor._llamar_con_reintento_correctivo(**_kwargs_base())

    assert datos["reintento_por_razonamiento"] is True


# ---------------------------------------------------------------------------
# 2. No se reintenta lo que no lo necesita
# ---------------------------------------------------------------------------

def test_exito_directo_no_reintenta(llamadas):
    registro = llamadas([SALIDA_OK])

    ok, _, _ = LLMExecutor._llamar_con_reintento_correctivo(**_kwargs_base())

    assert ok is True
    assert len(registro) == 1


def test_otro_error_no_se_reintenta(llamadas):
    """Solo el 'solo razonamiento' tiene reintento: el resto no se reitera."""
    otro = (False, "sin API key", {"error": "missing_api_key"})
    registro = llamadas([otro])

    ok, mensaje, datos = LLMExecutor._llamar_con_reintento_correctivo(**_kwargs_base())

    assert ok is False
    assert datos["error"] == "missing_api_key"
    assert len(registro) == 1


# ---------------------------------------------------------------------------
# 3. Si el reintento vuelve a fallar, no hay bucle
# ---------------------------------------------------------------------------

def test_si_el_reintento_tambien_falla_no_hay_bucle(llamadas):
    registro = llamadas([SALIDA_SOLO_RAZONAMIENTO, SALIDA_SOLO_RAZONAMIENTO])

    ok, mensaje, datos = LLMExecutor._llamar_con_reintento_correctivo(**_kwargs_base())

    assert ok is False
    assert datos["error"] == "only_reasoning"
    assert len(registro) == 2, "como mucho dos intentos"
    assert "razonamiento" in mensaje
