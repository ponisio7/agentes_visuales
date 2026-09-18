# tests/test_executor_helpers.py
"""Pruebas de las utilidades de parseo de ejecutores.

Cubre ``core/executors/helpers.py``: limpieza de fences, parseo robusto de
JSON y detección de resultados sospechosos (vacío / todos los valores
vacíos), que evita que un agente "termine bien" sin producir nada útil.
"""

from core.executors.helpers import (
    _extraer_json_balanceado,
    es_resultado_sospechoso,
    limpiar_fences_markdown,
    parsear_json_robusto,
)


class TestLimpiarFencesMarkdown:
    def test_fence_json(self):
        assert limpiar_fences_markdown('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_fence_con_espacios(self):
        assert limpiar_fences_markdown("  ```\n[1]\n```  ") == "[1]"

    def test_vacio(self):
        assert limpiar_fences_markdown("") == ""
        assert limpiar_fences_markdown(None) == ""

    def test_sin_fence(self):
        assert limpiar_fences_markdown('{"a": 1}') == '{"a": 1}'


class TestExtraerJsonBalanceado:
    def test_respeta_cadenas(self):
        texto = '{"s": "}"}'
        assert _extraer_json_balanceado(texto) == texto

    def test_texto_alrededor(self):
        assert _extraer_json_balanceado('x {"a": 1} y') == '{"a": 1}'

    def test_incompleto(self):
        assert _extraer_json_balanceado('{"a": 1') is None

    def test_vacio(self):
        assert _extraer_json_balanceado("") is None


class TestParsearJsonRobusto:
    def test_directo(self):
        assert parsear_json_robusto('{"a": 1}') == {"a": 1}

    def test_con_texto_alrededor(self):
        assert parsear_json_robusto('bla {"a": 1} bla') == {"a": 1}

    def test_markdown(self):
        assert parsear_json_robusto('```json\n{"a": 1}\n```') == {"a": 1}

    def test_array(self):
        assert parsear_json_robusto("Prefijo [1, 2] sufijo") == [1, 2]

    def test_invalido(self):
        assert parsear_json_robusto("no soy json") is None

    def test_vacio(self):
        assert parsear_json_robusto("") is None
        assert parsear_json_robusto(None) is None


class TestEsResultadoSospechoso:
    def test_none(self):
        sospechoso, razon = es_resultado_sospechoso(None)
        assert sospechoso is True
        assert "None" in razon

    def test_contenedores_vacios(self):
        for valor in ({}, [], ""):
            assert es_resultado_sospechoso(valor)[0] is True

    def test_dict_todos_vacios(self):
        sospechoso, razon = es_resultado_sospechoso(
            {"a": None, "b": "", "c": [], "d": {}, "e": "N/A", "f": "null"}
        )
        assert sospechoso is True
        assert "vacíos" in razon

    def test_dict_con_algun_valor(self):
        assert es_resultado_sospechoso({"a": 1, "b": None})[0] is False

    def test_dict_anidado_vacio(self):
        assert es_resultado_sospechoso({"a": {}})[0] is True

    def test_dict_anidado_con_datos(self):
        assert es_resultado_sospechoso({"a": {"b": 1}})[0] is False

    def test_escalar(self):
        assert es_resultado_sospechoso(0)[0] is False
        assert es_resultado_sospechoso(False)[0] is False

    def test_lista_con_datos(self):
        assert es_resultado_sospechoso([1])[0] is False
