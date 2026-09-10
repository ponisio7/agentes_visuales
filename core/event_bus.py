# core/event_bus.py
"""
Bus de eventos para comunicación desacoplada entre componentes.

CARACTERÍSTICAS:
- Suscripción/desuscripción de eventos
- Publicación de eventos con datos
- Soporte para múltiples suscriptores
- Thread-safe
- Logging de eventos
- Filtrado por tipo de evento
"""

import logging
import threading
import time
from typing import Dict, List, Callable, Any, Optional, Set
from dataclasses import dataclass, field
from enum import Enum, auto
from collections import defaultdict

logger = logging.getLogger(__name__)


# ============================================================
# TIPOS DE EVENTOS
# ============================================================

class EventType(Enum):
    """Tipos de eventos que puede emitir el sistema."""
    
    # ── Eventos de agente ──
    AGENTE_ACTUALIZADO = auto()
    AGENTE_INICIADO = auto()
    AGENTE_COMPLETADO = auto()
    AGENTE_ERROR = auto()
    AGENTE_TIMEOUT = auto()
    AGENTE_CANCELADO = auto()
    AGENTE_BLOQUEADO = auto()
    AGENTE_SALTADO = auto()
    AGENTE_REINTENTANDO = auto()
    AGENTE_PROGRESO = auto()
    
    # ── Eventos de ejecución ──
    EJECUCION_INICIADA = auto()
    EJECUCION_PAUSADA = auto()
    EJECUCION_REANUDADA = auto()
    EJECUCION_DETENIDA = auto()
    EJECUCION_TERMINADA = auto()
    
    # ── Eventos de log ──
    LOG_MENSAJE = auto()
    LOG_ERROR = auto()
    LOG_ADVERTENCIA = auto()
    
    # ── Eventos de sistema ──
    SISTEMA_INICIADO = auto()
    SISTEMA_CERRADO = auto()
    ESTADO_CAMBIADO = auto()


# ============================================================
# MODELO DE EVENTO
# ============================================================

@dataclass
class Event:
    """Representa un evento en el bus."""
    
    tipo: EventType
    datos: Any = None
    origen: str = ""
    timestamp: float = field(default_factory=time.time)
    
    def __post_init__(self):
        if not self.origen:
            self.origen = "desconocido"


# ============================================================
# CLASE PRINCIPAL: EVENT BUS (Singleton)
# ============================================================

class EventBus:
    """
    Bus de eventos central para comunicación desacoplada.
    Implementa el patrón Singleton para tener un único bus global.
    """
    
    _instance: Optional['EventBus'] = None
    _lock = threading.RLock()
    
    def __new__(cls) -> 'EventBus':
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self._suscriptores: Dict[EventType, List[Callable]] = defaultdict(list)
        self._suscriptores_todos: List[Callable] = []
        self._lock = threading.RLock()
        self._historial: List[Event] = []
        self._max_historial = 1000
        self._activo = True
        
        logger.info("EventBus inicializado")
    
    # ============================================================
    # SUSCRIPCIÓN
    # ============================================================
    
    def suscribir(self, tipo: EventType, callback: Callable) -> bool:
        """
        Suscribe un callback a un tipo de evento.
        
        Args:
            tipo: Tipo de evento a suscribir
            callback: Función a llamar cuando ocurra el evento
            
        Returns:
            bool: True si la suscripción fue exitosa
        """
        with self._lock:
            if callback not in self._suscriptores[tipo]:
                self._suscriptores[tipo].append(callback)
                logger.debug(f"Suscripción añadida: {tipo.name} → {callback.__name__}")
                return True
            return False
    
    def suscribir_todos(self, callback: Callable) -> bool:
        """
        Suscribe un callback a TODOS los eventos.
        
        Args:
            callback: Función a llamar para cualquier evento
            
        Returns:
            bool: True si la suscripción fue exitosa
        """
        with self._lock:
            if callback not in self._suscriptores_todos:
                self._suscriptores_todos.append(callback)
                logger.debug(f"Suscripción a todos los eventos: {callback.__name__}")
                return True
            return False
    
    def desuscribir(self, tipo: EventType, callback: Callable) -> bool:
        """
        Elimina una suscripción.
        
        Args:
            tipo: Tipo de evento
            callback: Callback a eliminar
            
        Returns:
            bool: True si se eliminó correctamente
        """
        with self._lock:
            if callback in self._suscriptores[tipo]:
                self._suscriptores[tipo].remove(callback)
                logger.debug(f"Suscripción eliminada: {tipo.name} → {callback.__name__}")
                return True
            return False
    
    def desuscribir_todos(self, callback: Callable) -> bool:
        """
        Elimina una suscripción a todos los eventos.
        
        Args:
            callback: Callback a eliminar
            
        Returns:
            bool: True si se eliminó correctamente
        """
        with self._lock:
            if callback in self._suscriptores_todos:
                self._suscriptores_todos.remove(callback)
                logger.debug(f"Suscripción a todos los eventos eliminada: {callback.__name__}")
                return True
            return False
    
    # ============================================================
    # PUBLICACIÓN
    # ============================================================
    
    def publicar(self, evento: Event) -> bool:
        """
        Publica un evento en el bus.
        
        Args:
            evento: Evento a publicar
            
        Returns:
            bool: True si se publicó correctamente
        """
        if not self._activo:
            logger.warning("EventBus inactivo, evento ignorado")
            return False
        
        with self._lock:
            # Guardar historial
            self._historial.append(evento)
            if len(self._historial) > self._max_historial:
                self._historial = self._historial[-self._max_historial:]
            
            # Obtener suscriptores específicos
            suscriptores = self._suscriptores.get(evento.tipo, []).copy()
            suscriptores_todos = self._suscriptores_todos.copy()
        
        # Ejecutar callbacks (fuera del lock)
        for callback in suscriptores:
            try:
                callback(evento)
            except Exception as e:
                logger.error(f"Error en callback {callback.__name__}: {e}")
        
        for callback in suscriptores_todos:
            try:
                callback(evento)
            except Exception as e:
                logger.error(f"Error en callback universal {callback.__name__}: {e}")
        
        logger.debug(f"Evento publicado: {evento.tipo.name} desde {evento.origen}")
        return True
    
    # ============================================================
    # MÉTODOS DE CONVENIENCIA PARA PUBLICAR
    # ============================================================
    
    def publicar_agente_actualizado(self, agente_id: str, origen: str = ""):
        """Publica evento de agente actualizado."""
        self.publicar(Event(
            tipo=EventType.AGENTE_ACTUALIZADO,
            datos={"agente_id": agente_id},
            origen=origen
        ))
    
    def publicar_agente_completado(self, agente_id: str, nombre: str, resultado: Any, origen: str = ""):
        """Publica evento de agente completado."""
        self.publicar(Event(
            tipo=EventType.AGENTE_COMPLETADO,
            datos={"agente_id": agente_id, "nombre": nombre, "resultado": resultado},
            origen=origen
        ))
    
    def publicar_agente_error(self, agente_id: str, nombre: str, error: str, origen: str = ""):
        """Publica evento de agente en error."""
        self.publicar(Event(
            tipo=EventType.AGENTE_ERROR,
            datos={"agente_id": agente_id, "nombre": nombre, "error": error},
            origen=origen
        ))
    
    def publicar_log(self, mensaje: str, color: str = "#333", origen: str = ""):
        """Publica evento de log."""
        self.publicar(Event(
            tipo=EventType.LOG_MENSAJE,
            datos={"mensaje": mensaje, "color": color},
            origen=origen
        ))
    
    def publicar_ejecucion_terminada(self, stats: Dict, origen: str = ""):
        """Publica evento de ejecución terminada."""
        self.publicar(Event(
            tipo=EventType.EJECUCION_TERMINADA,
            datos={"stats": stats},
            origen=origen
        ))
    
    def publicar_ejecucion_iniciada(self, total_agentes: int, origen: str = ""):
        """Publica evento de ejecución iniciada."""
        self.publicar(Event(
            tipo=EventType.EJECUCION_INICIADA,
            datos={"total_agentes": total_agentes},
            origen=origen
        ))
    
    def publicar_ejecucion_pausada(self, origen: str = ""):
        """Publica evento de ejecución pausada."""
        self.publicar(Event(
            tipo=EventType.EJECUCION_PAUSADA,
            datos={},
            origen=origen
        ))
    
    def publicar_ejecucion_reanudada(self, origen: str = ""):
        """Publica evento de ejecución reanudada."""
        self.publicar(Event(
            tipo=EventType.EJECUCION_REANUDADA,
            datos={},
            origen=origen
        ))
    
    def publicar_estado_cambiado(self, ejecutando: bool, origen: str = ""):
        """Publica evento de cambio de estado."""
        self.publicar(Event(
            tipo=EventType.ESTADO_CAMBIADO,
            datos={"ejecutando": ejecutando},
            origen=origen
        ))
    
    # ============================================================
    # UTILIDADES
    # ============================================================
    
    def obtener_historial(self, limit: int = 100) -> List[Event]:
        """Obtiene el historial de eventos."""
        with self._lock:
            return self._historial[-limit:]
    
    def obtener_estadisticas(self) -> Dict:
        """Obtiene estadísticas del bus."""
        with self._lock:
            tipos = defaultdict(int)
            for evento in self._historial:
                tipos[evento.tipo.name] += 1
            
            suscriptores = {}
            for tipo, lista in self._suscriptores.items():
                suscriptores[tipo.name] = len(lista)
            
            return {
                "total_eventos": len(self._historial),
                "tipos": dict(tipos),
                "suscriptores": suscriptores,
                "suscriptores_todos": len(self._suscriptores_todos),
                "activo": self._activo
            }
    
    def limpiar_historial(self):
        """Limpia el historial de eventos."""
        with self._lock:
            self._historial.clear()
            logger.info("Historial de eventos limpiado")
    
    def detener(self):
        """Detiene el bus de eventos."""
        self._activo = False
        logger.info("EventBus detenido")
    
    def reiniciar(self):
        """Reinicia el bus de eventos."""
        self._activo = True
        logger.info("EventBus reiniciado")
    
    # ============================================================
    # CONTEXTO PARA SUSCRIPCIÓN SEGURA
    # ============================================================
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.detener()


# ============================================================
# INSTANCIA GLOBAL
# ============================================================

_bus_instance: Optional[EventBus] = None

def obtener_bus() -> EventBus:
    """Obtiene la instancia global del EventBus."""
    global _bus_instance
    if _bus_instance is None:
        _bus_instance = EventBus()
    return _bus_instance