"""
Gestor de configuraciones de agentes - API pública estable.

Este paquete reemplaza al antiguo `storage/config_manager.py`.
Todos los imports existentes siguen funcionando:

    from storage.config_manager import ConfigManager
    # o
    from storage.config import ConfigManager
"""

from .manager import ConfigManager
from .security import (
    ConfigError,
    ConfigIntegrityError,
    ConfigSecurityError,
)

__all__ = [
    "ConfigManager",
    "ConfigError",
    "ConfigIntegrityError",
    "ConfigSecurityError",
]