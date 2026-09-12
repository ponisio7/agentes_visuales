# core/sandbox/__init__.py
"""
Paquete sandbox - API pública estable.

Reexporta las clases principales y constantes para mantener compatibilidad:
    from core.sandbox import PythonSandbox, SandboxResult, MAX_TEMP_FILES
"""

from .models import (
    SandboxResult,
    SandboxError,
    SandboxTimeoutError,
    SandboxSecurityError,
    SandboxResourceError,
)
from .temp_files import TempFileManager, MAX_TEMP_FILES, TEMP_FILE_AGE_LIMIT
from .cache import SandboxCache, CACHE_MAX_SIZE, CACHE_TTL
from .python_sandbox import (
    PythonSandbox,
    MAX_FILE_SIZE,
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT,
    MIN_MEMORY_LIMIT_MB,
    DANGEROUS_PATTERNS,
)

__all__ = [
    # Clases principales
    "PythonSandbox",
    "SandboxResult",
    "SandboxError",
    "SandboxTimeoutError",
    "SandboxSecurityError",
    "SandboxResourceError",
    "TempFileManager",
    "SandboxCache",
    # Constantes
    "MAX_TEMP_FILES",
    "TEMP_FILE_AGE_LIMIT",
    "CACHE_MAX_SIZE",
    "CACHE_TTL",
    "MAX_FILE_SIZE",
    "DEFAULT_TIMEOUT",
    "MAX_TIMEOUT",
    "MIN_MEMORY_LIMIT_MB",
    "DANGEROUS_PATTERNS",
]