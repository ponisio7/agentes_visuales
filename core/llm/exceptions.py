# core/llm/exceptions.py
"""
Excepciones personalizadas para el cliente LLM.
"""


class LLMError(Exception):
    """Excepción base para errores del LLM."""
    pass


class LLMConnectionError(LLMError):
    """Error de conexión con el LLM."""
    pass


class LLMResponseError(LLMError):
    """Error en la respuesta del LLM."""
    pass


class LLMConfigurationError(LLMError):
    """Error de configuración del LLM."""
    pass