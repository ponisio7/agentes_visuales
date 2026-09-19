# core/utils/json_llm.py
"""
Utilidades para extraer JSON de respuestas de LLM.

Los LLMs suelen devolver JSON envuelto en:
  - fences markdown (```json ... ```)
  - texto antes/después ("Aquí tienes el JSON: {...}")
  - comillas simples en lugar de dobles
  - comentarios // o #
  - trailing commas

Esta función intenta recuperar el JSON útil en orden de robustez.
"""

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


def _quitar_fences(texto: str) -> str:
    """Elimina fences markdown ```json ... ``` o ``` ... ```."""
    texto = re.sub(r'^```(?:json|javascript|js)?\s*', '', texto.strip(), flags=re.I)
    texto = re.sub(r'\s*```$', '', texto)
    return texto.strip()


def _primer_objeto_json(texto: str) -> str | None:
    """
    Devuelve el primer objeto/array JSON balanceado que aparezca en el texto.
    Útil cuando el LLM escribe texto antes o después del JSON.
    """
    inicio = None
    for i, c in enumerate(texto):
        if c in '{[':
            inicio = i
            break
    if inicio is None:
        return None

    abierto = texto[inicio]
    cerrado = '}' if abierto == '{' else ']'
    profundidad = 0
    en_string = False
    escape = False

    for i in range(inicio, len(texto)):
        c = texto[i]

        if escape:
            escape = False
            continue
        if c == '\\':
            escape = True
            continue
        if c == '"':
            en_string = not en_string
            continue
        if en_string:
            continue

        if c == abierto:
            profundidad += 1
        elif c == cerrado:
            profundidad -= 1
            if profundidad == 0:
                return texto[inicio:i + 1]

    return None


def _limpiar_json_sucio(texto: str) -> str:
    """Aplica limpiezas heurísticas a un JSON malformado por el LLM."""
    # Quitar comentarios // y #
    texto = re.sub(r'//[^\n]*', '', texto)
    texto = re.sub(r'#[^\n]*', '', texto)
    # Quitar trailing commas: {"a": 1,} → {"a": 1}
    texto = re.sub(r',(\s*[}\]])', r'\1', texto)
    # Comillas simples a dobles en claves: {'a': 1} → {"a": 1}
    # (solo si no hay comillas dobles ya)
    if '"' not in texto:
        texto = re.sub(r"'", '"', texto)
    # Claves sin comillas: {a: 1, b: 2} → {"a": 1, "b": 2}
    texto = re.sub(r'([{,])\s*([A-Za-z_][A-Za-z0-9_]*)\s*:', r'\1"\2":', texto)
    return texto.strip()


def extraer_json_de_llm(texto: Any) -> dict | list | None:
    """
    Intenta extraer un objeto/array JSON de una respuesta de LLM.

    Estrategia, en orden:
      1. Si ya es dict/list, devolverlo tal cual.
      2. Quitar fences markdown y probar json.loads directo.
      3. Extraer el primer objeto balanceado y probar json.loads.
      4. Limpiar el JSON (comentarios, trailing commas, comillas) y reintentar.

    Devuelve dict, list o None si no se pudo parsear nada.
    """
    if texto is None:
        return None
    if isinstance(texto, (dict, list)):
        return texto
    if not isinstance(texto, str):
        return None

    candidatos = []

    # 1) Directo tras quitar fences
    limpio = _quitar_fences(texto)
    candidatos.append(limpio)

    # 2) Primer objeto balanceado
    objeto = _primer_objeto_json(limpio)
    if objeto and objeto != limpio:
        candidatos.append(objeto)

    # 3) Limpieza heurística sobre los candidatos
    for c in list(candidatos):
        candidatos.append(_limpiar_json_sucio(c))

    for c in candidatos:
        if not c:
            continue
        try:
            data = json.loads(c)
            if isinstance(data, (dict, list)):
                return data
        except (json.JSONDecodeError, ValueError):
            continue

    logger.debug(
        "extraer_json_de_llm: no se pudo parsear. Primeros 200 chars: %r",
        texto[:200],
    )
    return None


__all__ = ["extraer_json_de_llm"]