# tests/test_goal_resolver.py
"""
Tests del modo «Resolver tarea» (V4.0).

El bucle se prueba con planificador y ejecutor inyectados: sin LLM, sin Qt y
sin Scheduler. Lo que se fija aquí es la LÓGICA del resolver, que es lo nuevo:

  - plan verificable → ejecutar → aceptado ⇒ resuelto;
  - no aceptado ⇒ re-planificar con el motivo concreto (no a ciegas);
  - plan sin contrato de aceptación ⇒ NO se ejecuta, se devuelve al planificador;
  - presupuesto agotado ⇒ parada honesta (antes y entre intentos);
  - fallos del planificador/ejecutor no revientan el bucle;
  - «terminado» no es «aceptado» (H6).
"""
from __future__ import annotations

import pytest

from core.budget_manager import BudgetManager
from core.goal_resolver import (
    PARADA_INTENTOS,
    PARADA_PRESUPUESTO,
    PARADA_VERIFICADO,
    GoalResolver,
    IntentoResolucion,
    ResultadoIntento,
    construir_evidencia,
    problemas_de_verificabilidad,
    resultado_desde_salida,
)
from core.problem_solver.models import ContratoAceptacion, ExecutionPlan, StepPlan


# ------------------------------------------------------------
# Planes de prueba
# ------------------------------------------------------------
def _plan(titulo: str, *, critico: bool = True, contrato: bool = True,
          terminal_contrato: bool = False) -> ExecutionPlan:
    pasos = [
        StepPlan(
            orden=1, nombre="Producir", tipo_agente="Python",
            es_critico=critico,
            aceptacion=ContratoAceptacion(archivos=["salida.txt"]) if contrato else None,
        ),
        StepPlan(
            orden=2, nombre="Escribir", tipo_agente="File",
            dependencia_ids=["Producir"],
            aceptacion=(
                ContratoAceptacion(archivos=["salida.txt"])
                if terminal_contrato else None
            ),
        ),
    ]
    return ExecutionPlan(id=titulo, titulo=titulo, pasos=pasos)


def _ok(motivo: str = "") -> ResultadoIntento:
    return ResultadoIntento(
        exito=True, motivo=motivo,
        aceptacion={"aceptada": True, "motivos": []},
        salida={"ok": True, "aceptacion": {"aceptada": True}},
    )


def _fallo(motivo: str = "falta el archivo salida.txt") -> ResultadoIntento:
    return ResultadoIntento(
        exito=False, motivo=motivo,
        aceptacion={"aceptada": False, "motivos": [motivo]},
        salida={"ok": False, "aceptacion": {"aceptada": False, "motivos": [motivo]}},
    )


class _PlanificadorFalso:
    """Devuelve planes en orden y guarda las evidencias recibidas."""

    def __init__(self, planes, error_en=None):
        self.planes = list(planes)
        self.error_en = set(error_en or [])
        self.llamadas: list[tuple[str, str]] = []

    def __call__(self, objetivo: str, evidencia: str):
        self.llamadas.append((objetivo, evidencia))
        if len(self.llamadas) in self.error_en:
            raise RuntimeError("el LLM no respondió")
        idx = min(len(self.llamadas) - 1, len(self.planes) - 1)
        return self.planes[idx]


class _EjecutorFalso:
    def __init__(self, resultados):
        self.resultados = list(resultados)
        self.planes: list[object] = []

    def __call__(self, plan, objetivo: str):
        self.planes.append(plan)
        if not self.resultados:
            return _ok()
        idx = min(len(self.planes) - 1, len(self.resultados) - 1)
        return self.resultados[idx]


# ------------------------------------------------------------
# 1. Camino feliz
# ------------------------------------------------------------
def test_exito_al_primer_intento():
    planif = _PlanificadorFalso([_plan("A")])
    ejec = _EjecutorFalso([_ok()])
    res = GoalResolver(planif, ejec, max_intentos=3).resolver("haz un informe")

    assert res.resuelto is True
    assert res.parada == PARADA_VERIFICADO
    assert res.n_intentos == 1
    assert res.ejecuciones == 1
    assert len(ejec.planes) == 1


def test_fallo_de_aceptacion_replanifica_y_verifica():
    planif = _PlanificadorFalso([_plan("A"), _plan("B")])
    ejec = _EjecutorFalso([_fallo("no existe salida.txt"), _ok()])
    res = GoalResolver(planif, ejec, max_intentos=2).resolver("haz un informe")

    assert res.resuelto is True
    assert res.parada == PARADA_VERIFICADO
    assert [i.numero for i in res.intentos] == [1, 2]
    assert res.intentos[0].exito is False
    assert res.intentos[1].exito is True
    # El segundo plan es OTRO plan, no el mismo.
    assert ejec.planes[0].id == "A"
    assert ejec.planes[1].id == "B"


def test_la_replanificacion_lleva_el_motivo_concreto():
    planif = _PlanificadorFalso([_plan("A"), _plan("B")])
    ejec = _EjecutorFalso([_fallo("el DOCX no tiene las 3 imágenes"), _ok()])
    GoalResolver(planif, ejec, max_intentos=2).resolver("haz un informe")

    assert planif.llamadas[0][1] == ""            # primer intento: sin evidencia
    evidencia = planif.llamadas[1][1]
    assert "superó la verificación" in evidencia
    assert "el DOCX no tiene las 3 imágenes" in evidencia


# ------------------------------------------------------------
# 2. El sistema decide la verificación
# ------------------------------------------------------------
def test_plan_sin_contrato_no_se_ejecuta_y_vuelve_al_planificador():
    sin_contrato = _plan("sin_contrato", critico=True, contrato=False,
                         terminal_contrato=False)
    con_contrato = _plan("con_contrato")
    planif = _PlanificadorFalso([sin_contrato, con_contrato])
    ejec = _EjecutorFalso([_ok()])

    res = GoalResolver(planif, ejec, max_intentos=2).resolver("haz un informe")

    assert res.resuelto is True
    assert res.n_intentos == 2
    assert res.ejecuciones == 1                    # solo se ejecutó el segundo
    assert res.intentos[0].ejecutado is False
    assert res.intentos[0].verificable is False
    # La evidencia del segundo intento exige declarar aceptación.
    assert "aceptacion" in planif.llamadas[1][1]


def test_plan_no_verificable_agota_intentos_y_lo_dice():
    sin_contrato = _plan("x", critico=True, contrato=False)
    planif = _PlanificadorFalso([sin_contrato, sin_contrato])
    ejec = _EjecutorFalso([_ok()])

    res = GoalResolver(planif, ejec, max_intentos=2).resolver("objetivo")

    assert res.resuelto is False
    assert res.parada == PARADA_INTENTOS
    assert res.ejecuciones == 0
    assert "no declara verificación" in res.motivo


def test_gate_desactivable():
    """Opt-out explícito: se ejecuta aunque no haya contrato."""
    planif = _PlanificadorFalso([_plan("x", critico=True, contrato=False)])
    ejec = _EjecutorFalso([_ok()])
    res = GoalResolver(
        planif, ejec, max_intentos=1, exigir_verificacion=False
    ).resolver("objetivo")
    assert res.resuelto is True
    assert res.ejecuciones == 1


def test_terminal_con_contrato_basta_aunque_no_haya_critico():
    plan = _plan("t", critico=False, contrato=False, terminal_contrato=True)
    assert problemas_de_verificabilidad(plan) == []


def test_problemas_de_verificabilidad_sin_pasos():
    assert problemas_de_verificabilidad(ExecutionPlan(id="v", titulo="v")) == [
        "el plan no tiene pasos"
    ]


def test_critico_con_contrato_es_verificable():
    assert problemas_de_verificabilidad(_plan("ok")) == []


# ------------------------------------------------------------
# 3. Paradas honestas
# ------------------------------------------------------------
def test_intentos_agotados_devuelve_motivo():
    planif = _PlanificadorFalso([_plan("A")])
    ejec = _EjecutorFalso([_fallo("siempre falla")])
    res = GoalResolver(planif, ejec, max_intentos=3).resolver("objetivo")

    assert res.resuelto is False
    assert res.parada == PARADA_INTENTOS
    assert res.n_intentos == 3
    assert res.ejecuciones == 3
    assert "siempre falla" in res.motivo


def test_presupuesto_agotado_antes_del_primer_intento():
    planif = _PlanificadorFalso([_plan("A")])
    ejec = _EjecutorFalso([_ok()])
    presupuesto = BudgetManager(max_llamadas=0)
    presupuesto.iniciar()

    res = GoalResolver(
        planif, ejec, presupuesto=presupuesto, max_intentos=3
    ).resolver("objetivo")

    assert res.resuelto is False
    assert res.parada == PARADA_PRESUPUESTO
    assert res.n_intentos == 0
    assert "presupuesto agotado" in res.motivo
    assert planif.llamadas == []


def test_presupuesto_agotado_entre_intentos():
    ejec = _EjecutorFalso([_fallo("no cumple")])
    presupuesto = BudgetManager(max_llamadas=1)

    def _planificador_que_gasta(objetivo, evidencia):
        # Cada planificación gasta una llamada: la primera agota el límite.
        presupuesto.registrar_llamada(tokens_prompt=10)
        return _plan("A")

    res = GoalResolver(
        _planificador_que_gasta, ejec, presupuesto=presupuesto, max_intentos=3
    ).resolver("objetivo")

    assert res.resuelto is False
    assert res.parada == PARADA_PRESUPUESTO
    assert res.n_intentos == 1          # solo el primero llegó a planificar
    assert res.ejecuciones == 1
    assert res.presupuesto.get("consumo", {}).get("llamadas") == 1


# ------------------------------------------------------------
# 4. Robustez
# ------------------------------------------------------------
def test_objetivo_vacio_lanza():
    with pytest.raises(ValueError):
        GoalResolver(_PlanificadorFalso([_plan("A")]), _EjecutorFalso([_ok()])).resolver("   ")


def test_planificador_que_falla_no_rompe_el_bucle():
    planif = _PlanificadorFalso([_plan("B")], error_en={1})
    ejec = _EjecutorFalso([_ok()])
    res = GoalResolver(planif, ejec, max_intentos=2).resolver("objetivo")

    assert res.resuelto is True                     # el segundo intento sí planeó
    assert res.intentos[0].error
    assert res.intentos[0].ejecutado is False
    assert res.ejecuciones == 1


def test_ejecutor_que_falla_no_rompe_el_bucle():
    planif = _PlanificadorFalso([_plan("A")])
    llamadas = {"n": 0}

    def _ejecutor_roto(plan, objetivo):
        llamadas["n"] += 1
        if llamadas["n"] == 1:
            raise RuntimeError("el scheduler explotó")
        return _ok()

    res = GoalResolver(planif, _ejecutor_roto, max_intentos=2).resolver("objetivo")

    assert res.resuelto is True
    assert res.intentos[0].error
    assert res.intentos[0].ejecutado is False


def test_max_intentos_invalido_se_normaliza_a_uno():
    planif = _PlanificadorFalso([_plan("A")])
    ejec = _EjecutorFalso([_fallo("no")])
    assert GoalResolver(planif, ejec, max_intentos=0).max_intentos == 1
    assert GoalResolver(planif, ejec, max_intentos="basura").max_intentos == 2


# ------------------------------------------------------------
# 5. Adaptador de salida del pipeline (H6: terminado ≠ aceptado)
# ------------------------------------------------------------
def test_resultado_desde_salida_exige_aceptacion():
    salida = {
        "ok": True,
        "aceptacion": {"aceptada": False, "motivos": ["el DOCX no tiene 3 imágenes"]},
        "agentes": [{"nombre": "Escribir", "ok": True}],
    }
    r = resultado_desde_salida(salida)
    assert r.exito is False
    assert "3 imágenes" in r.motivo


def test_resultado_desde_salida_exito():
    salida = {"ok": True, "aceptacion": {"aceptada": True, "motivos": []}, "agentes": []}
    r = resultado_desde_salida(salida)
    assert r.exito is True
    assert r.motivo == ""


def test_resultado_desde_salida_usa_los_errores_de_los_agentes():
    salida = {
        "ok": False,
        "aceptacion": {"aceptada": False, "motivos": []},
        "agentes": [{"nombre": "Buscar", "ok": False, "error": "timeout de red"}],
    }
    r = resultado_desde_salida(salida)
    assert r.exito is False
    assert "timeout de red" in r.motivo


# ------------------------------------------------------------
# 6. Serialización y evidencia
# ------------------------------------------------------------
def test_to_dict_es_serializable():
    import json

    planif = _PlanificadorFalso([_plan("A"), _plan("B")])
    ejec = _EjecutorFalso([_fallo("no"), _ok()])
    res = GoalResolver(planif, ejec, max_intentos=2).resolver("objetivo")
    datos = json.loads(json.dumps(res.to_dict(), ensure_ascii=False))
    assert datos["resuelto"] is True
    assert datos["parada"] == PARADA_VERIFICADO
    assert len(datos["intentos"]) == 2
    assert datos["intentos"][0]["aceptacion"]["aceptada"] is False


def test_construir_evidencia_de_intento_no_ejecutado():
    intento = IntentoResolucion(
        numero=1, ejecutado=False,
        problemas_verificabilidad=["ningún paso final declara contrato"],
    )
    texto = construir_evidencia(intento)
    assert "NO es verificable" in texto
    assert "ningún paso final declara contrato" in texto


# ------------------------------------------------------------
# 7. Riesgo de fallo (V4.0-3): observabilidad, nunca decisión
# ------------------------------------------------------------
def test_el_riesgo_estimado_queda_en_la_traza_sin_alterar_la_decision():
    planif = _PlanificadorFalso([_plan("A")])
    ejec = _EjecutorFalso([_ok()])
    def predictor(_plan):
        return {"probabilidad_fallo": 0.81, "confianza": "media", "disponible": True}

    res = GoalResolver(
        planif, ejec, max_intentos=1, predictor=predictor
    ).resolver("objetivo")

    assert res.resuelto is True
    assert res.intentos[0].riesgo_fallo["probabilidad_fallo"] == 0.81
    assert res.to_dict()["intentos"][0]["riesgo_fallo"]["confianza"] == "media"


def test_un_predictor_roto_no_rompe_el_bucle():
    planif = _PlanificadorFalso([_plan("A")])
    ejec = _EjecutorFalso([_ok()])

    def _predictor_roto(_plan):
        raise RuntimeError("modelo corrupto")

    res = GoalResolver(
        planif, ejec, max_intentos=1, predictor=_predictor_roto
    ).resolver("objetivo")

    assert res.resuelto is True
    assert res.intentos[0].riesgo_fallo == {}


def test_sin_predictor_la_traza_va_vacia():
    planif = _PlanificadorFalso([_plan("A")])
    ejec = _EjecutorFalso([_ok()])
    res = GoalResolver(planif, ejec, max_intentos=1).resolver("objetivo")
    assert res.intentos[0].riesgo_fallo == {}
