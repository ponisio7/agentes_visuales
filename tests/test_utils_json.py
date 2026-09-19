# tests/test_utils_json.py
"""Pruebas de robustez para la extracción de JSON del LLM (``core/utils.py``).

Estas funciones son la primera línea de defensa contra respuestas
malformadas del LLM. Un fallo aquí rompe el pipeline completo, por lo que
se cubren casos límite: llaves dentro de cadenas, escapes, JSON
concatenados, markdown y reparación de errores comunes.
"""

from core.utils import (
    extraer_json_balanceado,
    extraer_json_de_llm,
    limpiar_codigo,
)


class TestExtraerJsonBalanceado:
    """El escáner debe respetar cadenas y detenerse en el cierre correcto."""

    def test_vacio_o_none(self):
        assert extraer_json_balanceado("") is None
        assert extraer_json_balanceado(None) is None

    def test_sin_json(self):
        assert extraer_json_balanceado("esto no tiene llaves") is None

    def test_objeto_simple(self):
        assert extraer_json_balanceado('{"a": 1}') == '{"a": 1}'

    def test_array_simple(self):
        assert extraer_json_balanceado("[1, 2, 3]") == "[1, 2, 3]"

    def test_llaves_dentro_de_cadena(self):
        texto = '{"a": "un } y un { dentro"}'
        assert extraer_json_balanceado(texto) == texto

    def test_comillas_escapadas(self):
        texto = '{"a": "dice \\"hola\\" y sigue"}'
        assert extraer_json_balanceado(texto) == texto

    def test_texto_alrededor(self):
        assert extraer_json_balanceado('bla bla {"a": 1} bla') == '{"a": 1}'

    def test_json_concatenados_devuelve_el_primero(self):
        assert extraer_json_balanceado('{"a": 1}{"b": 2}') == '{"a": 1}'

    def test_anidados(self):
        assert extraer_json_balanceado('{"a": {"b": [1, 2]}}') == '{"a": {"b": [1, 2]}}'

    def test_json_incompleto(self):
        assert extraer_json_balanceado('{"a": [1, 2') is None

    def test_cadena_sin_cerrar(self):
        # La cadena nunca se cierra: no se debe devolver el objeto.
        assert extraer_json_balanceado('{"a": "sin cerrar') is None


class TestExtraerJsonDeLlm:
    """Parseo tolerante de respuestas reales del LLM."""

    def test_vacio(self):
        assert extraer_json_de_llm("") is None
        assert extraer_json_de_llm("   ") is None
        assert extraer_json_de_llm(None) is None

    def test_texto_natural(self):
        assert extraer_json_de_llm("Lo siento, no puedo responder eso.") is None

    def test_json_directo(self):
        assert extraer_json_de_llm('{"clave": "valor"}') == {"clave": "valor"}

    def test_json_con_texto_alrededor(self):
        resp = 'Aquí está el resultado:\n{"clave": 2}\nEspero que sirva.'
        assert extraer_json_de_llm(resp) == {"clave": 2}

    def test_markdown_fence(self):
        resp = '```json\n{"clave": 3}\n```'
        assert extraer_json_de_llm(resp) == {"clave": 3}

    def test_markdown_fence_sin_lenguaje(self):
        resp = "```\n[1, 2, 3]\n```"
        assert extraer_json_de_llm(resp) == [1, 2, 3]

    def test_comillas_simples(self):
        assert extraer_json_de_llm("{'a': 1, 'b': 2}") == {"a": 1, "b": 2}

    def test_claves_sin_comillas(self):
        assert extraer_json_de_llm("{a: 1, b: 2}") == {"a": 1, "b": 2}

    def test_comas_finales(self):
        assert extraer_json_de_llm('{"a": 1, "b": 2,}') == {"a": 1, "b": 2}
        assert extraer_json_de_llm("[1, 2, 3,]") == [1, 2, 3]

    def test_llave_extra_en_cadena(self):
        resp = '{"texto": "usa } para cerrar", "n": 1}'
        assert extraer_json_de_llm(resp) == {"texto": "usa } para cerrar", "n": 1}

    def test_multiple_json_devuelve_el_primero(self):
        assert extraer_json_de_llm('{"a": 1}\n{"b": 2}') == {"a": 1}


class TestLimpiarCodigo:
    """Limpieza de fences de markdown en bloques de código."""

    def test_fence_python(self):
        assert limpiar_codigo("```python\nprint('hola')\n```") == "print('hola')"

    def test_fence_sin_lenguaje(self):
        assert limpiar_codigo("```\nx = 1\n```") == "x = 1"

    def test_sin_fence(self):
        assert limpiar_codigo("x = 1") == "x = 1"

    def test_vacio(self):
        assert limpiar_codigo("") == ""


def test_ignora_json_de_plantilla_y_coge_el_siguiente():
    """El modelo suele echar el ejemplo en su razonamiento antes de responder."""
    respuesta = (
        'We need JSON {"datos": [{"url": "...", "texto": "..."}]} bla bla '
        'y la respuesta es {"datos": [{"url": "http://x", "texto": "hola"}]}'
    )

    assert extraer_json_de_llm(respuesta) == {
        "datos": [{"url": "http://x", "texto": "hola"}]
    }


def test_json_solo_plantilla_devuelve_none():
    assert extraer_json_de_llm('pienso {"a": "..."} y ya está') is None
