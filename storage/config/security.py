"""Seguridad de rutas y excepciones para ConfigManager."""

import os
import re
import logging
from typing import Set

logger = logging.getLogger(__name__)


# ============================================================
# EXCEPCIONES
# ============================================================

class ConfigError(Exception):
    """Excepción base para errores de configuración."""
    pass


class ConfigIntegrityError(ConfigError):
    """Error de integridad en archivo de configuración."""
    pass


class ConfigSecurityError(ConfigError):
    """Error de seguridad (path traversal, etc.)."""
    pass


# ============================================================
# CONSTANTES
# ============================================================

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXTENSIONS: Set[str] = {'.json'}
SCHEMA_VERSION = "2.0"

# Caracteres permitidos en nombres de archivo (whitelist)
SAFE_FILENAME_PATTERN = re.compile(r'^[A-Za-z0-9_\-]+$')


# ============================================================
# UTILIDADES DE SEGURIDAD
# ============================================================

def sanitizar(nombre: str) -> str:
    """
    Convierte un nombre arbitrario en un componente de nombre de archivo seguro.
    """
    if not nombre or not isinstance(nombre, str):
        return "config_default"

    # 1. Reemplazar caracteres no permitidos por '_'
    seguro = re.sub(r'[^A-Za-z0-9_\-]+', '_', nombre.strip())

    # 2. Eliminar guiones bajos múltiples
    seguro = re.sub(r'_+', '_', seguro)

    # 3. Eliminar guiones bajos al inicio y final
    seguro = seguro.strip('_')

    # 4. Si el resultado está vacío, usar un nombre por defecto
    if not seguro:
        return "config_default"

    # 5. Limitar longitud
    if len(seguro) > 200:
        seguro = seguro[:200]

    return seguro


def ruta_segura(config_dir: str, filename: str, allow_subdirs: bool = False) -> str:
    """
    Resuelve un nombre de archivo y verifica que esté dentro de config_dir.

    Args:
        config_dir: Directorio raíz permitido (absoluto)
        filename: Nombre del archivo o ruta relativa
        allow_subdirs: Si se permiten subdirectorios

    Returns:
        str: Ruta absoluta segura dentro de config_dir

    Raises:
        ConfigSecurityError: Si se detecta path traversal o nombre inválido
    """
    # Validar entrada
    if not filename or not filename.strip():
        raise ConfigSecurityError("Nombre de archivo vacío")

    filename = filename.strip()

    # Verificar extensión permitida
    ext = os.path.splitext(filename)[1].lower()
    if ext and ext not in ALLOWED_EXTENSIONS:
        raise ConfigSecurityError(f"Extensión no permitida: '{ext}'")

    # Normalizar y verificar path traversal
    normalized = os.path.normpath(filename)

    # Detectar intentos de salir del directorio
    if normalized.startswith('..') or os.path.isabs(normalized):
        raise ConfigSecurityError(f"Path traversal detectado: '{filename}'")

    # Si no se permiten subdirectorios, verificar que no haya path separators
    if not allow_subdirs and (os.path.sep in normalized or os.path.altsep and os.path.altsep in normalized):
        raise ConfigSecurityError(f"Subdirectorios no permitidos: '{filename}'")

    # Construir ruta absoluta
    ruta = os.path.realpath(os.path.join(config_dir, normalized))

    # Verificar que la ruta resultante está dentro de config_dir
    if not ruta.startswith(config_dir + os.sep) and ruta != config_dir:
        raise ConfigSecurityError(f"Ruta fuera del directorio de configuraciones: '{filename}'")

    return ruta