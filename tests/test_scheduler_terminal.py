# tests/test_scheduler_terminal.py
"""Pruebas de regresión de los estados terminales del Scheduler.

``_verificar_terminado_internal`` debe contar TIMEOUT, SALTADO y BLOQUEADO
(no solo COMPLETADO/ERROR/CANCELADO). Antes se omitían y la señal
``ejecucion_terminada`` nunca se emitía, dejando la UI colgada. También se
verifica que ``iniciar()`` reinicie cualquier estado terminal.
"""

import pytest

from core.agent import Agente, EstadoAgente
from core.scheduler import Scheduler


def _limpiar_scheduler(scheduler) -> None:
    try:
        scheduler.detener()
    except Exception:
        pass
    try:
        scheduler._executor.shutdown(wait=True, cancel_futures=True)
    except Exception:
        pass


@pytest.fixture
def scheduler():
    s = Scheduler(max_concurrent=2)
    yield s
    _limpiar_scheduler(s)


def _agregar(scheduler, **estados):
    for nombre, estado in estados.items():
        scheduler.agregar_agente(Agente(nombre=nombre, estado=estado))


class TestVerificarTerminado:
    def test_timeout_saltado_y_bloqueado_cuentan(self, scheduler):
        _agregar(
            scheduler,
            A1=EstadoAgente.TIMEOUT,
            A2=EstadoAgente.SALTADO,
            A3=EstadoAgente.BLOQUEADO,
        )
        emitido = []
        scheduler.ejecucion_terminada.connect(lambda: emitido.append(True))
        scheduler.ejecutando = True

        scheduler._verificar_terminado_internal()

        assert emitido == [True]
        assert scheduler.ejecutando is False
        assert scheduler._terminado_notificado is True

    def test_no_termina_con_agente_pendiente(self, scheduler):
        _agregar(scheduler, A1=EstadoAgente.COMPLETADO, A2=EstadoAgente.PENDIENTE)
        emitido = []
        scheduler.ejecucion_terminada.connect(lambda: emitido.append(True))
        scheduler.ejecutando = True

        scheduler._verificar_terminado_internal()

        assert emitido == []
        assert scheduler.ejecutando is True

    def test_sin_agentes_no_emite(self, scheduler):
        emitido = []
        scheduler.ejecucion_terminada.connect(lambda: emitido.append(True))
        scheduler._verificar_terminado_internal()
        assert emitido == []

    def test_no_emite_dos_veces(self, scheduler):
        _agregar(scheduler, A1=EstadoAgente.COMPLETADO)
        emitido = []
        scheduler.ejecucion_terminada.connect(lambda: emitido.append(True))
        scheduler.ejecutando = True

        scheduler._verificar_terminado_internal()
        scheduler._verificar_terminado_internal()

        assert emitido == [True]

    def test_estadisticas_incluyen_todos_los_terminales(self, scheduler):
        _agregar(
            scheduler,
            A1=EstadoAgente.TIMEOUT,
            A2=EstadoAgente.SALTADO,
            A3=EstadoAgente.BLOQUEADO,
            A4=EstadoAgente.COMPLETADO,
        )
        stats = scheduler._calcular_estadisticas_internal()
        assert stats["timeout"] == 1
        assert stats["saltados"] == 1
        assert stats["bloqueados"] == 1
        assert stats["completados"] == 1
        assert stats["total"] == 4


class TestIniciarReseteaTerminales:
    @pytest.mark.parametrize("estado", [
        EstadoAgente.COMPLETADO,
        EstadoAgente.ERROR,
        EstadoAgente.TIMEOUT,
        EstadoAgente.CANCELADO,
        EstadoAgente.SALTADO,
        EstadoAgente.BLOQUEADO,
    ])
    def test_iniciar_resetea_cualquier_terminal(self, scheduler, estado, monkeypatch):
        agente = Agente(nombre="A1", estado=estado, progreso=100, error="previo")
        scheduler.agregar_agente(agente)
        # Evitar que realmente lance subprocesos: solo probamos el reset.
        monkeypatch.setattr(scheduler, "_intentar_lanzar", lambda: None)

        scheduler.iniciar()

        assert agente.estado == EstadoAgente.PENDIENTE
        assert agente.progreso == 0
        assert agente.error == ""


class TestDetener:
    def test_detener_limpia_running(self, scheduler):
        agente = Agente(nombre="A1")
        scheduler.agregar_agente(agente)
        scheduler.running.add(agente.id)

        scheduler.detener()

        assert scheduler.running == set()
