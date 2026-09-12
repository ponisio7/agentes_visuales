# core/sandbox/temp_files.py
"""
Gestor de archivos temporales con limpieza automática (thread-safe).
"""

import os
import time
import threading
from collections import OrderedDict
from typing import Dict
import atexit

MAX_TEMP_FILES = 100
TEMP_FILE_AGE_LIMIT = 3600   # 1 hora: límite de antigüedad para limpieza forzada
STALE_THRESHOLD = 60         # segundos sin "touch" antes de considerar el archivo libre
CLEANUP_INTERVAL = 30        # frecuencia (s) con la que corre el worker


class TempFileManager:
    """
    Gestor de archivos temporales con limpieza automática.
    Thread-safe para uso en entornos con múltiples hilos.
    """

    _instance = None
    _class_lock = threading.RLock()

    def __new__(cls):
        with cls._class_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._lock = threading.RLock()
        self._temp_files: Dict[str, float] = OrderedDict()  # path -> último "touch"
        self._stop_event = threading.Event()
        self._cleanup_thread = None
        self._initialized = True
        self._start_cleanup_thread()

    def _start_cleanup_thread(self):
        """Inicia el hilo de limpieza automática."""
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_worker,
            daemon=True,
            name="TempFileCleaner",
        )
        self._cleanup_thread.start()

    def _cleanup_worker(self):
        """Borra periódicamente los archivos que llevan STALE_THRESHOLD sin uso."""
        while not self._stop_event.wait(CLEANUP_INTERVAL):
            now = time.time()
            with self._lock:
                a_borrar = [
                    p for p, ts in self._temp_files.items()
                    if now - ts > STALE_THRESHOLD
                ]
                for p in a_borrar:
                    self._temp_files.pop(p, None)
            for p in a_borrar:
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def stop(self):
        """Detiene el hilo de limpieza de forma ordenada."""
        self._stop_event.set()
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=2.0)

    def register(self, path: str) -> bool:
        """
        Registra un archivo temporal para limpieza automática.

        Returns:
            bool: True si se registró correctamente.
        """
        if not path or not os.path.exists(path):
            return False

        with self._lock:
            self._temp_files[path] = time.time()
            self._temp_files.move_to_end(path)

            # Eviction LRU: si nos pasamos del máximo, solo se saca el más viejo,
            # no se limpia todo (a diferencia de force=True).
            if len(self._temp_files) > MAX_TEMP_FILES:
                oldest = next(iter(self._temp_files))
                self._unregister_file(oldest)

        return True

    def touch(self, path: str):
        """
        Marca el archivo como 'todavía en uso', refrescando su timestamp.
        Llamar a esto mientras un proceso sigue leyendo/escribiendo el archivo
        evita que el worker lo borre por STALE_THRESHOLD.
        """
        with self._lock:
            if path in self._temp_files:
                self._temp_files[path] = time.time()
                self._temp_files.move_to_end(path)

    def unregister(self, path: str):
        with self._lock:
            self._unregister_file(path)

    def _unregister_file(self, path: str) -> bool:
        """Versión interna: debe llamarse ya con self._lock adquirido (RLock, es reentrante)."""
        if path in self._temp_files:
            del self._temp_files[path]
            try:
                if os.path.exists(path):
                    os.unlink(path)
                return True
            except OSError:
                return False
        return False

    def _cleanup_old_files(self, force: bool = False):
        """
        Limpia archivos temporales antiguos.

        Args:
            force: si es True, limpia TODOS los archivos registrados
                   (úsalo solo para cleanup_all/salida, no como eviction normal).
        """
        with self._lock:
            now = time.time()
            to_remove = [
                path for path, timestamp in self._temp_files.items()
                if force or (now - timestamp) > TEMP_FILE_AGE_LIMIT
            ]
            for path in to_remove:
                self._unregister_file(path)

    def cleanup_all(self):
        """Limpia todos los archivos temporales registrados."""
        with self._lock:
            paths = list(self._temp_files.keys())
            for path in paths:
                self._unregister_file(path)


def _cleanup_temp_files_at_exit():
    """Detiene el hilo de limpieza y borra archivos temporales al salir."""
    try:
        if TempFileManager._instance is not None:
            TempFileManager._instance.stop()
            TempFileManager._instance.cleanup_all()
    except Exception:
        pass


atexit.register(_cleanup_temp_files_at_exit)