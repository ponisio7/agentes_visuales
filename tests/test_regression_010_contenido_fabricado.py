"""Regresión 010 — el LLM no puede inventar contenido de relleno.

Bugs 1.7 y 1.8 del ROADMAP (punto 3.3):

    El propio LLM escribía, dentro del código del plan, un fallback que
    INVENTABA el contenido cuando el JSON no tenía la forma esperada::

        if not isinstance(ideas, list) or len(ideas) != 5:
            ideas = []
            for i in range(1, 6):
                ideas.append({'titulo': f'Idea {i}',
                              'descripcion': 'Descripción no disponible'})

    El plan "triunfaba" y el usuario recibía «Descripción no disponible» en el
    HTML final. El caso real está en
    ``logs/llm_response_20260913_091705_deepseek-v4-flash.txt``.

Se cierra por tres vías:
  1. El prompt del planificador lo prohíbe explícitamente.
  2. El validador RECHAZA el plan (BLOQUEANTE) si el código lleva literales de
     relleno, así que el solver reintenta con la instrucción correctiva.
  3. El gate Nivel 1 de verificación usa la MISMA lista de relleno, en vez de
     su propia tupla de «valores vacíos» que no cubría este caso.
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path

import pytest

from core.executors.helpers import es_resultado_sospechoso
from core.problem_solver.models import ExecutionPlan, StepPlan
from core.problem_solver.prompt_builder import PromptBuilder
from core.problem_solver.validator import PlanValidator
from core.utils import LITERALES_FABRICADOS, es_valor_placeholder

# El caso REAL, copiado del log.
CODIGO_FABRICADO = """respuesta_llm = contexto.get('GenerarIdeas', {})
json_data = respuesta_llm.get('json', {})
ideas = json_data.get('ideas', [])
if not isinstance(ideas, list) or len(ideas) != 5:
    ideas = []
    for i in range(1, 6):
        ideas.append({'titulo': f'Idea {i}', 'descripcion': 'Descripción no disponible'})
resultado = {'ideas': ideas}
"""

CODIGO_LEGITIMO = """llm = contexto.get('GenerarIdeas', {})
datos = llm.get('json', {}) if isinstance(llm.get('json'), dict) else {}
ideas = datos.get('ideas', [])
resultado = {'ideas': ideas}
"""

# Segundo caso REAL, sacado del corpus (medición de 3.10):
# logs/llm_response_20260912_175146_deepseek-v4-flash.txt :: FormatearResultado
# Sustituye una puntuación que falta por 'N/A' y sigue como si nada.
CODIGO_FABRICADO_CORPUS = """chiste_data = contexto.get('GenerarChiste', {})
chiste = chiste_data.get('respuesta_limpia', 'No se pudo generar el chiste.')
evaluacion_data = contexto.get('EvaluarChiste', {})
evaluacion_json = evaluacion_data.get('json', {})
puntuacion = evaluacion_json.get('puntuacion', 'N/A')
comentario = evaluacion_json.get('comentario', 'Sin comentario.')
resultado = {
    'chiste': chiste,
    'puntuacion': puntuacion,
    'comentario': comentario,
    'mensaje_final': f"Aquí tienes un chiste:\\n\\n{chiste}\\n\\nPuntuación: {puntuacion}/10 - {comentario}"
}
"""

LOGS = Path(__file__).resolve().parent.parent / "logs"


@pytest.fixture
def validador():
    log = logging.getLogger("test.fabricado")
    log.addHandler(logging.NullHandler())
    return PlanValidator(log)


# ---------------------------------------------------------------------------
# 1. El validador rechaza el contenido inventado
# ---------------------------------------------------------------------------

def test_bloquea_el_caso_real_del_log(validador):
    errores = validador._validar_codigo_python_ast(
        codigo=CODIGO_FABRICADO, nombre="EstructurarIdeas",
        nombres_agentes={"GenerarIdeas"}, tipos_agentes={"GenerarIdeas": "LLM"},
    )

    assert errores, "el relleno debe bloquear el plan"
    assert any("RELLENO" in e for e in errores)
    assert any("Descripción no disponible" in e for e in errores)


@pytest.mark.parametrize("relleno", [
    "Sin contenido", "sin datos", "N/A", "No disponible",
    "Lorem ipsum", "placeholder", "Texto de relleno", "Pendiente de rellenar",
])
def test_bloquea_cualquier_literal_de_relleno(validador, relleno):
    codigo = f"resultado = {{'texto': {relleno!r}}}\n"

    errores = validador._validar_codigo_python_ast(
        codigo=codigo, nombre="Paso", nombres_agentes=set(), tipos_agentes={},
    )

    assert any("RELLENO" in e for e in errores), f"no detectó {relleno!r}"


def test_no_bloquea_codigo_legitimo(validador):
    errores = validador._validar_codigo_python_ast(
        codigo=CODIGO_LEGITIMO, nombre="EstructurarIdeas",
        nombres_agentes={"GenerarIdeas"}, tipos_agentes={"GenerarIdeas": "LLM"},
    )

    assert errores == []


def test_el_plan_completo_se_rechaza(validador):
    plan = ExecutionPlan(problema_original="listar 5 ideas")
    plan.pasos = [
        StepPlan(orden=1, nombre="GenerarIdeas", tipo_agente="LLM",
                 dependencia_ids=[]),
        StepPlan(orden=2, nombre="EstructurarIdeas", tipo_agente="Python",
                 dependencia_ids=["GenerarIdeas"],
                 configuracion={"codigo": CODIGO_FABRICADO, "timeout": 30}),
    ]

    ok, errores = validador.validar_plan(plan)

    assert ok is False
    assert any("RELLENO" in e for e in errores)


# ---------------------------------------------------------------------------
# 2. El prompt lo prohíbe explícitamente
# ---------------------------------------------------------------------------

def test_el_prompt_prohibe_inventar_contenido():
    reglas = "\n".join(PromptBuilder.PYTHON_RULES)

    assert "PROHIBIDO INVENTAR CONTENIDO" in reglas
    assert "Descripción no disponible" in reglas
    # Y dice qué hacer en su lugar: dejar fallar para que se replanifique.
    assert "FALLE" in reglas
    assert "replanifique" in reglas


# ---------------------------------------------------------------------------
# 3. La lista de relleno es única y la comparten todos los gates
# ---------------------------------------------------------------------------

def test_la_lista_cubre_la_familia_no_disponible():
    for marcador in ("descripción no disponible", "no disponible",
                     "sin contenido", "lorem ipsum", "placeholder"):
        assert marcador in LITERALES_FABRICADOS


def test_es_valor_placeholder_detecta_el_relleno():
    assert es_valor_placeholder("Descripción no disponible") is True
    assert es_valor_placeholder("un texto real") is False
    assert es_valor_placeholder({"descripcion": "Descripción no disponible"}) is True


def test_es_resultado_sospechoso_detecta_el_relleno():
    """Antes tenía su propia tupla de 'vacíos' y esto pasaba el gate Nivel 1."""
    sospechoso, motivo = es_resultado_sospechoso(
        {"ideas": ["Descripción no disponible", "Sin contenido"]}
    )

    assert sospechoso is True
    assert "relleno" in motivo


def test_es_resultado_sospechoso_no_marca_contenido_real():
    sospechoso, _ = es_resultado_sospechoso(
        {"ideas": [{"titulo": "El dragón", "descripcion": "Un dragón amable"}]}
    )

    assert sospechoso is False


def test_es_resultado_sospechoso_sigue_detectando_vacio():
    assert es_resultado_sospechoso({})[0] is True
    assert es_resultado_sospechoso(None)[0] is True
    assert es_resultado_sospechoso({"a": "", "b": None})[0] is True


# ---------------------------------------------------------------------------
# 4. Cobertura sobre el corpus real (medición de 3.10)
# ---------------------------------------------------------------------------

def test_bloquea_un_segundo_caso_real_del_corpus(validador):
    """Este plan real sustituía una puntuación ausente por 'N/A'."""
    errores = validador._validar_codigo_python_ast(
        codigo=CODIGO_FABRICADO_CORPUS, nombre="FormatearResultado",
        nombres_agentes={"GenerarChiste", "EvaluarChiste"},
        tipos_agentes={"GenerarChiste": "LLM", "EvaluarChiste": "LLM"},
    )

    assert any("RELLENO" in e for e in errores)


def _cargar_herramienta():
    """Carga ``tools/cobertura_validador.py`` por ruta (no es un paquete)."""
    ruta = LOGS.parent / "tools" / "cobertura_validador.py"
    spec = importlib.util.spec_from_file_location("cobertura_validador", ruta)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


@pytest.mark.slow
@pytest.mark.skipif(
    not LOGS.is_dir(), reason="el corpus logs/ no está versionado (gitignore)"
)
def test_la_herramienta_mide_cobertura_sobre_logs_reales():
    """Muestra pequeña del corpus: la herramienta funciona y algo detecta.

    El corpus completo tarda ~40 s y ``logs/`` no está en git, así que la
    suite comprueba solo una muestra; la medición completa se lanza a mano con
    ``python tools/cobertura_validador.py``.
    """
    herramienta = _cargar_herramienta()

    informe = herramienta.analizar(LOGS, limite=40)

    assert informe["ficheros_analizados"] > 0
    assert informe["pasos_con_codigo"] > 0
    assert informe["por_regla"], "el corpus real debe disparar alguna regla"
