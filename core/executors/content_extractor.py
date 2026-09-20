# core/executors/content_extractor.py
"""
Extracción inteligente de contenido desde resultados de agentes,
sustitución de variables y utilidades relacionadas.
"""

import json
import logging
import os
import shlex
from typing import Any

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
    'imagenes',
    '_av_json',
    '_av_respuesta_limpia',
    '_av_respuesta',
)

_MAX_PROFUNDIDAD_EXTRACCION = 5

# ✅ FIX 1: separamos "estructura de documento" de "contenido crudo".
# Antes, 'html' y 'markdown' estaban en _CLAVES_ESTRUCTURA_DOCUMENTO,
# lo que impedía desenvolver {'html': '...'} y producía JSON basura
# en los archivos .html.

# Claves que indican "este dict ES un documento estructurado" (no desenvolver).
_CLAVES_ESTRUCTURA_DOCUMENTO = (
    'titulo', 'title',
    'cuento', 'informe', 'articulo',
    'imagenes', 'images',
    'secciones', 'sections', 'partes',
)

# Claves que contienen CONTENIDO CRUDO (un string que ES el output).
_CLAVES_CONTENIDO_CRUDO = (
    'html', 'markdown', 'md', 'svg', 'xml',
    'contenido', 'content',
    'texto', 'text', 'body', 'cuerpo',
    'respuesta_limpia', 'respuesta',
    'codigo', 'code', 'source', 'fuente',
)


# ============================================================
# EXTRACCIÓN RECURSIVA
# ============================================================

def extraer_contenido_relevante(
    valor: Any,
    _profundidad: int = 0,
    _visitados: set[int] | None = None
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

    # ✅ FIX 1: la condición de "documento completo" ahora exige que
    # NO haya contenido crudo con valor. Antes bastaba con que
    # existiera 'html' para devolver el dict entero.
    tiene_estructura = any(
        clave in valor for clave in _CLAVES_ESTRUCTURA_DOCUMENTO
    )
    tiene_contenido_crudo = any(
        clave in valor and valor[clave] not in (None, '', [], {})
        for clave in _CLAVES_CONTENIDO_CRUDO
    )
    if tiene_estructura and not tiene_contenido_crudo:
        return valor

    if _visitados is None:
        _visitados = set()
    valor_id = id(valor)
    if valor_id in _visitados:
        return valor
    _visitados.add(valor_id)

    # Si hay MÚLTIPLES claves prioritarias con contenido útil,
    # devolvemos el dict ENTERO para no perder información.
    claves_utiles = [
        clave for clave in _CLAVES_CONTENIDO_PRIORITARIAS
        if clave in valor and valor[clave] not in (None, '', [], {})
    ]
    if len(claves_utiles) >= 2:
        return valor

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

    for sub_valor in valor.values():
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

# Límites del aplanado de variables: evitan explotar en contextos grandes.
_MAX_PROFUNDIDAD_VARIABLES = 6
_MAX_ITEMS_VARIABLES = 20
_MAX_VARIABLES = 500


def _valor_variable(valor: Any) -> str:
    """Representación de un valor para sustituir en un texto."""
    if isinstance(valor, str):
        return valor
    try:
        return json.dumps(valor, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(valor)


def _aplanar_hijos(valor: Any, prefijo: str, variables: dict[str, str],
                   profundidad: int = 1) -> None:
    """
    Registra rutas anidadas de un valor: ``clave.subclave``, ``clave[0]``,
    ``clave.0`` y combinaciones (p. ej. ``Dep.items[0].href``).

    Acotado por profundidad, número de items por lista y total de variables.
    """
    if profundidad > _MAX_PROFUNDIDAD_VARIABLES or len(variables) >= _MAX_VARIABLES:
        return

    if isinstance(valor, dict):
        for clave, subvalor in valor.items():
            ruta = f"{prefijo}.{clave}"
            if ruta not in variables and len(variables) < _MAX_VARIABLES:
                variables[ruta] = _valor_variable(subvalor)
            _aplanar_hijos(subvalor, ruta, variables, profundidad + 1)
    elif isinstance(valor, (list, tuple)):
        for indice, subvalor in enumerate(valor[:_MAX_ITEMS_VARIABLES]):
            # Se aceptan las dos sintaxis: Dep.lista[0] y Dep.lista.0
            for ruta in (f"{prefijo}[{indice}]", f"{prefijo}.{indice}"):
                if ruta not in variables and len(variables) < _MAX_VARIABLES:
                    variables[ruta] = _valor_variable(subvalor)
                _aplanar_hijos(subvalor, ruta, variables, profundidad + 1)


def variables_disponibles(agente: Agente, contexto: dict) -> dict[str, str]:
    """Construye el diccionario de variables sustituibles.

    Además de las variables del agente y de primer nivel del contexto,
    aplana rutas anidadas con índices (``Dep.items[0].href``) para poder
    referenciar elementos concretos de listas de resultados.
    """
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

        # Aplanar claves anidadas (dicts, listas e índices)
        _aplanar_hijos(valor, clave, variables)

    return variables


def sustituir_variables(texto: str, variables: dict[str, str]) -> str:
    """Sustituye variables en un texto usando el patrón {variable}."""
    if not texto or "{" not in texto or not variables:
        return texto
    import re as _re
    # Claves más largas primero: evita que una clave corta (p. ej.
    # "Dep.items[0]") capture el prefijo de otra más específica
    # ("Dep.items[0].href").
    claves = sorted(variables.keys(), key=len, reverse=True)
    pattern = _re.compile(
        r"\{\s*(" + "|".join(_re.escape(k) for k in claves) + r")\s*\}"
    )
    return pattern.sub(lambda m: variables.get(m.group(1), m.group(0)), texto)


def sustituir_variables_shell(texto: str, variables: dict[str, str]) -> str:
    """Como ``sustituir_variables`` pero cita cada valor con ``shlex.quote``."""
    if not texto or "{" not in texto or not variables:
        return texto
    import re as _re
    pattern = _re.compile(
        r"\{\s*(" + "|".join(_re.escape(k) for k in variables.keys()) + r")\s*\}"
    )
    return pattern.sub(
        lambda m: shlex.quote(str(variables.get(m.group(1), m.group(0)))),
        texto,
    )


def resolver_ruta_en_contexto(contexto: dict, ruta: str) -> Any:
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

def extraer_primer_comando(comando: str) -> str | None:
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

    try:
        tokens = shlex.split(comando_stripped)
    except ValueError:
        tokens = comando_stripped.split()

    PALABRAS_RESERVADAS = frozenset({
        'if', 'then', 'else', 'elif', 'fi',
        'while', 'do', 'done', 'for', 'in',
        'case', 'esac', 'function', 'return',
        'command', 'builtin', 'type', 'which', 'hash',
        'test', '[', ']', '[[', ']]',
    })

    for token in tokens:
        if not token:
            continue
        if token in ('&&', '||', '|', ';', '&', '(', ')', '{', '}'):
            continue
        if token.startswith('-'):
            continue
        if any(c in token for c in ('>', '<', '`')):
            continue
        if '=' in token and not token.startswith('/'):
            izq, _, _ = token.partition('=')
            if izq.isidentifier():
                continue
        if token in PALABRAS_RESERVADAS:
            continue
        base = os.path.basename(token)
        if base in COMANDOS_PRIVILEGIADOS:
            return True

    return False
