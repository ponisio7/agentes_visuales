import ast
import re
import logging
import pytest
from core.problem_solver.validator import PlanValidator
from core.problem_solver.models import ExecutionPlan, StepPlan


@pytest.fixture
def validator():
    log = logging.getLogger("test.validator")
    log.addHandler(logging.NullHandler())
    return PlanValidator(log)


def _plan_con_codigo(codigo: str, nombres_agentes=None):
    """Helper: plan mínimo con un paso Python de código dado."""
    nombres_agentes = nombres_agentes or []
    pasos = [StepPlan(nombre="Test", tipo_agente="Python", configuracion={"codigo": codigo})]
    for n in nombres_agentes:
        pasos.append(StepPlan(nombre=n, tipo_agente="LLM", configuracion={}))
    return ExecutionPlan(pasos=pasos, problema_original="test")


def test_codigo_valido_pasa(validator):
    plan = _plan_con_codigo("resultado = {'x': 1}")
    valido, errores = validator.validar_plan(plan)
    assert valido is True
    assert errores == []


def test_syntax_error_detectado(validator):
    plan = _plan_con_codigo("resultado = {")
    valido, errores = validator.validar_plan(plan)
    assert valido is False
    assert any("SyntaxError" in e for e in errores)


def test_nombre_agente_como_variable_detectado(validator):
    plan = _plan_con_codigo(
        "html = GenerarCuentoConSVG",
        nombres_agentes=["GenerarCuentoConSVG"],
    )
    valido, errores = validator.validar_plan(plan)
    assert valido is False
    assert any("GenerarCuentoConSVG" in e for e in errores)


def test_contexto_get_es_correcto(validator):
    plan = _plan_con_codigo(
        "html = contexto.get('GenerarCuentoConSVG', {})",
        nombres_agentes=["GenerarCuentoConSVG"],
    )
    valido, errores = validator.validar_plan(plan)
    assert valido is True


def test_json_loads_placeholder_detectado(validator):
    plan = _plan_con_codigo("datos = json.loads('''{datos_llm}''')")
    valido, errores = validator.validar_plan(plan)
    assert valido is False
    assert any("placeholder" in e for e in errores)
