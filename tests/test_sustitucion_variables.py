# tests/test_sustitucion_variables.py
"""
Tests de la sustitución de variables del contexto en las configuraciones
de los agentes, incluidas rutas con índice (``Dep.lista[0].campo``).
"""

from core.agent import Agente, TipoAgente
from core.executors import content_extractor
from core.executors.content_extractor import (
    sustituir_variables,
    variables_disponibles,
)

CONTEXTO = {
    "Buscar": {
        "resultados": [
            {"title": "T1", "href": "https://ejemplo.test/1", "meta": {"lang": "uk"}},
            {"title": "T2", "href": "https://ejemplo.test/2", "meta": {"lang": "fa"}},
        ]
    }
}


def _variables():
    agente = Agente(nombre="Navegar", tipo=TipoAgente.BROWSER)
    return variables_disponibles(agente, CONTEXTO)


def test_sustituye_ruta_indexada_con_corchetes():
    variables = _variables()

    assert sustituir_variables("{Buscar.resultados[0].href}", variables) == \
        "https://ejemplo.test/1"


def test_sustituye_ruta_indexada_con_punto():
    variables = _variables()

    assert sustituir_variables("{Buscar.resultados.1.href}", variables) == \
        "https://ejemplo.test/2"


def test_sustituye_ruta_anidada_profunda():
    variables = _variables()

    assert sustituir_variables("{Buscar.resultados[1].meta.lang}", variables) == "fa"


def test_sustituye_lista_completa_sin_indice():
    """El comportamiento previo (lista entera como JSON) se mantiene."""
    variables = _variables()

    resultado = sustituir_variables("{Buscar.resultados}", variables)

    assert resultado.startswith("[")
    assert '"title": "T1"' in resultado


def test_clave_mas_larga_tiene_prioridad():
    """No debe sustituirse el prefijo de una ruta más específica."""
    variables = _variables()

    assert sustituir_variables("x={Buscar.resultados[0].href}", variables) == \
        "x=https://ejemplo.test/1"


def test_indice_fuera_del_limite_no_se_sustituye():
    variables = _variables()

    # El aplanado se acota a _MAX_ITEMS_VARIABLES por lista.
    plantilla = f"{{Buscar.resultados[{content_extractor._MAX_ITEMS_VARIABLES}].href}}"

    assert sustituir_variables(plantilla, variables) == plantilla


def test_numero_de_variables_acotado():
    contexto = {"Dep": {"items": [{"a": i, "b": i, "c": i} for i in range(200)]}}

    variables = variables_disponibles(Agente(nombre="X", tipo=TipoAgente.HTTP), contexto)

    assert len(variables) <= content_extractor._MAX_VARIABLES
