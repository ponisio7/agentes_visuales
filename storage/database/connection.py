# storage/database/connection.py
"""Gestión de conexiones SQLite thread-safe y transacciones."""
import sqlite3
import os
import time
import threading
import logging
from contextlib import contextmanager

from .schema import CONNECTION_TIMEOUT, MAX_RETRIES, RETRY_DELAY

logger = logging.getLogger(__name__)


class ConnectionManager:
    """Gestiona conexiones por hilo y transacciones con retry."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._lock = threading.RLock()

    # ============================================================
    # CONEXIONES (Thread-safe)
    # ============================================================
    def get_connection(self) -> sqlite3.Connection:
        """Obtiene o crea una conexión para el hilo actual."""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            try:
                db_dir = os.path.dirname(os.path.abspath(self.db_path))
                if db_dir:
                    os.makedirs(db_dir, exist_ok=True)

                conn = sqlite3.connect(
                    self.db_path,
                    timeout=CONNECTION_TIMEOUT,
                    isolation_level=None,
                    check_same_thread=False
                )
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA cache_size=10000")
                conn.execute("PRAGMA temp_store=MEMORY")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA encoding='UTF-8'")
                conn.row_factory = sqlite3.Row

                self._local.connection = conn
                logger.debug(f"Nueva conexión creada para hilo {threading.get_ident()}")
            except sqlite3.Error as e:
                logger.error(f"Error conectando a la base de datos: {e}")
                raise RuntimeError(f"Error al conectar a la base de datos: {e}")
        return self._local.connection

    def close_connection(self):
        """Cierra la conexión del hilo actual."""
        if hasattr(self._local, 'connection') and self._local.connection:
            try:
                self._local.connection.close()
            except Exception:
                pass
            self._local.connection = None

    @contextmanager
    def transaction(self, retries: int = MAX_RETRIES):
        """Context manager para transacciones con retry automático."""
        conn = self.get_connection()
        attempt = 0
        while attempt < retries:
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
                return
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < retries - 1:
                    attempt += 1
                    time.sleep(RETRY_DELAY * (attempt + 1))
                    logger.debug(f"Reintentando transacción ({attempt}/{retries})")
                    continue
                conn.execute("ROLLBACK")
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
        raise RuntimeError("No se pudo completar la transacción después de varios intentos")

    # Alias cortos para uso interno de los repositorios
    _get_connection = get_connection
    _close_connection = close_connection
    _transaction = transaction


__all__ = ["ConnectionManager"]