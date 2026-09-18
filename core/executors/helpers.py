# core/executors/helpers.py
"""
Utilidades de parseo y limpieza de respuestas del LLM.

Funciones puras sin estado. Se usan desde varios ejecutores.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def limpiar_fences_markdown(texto: str) -> str:
    """Elimina ```json ... ``` y espacios."""
    if not texto:
        return ""
    t = texto.strip()
    t = re.sub(r'^```[a-zA-Z]*\s*', '', t)
    t = re.sub(r'\s*```\s*$', '', t)
    return t.strip()


def parsear_json_robusto(texto: str) -> Any | None:
    """Intenta parsear JSON con múltiples estrategias."""
    if not texto:
        return None
    # 1. directo
    try:
        return json.loads(texto)
    except Exception:
        pass
    # 2. primer { ... último }
    try:
        ini = texto.find('{')
        fin = texto.rfind('}')
        if ini != -1 and fin != -1 and fin > ini:
            return json.loads(texto[ini:fin + 1])
    except Exception:
        pass
    # 3. primer [ ... último ]
    try:
        ini = texto.find('[')
        fin = texto.rfind(']')
        if ini != -1 and fin != -1 and fin > ini:
            return json.loads(texto[ini:fin + 1])
    except Exception:
        pass
    # 4. regex
    try:
        m = re.search(r'\{.*\}', texto, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except Exception:
        pass
    return None


def es_resultado_sospechoso(resultado: Any) -> tuple[bool, str]:
    """Detecta resultados vacíos que antes pasaban silenciosos."""
    if resultado is None:
        return True, "resultado es None"
    if resultado == {} or resultado == [] or resultado == "":
        return True, "resultado vacío"
    if isinstance(resultado, dict):
        vacios = (None, '', [], {}, 'N/A', 'null')
        if all(v in vacios for v in resultado.values()):
            return True, f"todos los valores vacíos: {list(resultado.keys())}"
        for k, v in resultado.items():
            if isinstance(v, dict) and not v:
                return True, f"clave '{k}' es dict vacío"
    return False, ""
