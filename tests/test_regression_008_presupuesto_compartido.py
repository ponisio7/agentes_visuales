"""Regresión 008 — el presupuesto compartido de ``resolve`` no se reinicia.

Bug 3.12 del ROADMAP:

    ``Scheduler.set_contexto_plan_b()`` llamaba a ``presupuesto.reset()``, que
    es **alias de** ``iniciar()`` (reinicia el reloj y el consumo). Como
    ``_ejecutar_plan`` lo invoca una vez por intento, el presupuesto de
    ``resolve`` se ponía a cero en cada intento: el límite de
    tiempo/llamadas/tokens **no acotaba el total**, justo lo contrario de lo
    que promete el diseño («un único presupuesto cubre la planificación y todos
    los intentos»).

Arreglo: ``reiniciar_presupuesto: bool = True``; el modo ``resolve`` pasa
``False`` porque recibe un presupuesto compartido.
"""
from __future__ import annotations

from core.budget_manager import BudgetManager
from core.scheduler import Scheduler


def _presupuesto_con_gasto(llamadas: int = 1) -> BudgetManager:
    presupuesto = BudgetManager(max_llamadas=10)
    presupuesto.iniciar()
    for _ in range(llamadas):
        presupuesto.registrar_llamada(tokens_prompt=10)
    return presupuesto


def _consumo(presupuesto: BudgetManager) -> dict:
    return presupuesto.resumen().get("consumo", {})


# ---------------------------------------------------------------------------
# 1. Con presupuesto compartido NO se reinicia
# ---------------------------------------------------------------------------

def test_reiniciar_presupuesto_false_conserva_el_consumo():
    presupuesto = _presupuesto_con_gasto(1)
    scheduler = Scheduler(max_concurrent=1, presupuesto=presupuesto)

    scheduler.set_contexto_plan_b(
        recovery=object(),
        problema_original="objetivo",
        plan_original=None,
        reiniciar_presupuesto=False,
    )

    assert _consumo(presupuesto)["llamadas"] == 1


def test_dos_intentos_comparten_el_presupuesto():
    """El criterio de cierre de 3.12: dos intentos SUMAN, no se reinician."""
    presupuesto = BudgetManager(max_llamadas=10)
    presupuesto.iniciar()

    for _ in range(2):
        # Cada intento construye su propio Scheduler, como hace _ejecutar_plan.
        scheduler = Scheduler(max_concurrent=1, presupuesto=presupuesto)
        scheduler.set_contexto_plan_b(
            recovery=object(),
            problema_original="objetivo",
            plan_original=None,
            reiniciar_presupuesto=False,
        )
        presupuesto.registrar_llamada(tokens_prompt=10)

    assert _consumo(presupuesto)["llamadas"] == 2


def test_el_presupuesto_agotado_sigue_detectandose_entre_intentos():
    """Con el consumo acumulado, el tope se alcanza de verdad."""
    presupuesto = BudgetManager(max_llamadas=2)
    presupuesto.iniciar()

    for _ in range(3):
        scheduler = Scheduler(max_concurrent=1, presupuesto=presupuesto)
        scheduler.set_contexto_plan_b(
            recovery=object(), problema_original="objetivo", plan_original=None,
            reiniciar_presupuesto=False,
        )
        if presupuesto.motivo_agotado():
            break
        presupuesto.registrar_llamada(tokens_prompt=10)

    assert presupuesto.motivo_agotado() is not None
    assert _consumo(presupuesto)["llamadas"] == 2


# ---------------------------------------------------------------------------
# 2. El comportamiento por defecto no cambia (run)
# ---------------------------------------------------------------------------

def test_por_defecto_si_reinicia():
    """``run`` no cambia: cada ejecución arranca el presupuesto de cero."""
    presupuesto = _presupuesto_con_gasto(1)
    scheduler = Scheduler(max_concurrent=1, presupuesto=presupuesto)

    scheduler.set_contexto_plan_b(
        recovery=object(), problema_original="problema", plan_original=None
    )

    assert _consumo(presupuesto)["llamadas"] == 0


def test_reiniciar_presupuesto_true_explicito_reinicia():
    presupuesto = _presupuesto_con_gasto(2)
    scheduler = Scheduler(max_concurrent=1, presupuesto=presupuesto)

    scheduler.set_contexto_plan_b(
        recovery=object(), problema_original="problema", plan_original=None,
        reiniciar_presupuesto=True,
    )

    assert _consumo(presupuesto)["llamadas"] == 0
