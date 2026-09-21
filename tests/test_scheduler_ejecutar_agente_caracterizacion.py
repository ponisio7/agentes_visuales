# tests/test_scheduler_ejecutar_agente_caracterizacion.py
"""
Tests de CARACTERIZACIÓN de ``Scheduler._ejecutar_agente`` (ROADMAP, punto 4.1).

Propósito: congelar el comportamiento **observable actual** del método más
grande del Scheduler (380 líneas, 7 fases) antes de refactorizarlo. Si la
extracción cambia algo, estos tests fallan; si pasan antes y después, el
refactor es mecánico y no un cambio de comportamiento.

Se aísla del sandbox sustituyendo ``AgentExecutor.ejecutar`` por un doble que
devuelve el resultado que cada caso necesita —siempre con ``monkeypatch``, para
no filtrar el parche a otros tests—: así se fijan las ramas de la máquina de
estados (éxito, error, reintento, timeout, cancelación, saltado, aceptación
fallida) sin depender de subprocesos ni de red.

Los tests existentes (``test_scheduler_aceptacion``, ``test_scheduler_plan_b``,
``test_flujo_e2e_aceptacion``…) siguen siendo la red principal; esto fija las
ramas que el refactor va a mover de sitio.
"""
from __future__ import annotations

import logging

import pytest

from core.agent import Agente, EstadoAgente, TipoAgente
from core.scheduler import Scheduler

LOG = logging.getLogger("test.caracterizacion.ejecutar_agente")
LOG.addHandler(logging.NullHandler())


# ------------------------------------------------------------
# Dobles
# ------------------------------------------------------------
class _VerificacionFalsa:
    aceptado = False

    def __init__(self, motivo="el artefacto no cumple el contrato"):
        self._motivo = motivo

    def motivo(self):
        return self._motivo

    def to_dict(self):
        return {"aceptado": False, "motivos": [self._motivo]}


def _instalar_executor(monkeypatch, funcion):
    monkeypatch.setattr(
        "core.executors.AgentExecutor.ejecutar", staticmethod(funcion)
    )


@pytest.fixture
def executor_ok(monkeypatch):
    _instalar_executor(monkeypatch, lambda *a, **k: (True, "ok", {"ok": True}))


@pytest.fixture
def executor_falla(monkeypatch):
    _instalar_executor(
        monkeypatch, lambda *a, **k: (False, "explotó", {})
    )


def _ejecutar(scheduler: Scheduler, qapp, esperar, timeout: float = 60.0) -> None:
    try:
        scheduler.iniciar()
        assert esperar(
            lambda: scheduler._terminado_notificado, timeout=timeout, qapp=qapp
        ), "la ejecución no terminó a tiempo"
    finally:
        try:
            scheduler.detener()
        except Exception:
            pass


def _scheduler(agentes: list[Agente]) -> Scheduler:
    scheduler = Scheduler(max_concurrent=2)
    for agente in agentes:
        scheduler.agregar_agente(agente)
    scheduler.resolver_dependencias()
    return scheduler


def _python(nombre: str, **extra) -> Agente:
    return Agente(nombre=nombre, tipo=TipoAgente.PYTHON, **extra)


def _agente(scheduler: Scheduler, nombre: str) -> Agente:
    return scheduler.obtener_agente_por_nombre(nombre)


# ------------------------------------------------------------
# Rama de ÉXITO
# ------------------------------------------------------------
def test_exito_marca_completado_y_guarda_resultado(executor_ok, qapp, esperar):
    scheduler = _scheduler([_python("A")])

    _ejecutar(scheduler, qapp, esperar)

    agente = _agente(scheduler, "A")
    assert agente.estado == EstadoAgente.COMPLETADO
    assert agente.resultado == {"ok": True}
    assert agente.progreso == 100
    assert agente.duracion >= 0
    assert agente.tiempo_inicio is not None and agente.tiempo_fin is not None


# ------------------------------------------------------------
# Rama de ERROR sin reintentos
# ------------------------------------------------------------
def test_fallo_sin_reintentos_marca_error(executor_falla, qapp, esperar):
    scheduler = _scheduler([_python("A", max_reintentos=0)])

    _ejecutar(scheduler, qapp, esperar)

    agente = _agente(scheduler, "A")
    assert agente.estado == EstadoAgente.ERROR
    assert agente.error == "explotó"
    assert agente.reintentos == 0
    assert agente.progreso == 100


# ------------------------------------------------------------
# Rama de TIMEOUT
# ------------------------------------------------------------
def test_timeout_se_clasifica_como_timeout(monkeypatch, qapp, esperar):
    _instalar_executor(monkeypatch, lambda *a, **k: (False, "Timeout tras 30s", {}))
    scheduler = _scheduler([_python("A", max_reintentos=0)])

    _ejecutar(scheduler, qapp, esperar)

    assert _agente(scheduler, "A").estado == EstadoAgente.TIMEOUT


# ------------------------------------------------------------
# Rama de REINTENTO
# ------------------------------------------------------------
def test_fallo_con_reintentos_disponibles_reintenta(monkeypatch, qapp, esperar):
    """Primer intento falla y el reintento acierta: ``reintentos == 1``."""
    llamadas: list[str] = []

    def _falso(agente, contexto=None, cancellation_token=None):
        llamadas.append(agente.nombre)
        if len(llamadas) == 1:
            return False, "fallo pasajero", {}
        return True, "ok", {"ok": True}

    _instalar_executor(monkeypatch, _falso)
    scheduler = _scheduler([_python("A", max_reintentos=1)])

    _ejecutar(scheduler, qapp, esperar)

    agente = _agente(scheduler, "A")
    assert agente.reintentos == 1
    assert agente.estado == EstadoAgente.COMPLETADO
    assert len(llamadas) == 2


def test_reintentos_agotados_marcan_error(executor_falla, qapp, esperar):
    scheduler = _scheduler([_python("A", max_reintentos=1)])

    _ejecutar(scheduler, qapp, esperar)

    agente = _agente(scheduler, "A")
    assert agente.estado == EstadoAgente.ERROR
    assert agente.reintentos == 1


# ------------------------------------------------------------
# Rama de BLOQUEO por dependencia fallida
# ------------------------------------------------------------
def test_dependencia_fallida_bloquea_al_dependiente(executor_falla, qapp, esperar):
    """Comportamiento REAL: el fallo bloquea al dependiente (BLOQUEADO).

    ``SALTADO`` es la otra rama (FASE 3) y es **defensiva**: solo se aplica si
    el dependiente llega a arrancar y descubre la dependencia rota. En el flujo
    normal ``_bloquear_dependientes`` se adelanta y lo deja en ``BLOQUEADO``;
    se comprobó que desactivar ese bloqueo no provoca ``SALTADO``, sino que la
    ejecución se queda colgada (el bloqueador es también quien finaliza a los
    dependientes). Por eso aquí se fija solo la rama observable, y el refactor
    debe preservar la otra por inspección.
    """
    scheduler = _scheduler([
        _python("A", max_reintentos=0),
        _python("B", dependencias_nombres=["A"], max_reintentos=0),
    ])

    _ejecutar(scheduler, qapp, esperar)

    assert _agente(scheduler, "A").estado == EstadoAgente.ERROR
    assert _agente(scheduler, "B").estado == EstadoAgente.BLOQUEADO


# ------------------------------------------------------------
# Rama de ACEPTACIÓN fallida (no reintenta)
# ------------------------------------------------------------
def test_aceptacion_fallida_es_error_final_sin_reintentos(
    executor_ok, monkeypatch, qapp, esperar
):
    monkeypatch.setattr(
        "core.verification.verificar_agente",
        lambda agente, resultado, **k: _VerificacionFalsa(),
    )
    scheduler = _scheduler([_python("A", max_reintentos=3)])

    _ejecutar(scheduler, qapp, esperar)

    agente = _agente(scheduler, "A")
    assert agente.estado == EstadoAgente.ERROR
    assert agente.reintentos == 0, "el contrato es determinista: no se reintenta"
    assert "Aceptación fallida" in (agente.error or "")


# ------------------------------------------------------------
# Cancelación y limpieza del token
# ------------------------------------------------------------
def test_cancelacion_durante_la_ejecucion_marca_cancelado(
    monkeypatch, qapp, esperar
):
    """El propio executor cancela su token: la FASE 5 debe verlo."""

    def _cancela(agente, contexto=None, cancellation_token=None):
        if cancellation_token is not None:
            cancellation_token.cancelar("cancelado en la prueba")
        return True, "ok", {"ok": True}

    _instalar_executor(monkeypatch, _cancela)
    scheduler = _scheduler([_python("A")])

    _ejecutar(scheduler, qapp, esperar)

    assert _agente(scheduler, "A").estado == EstadoAgente.CANCELADO


def test_el_token_de_cancelacion_se_limpia_al_terminar(executor_ok, qapp, esperar):
    scheduler = _scheduler([_python("A")])

    _ejecutar(scheduler, qapp, esperar)

    assert scheduler._tokens_activos == {}
