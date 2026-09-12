# storage/database/maintenance.py
"""Mantenimiento: limpieza, compactación, backups, integridad."""
import sqlite3
import os
import logging
import shutil
import threading
from datetime import datetime, timedelta
from typing import Dict, Tuple, Optional

from .connection import ConnectionManager
from .schema import (
    CLEANUP_DAYS, CONNECTION_TIMEOUT
)

logger = logging.getLogger(__name__)


class MaintenanceRepository:
    """Operaciones de mantenimiento y backup."""

    def __init__(self, conn_mgr: ConnectionManager):
        self.conn_mgr = conn_mgr
        self._lock = threading.RLock()

    def _obtener_columnas(self, conn: sqlite3.Connection, tabla: str) -> set:
        try:
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({tabla})")
            return {row[1] for row in cursor.fetchall()}
        except sqlite3.OperationalError:
            return set()

    # ============================================================
    # LIMPIEZA
    # ============================================================
    def cleanup_old_records(
        self,
        days: int = CLEANUP_DAYS,
        force: bool = False
    ) -> int:
        """Elimina registros antiguos (excepto 'activa'). VACUUM fuera de transacción."""
        try:
            if days < 0:
                logger.warning(f"Valor inválido para days: {days}. Debe ser >= 0.")
                return 0

            cutoff = (datetime.now() - timedelta(days=days)).isoformat()
            count = 0

            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                columnas = self._obtener_columnas(conn, 'ejecuciones')

                if 'estado' not in columnas:
                    logger.warning("Columna 'estado' no existe, omitiendo limpieza")
                    return 0

                cursor.execute(
                    'SELECT COUNT(*) FROM ejecuciones WHERE fecha < ? AND estado != ?',
                    (cutoff, 'activa')
                )
                row = cursor.fetchone()
                count = row[0] if row else 0

                if count < 10 and not force:
                    logger.debug(f"Limpieza automática: solo {count} registros antiguos, omitiendo")
                    return 0

                if count > 0:
                    cursor.execute(
                        'DELETE FROM ejecuciones WHERE fecha < ? AND estado != ?',
                        (cutoff, 'activa')
                    )
                    if cursor.rowcount >= 0:
                        count = cursor.rowcount
                    logger.info(f"Limpieza: {count} registros antiguos eliminados")

            if count > 1000:
                if self._ejecutar_vacuum():
                    logger.info("VACUUM ejecutado después de la limpieza")
                else:
                    logger.warning(
                        "Los registros fueron eliminados correctamente, "
                        "pero VACUUM no pudo ejecutarse"
                    )
            return count

        except sqlite3.Error as e:
            logger.warning(f"Error SQLite durante la limpieza: {e}")
            return 0
        except Exception as e:
            logger.warning(f"Error en limpieza: {e}")
            return 0

    def limpiar_ejecuciones_antiguas(self, dias: int = 30) -> int:
        """Elimina ejecuciones > N días. VACUUM fuera de transacción."""
        try:
            if dias < 0:
                logger.warning(f"Valor inválido para dias: {dias}. Debe ser >= 0.")
                return 0

            cutoff = (datetime.now() - timedelta(days=dias)).isoformat()
            count = 0

            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT COUNT(*) FROM ejecuciones WHERE fecha < ?', (cutoff,))
                row = cursor.fetchone()
                count = row[0] if row else 0

                if count > 0:
                    cursor.execute('DELETE FROM ejecuciones WHERE fecha < ?', (cutoff,))
                    if cursor.rowcount >= 0:
                        count = cursor.rowcount
                    logger.info(f"Limpiadas {count} ejecuciones antiguas (> {dias} días)")

            if count > 0:
                if not self._ejecutar_vacuum():
                    logger.warning(
                        "Las ejecuciones antiguas fueron eliminadas, "
                        "pero VACUUM no pudo ejecutarse"
                    )
            return count

        except sqlite3.Error as e:
            logger.error(f"Error SQLite limpiando ejecuciones antiguas: {e}")
            return 0
        except Exception as e:
            logger.error(f"Error limpiando ejecuciones antiguas: {e}")
            return 0

    def _check_and_cleanup(self):
        """Verifica tamaño de la BD y ejecuta limpieza si es necesario."""
        try:
            db_path = self.conn_mgr.db_path
            if not os.path.exists(db_path):
                return
            size = os.path.getsize(db_path)
            if size > 50 * 1024 * 1024:
                logger.info(f"Base de datos grande ({size / 1024 / 1024:.1f} MB), ejecutando limpieza")
                self.cleanup_old_records()
        except Exception as e:
            logger.warning(f"Error verificando tamaño: {e}")

    # ============================================================
    # COMPACTAR
    # ============================================================
    def compactar_db(self) -> bool:
        """Compacta físicamente mediante VACUUM (fuera de transacción)."""
        try:
            return self._ejecutar_vacuum()
        except Exception as e:
            logger.error(f"Error compactando base de datos: {e}")
            return False

    def _ejecutar_vacuum(self) -> bool:
        """Ejecuta VACUUM con conexión independiente."""
        vacuum_conn = None
        db_path = self.conn_mgr.db_path
        try:
            with self._lock:
                vacuum_conn = sqlite3.connect(
                    db_path,
                    timeout=CONNECTION_TIMEOUT,
                    isolation_level=None,
                    check_same_thread=False
                )
                vacuum_conn.execute(
                    f"PRAGMA busy_timeout = {int(CONNECTION_TIMEOUT * 1000)}"
                )
                if vacuum_conn.in_transaction:
                    vacuum_conn.commit()
                vacuum_conn.execute("VACUUM")

            logger.info("Base de datos compactada correctamente (VACUUM)")
            return True
        except sqlite3.Error as e:
            logger.error(f"Error ejecutando VACUUM: {e}")
            return False
        except Exception as e:
            logger.error(f"Error inesperado ejecutando VACUUM: {e}")
            return False
        finally:
            if vacuum_conn is not None:
                try:
                    vacuum_conn.close()
                except Exception:
                    pass

    # ============================================================
    # INTEGRIDAD
    # ============================================================
    def verificar_integridad(self) -> Tuple[bool, str]:
        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check")
                result = cursor.fetchone()
                if result and result[0] == "ok":
                    return True, "Base de datos íntegra"
                return False, result[0] if result else "Error desconocido"
        except sqlite3.Error as e:
            return False, f"Error de SQLite: {e}"

    # ============================================================
    # BACKUP / RESTORE
    # ============================================================
    def backup(self, ruta: Optional[str] = None) -> Optional[str]:
        db_path = self.conn_mgr.db_path
        if not ruta:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            ruta = f"{db_path}.backup_{timestamp}"
        try:
            self.verificar_integridad()
            shutil.copy2(db_path, ruta)
            logger.info(f"Backup creado: {ruta}")
            return ruta
        except Exception as e:
            logger.error(f"Error creando backup: {e}")
            return None

    def restore_backup(self, ruta: str) -> bool:
        db_path = self.conn_mgr.db_path
        if not os.path.exists(ruta):
            logger.error(f"Backup no encontrado: {ruta}")
            return False
        try:
            self.conn_mgr.close_connection()

            try:
                conn = sqlite3.connect(ruta, timeout=1)
                conn.execute("PRAGMA integrity_check")
                conn.close()
            except Exception as e:
                logger.error(f"Backup corrupto: {e}")
                return False

            if os.path.exists(db_path):
                backup_path = f"{db_path}.before_restore"
                shutil.copy2(db_path, backup_path)
                logger.info(f"Backup de base actual creado: {backup_path}")

            shutil.copy2(ruta, db_path)
            logger.info(f"Backup restaurado desde {ruta}")
            return True
        except Exception as e:
            logger.error(f"Error restaurando backup: {e}")
            return False

    # ============================================================
    # MANTENIMIENTO COMPLETO
    # ============================================================
    def maintenance(self, days: int = CLEANUP_DAYS) -> Dict:
        result = {
            'limpieza': 0,
            'compactado': False,
            'integro': False,
            'mensaje': ''
        }
        try:
            integro, mensaje = self.verificar_integridad()
            result['integro'] = integro
            result['mensaje'] = mensaje

            if not integro:
                logger.error(f"Base de datos corrupta: {mensaje}")
                return result

            result['limpieza'] = self.cleanup_old_records(days=days, force=True)

            if result['limpieza'] > 100:
                result['compactado'] = self.compactar_db()

            logger.info(f"Mantenimiento completado: {result}")
            return result
        except Exception as e:
            logger.error(f"Error en mantenimiento: {e}")
            result['mensaje'] = f"Error: {e}"
            return result


__all__ = ["MaintenanceRepository"]