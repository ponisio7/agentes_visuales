# tests/test_desenvolver_contenido_web.py
"""
Tests del fallback genérico de ``_desenvolver_contenido_web`` para dicts
que contienen listas de strings (bug de las ejecuciones 442/443).
"""

from core.executors.file_executor import FileExecutor


def test_desenvolver_dict_con_lista_de_strings():
    contenido = {'titulares': ['a', 'b', 'c'], 'total': 3}

    assert FileExecutor._desenvolver_contenido_web(contenido) == 'a\nb\nc'


def test_desenvolver_dict_con_lista_vacia():
    contenido = {'items': [], 'total': 0}

    # Sin listas de strings no vacías no hay nada que desenvolver.
    assert FileExecutor._desenvolver_contenido_web(contenido) is None


def test_desenvolver_dict_con_varias_listas():
    contenido = {'a': ['x', 'y'], 'b': ['1', '2']}

    assert FileExecutor._desenvolver_contenido_web(contenido) == 'x\ny'


def test_desenvolver_lista_mixta_no_se_usa():
    """Una lista con elementos no-str no es "lista de strings"."""
    contenido = {'datos': ['a', 1, None]}

    assert FileExecutor._desenvolver_contenido_web(contenido) is None


def test_claves_conocidas_tienen_prioridad():
    """El fallback nuevo no cambia el comportamiento previo."""
    contenido = {'texto': 'principal', 'otros': ['a', 'b']}

    assert FileExecutor._desenvolver_contenido_web(contenido) == 'principal'


def test_lista_de_strings_directa_sigue_funcionando():
    assert FileExecutor._desenvolver_contenido_web(['uno', 'dos']) == 'uno\ndos'
