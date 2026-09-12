# core/sandbox/_logging.py
"""
Logging seguro desde hilos worker del sandbox.

Este módulo corre dentro de ThreadPoolExecutor mientras PyQt6 (y, en
tests, pytest con su TerminalReporter) están activos en el proceso.
El logging estándar es thread-safe a nivel de Handler, pero el
capturador de pytest no está pensado para escrituras concurrentes.
_safe_log serializa la emisión y cae a stderr directo si algo falla.
"""
import logging
import sys
import threading
from typing import Any

logger = logging.getLogger(__name__)

# Serializa la emisión de logs. Compartido por todo el paquete sandbox.
_log_lock = threading.Lock()


def _safe_log(level: str, msg: str, *args: Any, **kwargs: Any) -> None:
    """Emite un mensaje de log serializado; nunca deja escapar una excepción."""
    try:
        with _log_lock:
            getattr(logger, level)(msg, *args, **kwargs)
    except Exception:
        try:
            texto = (msg % args) if args else msg
            sys.stderr.write(f"[sandbox:{level}] {texto}\n")
        except Exception:
            pass


__all__ = ["_safe_log", "_log_lock", "logger"]