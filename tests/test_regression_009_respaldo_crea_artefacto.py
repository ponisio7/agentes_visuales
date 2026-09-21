"""Regresión 009 — el plan de respaldo materializa el artefacto pedido.

Bugs 1.2 y 1.10 del ROADMAP (punto 3.1):

    ``ProblemSolver._crear_plan_fallback`` devolvía un único paso Python que
    solo construía ``{'status': 'ok', 'problema': 'Resuelto con fallback'}``.
    El archivo pedido **nunca se creaba** y el sistema aparentaba haber tenido
    éxito: el «fallback que simula que hizo algo». El paso por defecto de
    ``PlanBuilder`` tenía el mismo defecto.

Arreglo: el respaldo detecta el archivo nombrado en el enunciado y emite un
paso Python (contenido mínimo) + un paso File que **escribe de verdad**. Si no
hay archivo identificable, declara ``fallback_sin_artefacto`` en vez de un
``ok`` falso.
"""
from __future__ import annotations

import logging

import pytest

from core.problem_solver.artefacto import detectar_artefacto
from core.problem_solver.builder import PlanBuilder
from core.problem_solver.code_corrector import PythonCodeCorrector
from core.problem_solver.solver import ProblemSolver
from core.problem_solver.validator import PlanValidator
from core.scheduler import Scheduler


class _SolverFalso:
    """Solo necesita ``logger``: ``_crear_plan_fallback`` no usa nada más."""

    logger = logging.getLogger("test.fallback")


def _plan_de_respaldo(problema: str):
    plan_dict = ProblemSolver._crear_plan_fallback(_SolverFalso(), problema)
    log = logging.getLogger("test.fallback")
    log.addHandler(logging.NullHandler())
    validador = PlanValidator(log)
    builder = PlanBuilder(log, PythonCodeCorrector(), validador)
    plan = builder.construir_plan(problema, plan_dict)
    plan.agentes_generados = builder.generar_agentes(plan)
    ok, errores = validador.validar_plan(plan)
    return plan, ok, errores


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


# ---------------------------------------------------------------------------
# 1. Detección del artefacto en el enunciado
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("enunciado,esperado", [
    ("crear saludo.txt", "saludo.txt"),
    ("hazme un informe.md sobre el bitcoin", "informe.md"),
    ("genera datos.csv con las ventas", "datos.csv"),
    ("guarda la config en config.json", "config.json"),
    ("escribe una página index.html", "index.html"),
    ("sin archivo que crear, solo piensa", None),
    ("hazme un documento.docx bonito", None),  # binario: no lo escribe el respaldo
    ("", None),
])
def test_detecta_el_artefacto(enunciado, esperado):
    assert detectar_artefacto(enunciado) == esperado


# ---------------------------------------------------------------------------
# 2. El plan de respaldo declara el paso File
# ---------------------------------------------------------------------------

def test_el_respaldo_declara_un_paso_file():
    plan_dict = ProblemSolver._crear_plan_fallback(_SolverFalso(), "crear saludo.txt")

    pasos = plan_dict["pasos"]
    assert len(pasos) == 2
    assert pasos[0]["tipo"] == "Python"
    assert pasos[1]["tipo"] == "File"
    assert pasos[1]["configuracion"]["operacion"] == "escribir"
    assert pasos[1]["configuracion"]["archivo_destino"] == "saludo.txt"
    assert pasos[1]["dependencias"] == [pasos[0]["nombre"]]


def test_el_respaldo_no_declara_status_ok_falso():
    plan_dict = ProblemSolver._crear_plan_fallback(_SolverFalso(), "crear saludo.txt")

    codigo = plan_dict["pasos"][0]["configuracion"]["codigo"]
    assert "'ok'" not in codigo
    assert "Resuelto con fallback" not in codigo


def test_sin_artefacto_el_respaldo_es_honesto():
    plan_dict = ProblemSolver._crear_plan_fallback(_SolverFalso(), "piensa en la vida")

    assert len(plan_dict["pasos"]) == 1
    codigo = plan_dict["pasos"][0]["configuracion"]["codigo"]
    assert "fallback_sin_artefacto" in codigo
    assert "False" in codigo


def test_el_plan_valido_no_tiene_errores_bloqueantes():
    _, ok, errores = _plan_de_respaldo("crear saludo.txt")

    assert ok, f"el plan de respaldo debe ser válido: {errores}"


# ---------------------------------------------------------------------------
# 3. Ejecución real: el archivo existe y no está vacío
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_el_respaldo_crea_el_archivo(tmp_path, monkeypatch, qapp, esperar):
    """El criterio de cierre de 3.1: el artefacto existe tras ejecutar."""
    monkeypatch.chdir(tmp_path)
    plan, ok, errores = _plan_de_respaldo("crear saludo.txt")
    assert ok, errores

    scheduler = Scheduler(max_concurrent=2)
    scheduler.agregar_agentes(plan.agentes_generados)
    scheduler.resolver_dependencias()
    _ejecutar(scheduler, qapp, esperar)

    destino = tmp_path / "saludo.txt"
    assert destino.exists(), "el plan de respaldo debe crear el archivo"
    assert destino.read_text(encoding="utf-8").strip(), "no puede quedar vacío"


@pytest.mark.slow
def test_el_paso_por_defecto_de_builder_tambien_crea_el_archivo(
    tmp_path, monkeypatch, qapp, esperar
):
    """Mismo defecto en ``PlanBuilder.construir_plan`` con un plan sin pasos."""
    monkeypatch.chdir(tmp_path)
    log = logging.getLogger("test.fallback")
    validador = PlanValidator(log)
    builder = PlanBuilder(log, PythonCodeCorrector(), validador)

    plan = builder.construir_plan("crear resumen.txt", {"titulo": "Sin pasos"})
    plan.agentes_generados = builder.generar_agentes(plan)
    ok, errores = validador.validar_plan(plan)
    assert ok, errores

    scheduler = Scheduler(max_concurrent=2)
    scheduler.agregar_agentes(plan.agentes_generados)
    scheduler.resolver_dependencias()
    _ejecutar(scheduler, qapp, esperar)

    destino = tmp_path / "resumen.txt"
    assert destino.exists()
    assert destino.read_text(encoding="utf-8").strip()


@pytest.mark.slow
def test_sin_artefacto_no_se_crea_nada_y_no_finge_exito(
    tmp_path, monkeypatch, qapp, esperar
):
    monkeypatch.chdir(tmp_path)
    plan, ok, _ = _plan_de_respaldo("piensa en la vida")

    scheduler = Scheduler(max_concurrent=2)
    scheduler.agregar_agentes(plan.agentes_generados)
    scheduler.resolver_dependencias()
    _ejecutar(scheduler, qapp, esperar)

    # El arranque crea infraestructura (BD de aprendizaje, logs); lo que no
    # puede aparecer es ningún artefacto inventado por el respaldo.
    infraestructura = {"agent_history.db", "learning_models", "logs"}
    creados = {
        p.name for p in tmp_path.iterdir() if p.name not in infraestructura
    }
    assert creados == set(), f"no debe inventarse ningún archivo: {creados}"

    # Y el resultado debe DECLARAR el fallo, no un 'ok' falso.
    resultado = repr(plan.agentes_generados[0].resultado)
    assert "fallback_sin_artefacto" in resultado
