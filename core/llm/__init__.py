# core/llm/__init__.py
"""
Cliente LLM para consultas a DeepSeek - API pública estable.

Uso:
    from core.llm import LLMClient, crear_cliente
    from core.llm import LLMError, LLMConnectionError
"""

from typing import Optional  # ← ARRIBA, antes de usarlo

from .client import (
    LLMClient,
    DEFAULT_MODEL,
    FALLBACK_MODELS,
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_THINKING_ENABLED,
    VALID_REASONING_EFFORTS,
)
from .exceptions import (
    LLMError,
    LLMConnectionError,
    LLMResponseError,
    LLMConfigurationError,
)
from .env_loader import (
    cargar_entorno_desde_archivos,
)

__all__ = [
    # Cliente
    "LLMClient",
    # Excepciones
    "LLMError",
    "LLMConnectionError",
    "LLMResponseError",
    "LLMConfigurationError",
    # Env loader
    "cargar_entorno_desde_archivos",
    # Constantes
    "DEFAULT_MODEL",
    "FALLBACK_MODELS",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT",
    "DEFAULT_MAX_RETRIES",
    "DEFAULT_REASONING_EFFORT",
    "DEFAULT_THINKING_ENABLED",
    "VALID_REASONING_EFFORTS",
]


# ============================================================
# FUNCIÓN DE AYUDA PARA USO RÁPIDO
# ============================================================

def crear_cliente(
    api_key: Optional[str] = None,
    modelo: str = DEFAULT_MODEL
) -> LLMClient:
    """Función rápida para crear un cliente LLM."""
    return LLMClient(api_key=api_key, default_model=modelo)