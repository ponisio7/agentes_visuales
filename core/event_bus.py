# core/event_bus.py
"""
Bus de eventos para comunicación desacoplada entre componentes.

CARACTERÍSTICAS:
- Suscripción/desuscripción de eventos
- Publicación de eventos con datos
- Soporte para múltiples suscriptores
- Thread-safe (lock en suscripción/publicación)
- Logging de eventos
- Filtrado por tipo de evento
- Snapshot defensivo de payloads (anti race worker ↔ hilo principal)

IMPORTANTE — THREADING / Qt:
Los callbacks de suscribir() se ejecutan en el hilo del publicador.
Si el publicador es un worker (ThreadPoolExecutor) y el callback toca
widgets Qt (QTextEdit, labels, etc.), el suscriptor DEBE marshallar
al hilo principal, por ejemplo:

    self._invocar_en_hilo_principal = pyqtSignal(object)
    self._invocar_en_hilo_principal.connect(lambda fn: fn(), Qt.QueuedConnection)

    def _en_hilo_principal(handler):
        def _wrapper(evento):
            # Preferible: solo IDs; el handler consulta al scheduler
            self._invocar_en_hilo_principal.emit(lambda: handler(evento))
        return _wrapper

Los métodos de conveniencia ya hacen snapshot superficial de dicts
para reducir races sobre el payload.
"""

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _nombre_callback(callback: Callable) -> str:
    """Nombre legible de un callback, seguro ante callables sin ``__name__``.

    ``functools.partial`` u otros invocables no exponen ``__name__``; usar
    ``_nombre_callback(callback)`` directamente lanzaría ``AttributeError`` dentro del
    manejo de errores y abortaría la publicación del evento.
    """
    return getattr(callback, "__name__", None) or repr(callback)


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
        with EventBus._lock:
            if self._initialized:
                return

            self._initialized = True
            self._suscriptores: dict[EventType, list[Callable]] = defaultdict(list)
            self._suscriptores_todos: list[Callable] = []
            self._lock = threading.RLock()
            self._historial: list[Event] = []
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
                logger.debug(f"Suscripción añadida: {tipo.name} → {_nombre_callback(callback)}")
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
                logger.debug(f"Suscripción a todos los eventos: {_nombre_callback(callback)}")
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
                logger.debug(f"Suscripción eliminada: {tipo.name} → {_nombre_callback(callback)}")
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
                logger.debug(f"Suscripción a todos los eventos eliminada: {_nombre_callback(callback)}")
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
                logger.error(f"Error en callback {_nombre_callback(callback)}: {e}")
        
        for callback in suscriptores_todos:
            try:
                callback(evento)
            except Exception as e:
                logger.error(f"Error en callback universal {_nombre_callback(callback)}: {e}")
        
        logger.debug(f"Evento publicado: {evento.tipo.name} desde {evento.origen}")
        return True
    
    # ============================================================
    # HELPERS DE SNAPSHOT (anti race entre worker y hilo principal)
    # ============================================================

    @staticmethod
    def _snapshot_datos(datos: Any, _profundidad: int = 0, _max: int = 4) -> Any:
        """
        Copia defensiva (recursiva con límite) de los datos del evento.

        Evita que el hilo worker mute el dict después de publicar y que el
        handler en el hilo principal lea un estado a medio escribir
        (síntoma típico: QTextCursor out of range / corrupción de UI).

        Profundidad máxima 4: suficiente para resultados HTTP/LLM/Loop
        sin costar una deepcopy completa de estructuras enormes.
        """
        if datos is None or _profundidad > _max:
            return datos
        if isinstance(datos, (str, int, float, bool)):
            return datos
        if isinstance(datos, dict):
            try:
                return {
                    k: EventBus._snapshot_datos(v, _profundidad + 1, _max)
                    for k, v in datos.items()
                }
            except Exception:
                try:
                    return dict(datos)
                except Exception:
                    return datos
        if isinstance(datos, list):
            try:
                return [
                    EventBus._snapshot_datos(v, _profundidad + 1, _max)
                    for v in datos
                ]
            except Exception:
                try:
                    return list(datos)
                except Exception:
                    return datos
        # Otros tipos (objetos, etc.): pasar por referencia; no clonar
        return datos

    # ============================================================
    # MÉTODOS DE CONVENIENCIA PARA PUBLICAR
    # ============================================================
    
    def publicar_agente_actualizado(self, agente_id: str, origen: str = ""):
        """Publica evento de agente actualizado (solo ID — seguro para cross-thread)."""
        self.publicar(Event(
            tipo=EventType.AGENTE_ACTUALIZADO,
            datos={"agente_id": agente_id},
            origen=origen
        ))
    
    def publicar_agente_completado(
        self,
        agente_id: str,
        nombre: str,
        resultado: Any = None,
        origen: str = ""
    ):
        """
        Publica evento de agente completado.

        El `resultado` se snapshottea para que el handler del hilo principal
        no vea mutaciones posteriores del worker. Preferible que el handler
        use solo `agente_id` y consulte al scheduler.
        """
        self.publicar(Event(
            tipo=EventType.AGENTE_COMPLETADO,
            datos=self._snapshot_datos({
                "agente_id": agente_id,
                "nombre": nombre,
                "resultado": resultado,
            }),
            origen=origen
        ))
    
    def publicar_agente_error(
        self,
        agente_id: str,
        nombre: str,
        error: str,
        origen: str = ""
    ):
        """Publica evento de agente en error."""
        self.publicar(Event(
            tipo=EventType.AGENTE_ERROR,
            datos={
                "agente_id": agente_id,
                "nombre": nombre,
                "error": str(error) if error is not None else "",
            },
            origen=origen
        ))
    
    def publicar_log(self, mensaje: str, color: str = "#333", origen: str = ""):
        """Publica evento de log."""
        self.publicar(Event(
            tipo=EventType.LOG_MENSAJE,
            datos={"mensaje": str(mensaje), "color": color},
            origen=origen
        ))
    
    def publicar_ejecucion_terminada(self, stats: dict, origen: str = ""):
        """Publica evento de ejecución terminada (stats snapshotteados)."""
        self.publicar(Event(
            tipo=EventType.EJECUCION_TERMINADA,
            datos={"stats": self._snapshot_datos(stats) or {}},
            origen=origen
        ))
    
    def publicar_ejecucion_iniciada(self, total_agentes: int, origen: str = ""):
        """Publica evento de ejecución iniciada."""
        self.publicar(Event(
            tipo=EventType.EJECUCION_INICIADA,
            datos={"total_agentes": int(total_agentes)},
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
            datos={"ejecutando": bool(ejecutando)},
            origen=origen
        ))
    
    # ============================================================
    # UTILIDADES
    # ============================================================
    
    def obtener_historial(self, limit: int = 100) -> list[Event]:
        """Obtiene el historial de eventos."""
        with self._lock:
            return self._historial[-limit:]
    
    def obtener_estadisticas(self) -> dict:
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

_bus_instance: EventBus | None = None
_bus_instance_lock = threading.Lock()

def obtener_bus() -> EventBus:
    """Obtiene la instancia global del EventBus (thread-safe)."""
    global _bus_instance
    if _bus_instance is None:
        with _bus_instance_lock:
            if _bus_instance is None:
                _bus_instance = EventBus()
    return _bus_instance
