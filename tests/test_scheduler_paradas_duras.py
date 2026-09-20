# tests/test_scheduler_paradas_duras.py
"""Integración de las paradas duras en el Scheduler (V3.8-1).

Antes de V3.8, ``_bloquear_dependientes`` intentaba un Plan B ante cualquier
fallo terminal: una llamada al LLM (dinero y ~20 s) aunque el fallo no se
pudiera arreglar reescribiendo el plan. Ahora el ``RecoveryManager`` decide
primero y las paradas duras abortan sin gastar intento.

No hay red ni LLM real: ``recovery`` es un doble que cuenta llamadas.
"""
import pytest

from core.agent import Agente, EstadoAgente
from core.budget_manager import BudgetManager
from core.recovery_manager import API_KEY_MISSING, BUDGET_EXCEEDED, DEPENDENCY_MISSING
from core.scheduler import Scheduler


class _RecoveryEspia:
    """Doble de ``PlanRecovery`` que solo cuenta si le pidieron un Plan B."""

    db_path = ""

    def __init__(self, plan=None):
        self.plan = plan
        self.llamadas: list[dict] = []

    def generar_plan_b(self, **kwargs):
        self.llamadas.append(kwargs)
        return self.plan


@pytest.fixture
def scheduler_v38():
    scheduler = Scheduler(max_concurrent=2)
    yield scheduler
    try:
        scheduler.detener()
    except Exception:
        pass
    scheduler._executor.shutdown(wait=True, cancel_futures=True)


def _preparar(scheduler, recovery, nombre="A1"):
    scheduler.set_contexto_plan_b(recovery, "problema de prueba", None)
    agente = Agente(nombre=nombre, duracion=0.0, codigo_python="resultado = {}")
    agente.max_reintentos = 0
    scheduler.agregar_agentes([agente])
    scheduler.resolver_dependencias()
    agente.estado = EstadoAgente.EJECUTANDO
    return agente


# ============================================================
# Parada dura: no se gasta Plan B
# ============================================================

def test_parada_dura_no_llama_al_llm_ni_gasta_intento(scheduler_v38):
    recovery = _RecoveryEspia()
    agente = _preparar(scheduler_v38, recovery)

    lanzado = scheduler_v38._bloquear_dependientes(
        agente.id, "No se encontró DEEPSEEK_API_KEY"
    )

    assert lanzado is False
    assert recovery.llamadas == [], (
        "una parada dura no debe pedir un Plan B al LLM"
    )
    assert scheduler_v38._plan_b_intentos == 0, "no debe consumir intentos"
    assert scheduler_v38._parada_dura is not None
    assert scheduler_v38._parada_dura["codigo"] == API_KEY_MISSING


def test_parada_dura_bloquea_a_los_dependientes(scheduler_v38):
    recovery = _RecoveryEspia()
    scheduler = scheduler_v38
    scheduler.set_contexto_plan_b(recovery, "problema", None)

    productor = Agente(nombre="Productor", duracion=0.0, codigo_python="x = 1")
    consumidor = Agente(
        nombre="Consumidor",
        duracion=0.0,
        codigo_python="y = 2",
        dependencias_nombres=["Productor"],
    )
    productor.max_reintentos = 0
    consumidor.max_reintentos = 0
    scheduler.agregar_agentes([productor, consumidor])
    scheduler.resolver_dependencias()
    productor.estado = EstadoAgente.EJECUTANDO

    scheduler._bloquear_dependientes(productor.id, "Cliente LLM no disponible")

    assert consumidor.estado == EstadoAgente.BLOQUEADO
    assert recovery.llamadas == []


def test_parada_dura_se_expone_en_la_aceptacion(scheduler_v38):
    recovery = _RecoveryEspia()
    agente = _preparar(scheduler_v38, recovery)
    agente.estado = EstadoAgente.ERROR
    agente.mensaje = "No se encontró DEEPSEEK_API_KEY"

    scheduler_v38._bloquear_dependientes(agente.id, agente.mensaje)
    aceptacion = scheduler_v38.obtener_resultado_aceptacion()

    assert aceptacion["aceptada"] is False
    assert "parada_dura" in aceptacion
    assert aceptacion["parada_dura"]["codigo"] == API_KEY_MISSING
    assert aceptacion["parada_dura"]["motivo"]


def test_error_de_importacion_es_parada_dura(scheduler_v38):
    recovery = _RecoveryEspia()
    agente = _preparar(scheduler_v38, recovery)

    scheduler_v38._manejar_error_importacion(agente, ImportError("No module named 'playwright'"))

    assert scheduler_v38._parada_dura["codigo"] == DEPENDENCY_MISSING
    assert recovery.llamadas == []


# ============================================================
# Fallo recuperable: se sigue intentando el Plan B
# ============================================================

def test_fallo_recuperable_si_pide_plan_b(scheduler_v38):
    recovery = _RecoveryEspia(plan=None)
    agente = _preparar(scheduler_v38, recovery)

    lanzado = scheduler_v38._bloquear_dependientes(agente.id, "Error: fallo simulado")

    assert lanzado is False  # el doble no devuelve plan válido
    assert len(recovery.llamadas) == 1, "un fallo recuperable sí debe intentar Plan B"
    assert scheduler_v38._parada_dura is None


def test_fallo_de_aceptacion_en_runtime_si_pide_plan_b(scheduler_v38):
    """El fallo de aceptación es el caso de uso del Plan B: nunca parada dura."""
    recovery = _RecoveryEspia(plan=None)
    agente = _preparar(scheduler_v38, recovery)

    scheduler_v38._bloquear_dependientes(
        agente.id, "Aceptación fallida: ruta no permitida en el contrato"
    )

    assert len(recovery.llamadas) == 1
    assert scheduler_v38._parada_dura is None


# ============================================================
# El contexto de una ejecución nueva limpia la parada previa
# ============================================================

def test_set_contexto_limpia_la_parada_dura(scheduler_v38):
    recovery = _RecoveryEspia()
    agente = _preparar(scheduler_v38, recovery)
    scheduler_v38._bloquear_dependientes(agente.id, "Cliente LLM no disponible")
    assert scheduler_v38._parada_dura is not None

    scheduler_v38.set_contexto_plan_b(_RecoveryEspia(), "otro problema", None)

    assert scheduler_v38._parada_dura is None
    assert scheduler_v38.recovery_manager.hubo_parada_dura() is False


def test_manager_roto_no_impide_el_plan_b(scheduler_v38):
    """Si el RecoveryManager falla, prevalece el comportamiento anterior."""
    recovery = _RecoveryEspia(plan=None)
    agente = _preparar(scheduler_v38, recovery)

    class _ManagerRoto:
        def decidir(self, **kwargs):
            raise RuntimeError("boom")

        def reset(self):
            pass

    scheduler_v38.recovery_manager = _ManagerRoto()
    scheduler_v38._bloquear_dependientes(agente.id, "Error: fallo simulado")

    assert len(recovery.llamadas) == 1


# ============================================================
# Presupuesto agotado (V3.8-2): parada dura sin depender del texto
# ============================================================

def test_presupuesto_agotado_es_parada_dura_sin_plan_b(scheduler_v38):
    recovery = _RecoveryEspia()
    agente = _preparar(scheduler_v38, recovery)
    scheduler_v38.presupuesto = BudgetManager(max_llamadas=1)
    scheduler_v38.presupuesto.registrar_llamada()

    scheduler_v38._bloquear_dependientes(agente.id, "Error: fallo simulado")

    assert recovery.llamadas == [], "sin presupuesto no se llama al LLM"
    assert scheduler_v38._parada_dura is not None
    assert scheduler_v38._parada_dura["codigo"] == BUDGET_EXCEEDED


def test_aceptacion_expone_el_consumo_del_presupuesto(scheduler_v38):
    recovery = _RecoveryEspia()
    agente = _preparar(scheduler_v38, recovery)
    scheduler_v38.presupuesto = BudgetManager(max_llamadas=10)
    scheduler_v38.presupuesto.iniciar()
    scheduler_v38.presupuesto.registrar_llamada(tokens_prompt=10, tokens_completion=5)
    agente.estado = EstadoAgente.COMPLETADO

    aceptacion = scheduler_v38.obtener_resultado_aceptacion()

    assert aceptacion["presupuesto"]["consumo"]["llamadas"] == 1
    assert aceptacion["presupuesto"]["consumo"]["tokens_total"] == 15
    assert aceptacion["presupuesto"]["agotado"] is False
