"""
Test de contrato: GenerarCuento debe devolver JSON con 'cuento' y
'descripciones_imagenes'. Si devuelve texto natural, GenerarURLs falla.
"""
import json
from core.executors.helpers import parsear_json_robusto, es_resultado_sospechoso


def test_respuesta_texto_natural_falla():
    """La respuesta actual de GenerarCuento (texto natural) NO cumple el contrato."""
    respuesta_llm = "Título: El reloj de arena\n\nEn un pueblo costero vivía Mateo..."
    datos = parsear_json_robusto(respuesta_llm)
    assert datos is None, "texto natural no es JSON"
    # El consumidor falla:
    descripciones = (datos or {}).get("descripciones_imagenes", [])
    assert descripciones == [], "sin descripciones, GenerarURLs falla"


def test_respuesta_json_valida_pasa():
    """La respuesta esperada SÍ cumple el contrato."""
    respuesta_llm = json.dumps({
        "cuento": "Había una vez...",
        "descripciones_imagenes": ["a castle at sunset", "a dragon flying"],
    })
    datos = parsear_json_robusto(respuesta_llm)
    assert datos is not None
    assert datos["cuento"].startswith("Había")
    assert len(datos["descripciones_imagenes"]) == 2