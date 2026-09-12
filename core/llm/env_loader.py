# core/llm/env_loader.py
"""
Carga de variables de entorno desde archivos de configuración de fallback.

Busca en rutas comunes (~/.config/agentes_visuales/env, ~/.config/deepseek.env,
~/.deepseek_key) para que la API key funcione igual desde terminal, menú,
cron, systemd, etc.
"""

import os
import stat
import logging
from pathlib import Path
from typing import Optional, Dict, Tuple

logger = logging.getLogger(__name__)


# Rutas de búsqueda en orden de prioridad (el primer archivo con
# al menos una variable soportada gana).
_FALLBACK_ENV_PATHS = [
    Path.home() / ".config" / "agentes_visuales" / "env",
    Path.home() / ".config" / "deepseek.env",
    Path.home() / ".deepseek_key",
]

# Variables reconocidas en el archivo
_VARIABLES_SOPORTADAS = frozenset({
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
})


def _parsear_linea_env(linea: str) -> Optional[Tuple[str, str]]:
    """
    Parsea una línea tipo 'export VAR="valor"' o 'VAR=valor'.
    Devuelve (clave, valor) o None si no es una asignación válida.
    """
    linea = linea.strip()
    if not linea or linea.startswith("#"):
        return None

    # Quitar 'export ' si está
    if linea.startswith("export "):
        linea = linea[7:].strip()

    if "=" not in linea:
        return None

    clave, _, valor = linea.partition("=")
    clave = clave.strip()
    valor = valor.strip()

    # Quitar comentarios al final del valor (solo si van precedidos de espacio)
    if " #" in valor and not valor.startswith("#"):
        valor = valor.split(" #", 1)[0].strip()

    # Quitar comillas envolventes
    if len(valor) >= 2:
        if (valor[0] == '"' and valor[-1] == '"') or \
           (valor[0] == "'" and valor[-1] == "'"):
            valor = valor[1:-1]

    return clave, valor


def _verificar_permisos_archivo(path: Path) -> None:
    """Advierte si un archivo de secretos tiene permisos demasiado abiertos."""
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            permisos = oct(mode & 0o777)
            logger.warning(
                f"⚠️ {path} tiene permisos demasiado abiertos ({permisos}). "
                f"Recomendado: chmod 600 {path}"
            )
    except OSError as e:
        logger.debug(f"No se pudieron verificar permisos de {path}: {e}")


def cargar_entorno_desde_archivos() -> Dict[str, str]:
    """
    Carga variables de entorno desde archivos de configuración de fallback.

    Busca en rutas comunes (~/.config/agentes_visuales/env, etc.) y devuelve
    un dict con las variables encontradas. Solo considera variables declaradas
    en _VARIABLES_SOPORTADAS.

    La búsqueda para en el primer archivo que contenga al menos una variable
    soportada, para evitar que un archivo antiguo sobrescriba a uno nuevo.

    Returns:
        Dict[str, str]: Variables encontradas (puede estar vacío).
    """
    for path in _FALLBACK_ENV_PATHS:
        if not path.exists() or not path.is_file():
            continue

        _verificar_permisos_archivo(path)

        encontradas: Dict[str, str] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for num_linea, linea in enumerate(f, 1):
                    resultado = _parsear_linea_env(linea)
                    if resultado is None:
                        continue
                    clave, valor = resultado
                    if clave in _VARIABLES_SOPORTADAS and valor:
                        encontradas[clave] = valor
                    elif clave in _VARIABLES_SOPORTADAS and not valor:
                        logger.warning(
                            f"⚠️ {path}:{num_linea} — '{clave}' está vacía"
                        )
        except OSError as e:
            logger.warning(f"No se pudo leer {path}: {e}")
            continue

        if encontradas:
            logger.debug(
                f"✅ Variables cargadas desde {path}: {sorted(encontradas.keys())}"
            )
            return encontradas

    return {}