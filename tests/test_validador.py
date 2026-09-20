"""Pruebas del validador AST de código Python de ``PlanValidator``.

Se comprueba la función pura ``_validar_codigo_python_ast`` que detecta
SyntaxError, nombres de agente usados como variables, alias de contexto
inventados (``dependencias``), ``json.loads`` con placeholder literal,
nombres inventados usados como literal (N2) y sintaxis de plantilla ``{{X}}``.
"""
import pytest

from core.problem_solver.validator import PlanValidator

NOMBRES = {"GenerarCuento", "CrearImagenes"}


@pytest.mark.parametrize(
    "codigo, esperado, descripcion",
    [
        (
            "datos = GenerarCuento\ntexto = datos['cuento']",
            1,
            "Nombre de agente como variable",
        ),
        (
            "datos = {{CrearImagenes}}['imagenes']",
            2,
            "Sintaxis de plantilla (set-like + Jinja)",
        ),
        (
            "import json\ndatos = json.loads('''{datos_llm}''')",
            1,
            "json.loads con placeholder",
        ),
        (
            "x = 1 / 0",
            0,
            "ZeroDivisionError (no detectado por AST)",
        ),
        (
            "datos = contexto.get('GenerarCuento', {})",
            0,
            "Código correcto",
        ),
        (
            "cuento = dependencias.get('GenerarCuento', '')",
            1,
            "N1: alias 'dependencias' como variable suelta (NameError en sandbox)",
        ),
        (
            "dependencias = contexto\ncuento = dependencias.get('GenerarCuento', '')",
            0,
            "N1: 'dependencias' ligado localmente antes de usarse es legítimo",
        ),
        (
            "html = f'''<style>body {{ margin: 0; }}</style>'''",
            0,
            "Escape de llaves en f-string (CSS embebido): NO es plantilla",
        ),
        (
            "html = f'''<style>body {{ font-family: Arial; }}</style>'''\n"
            "resultado = {'contenido': html}",
            0,
            "CSS embebido con punto y coma/llaves: NO es plantilla",
        ),
    ],
)
def test_validar_codigo_python_ast(codigo, esperado, descripcion):
    errores = PlanValidator._validar_codigo_python_ast(
        codigo=codigo,
        nombre="TestPaso",
        nombres_agentes=NOMBRES,
    )
    assert len(errores) == esperado, f"{descripcion}: {errores}"


# ---------------------------------------------------------------------------
# N2: el LLM se INVENTA UN NOMBRE para el contenido y lo usa como literal.
#
# ``__FARSI_JSON__`` no es especial: es un nombre que el LLM se inventó para
# el caso concreto (traducir noticias en farsi). Podría haber sido
# ``__UCRAINIAN_JSON__``, ``__NEWS_HTML__``, ``__CUENTO_DRAGON__`` o
# ``__CALCULADORA_JS__``. Por eso la regla NO se ancla al caso concreto ni a
# una lista de nombres: detecta la forma del nombre inventado
# (MAYÚSCULAS_CON_GUIONES_BAJOS, con o sin envoltura ``__...__``).
#
# Decisiones de diseño documentadas (ver HARNESS_REPORT_v3.2.1.md):
#   - Se exige al menos un guion bajo, para no marcar constantes de una sola
#     palabra legítimas en código generado (``"GET"``, ``"POST"``, ``"CSV"``).
#   - Un nombre en minúsculas (``__key__``, ``__main__``) NO bloquea: son
#     dunders legítimos de Python.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "codigo, esperado, descripcion, aguja",
    [
        # --- Los casos del bug, tal como se reprodujeron ---
        (
            'import json\nfarsi = json.loads("__FARSI_JSON__")',
            1,
            'N2: json.loads("__FARSI_JSON__") (comillas dobles)',
            "__FARSI_JSON__",
        ),
        (
            "import json\nfarsi = json.loads('__FARSI_JSON__')",
            1,
            "N2: json.loads('__FARSI_JSON__') (comillas simples)",
            "__FARSI_JSON__",
        ),
        (
            'farsi = "__FARSI_JSON__"',
            1,
            "N2: asignación directa del nombre inventado",
            "__FARSI_JSON__",
        ),
        (
            'import json\nfarsi = json.loads("""__FARSI_JSON__""")',
            1,
            "N2: json.loads con triple comilla",
            "__FARSI_JSON__",
        ),
        # --- Generalidad: el mismo patrón con OTROS nombres inventados ---
        (
            'import json\ntexto = json.loads("__UCRAINIAN_JSON__")',
            1,
            "N2 general: __UCRAINIAN_JSON__ (otro caso de uso)",
            "__UCRAINIAN_JSON__",
        ),
        (
            'import json\nhtml = json.loads(\'__NEWS_HTML__\')',
            1,
            "N2 general: __NEWS_HTML__",
            "__NEWS_HTML__",
        ),
        (
            'cuento = "__CUENTO_DRAGON__"',
            1,
            "N2 general: __CUENTO_DRAGON__",
            "__CUENTO_DRAGON__",
        ),
        (
            'script = "__CALCULADORA_JS__"',
            1,
            "N2 general: __CALCULADORA_JS__",
            "__CALCULADORA_JS__",
        ),
        (
            'datos = "FARSI_JSON"',
            1,
            "N2 general: nombre inventado SIN envoltura __",
            "FARSI_JSON",
        ),
        (
            'datos = "_NOTICIAS_ES"',
            1,
            "N2 general: nombre inventado con un solo guion bajo",
            "_NOTICIAS_ES",
        ),
        # --- Regresión: lo que ya funcionaba sigue funcionando ---
        (
            'import json\nfarsi = json.loads("""{farsi_json}""")',
            1,
            "Regresión N2: el placeholder con llaves sigue detectado",
            "{farsi_json}",
        ),
        # --- Falsos positivos a evitar ---
        (
            "print(__name__)",
            0,
            "Falso positivo: __name__ es un dunder legítimo (minúsculas)",
            None,
        ),
        (
            'import logging\nlogging.info(f"file={__file__}")',
            0,
            "Falso positivo: __file__ dentro de f-string es un dunder legítimo",
            None,
        ),
        (
            'if __name__ == "__main__":\n    pass',
            0,
            "Falso positivo: el literal '__main__' es legítimo (minúsculas)",
            None,
        ),
        (
            'config = {"__key__": "value"}',
            0,
            "Falso positivo: '__key__' en minúsculas",
            None,
        ),
        (
            'metodo = "GET"',
            0,
            "Falso positivo: constante de una sola palabra 'GET'",
            None,
        ),
        (
            'formato = "CSV"',
            0,
            "Falso positivo: constante de una sola palabra 'CSV'",
            None,
        ),
        (
            'cabecera = {"Content-Type": "application/json"}',
            0,
            "Falso positivo: cabecera HTTP normal",
            None,
        ),
        (
            'codificacion = "UTF-8"',
            0,
            "Falso positivo: 'UTF-8' no es un identificador",
            None,
        ),
        (
            'contexto["__FARSI_JSON__"] = 1',
            0,
            "LHS: el nombre es una clave elegida por el código, no un placeholder",
            None,
        ),
    ],
)
def test_n2_nombre_inventado_usado_como_literal(codigo, esperado, descripcion, aguja):
    errores = PlanValidator._validar_codigo_python_ast(
        codigo=codigo,
        nombre="TestPaso",
        nombres_agentes=NOMBRES,
    )
    assert len(errores) == esperado, f"{descripcion}: {errores}"
    if esperado == 1:
        assert errores[0].startswith("BLOQUEANTE: TestPaso:"), errores[0]
        assert aguja in errores[0], errores[0]
