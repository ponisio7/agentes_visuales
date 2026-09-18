# core/utils.py
"""
Utilidades para extracción de JSON de respuestas del LLM.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def extraer_json_de_llm(respuesta: str) -> dict[str, Any] | list | None:
    """
    Extrae un JSON válido de una respuesta del LLM.
    
    Estrategias:
    1. Limpiar markdown (```json ... ```)
    2. Buscar el primer { o [ y último } o ]
    3. Reparar errores comunes (comillas simples, comas finales)
    
    Args:
        respuesta: Texto de respuesta del LLM
        
    Returns:
        Optional[Dict | list]: JSON parseado, o None si falla
    """
    if not respuesta or not respuesta.strip():
        return None
    
    # ── 1. Limpiar markdown ──
    limpio = re.sub(r'^```json\s*', '', respuesta, flags=re.MULTILINE)
    limpio = re.sub(r'^```\s*', '', limpio, flags=re.MULTILINE)
    limpio = re.sub(r'\s*```$', '', limpio, flags=re.MULTILINE)
    limpio = limpio.strip()
    
    # ── 2. Buscar JSON (objeto o array) ──
    # Buscar el primer { o [
    inicio_objeto = limpio.find('{')
    inicio_array = limpio.find('[')
    
    # Determinar cuál aparece primero
    if inicio_objeto == -1 and inicio_array == -1:
        logger.debug(f"No se encontró JSON en la respuesta: {limpio[:100]}...")
        return None
    
    if inicio_objeto == -1:
        inicio = inicio_array
        fin = limpio.rfind(']')
        es_array = True
    elif inicio_array == -1:
        inicio = inicio_objeto
        fin = limpio.rfind('}')
        es_array = False
    else:
        if inicio_objeto < inicio_array:
            inicio = inicio_objeto
            fin = limpio.rfind('}')
            es_array = False
        else:
            inicio = inicio_array
            fin = limpio.rfind(']')
            es_array = True
    
    if fin == -1 or fin <= inicio:
        logger.debug(f"No se encontró JSON completo: {limpio[:100]}...")
        return None
    
    json_str = limpio[inicio:fin + 1]
    
    # ── 3. Intentar parsear directamente ──
    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        pass
    
    # ── 4. Reparar errores comunes ──
    try:
        # Comillas simples → dobles (claves y strings)
        reparado = re.sub(r"([{,])\s*'([^']*)'\s*:", r'\1"\2":', json_str)
        reparado = re.sub(r":\s*'([^']*)'", r': "\1"', reparado)
        
        # Claves sin comillas → con comillas
        reparado = re.sub(r'([{,])\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', reparado)
        
        # Comas finales
        reparado = re.sub(r',\s*}', '}', reparado)
        reparado = re.sub(r',\s*]', ']', reparado)
        
        # Strings mal formados
        reparado = re.sub(r'\\"', '"', reparado)
        reparado = re.sub(r'"{2,}', '"', reparado)
        
        # Eliminar caracteres de control no imprimibles
        reparado = ''.join(char for char in reparado if ord(char) >= 32 or char in '\n\r\t')
        
        # Intentar parsear
        return json.loads(reparado)
        
    except json.JSONDecodeError as e:
        logger.debug(f"No se pudo reparar el JSON: {e}")
        return None


def limpiar_codigo(codigo: str) -> str:
    """
    Limpia código de posibles marcas de markdown.
    
    Args:
        codigo: Código con posibles marcas de markdown
        
    Returns:
        str: Código limpio
    """
    codigo = re.sub(r'^```\w*\s*\n', '', codigo, flags=re.MULTILINE)
    codigo = re.sub(r'\n```\s*$', '', codigo, flags=re.MULTILINE)
    return codigo.strip()
