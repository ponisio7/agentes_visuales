# core/cancellation.py
"""
Sistema de cancelación para operaciones en ejecución.
Permite cancelar workers de forma segura y coordinada.
"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# ESTADO DEL TOKEN
# ============================================================

class CancellationState(Enum):
    """Estado de un token de cancelación."""
    ACTIVE = auto()      # Operación en curso, no cancelada
    CANCELLED = auto()   # Cancelación solicitada
    COMPLETED = auto()   # Operación completada (token inactivo)


# ============================================================
# TOKEN DE CANCELACIÓN
# ============================================================

@dataclass
class CancellationToken:
    """
    Token de cancelación para controlar operaciones en ejecución.
    Thread-safe.
    """
    
    id: str = field(default_factory=lambda: f"token_{int(time.time()*1000)}")
    estado: CancellationState = CancellationState.ACTIVE
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)
    _callbacks: list[Callable] = field(default_factory=list, repr=False)
    _metadata: dict[str, Any] = field(default_factory=dict)
    
    def cancelar(self, razon: str = "Cancelado por usuario") -> bool:
        """
        Solicita la cancelación de la operación.
        
        Args:
            razon: Razón de la cancelación
            
        Returns:
            bool: True si se canceló correctamente
        """
        with self._lock:
            if self.estado in (CancellationState.CANCELLED, CancellationState.COMPLETED):
                return False
            
            self.estado = CancellationState.CANCELLED
            self._metadata['razon_cancelacion'] = razon
            self._metadata['timestamp_cancelacion'] = time.time()
            
            # Ejecutar callbacks de cancelación
            for callback in self._callbacks:
                try:
                    callback(self)
                except Exception as e:
                    logger.warning(f"Error en callback de cancelación: {e}")
            
            logger.debug(f"Token {self.id} cancelado: {razon}")
            return True
    
    def esta_cancelado(self) -> bool:
        """Verifica si se ha solicitado cancelación."""
        with self._lock:
            return self.estado == CancellationState.CANCELLED
    
    def esta_activo(self) -> bool:
        """Verifica si el token está activo (no cancelado ni completado)."""
        with self._lock:
            return self.estado == CancellationState.ACTIVE
    
    def completar(self):
        """Marca el token como completado."""
        with self._lock:
            if self.estado == CancellationState.ACTIVE:
                self.estado = CancellationState.COMPLETED
    
    def agregar_callback(self, callback: Callable):
        """
        Agrega un callback que se ejecutará cuando se cancele.
        
        Args:
            callback: Función a llamar con el token como argumento
        """
        with self._lock:
            if callback not in self._callbacks:
                self._callbacks.append(callback)
    
    def eliminar_callback(self, callback: Callable):
        """Elimina un callback registrado."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)
    
    def obtener_metadata(self, key: str, default: Any = None) -> Any:
        """Obtiene un valor de metadata."""
        with self._lock:
            return self._metadata.get(key, default)
    
    def establecer_metadata(self, key: str, value: Any):
        """Establece un valor en metadata."""
        with self._lock:
            self._metadata[key] = value
    
    def __repr__(self) -> str:
        return f"CancellationToken(id={self.id}, estado={self.estado.name})"


# ============================================================
# GESTOR DE TOKENS
# ============================================================

class CancellationManager:
    """
    Gestor centralizado de tokens de cancelación.
    Permite cancelar operaciones por ID o en grupo.
    """
    
    def __init__(self):
        self._tokens: dict[str, CancellationToken] = {}
        self._lock = threading.RLock()
        self._logger = logging.getLogger(f"{__name__}.CancellationManager")
    
    def crear_token(self, metadata: dict | None = None) -> CancellationToken:
        """
        Crea un nuevo token de cancelación.
        
        Args:
            metadata: Metadata inicial para el token
            
        Returns:
            CancellationToken: Nuevo token
        """
        token = CancellationToken()
        if metadata:
            for key, value in metadata.items():
                token.establecer_metadata(key, value)
        
        with self._lock:
            self._tokens[token.id] = token
        
        self._logger.debug(f"Token creado: {token.id}")
        return token
    
    def obtener_token(self, token_id: str) -> CancellationToken | None:
        """Obtiene un token por su ID."""
        with self._lock:
            return self._tokens.get(token_id)
    
    def cancelar_token(self, token_id: str, razon: str = "Cancelado por usuario") -> bool:
        """
        Cancela un token específico.
        
        Args:
            token_id: ID del token a cancelar
            razon: Razón de la cancelación
            
        Returns:
            bool: True si se canceló correctamente
        """
        with self._lock:
            token = self._tokens.get(token_id)
            if not token:
                return False
            
            resultado = token.cancelar(razon)
            if resultado:
                self._logger.info(f"Token cancelado: {token_id} ({razon})")
            return resultado
    
    def cancelar_todos(self, razon: str = "Cancelación masiva") -> int:
        """
        Cancela todos los tokens activos.
        
        Args:
            razon: Razón de la cancelación
            
        Returns:
            int: Número de tokens cancelados
        """
        with self._lock:
            cancelados = 0
            for token_id, token in list(self._tokens.items()):
                if token.esta_activo():
                    token.cancelar(razon)
                    cancelados += 1
            
            self._logger.info(f"Cancelados {cancelados} tokens: {razon}")
            return cancelados
    
    def cancelar_por_agente(self, agente_id: str, razon: str = "Cancelado por usuario") -> bool:
        """
        Cancela el token asociado a un agente específico.
        
        Args:
            agente_id: ID del agente
            razon: Razón de la cancelación
            
        Returns:
            bool: True si se canceló correctamente
        """
        with self._lock:
            for token_id, token in self._tokens.items():
                if token.obtener_metadata('agente_id') == agente_id:
                    if token.esta_activo():
                        token.cancelar(razon)
                        self._logger.info(f"Token cancelado para agente {agente_id}: {razon}")
                        return True
            return False
    
    def eliminar_token(self, token_id: str) -> bool:
        """Elimina un token del gestor."""
        with self._lock:
            if token_id in self._tokens:
                del self._tokens[token_id]
                return True
            return False
    
    def limpiar_completados(self) -> int:
        """Elimina todos los tokens completados o cancelados."""
        with self._lock:
            a_eliminar = [
                tid for tid, token in self._tokens.items()
                if token.estado in (CancellationState.COMPLETED, CancellationState.CANCELLED)
            ]
            for tid in a_eliminar:
                del self._tokens[tid]
            
            if a_eliminar:
                self._logger.debug(f"Limpiados {len(a_eliminar)} tokens completados")
            return len(a_eliminar)
    
    def obtener_estadisticas(self) -> dict:
        """Obtiene estadísticas del gestor."""
        with self._lock:
            total = len(self._tokens)
            activos = sum(1 for t in self._tokens.values() if t.esta_activo())
            cancelados = sum(1 for t in self._tokens.values() if t.estado == CancellationState.CANCELLED)
            completados = sum(1 for t in self._tokens.values() if t.estado == CancellationState.COMPLETED)
            
            return {
                "total": total,
                "activos": activos,
                "cancelados": cancelados,
                "completados": completados,
            }


# ============================================================
# INSTANCIA GLOBAL
# ============================================================

_cancellation_manager: CancellationManager | None = None

def obtener_gestor_cancelacion() -> CancellationManager:
    """Obtiene la instancia global del gestor de cancelación."""
    global _cancellation_manager
    if _cancellation_manager is None:
        _cancellation_manager = CancellationManager()
    return _cancellation_manager
