# tests/test_scheduler_plan_b_caracterizacion.py
"""
Tests de CARACTERIZACIÓN de ``Scheduler._intentar_plan_b`` (V4.0, punto 5).

Propósito: congelar el comportamiento **observable actual** del bloque más
acoplado del Scheduler antes de refactorizarlo. Si el refactor cambia algo,
estos tests fallan; si pasan antes y después, el refactor es una extracción
mecánica y no un cambio de comportamiento.

Cubren lo que los tests existentes (``test_scheduler_plan_b.py``,
``test_plan_b_adaptativo.py``) no fijaban:

  - guard de ``_detenido`` (no llama al LLM ni consume intento);
  - camino «sin plan»: bloqueo de todos los no terminales y fin de ejecución;
  - limpieza completa de estado en el camino de éxito;
  - política de reutilización: solo artefactos verificados y aceptados;
  - excepción del recovery: se libera el turno y se devuelve False;
  - contabilidad del intento: estrategia escalonada, traza y firmas fallidas;
  - qué recibe exactamente ``recovery.generar_plan_b``.

Ninguno de estos tests necesita LLM ni red: el recovery se inyecta.
"""
from __future__ import annotations

import pytest

from core.agent import Agente, EstadoAgente, TipoAgente
from core.plan_recovery import ESTRATEGIAS, firma_plan
from core.problem_solver.models import ExecutionPlan, StepPlan
from core.scheduler import Scheduler


# ------------------------------------------------------------
# Dobles
# ------------------------------------------------------------
class _PlanFalso:
    def __init__(self, agentes, pasos=None):
        self.agentes_generados = agentes
        self.pasos = pasos or []


class _RecoveryFalso:
    db_path = ""

    def __init__(self, plan=None, error: Exception | None = None):
        self._plan = plan
        self._error = error
        self.llamadas: list[dict] = []

    def generar_plan_b(self, **kwargs):
        self.llamadas.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._plan


def _agente(nombre: str, estado: EstadoAgente = EstadoAgente.PENDIENTE) -> Agente:
    a = Agente(nombre=nombre, tipo=TipoAgente.PYTHON, codigo_python="resultado = {}")
    a.estado = estado
    return a


@pytest.fixture()
def scheduler(monkeypatch):
    """Scheduler rápido con ``iniciar()`` neutralizado (no arranca hilos)."""
    s = Scheduler(max_concurrent=1)
    monkeypatch.setattr(s, "iniciar", lambda *a, **k: None)
    try:
        yield s
    finally:
        try:
            s.detener()
        except Exception:
            pass
        s._executor.shutdown(wait=True, cancel_futures=True)


def _contexto(s: Scheduler, plan_original: ExecutionPlan | None = None) -> None:
    s._problema_original = "problema de prueba"
    s._plan_original = plan_original or ExecutionPlan(
        problema_original="problema de prueba",
        pasos=[StepPlan(nombre="D", tipo_agente="Python")],
    )


# ------------------------------------------------------------
# 1. Guard de detenido
# ------------------------------------------------------------
def test_detenido_no_llama_al_recovery_ni_consume_intento(scheduler):
    _contexto(scheduler)
    fallido = _agente("D", EstadoAgente.ERROR)
    recovery = _RecoveryFalso(_PlanFalso([_agente("N")]))
    scheduler.recovery = recovery
    scheduler._plan_b_en_progreso = True   # como si _reclamar_plan_b lo hubiera dado
    scheduler._detenido = True

    assert scheduler._intentar_plan_b(fallido, "boom") is False

    assert recovery.llamadas == []
    assert scheduler._plan_b_en_progreso is False   # el turno se libera
    assert scheduler._plan_b_intentos == 0          # no se gasta intento
    assert scheduler._reparaciones_intentadas == []


# ------------------------------------------------------------
# 2. Camino «sin plan»: bloqueo total y fin de ejecución
# ------------------------------------------------------------
def test_sin_plan_bloquea_los_no_terminales_y_suelta_el_turno(scheduler):
    _contexto(scheduler)
    completado = _agente("A", EstadoAgente.COMPLETADO)
    completado.resultado = {"x": 1}
    pendiente = _agente("B", EstadoAgente.PENDIENTE)
    corriendo = _agente("C", EstadoAgente.EJECUTANDO)
    fallido = _agente("D", EstadoAgente.ERROR)

    for a in (completado, pendiente, corriendo, fallido):
        scheduler.agregar_agente(a)
    scheduler.running.add(corriendo.id)

    scheduler.recovery = _RecoveryFalso(None)      # el LLM no da plan

    assert scheduler._intentar_plan_b(fallido, "falló la aceptación") is False

    # El pendiente se bloquea...
    assert pendiente.estado == EstadoAgente.BLOQUEADO
    assert pendiente.progreso == 100
    assert pendiente.id in scheduler.completed
    assert "Plan B agotado" in pendiente.mensaje
    # ...el ya completado no se toca...
    assert completado.estado == EstadoAgente.COMPLETADO
    # ...y el que está corriendo se deja terminar.
    assert corriendo.estado == EstadoAgente.EJECUTANDO

    assert scheduler._plan_b_en_progreso is False
    assert scheduler._plan_b_intentos == 1


def test_sin_plan_registra_la_traza_del_intento(scheduler):
    _contexto(scheduler)
    fallido = _agente("D", EstadoAgente.ERROR)
    scheduler.agregar_agente(fallido)
    scheduler.recovery = _RecoveryFalso(None)

    scheduler._intentar_plan_b(fallido, "razón concreta")

    assert len(scheduler._reparaciones_intentadas) == 1
    traza = scheduler._reparaciones_intentadas[0]
    assert traza["intento"] == 1
    assert traza["estrategia"] == ESTRATEGIAS[0]
    assert traza["agente"] == "D"
    assert traza["error"] == "razón concreta"


# ------------------------------------------------------------
# 3. Camino de éxito: limpieza de estado y nuevo plan
# ------------------------------------------------------------
def test_exito_limpia_el_estado_y_actualiza_el_plan_original(scheduler):
    plan_viejo = ExecutionPlan(
        problema_original="problema de prueba",
        pasos=[StepPlan(nombre="D", tipo_agente="Python")],
    )
    _contexto(scheduler, plan_viejo)
    fallido = _agente("D", EstadoAgente.ERROR)
    scheduler.agregar_agente(fallido)

    # Estado sucio que el Plan B debe limpiar.
    scheduler.completed.add(fallido.id)
    scheduler.running.add("fantasma")
    scheduler._cancelados.add("fantasma")
    scheduler._loops_activos.add("x")
    scheduler._loop_items_procesados["x"] = 2
    scheduler._verificaciones["fantasma"] = {"aceptado": True}
    scheduler._ultima_aceptacion = {"aceptada": False}
    scheduler._terminado_notificado = True
    scheduler._tiempo_inicio_ejecucion = 123.0

    nuevo = _agente("N")
    plan_b = _PlanFalso([nuevo])
    recovery = _RecoveryFalso(plan_b)
    scheduler.recovery = recovery

    assert scheduler._intentar_plan_b(fallido, "boom") is True

    assert scheduler._plan_original is plan_b
    assert scheduler._plan_b_en_progreso is False
    assert scheduler._plan_b_intentos == 1
    assert list(scheduler.agentes.values()) == [nuevo]
    assert scheduler.completed == set()
    assert scheduler.running == set()
    assert scheduler._cancelados == set()
    assert scheduler._loops_activos == set()
    assert scheduler._loop_items_procesados == {}
    assert scheduler._verificaciones == {}
    assert scheduler._ultima_aceptacion is None
    assert scheduler._terminado_notificado is False
    assert scheduler._tiempo_inicio_ejecucion is None


def test_exito_pasa_el_contexto_correcto_al_recovery(scheduler):
    plan_viejo = ExecutionPlan(
        problema_original="problema de prueba",
        pasos=[StepPlan(nombre="D", tipo_agente="Python")],
    )
    _contexto(scheduler, plan_viejo)
    fallido = _agente("D", EstadoAgente.ERROR)
    scheduler.agregar_agente(fallido)
    recovery = _RecoveryFalso(_PlanFalso([_agente("N")]))
    scheduler.recovery = recovery

    scheduler._intentar_plan_b(fallido, "la razón del fallo")

    kwargs = recovery.llamadas[0]
    assert kwargs["problema_original"] == "problema de prueba"
    assert kwargs["plan_fallido"] is plan_viejo
    assert kwargs["agente_fallido"] is fallido
    assert kwargs["error"] == "la razón del fallo"
    assert kwargs["estrategia"] == ESTRATEGIAS[0]
    assert kwargs["intento"] == 1
    # La firma del plan que acaba de fallar entra en el conjunto anti-repetición.
    assert firma_plan(plan_viejo) in kwargs["firmas_fallidas"]
    assert firma_plan(plan_viejo) in scheduler._firmas_plan_fallidas


def test_la_estrategia_escala_con_los_intentos_previos(scheduler):
    _contexto(scheduler)
    fallido = _agente("D", EstadoAgente.ERROR)
    scheduler.agregar_agente(fallido)
    recovery = _RecoveryFalso(_PlanFalso([_agente("N")]))
    scheduler.recovery = recovery
    scheduler._plan_b_intentos = 1          # ya se gastó un intento

    scheduler._intentar_plan_b(fallido, "boom")

    assert recovery.llamadas[0]["intento"] == 2
    assert recovery.llamadas[0]["estrategia"] == ESTRATEGIAS[1]


# ------------------------------------------------------------
# 4. Política de reutilización
# ------------------------------------------------------------
def test_solo_se_reutilizan_artefactos_verificados_y_aceptados(scheduler):
    _contexto(scheduler)
    fallido = _agente("D", EstadoAgente.ERROR)
    scheduler.agregar_agente(fallido)

    bueno = _agente("A", EstadoAgente.COMPLETADO)
    bueno.resultado = {"ok": 1}
    rechazado = _agente("C", EstadoAgente.COMPLETADO)
    rechazado.resultado = {"malo": 2}
    sin_resultado = _agente("S", EstadoAgente.COMPLETADO)
    sin_resultado.resultado = None

    for a in (bueno, rechazado, sin_resultado):
        scheduler.agregar_agente(a)
    scheduler._verificaciones[bueno.id] = {"aceptado": True, "motivos": []}
    scheduler._verificaciones[rechazado.id] = {"aceptado": False, "motivos": ["no cumple"]}

    # El plan B repite los tres por nombre/código (misma firma de agente).
    gemelo_bueno = _agente("A")
    gemelo_rechazado = _agente("C")
    gemelo_sin_resultado = _agente("S")
    recovery = _RecoveryFalso(
        _PlanFalso([gemelo_bueno, gemelo_rechazado, gemelo_sin_resultado])
    )
    scheduler.recovery = recovery

    assert scheduler._intentar_plan_b(fallido, "boom") is True

    # El aceptado se reutiliza con su resultado y su verificación.
    assert gemelo_bueno.estado == EstadoAgente.COMPLETADO
    assert gemelo_bueno.resultado == {"ok": 1}
    assert "reutilizado" in gemelo_bueno.mensaje
    assert scheduler._verificaciones[gemelo_bueno.id]["aceptado"] is True

    # El rechazado NO: su artefacto no pasó el contrato.
    assert gemelo_rechazado.estado == EstadoAgente.PENDIENTE
    assert gemelo_rechazado.resultado is None

    # El que no tiene resultado tampoco.
    assert gemelo_sin_resultado.estado == EstadoAgente.PENDIENTE


# ------------------------------------------------------------
# 5. Robustez
# ------------------------------------------------------------
def test_excepcion_del_recovery_libera_el_turno_y_devuelve_false(scheduler):
    _contexto(scheduler)
    fallido = _agente("D", EstadoAgente.ERROR)
    scheduler.agregar_agente(fallido)
    scheduler.recovery = _RecoveryFalso(error=RuntimeError("el LLM explotó"))

    assert scheduler._intentar_plan_b(fallido, "boom") is False
    assert scheduler._plan_b_en_progreso is False
    assert scheduler._plan_b_intentos == 1     # se incrementa antes de la llamada
    # El agente no se bloquea: la excepción no pasa por el camino «sin plan».
    assert fallido.estado == EstadoAgente.ERROR


def test_recovery_sin_atributo_db_path_no_rompe(scheduler):
    """``getattr(self.recovery, 'db_path', '')`` es tolerante."""
    _contexto(scheduler)
    fallido = _agente("D", EstadoAgente.ERROR)
    scheduler.agregar_agente(fallido)

    class _RecoverySinDb:
        def __init__(self):
            self.llamadas = []

        def generar_plan_b(self, **kwargs):
            self.llamadas.append(kwargs)
            return None

    scheduler.recovery = _RecoverySinDb()

    assert scheduler._intentar_plan_b(fallido, "boom") is False
