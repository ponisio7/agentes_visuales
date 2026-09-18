"""
tests/test_plan_recovery.py

Tests de PlanRecovery.generar_plan_b() usando un ProblemSolver REAL
(Opción 2 del análisis): se mockea solo el llm_client, así que
_construir_plan / _generar_agentes / PythonCodeCorrector corren tal
cual corren en producción. Es la opción más fiel al comportamiento real.

Requiere: pip install pytest
Ejecutar: pytest tests/test_plan_recovery.py -v
"""
from unittest.mock import Mock

import pytest

from core.agent import Agente, TipoAgente
from core.plan_recovery import PlanRecovery
from core.problem_solver import ProblemSolver

# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def agente_fallido():
    """Agente cuyo fallo dispara la generación del Plan B."""
    agente = Mock(spec=Agente)
    agente.nombre = "DescargarDatos"
    agente.tipo = TipoAgente.HTTP
    return agente


@pytest.fixture
def llm_client_mock():
    """
    Cliente LLM mockeado. Simula lo mínimo que ProblemSolver.__init__
    necesita para no explotar (comprueba `disponible`).
    """
    mock = Mock()
    mock.disponible = True
    mock.default_model = "deepseek-v4-flash"
    return mock


@pytest.fixture
def problem_solver_real(llm_client_mock):
    """ProblemSolver real, con el llm_client mockeado."""
    return ProblemSolver(llm_client=llm_client_mock)


@pytest.fixture
def plan_recovery(llm_client_mock, problem_solver_real):
    return PlanRecovery(
        llm_client=llm_client_mock,
        problem_solver=problem_solver_real,
        db_path=":memory:",
    )


# ============================================================
# RESPUESTAS SIMULADAS DEL LLM
# ============================================================

# Respuesta con código Python sintácticamente INVÁLIDO
# (falta ':' tras el if -> el corrector no lo puede arreglar)
RESPUESTA_ROTA = """{
  "titulo": "Plan alternativo (roto)",
  "analisis": "Usa una fuente de datos distinta",
  "estimacion_tiempo_segundos": 20,
  "pasos": [
    {
      "orden": 1,
      "nombre": "ProcesarDatos",
      "descripcion": "Procesa los datos descargados",
      "tipo": "Python",
      "dependencias": [],
      "configuracion": {
        "codigo": "x = 5\\nif x > 3\\n    resultado = {'ok': True}",
        "timeout": 30
      },
      "justificacion": "Paso principal"
    }
  ]
}"""

# Misma idea, pero con código Python VÁLIDO
RESPUESTA_BUENA = """{
  "titulo": "Plan alternativo (corregido)",
  "analisis": "Usa una fuente de datos distinta",
  "estimacion_tiempo_segundos": 20,
  "pasos": [
    {
      "orden": 1,
      "nombre": "ProcesarDatos",
      "descripcion": "Procesa los datos descargados",
      "tipo": "Python",
      "dependencias": [],
      "configuracion": {
        "codigo": "x = 5\\nif x > 3:\\n    resultado = {'ok': True}\\nelse:\\n    resultado = {'ok': False}",
        "timeout": 30
      },
      "justificacion": "Paso principal"
    }
  ]
}"""


# ============================================================
# TESTS
# ============================================================

def test_reintenta_y_corrige_sintaxis(
    plan_recovery, llm_client_mock, agente_fallido
):
    """
    Primera respuesta del LLM trae código roto -> falla la validación
    de sintaxis -> PlanRecovery reintenta una vez -> segunda respuesta
    es válida -> se devuelve el plan corregido.
    """
    llm_client_mock.chat.side_effect = [RESPUESTA_ROTA, RESPUESTA_BUENA]

    plan_b = plan_recovery.generar_plan_b(
        problema_original="Descargar y procesar datos de una API",
        plan_fallido=None,
        agente_fallido=agente_fallido,
        error="Timeout conectando a la API",
    )

    assert plan_b is not None
    assert plan_b.titulo == "Plan alternativo (corregido)"
    assert len(plan_b.agentes_generados) == 1
    assert llm_client_mock.chat.call_count == 2  # 1 intento + 1 reintento


def test_plan_valido_a_la_primera(
    plan_recovery, llm_client_mock, agente_fallido
):
    """Si el LLM responde bien a la primera, no debe haber reintento."""
    llm_client_mock.chat.side_effect = [RESPUESTA_BUENA]

    plan_b = plan_recovery.generar_plan_b(
        problema_original="Descargar y procesar datos de una API",
        plan_fallido=None,
        agente_fallido=agente_fallido,
        error="Timeout conectando a la API",
    )

    assert plan_b is not None
    assert len(plan_b.agentes_generados) == 1
    assert llm_client_mock.chat.call_count == 1


def test_json_invalido_devuelve_none(
    plan_recovery, llm_client_mock, agente_fallido
):
    """Si el LLM no devuelve JSON parseable, generar_plan_b devuelve None."""
    llm_client_mock.chat.side_effect = ["esto no es JSON en absoluto"]

    plan_b = plan_recovery.generar_plan_b(
        problema_original="Descargar y procesar datos de una API",
        plan_fallido=None,
        agente_fallido=agente_fallido,
        error="Timeout conectando a la API",
    )

    assert plan_b is None


def test_respuesta_vacia_devuelve_none(
    plan_recovery, llm_client_mock, agente_fallido
):
    """Si el LLM devuelve string vacío, generar_plan_b devuelve None."""
    llm_client_mock.chat.side_effect = [""]

    plan_b = plan_recovery.generar_plan_b(
        problema_original="Descargar y procesar datos de una API",
        plan_fallido=None,
        agente_fallido=agente_fallido,
        error="Timeout conectando a la API",
    )

    assert plan_b is None


def test_ambas_versiones_rotas_devuelve_none(
    plan_recovery, llm_client_mock, agente_fallido
):
    """Si ni la primera ni la segunda versión compilan, se descarta el plan."""
    llm_client_mock.chat.side_effect = [RESPUESTA_ROTA, RESPUESTA_ROTA]

    plan_b = plan_recovery.generar_plan_b(
        problema_original="Descargar y procesar datos de una API",
        plan_fallido=None,
        agente_fallido=agente_fallido,
        error="Timeout conectando a la API",
    )

    assert plan_b is None
    assert llm_client_mock.chat.call_count == 2
