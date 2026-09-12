# core/llm_client.py
"""
DEPRECATED: Este módulo se ha movido a `core.llm`.

Mantiene compatibilidad hacia atrás. Usa:
    from core.llm import LLMClient
"""

from core.llm import *  # noqa: F401,F403
from core.llm import (  # noqa: F401
    LLMClient,
    LLMError,
    LLMConnectionError,
    LLMResponseError,
    LLMConfigurationError,
    cargar_entorno_desde_archivos,
    DEFAULT_MODEL,
    FALLBACK_MODELS,
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    DEFAULT_MAX_RETRIES,
    DEFAULT_REASONING_EFFORT,
    DEFAULT_THINKING_ENABLED,
    VALID_REASONING_EFFORTS,
)
from core.llm import crear_cliente  # noqa: F401