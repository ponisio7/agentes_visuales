# tests/test_scheduler_plan_b.py
"""Regresión de H2: el Plan B no debe ejecutar la llamada al LLM con el
lock del scheduler tomado.

``_ejecutar_agente`` (FASE 5) llamaba a ``_bloquear_dependientes`` dentro de
``with self._lock:`` y este, a su vez, a ``_intentar_plan_b``, que hace
``llm_client.chat(...)`` (red, ~15-20 s). Durante ese tiempo ``obtener_estadisticas()``
(el lock es el mismo) bloqueaba la UI y los demás workers se quedaban parados
al llegar a FASE 5.

El test sustituye ``recovery.generar_plan_b`` por uno que se queda dentro
hasta que el test lo libera, y comprueba que mientras tanto
``obtener_estadisticas()`` (que necesita el lock) responde.
"""
import threading
import time

import pytest

from core.agent import Agente, EstadoAgente
from core.scheduler import Scheduler


class _RecoveryLento:
    """``generar_plan_b`` que avisa de que entró y espera a que lo liberen."""

    def __init__(self):
        self.entro = threading.Event()
        self.liberar = threading.Event()

    def generar_plan_b(self, **kwargs):  # noqa: ARG002 - firma de la interfaz
        self.entro.set()
        self.liberar.wait(timeout=10)
        # Sin plan válido: se bloquean los no terminales y la ejecución acaba.
        return None


class _ExecutorFalla:
    """Sustituye a ``AgentExecutor`` para forzar el camino de error de FASE 5."""

    def ejecutar(self, *args, **kwargs):  # noqa: ARG002
        return False, "Error: fallo simulado", {}


@pytest.fixture
def scheduler_h2(monkeypatch):
    import core.executors as exec_mod

    monkeypatch.setattr(exec_mod, "AgentExecutor", _ExecutorFalla)

    scheduler = Scheduler(max_concurrent=2)
    yield scheduler
    try:
        scheduler.detener()
    except Exception:
        pass
    scheduler._executor.shutdown(wait=True, cancel_futures=True)


def test_plan_b_no_bloquea_el_lock_del_scheduler(scheduler_h2):
    scheduler = scheduler_h2
    recovery = _RecoveryLento()
    scheduler.set_contexto_plan_b(recovery, "problema de prueba", plan_original=None)

    agente = Agente(nombre="A1", duracion=0.0, codigo_python="resultado = {}")
    agente.max_reintentos = 0  # sin reintentos: el fallo llega a _bloquear_dependientes
    scheduler.agregar_agentes([agente])
    scheduler.resolver_dependencias()
    # ``_ejecutar_agente`` solo acepta LISTO → EJECUTANDO.
    agente.estado = EstadoAgente.LISTO
    scheduler.running.add(agente.id)

    # Ejecuta el worker a mano para controlar su ciclo de vida.
    worker = threading.Thread(
        target=scheduler._ejecutar_agente,
        args=(agente,),
        daemon=True,
    )
    worker.start()
    try:
        assert recovery.entro.wait(timeout=5), (
            "el worker no llegó a generar el Plan B"
        )

        # ``obtener_estadisticas`` toma el mismo lock que retenía FASE 5.
        # Se consulta desde otro hilo para no colgar el test si hay regresión.
        consultado = threading.Event()

        def consultar():
            scheduler.obtener_estadisticas()
            consultado.set()

        hilo = threading.Thread(target=consultar, daemon=True)
        hilo.start()
        hilo.join(timeout=1.0)
        bloqueado = not consultado.is_set()
    finally:
        # Pase lo que pase, no dejar el worker esperando 10 s.
        recovery.liberar.set()

    worker.join(timeout=5)

    assert not bloqueado, (
        "obtener_estadisticas() quedó bloqueado mientras el Plan B llamaba "
        "al LLM: la llamada de red se ejecuta con el lock del scheduler tomado"
    )
    assert not worker.is_alive(), "el worker no terminó tras liberar el Plan B"
    assert agente.estado == EstadoAgente.ERROR


def test_plan_b_reserva_una_sola_vez_por_agente(scheduler_h2):
    """La reserva del Plan B debe ser atómica: sin el lock que serializaba a
    los workers, dos fallos simultáneos no pueden lanzar dos Plan B."""
    scheduler = scheduler_h2
    recovery = _RecoveryLento()
    scheduler.set_contexto_plan_b(recovery, "problema de prueba", plan_original=None)

    agente = Agente(nombre="A1", duracion=0.0, codigo_python="resultado = {}")
    scheduler.agregar_agentes([agente])

    assert scheduler._reclamar_plan_b() is True
    # La segunda reclamación (otro worker) se rechaza mientras el primero
    # sigue dentro del LLM.
    assert scheduler._reclamar_plan_b() is False
    assert scheduler._plan_b_en_progreso is True
    recovery.liberar.set()


class _PlanBFalso:
    def __init__(self, agentes):
        self.agentes_generados = agentes


class _RecoveryConPlan:
    def __init__(self, plan):
        self.plan = plan

    def generar_plan_b(self, **kwargs):  # noqa: ARG002
        return self.plan


class _ExecutorOk:
    def ejecutar(self, *args, **kwargs):  # noqa: ARG002
        return True, "ok", {"ok": True}


def test_plan_b_exitoso_reemplaza_la_ejecucion(scheduler_h2, monkeypatch):
    """El camino de éxito del Plan B (``_bloquear_dependientes`` → True) debe
    seguir relanzando la ejecución con los agentes nuevos; FASE 5 retorna sin
    tocar el estado del plan anterior."""
    import core.executors as exec_mod

    monkeypatch.setattr(exec_mod, "AgentExecutor", _ExecutorOk)

    scheduler = scheduler_h2
    scheduler._max_intentos_plan_b = 1
    nuevo = Agente(nombre="B1", duracion=0.0, codigo_python="resultado = {}")
    scheduler.set_contexto_plan_b(
        _RecoveryConPlan(_PlanBFalso([nuevo])), "problema de prueba", None
    )

    viejo = Agente(nombre="A1", duracion=0.0, codigo_python="resultado = {}")
    scheduler.agregar_agentes([viejo])
    scheduler.resolver_dependencias()
    viejo.estado = EstadoAgente.EJECUTANDO

    assert scheduler._bloquear_dependientes(viejo.id, "Error: fallo") is True
    nombres = {a.nombre for a in scheduler.agentes.values()}
    assert nombres == {"B1"}, "el Plan B debe reemplazar a los agentes anteriores"

    for _ in range(100):
        if nuevo.estado == EstadoAgente.COMPLETADO:
            break
        time.sleep(0.05)
    assert nuevo.estado == EstadoAgente.COMPLETADO
