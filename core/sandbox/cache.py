# core/sandbox/cache.py
"""
Caché LRU para resultados de ejecución de código en el sandbox.
"""

import hashlib
import json
import time
import threading
from collections import OrderedDict
from typing import Dict, Optional

from .models import SandboxResult

# Constantes
CACHE_MAX_SIZE = 100
CACHE_TTL = 300  # 5 minutos


class SandboxCache:
    """Caché LRU para resultados de ejecución de código."""

    def __init__(self, max_size: int = CACHE_MAX_SIZE, ttl: int = CACHE_TTL):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0

    def _get_key(self, codigo: str, contexto_hash: str) -> str:
        """Genera una clave única para el caché."""
        code_hash = hashlib.sha256(codigo.encode('utf-8')).hexdigest()
        return f"{code_hash}_{contexto_hash}"

    def get(self, codigo: str, contexto: Dict) -> Optional[SandboxResult]:
        """
        Obtiene un resultado del caché si existe y es válido.

        Args:
            codigo: Código ejecutado
            contexto: Contexto usado

        Returns:
            Optional[SandboxResult]: Resultado cacheado o None
        """
        contexto_hash = hashlib.sha256(
            json.dumps(contexto, sort_keys=True, default=str).encode('utf-8')
        ).hexdigest()

        key = self._get_key(codigo, contexto_hash)

        with self._lock:
            if key in self._cache:
                entry = self._cache[key]
                if time.time() - entry['timestamp'] < self.ttl:
                    self._cache.move_to_end(key)
                    self._hits += 1
                    return entry['result']
                else:
                    del self._cache[key]

            self._misses += 1
            return None

    def put(self, codigo: str, contexto: Dict, result: SandboxResult):
        """
        Guarda un resultado en el caché.

        Args:
            codigo: Código ejecutado
            contexto: Contexto usado
            result: Resultado a cachear
        """
        contexto_hash = hashlib.sha256(
            json.dumps(contexto, sort_keys=True, default=str).encode('utf-8')
        ).hexdigest()

        key = self._get_key(codigo, contexto_hash)

        with self._lock:
            if len(self._cache) >= self.max_size:
                oldest = next(iter(self._cache))
                del self._cache[oldest]

            self._cache[key] = {
                'timestamp': time.time(),
                'result': result
            }

    def clear(self):
        """Limpia todo el caché."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0

    def get_stats(self) -> Dict[str, int]:
        """Obtiene estadísticas del caché."""
        with self._lock:
            return {
                'size': len(self._cache),
                'hits': self._hits,
                'misses': self._misses,
                'hit_ratio': self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0
            }