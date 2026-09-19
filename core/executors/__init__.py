# core/executors/__init__.py
"""
Ejecutores de agentes - API pública.

Este paquete reemplaza al antiguo `core/executors.py` manteniendo
la misma API pública:

    from core.executors import AgentExecutor

El resto del proyecto no necesita cambiar sus imports.
"""

# Reexport por compatibilidad con código antiguo que hacía
# `from core.executors import PythonSandbox` (el executors.py
# monolítico importaba PythonSandbox a nivel de módulo y Python
# lo exponía implícitamente).
from core.sandbox import PythonSandbox

from .browser_executor import BrowserExecutor
from .dispatcher import AgentExecutor
from .loop_executor import LoopExecutor
from .search_executor import SearchExecutor

__all__ = [
    "AgentExecutor",
    "BrowserExecutor",
    "LoopExecutor",
    "PythonSandbox",
    "SearchExecutor",
]
