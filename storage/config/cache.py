"""Caché LRU thread-safe para configuraciones."""

import os
import time
import threading
from typing import Dict, Optional


class ConfigCache:
    """
    Caché LRU simple y thread-safe para configuraciones cargadas.

    Uso:
        cache = ConfigCache(max_size=20)
        cache.put("/ruta/archivo.json", data)
        data = cache.get("/ruta/archivo.json")
        cache.invalidate("/ruta/archivo.json")
    """

    def __init__(self, max_size: int = 20):
        self.max_size = max_size
        self._cache: Dict[str, Dict] = {}
        self._accessed: Dict[str, float] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _key(ruta: str) -> str:
        return os.path.normpath(ruta)

    def get(self, ruta: str) -> Optional[Dict]:
        """Obtiene datos de la caché, actualizando el timestamp de acceso."""
        with self._lock:
            key = self._key(ruta)
            if key in self._cache:
                self._accessed[key] = time.time()
                return self._cache[key]
            return None

    def put(self, ruta: str, data: Dict):
        """Guarda datos en la caché, aplicando política LRU."""
        with self._lock:
            key = self._key(ruta)

            if len(self._cache) >= self.max_size:
                # Eliminar el menos recientemente usado
                oldest = min(self._accessed, key=self._accessed.get)
                self._cache.pop(oldest, None)
                self._accessed.pop(oldest, None)

            self._cache[key] = data
            self._accessed[key] = time.time()

    def invalidate(self, ruta: str):
        """Invalida una entrada específica."""
        with self._lock:
            key = self._key(ruta)
            self._cache.pop(key, None)
            self._accessed.pop(key, None)

    def clear(self):
        """Limpia toda la caché."""
        with self._lock:
            self._cache.clear()
            self._accessed.clear()

    def size(self) -> int:
        with self._lock:
            return len(self._cache)