# core/sandbox/models.py
"""
Modelos de datos y excepciones del sandbox.
"""

from dataclasses import dataclass, field
from typing import Dict, Any, Optional


@dataclass
class SandboxResult:
    """Resultado de una ejecución en el sandbox."""
    success: bool
    message: str
    result: Dict[str, Any]
    execution_time: float = 0.0
    memory_used: Optional[int] = None
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class SandboxError(Exception):
    """Excepción base para errores del sandbox."""
    pass


class SandboxTimeoutError(SandboxError):
    """Error de timeout en la ejecución."""
    pass


class SandboxSecurityError(SandboxError):
    """Error de seguridad en la ejecución."""
    pass


class SandboxResourceError(SandboxError):
    """Error de recursos (memoria, archivos, etc.)."""
    pass