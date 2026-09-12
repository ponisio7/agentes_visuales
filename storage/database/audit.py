# storage/database/audit.py
"""Repositorio de auditoría."""
import os
import sqlite3
import logging
from datetime import datetime
from typing import List, Dict

from .connection import ConnectionManager

logger = logging.getLogger(__name__)


class AuditRepository:
    """Registra y consulta acciones en la tabla de auditoría."""

    def __init__(self, conn_mgr: ConnectionManager):
        self.conn_mgr = conn_mgr

    def registrar(self, conn: sqlite3.Connection, accion: str, detalle: str = ""):
        """Registra una acción en la tabla de auditoría."""
        try:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO auditoria (timestamp, usuario, accion, detalle)
                VALUES (?, ?, ?, ?)
            ''', (
                datetime.now().isoformat(),
                os.environ.get('USER', 'desconocido'),
                accion,
                detalle[:500]
            ))
        except Exception as e:
            logger.warning(f"Error registrando auditoría: {e}")

    # Alias con el nombre original
    _registrar_auditoria = registrar

    def obtener_auditoria(self, limit: int = 100) -> List[Dict]:
        """Obtiene el log de auditoría."""
        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT * FROM auditoria
                    ORDER BY timestamp DESC
                    LIMIT ?
                ''', (limit,))
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error obteniendo auditoría: {e}")
            return []


__all__ = ["AuditRepository"]