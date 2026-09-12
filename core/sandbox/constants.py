# core/sandbox/constants.py
"""Constantes del sandbox."""

# Tamaño máximo de código o archivo aceptado
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB

# Timeouts
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 3600  # 1 hora máximo

# Límite mínimo de memoria (cubre el arranque del intérprete hijo)
MIN_MEMORY_LIMIT_MB = 64

# Patrones potencialmente peligrosos (informativo; no se usan activamente
# en la versión actual pero se mantienen por compatibilidad con __init__.py)
DANGEROUS_PATTERNS = [
    (r'"""', '\\"\\"\\"'),
    (r"'''", "\\'\\'\\'"),
    (r'`', '\\`'),
    (r'\$', '\\$'),
]


__all__ = [
    "MAX_FILE_SIZE",
    "DEFAULT_TIMEOUT",
    "MAX_TIMEOUT",
    "MIN_MEMORY_LIMIT_MB",
    "DANGEROUS_PATTERNS",
]