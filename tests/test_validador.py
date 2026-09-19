"""Pruebas del validador AST de código Python de ``PlanValidator``.

Se comprueba la función pura ``_validar_codigo_python_ast`` que detecta
SyntaxError, nombres de agente usados como variables, ``json.loads`` con
placeholder literal y sintaxis de plantilla ``{{X}}``.
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
