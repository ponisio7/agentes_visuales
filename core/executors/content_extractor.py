# core/executors/content_extractor.py
"""
Extracción inteligente de contenido desde resultados de agentes,
sustitución de variables y utilidades relacionadas.
"""

import os
import json
import shlex
import logging
from typing import Dict, Any, Optional, Set, List

from core.agent import Agente

from .security import COMANDOS_PRIVILEGIADOS

logger = logging.getLogger(__name__)


# ============================================================
# CLAVES PRIORITARIAS PARA EXTRACCIÓN
# ============================================================

_CLAVES_CONTENIDO_PRIORITARIAS = (
    'html',
    'markdown',
    'json',
    'contenido',
    'respuesta_limpia',
    'respuesta',
    'body',
    'stdout',
    'items',
    'resultado',
    'data',
    'texto',
    'output',
    '_av_json',
    '_av_respuesta_limpia',
    '_av_respuesta',
)

_MAX_PROFUNDIDAD_EXTRACCION = 5


# ============================================================
# EXTRACCIÓN RECURSIVA
# ============================================================

def extraer_contenido_relevante(
    valor: Any,
    _profundidad: int = 0,
    _visitados: Optional[Set[int]] = None
) -> Any:
    """
    Extrae el contenido más relevante de un resultado de agente.
    Búsqueda recursiva con límite de profundidad y anti-ciclos.
    """
    if _profundidad > _MAX_PROFUNDIDAD_EXTRACCION:
        return valor

    if valor is None:
        return None
    if isinstance(valor, (str, int, float, bool)):
        return valor
    if isinstance(valor, list):
        return valor

    if not isinstance(valor, dict):
        return str(valor)

    if _visitados is None:
        _visitados = set()
    valor_id = id(valor)
    if valor_id in _visitados:
        return valor
    _visitados.add(valor_id)

    for clave in _CLAVES_CONTENIDO_PRIORITARIAS:
        if clave not in valor:
            continue
        encontrado = valor[clave]
        if encontrado is None:
            continue
        if isinstance(encontrado, str) and not encontrado.strip():
            continue
        if isinstance(encontrado, (list, dict)) and not encontrado:
            continue

        if isinstance(encontrado, (str, int, float, bool)):
            return encontrado
        if isinstance(encontrado, dict):
            resultado_recursivo = extraer_contenido_relevante(
                encontrado, _profundidad + 1, _visitados
            )
            if resultado_recursivo is not encontrado:
                return resultado_recursivo
        if isinstance(encontrado, list):
            return encontrado

    for clave, sub_valor in valor.items():
        if isinstance(sub_valor, dict) and sub_valor:
            resultado_recursivo = extraer_contenido_relevante(
                sub_valor, _profundidad + 1, _visitados
            )
            if resultado_recursivo is not sub_valor:
                return resultado_recursivo

    return valor


# ============================================================
# VARIABLES Y SUSTITUCIÓN
# ============================================================

def variables_disponibles(agente: Agente, contexto: Dict) -> Dict[str, str]:
    """Construye el diccionario de variables sustituibles."""
    import time as _time
    contexto = contexto or {}
    ahora = _time.localtime()

    variables = {
        "contexto": json.dumps(contexto, indent=2, default=str) if contexto else "",
        "resultado": json.dumps(contexto, indent=2, default=str) if contexto else "",
        "nombre": agente.nombre,
        "descripcion": agente.descripcion,
        "id": agente.id,
        "fecha": _time.strftime("%Y-%m-%d", ahora),
        "hora": _time.strftime("%H:%M:%S", ahora),
    }

    for clave, valor in contexto.items():
        if clave in variables:
            continue
        valor_extraido = extraer_contenido_relevante(valor)
        if isinstance(valor_extraido, str):
            variables[clave] = valor_extraido
        elif isinstance(valor_extraido, (dict, list)):
            try:
                variables[clave] = json.dumps(
                    valor_extraido, indent=2, default=str, ensure_ascii=False
                )
            except (TypeError, ValueError):
                variables[clave] = str(valor_extraido)
        else:
            variables[clave] = str(valor_extraido)

    return variables


def sustituir_variables(texto: str, variables: Dict[str, str]) -> str:
    """Sustituye variables en un texto usando el patrón {variable}."""
    if not texto or "{" not in texto or not variables:
        return texto
    import re as _re
    pattern = _re.compile(
        r"\{\s*(" + "|".join(_re.escape(k) for k in variables.keys()) + r")\s*\}"
    )
    return pattern.sub(lambda m: variables.get(m.group(1), m.group(0)), texto)


def resolver_ruta_en_contexto(contexto: Dict, ruta: str) -> Any:
    """Resuelve una ruta en el contexto (ej: 'Dependencia.clave')."""
    if not ruta:
        return None
    partes = ruta.split('.')
    valor = contexto.get(partes[0])
    for parte in partes[1:]:
        if isinstance(valor, dict):
            valor = valor.get(parte)
        else:
            return None
    return valor


# ============================================================
# DETECCIÓN DE COMANDOS PRIVILEGIADOS
# ============================================================

def extraer_primer_comando(comando: str) -> Optional[str]:
    """Extrae el primer comando 'real' de una cadena shell."""
    if not comando or not comando.strip():
        return None

    try:
        tokens = shlex.split(comando)
    except ValueError:
        tokens = comando.split()

    PALABRAS_RESERVADAS = frozenset({
        'if', 'then', 'else', 'elif', 'fi', 'while', 'do', 'done',
        'for', 'in', 'case', 'esac', 'function', 'return',
    })

    for token in tokens:
        if not token:
            continue
        if token in ('&&', '||', '|', ';', '&', '(', ')', '{', '}'):
            continue
        if token.startswith('-'):
            continue
        if '=' in token and not token.startswith('/') and not token.startswith('./'):
            parte_izq, _, _ = token.partition('=')
            if parte_izq.isidentifier():
                continue
        if token in PALABRAS_RESERVADAS:
            continue
        return os.path.basename(token)
    return None


def comando_requiere_root(comando: str) -> bool:
    """Detecta si un comando shell requiere privilegios de root."""
    if not comando:
        return False
    comando_stripped = comando.strip()
    if comando_stripped.startswith(('sudo ', 'pkexec ', 'doas ')):
        return False
    primer_comando = extraer_primer_comando(comando_stripped)
    if not primer_comando:
        return False
    return primer_comando in COMANDOS_PRIVILEGIADOS