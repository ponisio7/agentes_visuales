"""Regresión 001 — el evaluador LLM nunca se queda sin cliente en silencio.

Bug 1.14 del ROADMAP (punto 3.2):

    Evaluador no disponible: 'NoneType' object has no attribute 'chat'

``LearningEngine`` es un singleton perezoso y ``obtener_learning_engine()``
tiene ``llm_client=None`` por defecto. Tres rutas reales lo llamaban sin
cliente (``core/problem_solver/solver.py:125``, ``:137`` y
``core/scheduler.py:832``), de modo que el evaluador se quedaba sin LLM para
todo el proceso y el aprendizaje por refuerzo se degradaba **en silencio**:
score neutro 0.5 y un WARNING críptico.

Estos tests fijan el comportamiento nuevo:
  1. Si falta el cliente, se resuelve el compartido del proceso.
  2. Si no se puede resolver, el fallo es un ERROR explícito y accionable,
     no el críptico ``NoneType``.
  3. El camino feliz (cliente inyectado) no cambia.
"""
from __future__ import annotations

import logging

import pytest

from learning.engine import LearningEngine
from learning.reward_llm import (
    MOTIVO_SIN_CLIENTE,
    EvaluadorLLM,
    resolver_cliente_compartido,
)

JSON_BUENO = '{"score": 0.9, "justificacion": "objetivo cumplido"}'


class _ClienteFalso:
    """Cliente LLM mínimo: registra las llamadas y devuelve un JSON fijo."""

    def __init__(self, respuesta: str = JSON_BUENO):
        self.respuesta = respuesta
        self.llamadas: list[dict] = []

    def chat(self, **kwargs):
        self.llamadas.append(kwargs)
        return self.respuesta


@pytest.fixture
def cliente_disponible(monkeypatch):
    """Simula que el cliente compartido del proceso SÍ se puede resolver."""
    falso = _ClienteFalso()
    monkeypatch.setattr(
        "core.llm_client.obtener_llm_client_compartido", lambda *a, **k: falso
    )
    return falso


@pytest.fixture
def cliente_no_disponible(monkeypatch):
    """Simula que el cliente compartido NO se puede resolver (sin API key, etc.)."""

    def _explota(*a, **k):
        raise RuntimeError("DEEPSEEK_API_KEY no configurada")

    monkeypatch.setattr(
        "core.llm_client.obtener_llm_client_compartido", _explota
    )


# ---------------------------------------------------------------------------
# 1. Resolución perezosa del cliente
# ---------------------------------------------------------------------------

def test_evaluador_sin_cliente_resuelve_el_compartido(cliente_disponible):
    evaluador = EvaluadorLLM(None)

    evaluacion = evaluador.evaluar("crear saludo.txt", "saludo.txt creado")

    assert evaluacion.score == pytest.approx(0.9)
    assert evaluador.llm_client is cliente_disponible
    assert len(cliente_disponible.llamadas) == 1


def test_set_llm_client_inyecta_el_cliente():
    falso = _ClienteFalso()
    evaluador = EvaluadorLLM(None)

    evaluador.set_llm_client(falso)

    assert evaluador.llm_client is falso
    assert evaluador.evaluar("obj", "res").score == pytest.approx(0.9)


def test_resolver_cliente_compartido_devuelve_none_si_falla(cliente_no_disponible):
    assert resolver_cliente_compartido() is None


# ---------------------------------------------------------------------------
# 2. El fallo de configuración es VISIBLE, no silencioso
# ---------------------------------------------------------------------------

def test_evaluador_sin_cliente_loguea_error_accionable(
    cliente_no_disponible, caplog
):
    caplog.set_level(logging.ERROR, logger="learning.reward_llm")
    evaluador = EvaluadorLLM(None)

    evaluacion = evaluador.evaluar("crear saludo.txt", "nada")

    assert evaluacion.score == 0.5
    assert evaluacion.justificacion == MOTIVO_SIN_CLIENTE

    errores = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errores, "el fallo de configuración debe registrarse como ERROR"
    assert "SIN CLIENTE" in caplog.text
    # El mensaje accionable sustituye al críptico de NoneType.
    assert "set_llm_client" in caplog.text


def test_evaluador_sin_cliente_no_reproduce_el_error_noneType(
    cliente_no_disponible, caplog
):
    """El síntoma original era este texto exacto; ya no debe aparecer."""
    caplog.set_level(logging.DEBUG, logger="learning.reward_llm")
    EvaluadorLLM(None).evaluar("obj", "res")

    assert "NoneType" not in caplog.text
    assert "has no attribute 'chat'" not in caplog.text


def test_evaluador_sin_cliente_no_llama_a_chat(cliente_no_disponible):
    """Sin cliente no se intenta la llamada: se falla antes y con claridad."""
    evaluador = EvaluadorLLM(None)

    evaluador.evaluar("obj", "res")

    assert evaluador.llm_client is None


# ---------------------------------------------------------------------------
# 3. LearningEngine resuelve el cliente al construirse
# ---------------------------------------------------------------------------

def _construir_engine(tmp_path, llm_client):
    return LearningEngine(
        db_path=str(tmp_path / "historial.db"),
        llm_client=llm_client,
        ruta_modelos=str(tmp_path / "modelos"),
    )


def test_learning_engine_sin_cliente_resuelve_el_compartido(
    tmp_path, cliente_disponible
):
    engine = _construir_engine(tmp_path, llm_client=None)

    assert engine.llm_client is cliente_disponible
    assert engine.evaluador.llm_client is cliente_disponible


def test_learning_engine_sin_cliente_loguea_error(
    tmp_path, cliente_no_disponible, caplog
):
    caplog.set_level(logging.ERROR, logger="learning.engine")

    engine = _construir_engine(tmp_path, llm_client=None)

    assert engine.llm_client is None
    assert "SIN CLIENTE LLM" in caplog.text


def test_learning_engine_respeta_el_cliente_inyectado(tmp_path):
    """Un cliente explícito nunca debe ser sustituido por el compartido."""
    explicito = _ClienteFalso()
    engine = _construir_engine(tmp_path, llm_client=explicito)

    assert engine.llm_client is explicito
    assert engine.evaluador.llm_client is explicito


# ---------------------------------------------------------------------------
# 4. El camino feliz no cambia
# ---------------------------------------------------------------------------

def test_evaluador_con_cliente_evalua_con_thinking_desactivado():
    falso = _ClienteFalso()
    evaluador = EvaluadorLLM(falso, modelo="modelo-x")

    evaluacion = evaluador.evaluar("obj", "res", traza="paso 1")

    assert evaluacion.score == pytest.approx(0.9)
    assert evaluacion.modelo == "modelo-x"
    llamada = falso.llamadas[0]
    assert llamada["thinking_enabled"] is False
    assert llamada["model"] == "modelo-x"
