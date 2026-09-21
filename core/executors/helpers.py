# core/executors/helpers.py
"""
Utilidades de parseo y limpieza de respuestas del LLM.

Funciones puras sin estado. Se usan desde varios ejecutores.
"""

import json
import logging
import re
from typing import Any

from core.utils import es_valor_placeholder

logger = logging.getLogger(__name__)


def limpiar_fences_markdown(texto: str) -> str:
    """Elimina ```json ... ``` y espacios."""
    if not texto:
        return ""
    t = texto.strip()
    t = re.sub(r'^```[a-zA-Z]*\s*', '', t)
    t = re.sub(r'\s*```\s*$', '', t)
    return t.strip()


def _extraer_json_balanceado(texto: str) -> str | None:
    """Extrae el primer objeto/array JSON balanceado de un texto.

    Respeta las llaves dentro de cadenas y se detiene en el cierre que
    equilibra la apertura, así funciona con texto adicional alrededor o
    con varios JSON concatenados.
    """
    if not texto:
        return None
    inicio = None
    apertura = None
    for i, c in enumerate(texto):
        if c in "[{":
            inicio = i
            apertura = c
            break
    if inicio is None:
        return None

    cierre = "]" if apertura == "[" else "}"
    profundidad = 0
    en_cadena = False
    escape = False
    for i in range(inicio, len(texto)):
        c = texto[i]
        if en_cadena:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                en_cadena = False
            continue
        if c == '"':
            en_cadena = True
        elif c == apertura:
            profundidad += 1
        elif c == cierre:
            profundidad -= 1
            if profundidad == 0:
                return texto[inicio:i + 1]
    return None


def parsear_json_robusto(texto: str) -> Any | None:
    """Intenta parsear JSON con múltiples estrategias."""
    if not texto:
        return None
    # 1. directo
    try:
        return json.loads(texto)
    except Exception:
        pass
    # 2. primer valor {...}/[...] balanceado
    candidato = _extraer_json_balanceado(texto)
    if candidato:
        try:
            return json.loads(candidato)
        except Exception:
            pass
    # 3. limpiar fences markdown y reintentar
    try:
        limpio = limpiar_fences_markdown(texto)
        if limpio and limpio != texto:
            return json.loads(limpio)
    except Exception:
        pass
    return None


def es_resultado_sospechoso(resultado: Any) -> tuple[bool, str]:
    """Detecta resultados vacíos o de RELLENO que antes pasaban silenciosos.

    Usa ``es_valor_placeholder`` como única fuente de verdad (3.3): antes tenía
    su propia tupla de «valores vacíos» que no incluía el contenido fabricado
    (``'Descripción no disponible'`` y compañía), así que el relleno del LLM
    pasaba el gate Nivel 1 como si fuera un resultado válido.
    """
    if resultado is None:
        return True, "resultado es None"
    if isinstance(resultado, dict):
        # El caso dict se comprueba primero para poder nombrar las claves.
        if all(es_valor_placeholder(v) for v in resultado.values()):
            return True, (
                f"todos los valores vacíos o de relleno: {list(resultado.keys())}"
            )
        for k, v in resultado.items():
            if isinstance(v, dict) and not v:
                return True, f"clave '{k}' es dict vacío"
        return False, ""
    if es_valor_placeholder(resultado):
        return True, "resultado vacío o de relleno"
    return False, ""
