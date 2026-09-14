# core/executors/cache.py
"""
Caché HTTP (LRU con TTL) y rate limiter para APIs externas.
Thread-safe.
"""

import json
import time
import hashlib
import threading
import logging
from typing import Dict, Optional, Any, Union
from collections import OrderedDict

logger = logging.getLogger(__name__)


HTTP_CACHE_SIZE = 100
HTTP_CACHE_TTL = 300  # 5 minutos


class HTTPCache:
    """Caché LRU para resultados de peticiones HTTP. Thread-safe con TTL."""

    def __init__(self, max_size: int = HTTP_CACHE_SIZE, ttl: int = HTTP_CACHE_TTL):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.RLock()
        self._stats = {"hits": 0, "misses": 0}

    def _generate_key(self, url: str, method: str, headers: Dict, body: Optional[Any]) -> str:
        headers_normalized = {k.lower(): v for k, v in (headers or {}).items()}
        headers_json = json.dumps(headers_normalized, sort_keys=True)
        body_str = ""
        if body is not None:
            try:
                body_str = json.dumps(body, sort_keys=True, default=str)
            except (TypeError, ValueError):
                body_str = str(body)
        key_str = f"{method.upper()}|{url}|{headers_json}|{body_str}"
        return hashlib.sha256(key_str.encode('utf-8')).hexdigest()

    def get(self, url: str, method: str, headers: Dict, body: Optional[Any]) -> Optional[Dict]:
        key = self._generate_key(url, method, headers, body)
        with self._lock:
            if key not in self._cache:
                self._stats["misses"] += 1
                return None
            entry = self._cache[key]
            if time.time() - entry['timestamp'] > self.ttl:
                del self._cache[key]
                self._stats["misses"] += 1
                return None
            self._cache.move_to_end(key)
            self._stats["hits"] += 1
            return entry['result']

    def put(self, url: str, method: str, headers: Dict, body: Optional[Any], result: Dict):
        key = self._generate_key(url, method, headers, body)
        with self._lock:
            if len(self._cache) >= self.max_size:
                oldest = next(iter(self._cache))
                del self._cache[oldest]
            self._cache[key] = {'timestamp': time.time(), 'result': result}

    def clear(self):
        with self._lock:
            self._cache.clear()
            self._stats = {"hits": 0, "misses": 0}

    def get_stats(self) -> Dict[str, Union[int, float]]:
        with self._lock:
            total = self._stats["hits"] + self._stats["misses"]
            return {
                'size': len(self._cache),
                'hits': self._stats["hits"],
                'misses': self._stats["misses"],
                'hit_ratio': self._stats["hits"] / total if total > 0 else 0
            }


class RateLimiter:
    """Rate limiter simple para APIs externas. Thread-safe."""

    def __init__(self, calls_per_second: float = 10):
        self.calls_per_second = calls_per_second
        self._last_call = 0.0
        self._lock = threading.RLock()

    def wait(self):
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            min_interval = 1.0 / self.calls_per_second
            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)
            self._last_call = time.time()
