# core/agent/__init__.py
"""
Modelo de datos para agentes ejecutables - API pública estable.

Reexporta todos los componentes para compatibilidad con:
    from core.agent import Agente, EstadoAgente, TipoAgente
"""

from .enums import EstadoAgente, TipoAgente
from .validator import AgenteValidator
from .model import Agente
from .type_config import (
    TIPOS_AGENTES_CONFIG,
    obtener_config_tipo,
    obtener_tipos_por_categoria,
    obtener_categorias,
)

__all__ = [
    'Agente',
    'EstadoAgente',
    'TipoAgente',
    'AgenteValidator',
    'TIPOS_AGENTES_CONFIG',
    'obtener_config_tipo',
    'obtener_tipos_por_categoria',
    'obtener_categorias',
]