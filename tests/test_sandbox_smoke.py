"""Smoke tests del sandbox de Python.

Cubren casos que históricamente rompían la construcción del script:
diccionarios con llaves, f-strings con llaves y docstrings con triples
comillas.
"""
from core.sandbox import PythonSandbox


def test_diccionario_con_llaves():
    ok, msg, meta = PythonSandbox.ejecutar(
        "datos = {'a': 1, 'b': 2}\nresultado = sum(datos.values())",
        {},
    )
    assert ok is True, msg
    assert meta == 3


def test_fstring_con_llaves():
    ok, msg, meta = PythonSandbox.ejecutar(
        'nombre = "pepe"\n'
        'resultado = f"Hola {nombre}, tienes {2+3} mensajes"',
        {},
    )
    assert ok is True, msg
    assert meta == "Hola pepe, tienes 5 mensajes"


def test_docstring_con_triple_comilla():
    codigo = (
        "def foo():\n"
        '    """Docstring normal"""\n'
        "    return 42\n"
        "resultado = foo()"
    )
    ok, msg, meta = PythonSandbox.ejecutar(codigo, {})
    assert ok is True, msg
    assert meta == 42
