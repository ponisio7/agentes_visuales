# core/agent/enums.py
"""
Enumeraciones para agentes: estados y tipos.
"""

import logging
from enum import Enum

logger = logging.getLogger(__name__)


# ============================================================
# ENUMERACIONES
# ============================================================

class EstadoAgente(Enum):
    """Estados posibles de un agente durante su ciclo de vida."""
    
    # ── Estados iniciales ──
    PENDIENTE = "Pendiente"
    EN_COLA = "En cola"              # ← NUEVO: Esperando turno para ejecutar
    ESPERANDO = "Esperando dependencias"
    
    # ── Estados de ejecución ──
    LISTO = "Listo para ejecutar"
    EJECUTANDO = "Ejecutando"
    REINTENTANDO = "Reintentando"    # ← NUEVO: En proceso de reintento
    
    # ── Estados terminales ──
    COMPLETADO = "Completado"
    ERROR = "Error"
    TIMEOUT = "Timeout"              # ← NUEVO: Excedió tiempo límite
    CANCELADO = "Cancelado"
    SALTADO = "Saltado"              # ← NUEVO: Omitido por dependencia fallida
    BLOQUEADO = "Bloqueado"
    
    @classmethod
    def es_terminal(cls, estado: 'EstadoAgente') -> bool:
        """Indica si el estado es terminal (no puede cambiar)."""
        return estado in (
            cls.COMPLETADO, cls.ERROR, cls.TIMEOUT,
            cls.CANCELADO, cls.SALTADO, cls.BLOQUEADO
        )
    
    @classmethod
    def es_activo(cls, estado: 'EstadoAgente') -> bool:
        """Indica si el agente está en ejecución activa."""
        return estado in (cls.LISTO, cls.EJECUTANDO, cls.REINTENTANDO)
    
    @classmethod
    def puede_ejecutarse(cls, estado: 'EstadoAgente') -> bool:
        """Indica si el agente puede ser ejecutado."""
        return estado in (cls.PENDIENTE, cls.EN_COLA, cls.REINTENTANDO)
    
    @classmethod
    def color(cls, estado: 'EstadoAgente') -> str:
        """Retorna el color asociado al estado para UI."""
        colores = {
            cls.PENDIENTE: "#6c757d",
            cls.EN_COLA: "#6c757d",
            cls.ESPERANDO: "#fd7e14",
            cls.LISTO: "#28a745",
            cls.EJECUTANDO: "#007bff",
            cls.REINTENTANDO: "#ffc107",
            cls.COMPLETADO: "#28a745",
            cls.ERROR: "#dc3545",
            cls.TIMEOUT: "#dc3545",
            cls.CANCELADO: "#6c757d",
            cls.SALTADO: "#6c757d",
            cls.BLOQUEADO: "#8b0000",
        }
        return colores.get(estado, "#6c757d")
    
    @classmethod
    def emoji(cls, estado: 'EstadoAgente') -> str:
        """Retorna el emoji asociado al estado para UI."""
        emojis = {
            cls.PENDIENTE: "⏳",
            cls.EN_COLA: "📋",
            cls.ESPERANDO: "🔄",
            cls.LISTO: "✅",
            cls.EJECUTANDO: "⚡",
            cls.REINTENTANDO: "🔄",
            cls.COMPLETADO: "✅",
            cls.ERROR: "❌",
            cls.TIMEOUT: "⏱️",
            cls.CANCELADO: "⛔",
            cls.SALTADO: "⏭️",
            cls.BLOQUEADO: "🚫",
        }
        return emojis.get(estado, "❓")


class TipoAgente(Enum):
    """Tipos de agentes soportados."""
    
    PYTHON = "Python"
    SHELL = "Shell"
    LLM = "LLM"
    HTTP = "HTTP"
    FILE = "File"
    LOOP = "Loop"
   
    @classmethod
    def categoria(cls, tipo: 'TipoAgente') -> str:
        """Retorna la categoría del tipo de agente."""
        categorias = {
            cls.PYTHON: "Programación",
            cls.SHELL: "Sistema",
            cls.LLM: "IA",
            cls.HTTP: "Red",
            cls.FILE: "Archivos",
            cls.LOOP: "Estructura",
        }
        return categorias.get(tipo, "Otro")
    
    @classmethod
    def icono(cls, tipo: 'TipoAgente') -> str:
        """Retorna el icono asociado al tipo para UI."""
        iconos = {
            cls.PYTHON: "🐍",
            cls.SHELL: "💻",
            cls.LLM: "🧠",
            cls.HTTP: "🌐",
            cls.FILE: "📄",
            cls.LOOP: "🔄",
        }
        return iconos.get(tipo, "📦")
    
    @classmethod
    def from_string(cls, value: str) -> 'TipoAgente':
        """Convierte un string a TipoAgente de forma segura."""
        try:
            return cls(value)
        except ValueError:
            # Intentar buscar por nombre sin espacios
            for member in cls:
                if member.value.lower().replace(" ", "") == value.lower().replace(" ", ""):
                    return member
            return cls.PYTHON