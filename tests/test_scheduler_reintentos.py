# tests/test_scheduler_reintentos.py
"""Regresiones de la ruta de reintento y del cierre del worker del scheduler.

Hallazgos de la auditoría v3.1 (informe H1 y H5):

* **H1a**: el ``finally`` del worker borraba ``_tokens_activos[agente.id]``
  sin comprobar que el token fuera el suyo. En la ruta de reintento el mismo
  agente puede tener ya otro worker con un token nuevo, y el viejo se lo
  llevaba por delante: el worker vivo quedaba fuera del mapa de cancelación.
* **H1b**: ``razon`` solo se asignaba si el token estaba cancelado. Si el
  agente estaba en ``_cancelados`` sin que el token lo estuviera, FASE 5
  lanzaba ``UnboundLocalError`` en el hilo worker (fallo silencioso: nadie
  consulta el Future del ThreadPoolExecutor).
* **H5**: el reintento despachado por la señal encolada hacía ``submit`` sin
  pasar el agente a ``LISTO`` ni sumarlo a ``running``; ``_ejecutar_agente``
  abortaba con "No se puede ejecutar" porque ``EN_COLA → EJECUTANDO`` no es
  una arista válida, y el reintento se perdía.
"""
import pytest

from core.agent import Agente, EstadoAgente
from core.scheduler import Scheduler


@pytest.fixture
def scheduler_uno():
    scheduler = Scheduler(max_concurrent=1)
    agente = Agente(nombre="A1", duracion=0.0, codigo_python="resultado = {'ok': True}")
    scheduler.agregar_agentes([agente])
    scheduler.resolver_dependencias()
    yield scheduler, agente
    try:
        scheduler.detener()
    except Exception:
        pass
    scheduler._executor.shutdown(wait=True, cancel_futures=True)


class _ExecutorFalso:
    """Sustituye a ``AgentExecutor`` para no lanzar el sandbox real."""

    def __init__(self, durante=None, exito=True):
        self.durante = durante
        self.exito = exito
        self.llamadas = 0

    def ejecutar(self, agente, contexto, cancellation_token=None):
        self.llamadas += 1
        if self.durante is not None:
            self.durante(agente, cancellation_token)
        return self.exito, "ok", {"ok": True}


def _parchear_executor(monkeypatch, fake):
    import core.executors as exec_mod

    monkeypatch.setattr(exec_mod, "AgentExecutor", fake)


def _preparar(agente, scheduler):
    agente.estado = EstadoAgente.LISTO
    scheduler.running.add(agente.id)


class TestTokenDelWorker:
    def test_no_borra_el_token_de_un_reintento_posterior(self, scheduler_uno, monkeypatch):
        """H1a: el token de un worker más reciente no debe borrarse."""
        scheduler, agente = scheduler_uno
        token_nuevo = scheduler._gestor_cancelacion.crear_token({"agente_id": agente.id})

        def durante(ag, _token):
            # Simula que el reintento ya lanzó otro worker con token nuevo
            # mientras este worker todavía está en FASE 4.
            scheduler._tokens_activos[ag.id] = token_nuevo

        _parchear_executor(monkeypatch, _ExecutorFalso(durante=durante))
        _preparar(agente, scheduler)

        scheduler._ejecutar_agente(agente)

        assert scheduler._tokens_activos.get(agente.id) is token_nuevo, (
            "el worker saliente borró el token de un reintento posterior"
        )

    def test_cancelado_sin_token_cancelado_no_revienta(self, scheduler_uno, monkeypatch):
        """H1b: FASE 5 no debe lanzar UnboundLocalError por ``razon``."""
        scheduler, agente = scheduler_uno

        def durante(ag, _token):
            # El usuario pulsa Detener: se marca cancelado pero el token no
            # llega a cancelarse (ventana de carrera).
            scheduler._cancelados.add(ag.id)

        _parchear_executor(monkeypatch, _ExecutorFalso(durante=durante))
        _preparar(agente, scheduler)

        # Sin el arreglo esto lanzaba UnboundLocalError dentro del worker.
        scheduler._ejecutar_agente(agente)

        assert agente.estado == EstadoAgente.CANCELADO
        assert agente.id not in scheduler._cancelados


class TestReintentoEncolado:
    def test_pasa_a_listo_y_suma_a_running(self, scheduler_uno, monkeypatch):
        """H5: el reintento encolado debe ejecutarse de verdad."""
        scheduler, agente = scheduler_uno
        agente.estado = EstadoAgente.EN_COLA
        lanzados = []

        monkeypatch.setattr(
            scheduler, "_ejecutar_agente", lambda ag, ctx=None: lanzados.append(ag.id)
        )

        scheduler._on_reintentar_agente(agente.id, {})
        scheduler._executor.shutdown(wait=True, cancel_futures=True)

        assert lanzados == [agente.id]
        assert agente.estado == EstadoAgente.LISTO
        assert agente.id in scheduler.running

    def test_ignora_duplicado_si_ya_esta_en_running(self, scheduler_uno, monkeypatch):
        """La señal encolada no debe duplicar un worker ya lanzado."""
        scheduler, agente = scheduler_uno
        agente.estado = EstadoAgente.EN_COLA
        scheduler.running.add(agente.id)
        lanzados = []

        monkeypatch.setattr(
            scheduler, "_ejecutar_agente", lambda ag, ctx=None: lanzados.append(ag.id)
        )

        scheduler._on_reintentar_agente(agente.id, {})

        assert lanzados == []
