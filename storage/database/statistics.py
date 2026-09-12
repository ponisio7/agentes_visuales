# storage/database/statistics.py
"""Repositorio de estadísticas agregadas."""
import sqlite3
import os
import logging
from datetime import datetime, timedelta
from typing import Dict

from .connection import ConnectionManager
from .schema import DB_VERSION

logger = logging.getLogger(__name__)


class StatisticsRepository:
    """Consultas agregadas y metadatos de la base de datos."""

    def __init__(self, conn_mgr: ConnectionManager):
        self.conn_mgr = conn_mgr

    def obtener_estadisticas(self, dias: int = 30) -> Dict:
        """Estadísticas agregadas de los últimos N días."""
        try:
            cutoff = (datetime.now() - timedelta(days=dias)).isoformat()
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()

                cursor.execute('SELECT COUNT(*) FROM ejecuciones WHERE fecha > ?', (cutoff,))
                total = cursor.fetchone()[0]

                cursor.execute('''
                    SELECT
                        AVG(duracion_total) as avg_duracion,
                        AVG(agentes_total) as avg_agentes,
                        AVG(completados) as avg_completados,
                        AVG(errores) as avg_errores,
                        SUM(completados) as total_completados,
                        SUM(errores) as total_errores,
                        SUM(cancelados) as total_cancelados
                    FROM ejecuciones WHERE fecha > ?
                ''', (cutoff,))
                stats = cursor.fetchone()

                por_estado = {}
                cursor.execute('''
                    SELECT estado, COUNT(*) as count
                    FROM ejecuciones
                    WHERE fecha > ?
                    GROUP BY estado
                ''', (cutoff,))
                por_estado = {row['estado']: row['count'] for row in cursor.fetchall()}

                return {
                    'total_ejecuciones': total,
                    'duracion_promedio': stats[0] or 0,
                    'agentes_promedio': stats[1] or 0,
                    'completados_promedio': stats[2] or 0,
                    'errores_promedio': stats[3] or 0,
                    'total_completados': stats[4] or 0,
                    'total_errores': stats[5] or 0,
                    'total_cancelados': stats[6] or 0,
                    'por_estado': por_estado,
                    'dias': dias
                }
        except sqlite3.Error as e:
            logger.error(f"Error obteniendo estadísticas: {e}")
            return {}

    def obtener_info_db(self) -> Dict:
        """Información general de la base de datos."""
        try:
            db_path = self.conn_mgr.db_path
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                size = os.path.getsize(db_path) if os.path.exists(db_path) else 0

                cursor.execute("SELECT COUNT(*) FROM ejecuciones")
                total_ejecuciones = cursor.fetchone()[0]

                cursor.execute("SELECT COUNT(*) FROM agentes_ejecucion")
                total_agentes = cursor.fetchone()[0]

                cursor.execute('''
                    SELECT fecha, estado FROM ejecuciones
                    ORDER BY fecha DESC LIMIT 1
                ''')
                ultima = cursor.fetchone()

                return {
                    'ruta': db_path,
                    'tamaño_bytes': size,
                    'tamaño_mb': size / (1024 * 1024),
                    'total_ejecuciones': total_ejecuciones,
                    'total_agentes': total_agentes,
                    'ultima_ejecucion': dict(ultima) if ultima else None,
                    'version': DB_VERSION
                }
        except Exception as e:
            logger.error(f"Error obteniendo info de DB: {e}")
            return {}


__all__ = ["StatisticsRepository"]