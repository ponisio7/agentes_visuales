# core/agent/type_config.py
"""
Registro de tipos de agentes y configuración por defecto.
"""

from .enums import TipoAgente


# ============================================================
# REGISTRO DE TIPOS DE AGENTES (para UI)
# ============================================================

TIPOS_AGENTES_CONFIG = {
    TipoAgente.PYTHON: {
        'descripcion': 'Ejecuta código Python en un sandbox aislado',
        'icono': '🐍',
        'categoria': 'Programación',
        'campos_requeridos': ['codigo_python'],
    },
    TipoAgente.SHELL: {
        'descripcion': 'Ejecuta comandos de shell del sistema',
        'icono': '💻',
        'categoria': 'Sistema',
        'campos_requeridos': ['comando_shell'],
    },
    TipoAgente.LLM: {
        'descripcion': 'Llama a modelos de lenguaje (DeepSeek)',
        'icono': '🧠',
        'categoria': 'IA',
        'campos_requeridos': ['prompt_llm', 'modelo_llm'],
        'campos_opcionales': [
            'temperatura_llm',
            'max_tokens_llm',
            'reasoning_effort_llm',
            'thinking_enabled_llm',
        ],
    },
    TipoAgente.HTTP: {
        'descripcion': 'Realiza peticiones HTTP/HTTPS',
        'icono': '🌐',
        'categoria': 'Red',
        'campos_requeridos': ['url_http'],
    },
    TipoAgente.FILE: {
        'descripcion': 'Operaciones con archivos del sistema',
        'icono': '📄',
        'categoria': 'Archivos',
        'campos_requeridos': ['operacion_file'],
    },
    TipoAgente.LOOP: {
        'descripcion': 'Itera sobre una lista de items',
        'icono': '🔄',
        'categoria': 'Estructura',
        'campos_requeridos': ['fuente_items', 'codigo_por_item'],
    },
}


def obtener_config_tipo(tipo: TipoAgente) -> dict:
    """Obtiene la configuración por defecto para un tipo de agente."""
    return TIPOS_AGENTES_CONFIG.get(tipo, {})


def obtener_tipos_por_categoria(categoria: str) -> list:
    """Obtiene los tipos de agente por categoría."""
    return [
        tipo for tipo, config in TIPOS_AGENTES_CONFIG.items()
        if config.get('categoria') == categoria
    ]


def obtener_categorias() -> list:
    """Obtiene todas las categorías de agentes disponibles."""
    return sorted(set(
        config.get('categoria', 'Otro')
        for config in TIPOS_AGENTES_CONFIG.values()
    ))