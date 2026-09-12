# storage/database/migrations.py
"""Inicialización, verificación, migración y reparación del esquema."""
import sqlite3
import logging
import os
from datetime import datetime
from typing import Dict, Any, Set

from .connection import ConnectionManager
from .schema import DB_VERSION, SCHEMA_DEFINITION, INDEX_DEFINITION

logger = logging.getLogger(__name__)


class SchemaManager:
    """Se encarga de crear, verificar, migrar y reparar el esquema."""

    def __init__(self, conn_mgr: ConnectionManager):
        self.conn_mgr = conn_mgr

    # ============================================================
    # UTILIDADES DE ESQUEMA
    # ============================================================
    def _obtener_columnas(self, conn: sqlite3.Connection, tabla: str) -> Set[str]:
        try:
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({tabla})")
            return {row[1] for row in cursor.fetchall()}
        except sqlite3.OperationalError:
            return set()

    def _obtener_tablas(self, conn: sqlite3.Connection) -> Set[str]:
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            return {row[0] for row in cursor.fetchall()}
        except sqlite3.OperationalError:
            return set()

    def _crear_tabla_ejecuciones(self, conn: sqlite3.Connection):
        conn.execute('''
            CREATE TABLE ejecuciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT NOT NULL,
                duracion_total REAL DEFAULT 0,
                agentes_total INTEGER DEFAULT 0,
                completados INTEGER DEFAULT 0,
                errores INTEGER DEFAULT 0,
                cancelados INTEGER DEFAULT 0,
                estado TEXT DEFAULT 'completada',
                ejecutor TEXT DEFAULT '',
                tags TEXT DEFAULT '',
                notas TEXT DEFAULT ''
            )
        ''')

    def _crear_tabla_agentes_ejecucion(self, conn: sqlite3.Connection):
        conn.execute('''
            CREATE TABLE agentes_ejecucion (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ejecucion_id INTEGER NOT NULL,
                agente_id TEXT NOT NULL,
                nombre TEXT NOT NULL,
                tipo TEXT NOT NULL,
                estado TEXT NOT NULL,
                duracion REAL DEFAULT 0,
                dependencias TEXT DEFAULT '[]',
                resultado TEXT DEFAULT '',
                error TEXT DEFAULT '',
                orden INTEGER DEFAULT 0,
                progreso INTEGER DEFAULT 0,
                FOREIGN KEY (ejecucion_id) REFERENCES ejecuciones(id) ON DELETE CASCADE
            )
        ''')

    # ============================================================
    # VERIFICACIÓN DE ESQUEMA
    # ============================================================
    def verificar_esquema(self):
        """Verifica consistencia del esquema y añade columnas faltantes."""
        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                tablas_existentes = self._obtener_tablas(conn)

                if 'version' not in tablas_existentes:
                    logger.warning("Tabla 'version' no existe, recreando...")
                    cursor.execute('''
                        CREATE TABLE version (
                            version INTEGER PRIMARY KEY,
                            fecha_actualizacion TEXT NOT NULL
                        )
                    ''')
                    cursor.execute(
                        'INSERT INTO version (version, fecha_actualizacion) VALUES (?, ?)',
                        (1, datetime.now().isoformat())
                    )

                if 'ejecuciones' not in tablas_existentes:
                    logger.warning("Tabla 'ejecuciones' no existe, recreando...")
                    self._crear_tabla_ejecuciones(conn)

                if 'agentes_ejecucion' not in tablas_existentes:
                    logger.warning("Tabla 'agentes_ejecucion' no existe, recreando...")
                    self._crear_tabla_agentes_ejecucion(conn)

                if 'auditoria' not in tablas_existentes:
                    logger.warning("Tabla 'auditoria' no existe, recreando...")
                    cursor.execute('''
                        CREATE TABLE auditoria (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            usuario TEXT DEFAULT '',
                            accion TEXT NOT NULL,
                            detalle TEXT DEFAULT '',
                            ip TEXT DEFAULT ''
                        )
                    ''')

                for tabla, columnas_requeridas in SCHEMA_DEFINITION.items():
                    if tabla not in self._obtener_tablas(conn):
                        continue
                    columnas_existentes = self._obtener_columnas(conn, tabla)
                    for columna, definicion in columnas_requeridas.items():
                        if columna not in columnas_existentes:
                            logger.warning(
                                f"Columna '{columna}' faltante en '{tabla}', añadiendo..."
                            )
                            try:
                                cursor.execute(
                                    f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}"
                                )
                                logger.info(f"✅ Columna añadida: {tabla}.{columna}")
                            except sqlite3.OperationalError as e:
                                logger.error(f"Error añadiendo columna {tabla}.{columna}: {e}")

                logger.debug("Verificación de esquema completada")
        except Exception as e:
            logger.error(f"Error verificando esquema: {e}")
            raise

    _verificar_esquema = verificar_esquema

    # ============================================================
    # DIAGNÓSTICO
    # ============================================================
    def diagnostico(self) -> Dict[str, Any]:
        """Retorna un diagnóstico completo del estado de la base de datos."""
        db_path = self.conn_mgr.db_path
        resultado = {
            "ruta": db_path,
            "existe": os.path.exists(db_path),
            "tamaño_bytes": 0,
            "version": DB_VERSION,
            "tablas": {},
            "columnas_faltantes": [],
            "integridad": "unknown",
        }
        try:
            if resultado["existe"]:
                resultado["tamaño_bytes"] = os.path.getsize(db_path)

            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check")
                integrity_result = cursor.fetchone()
                resultado["integridad"] = integrity_result[0] if integrity_result else "unknown"

                tablas_existentes = self._obtener_tablas(conn)
                for tabla in tablas_existentes:
                    if tabla.startswith("sqlite_"):
                        continue
                    columnas = self._obtener_columnas(conn, tabla)
                    resultado["tablas"][tabla] = sorted(columnas)
                    if tabla in SCHEMA_DEFINITION:
                        requeridas = set(SCHEMA_DEFINITION[tabla].keys())
                        faltantes = requeridas - columnas
                        if faltantes:
                            resultado["columnas_faltantes"].append({
                                "tabla": tabla,
                                "columnas": sorted(faltantes)
                            })
            return resultado
        except Exception as e:
            logger.error(f"Error en diagnóstico: {e}")
            resultado["error"] = str(e)
            return resultado

    # ============================================================
    # REPARACIÓN
    # ============================================================
    def reparar_esquema(self) -> Dict[str, Any]:
        """Repara el esquema si está corrupto."""
        resultado = {
            'exito': False,
            'columnas_añadidas': [],
            'tablas_creadas': [],
            'indices_creados': [],
            'errores': []
        }
        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                tablas_existentes = self._obtener_tablas(conn)

                if 'version' not in tablas_existentes:
                    cursor.execute('''
                        CREATE TABLE version (
                            version INTEGER PRIMARY KEY,
                            fecha_actualizacion TEXT NOT NULL
                        )
                    ''')
                    cursor.execute(
                        'INSERT INTO version (version, fecha_actualizacion) VALUES (?, ?)',
                        (DB_VERSION, datetime.now().isoformat())
                    )
                    resultado['tablas_creadas'].append('version')

                if 'ejecuciones' not in tablas_existentes:
                    self._crear_tabla_ejecuciones(conn)
                    resultado['tablas_creadas'].append('ejecuciones')

                if 'agentes_ejecucion' not in tablas_existentes:
                    self._crear_tabla_agentes_ejecucion(conn)
                    resultado['tablas_creadas'].append('agentes_ejecucion')

                if 'auditoria' not in tablas_existentes:
                    cursor.execute('''
                        CREATE TABLE auditoria (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            timestamp TEXT NOT NULL,
                            usuario TEXT DEFAULT '',
                            accion TEXT NOT NULL,
                            detalle TEXT DEFAULT '',
                            ip TEXT DEFAULT ''
                        )
                    ''')
                    resultado['tablas_creadas'].append('auditoria')

                for tabla, columnas_requeridas in SCHEMA_DEFINITION.items():
                    if tabla not in self._obtener_tablas(conn):
                        continue
                    columnas_existentes = self._obtener_columnas(conn, tabla)
                    for columna, definicion in columnas_requeridas.items():
                        if columna not in columnas_existentes:
                            try:
                                cursor.execute(
                                    f"ALTER TABLE {tabla} ADD COLUMN {columna} {definicion}"
                                )
                                resultado['columnas_añadidas'].append(f"{tabla}.{columna}")
                                logger.info(f"✅ Columna reparada: {tabla}.{columna}")
                            except sqlite3.OperationalError as e:
                                resultado['errores'].append(f"{tabla}.{columna}: {e}")

                self.crear_indices()
                resultado['indices_creados'].append('todos')
                resultado['exito'] = len(resultado['errores']) == 0
                logger.info(f"Reparación de esquema completada: {resultado}")
        except Exception as e:
            logger.error(f"Error reparando esquema: {e}")
            resultado['errores'].append(str(e))
        return resultado

    # ============================================================
    # INIT + MIGRACIONES
    # ============================================================
    def init_db(self):
        """Crea la estructura base si no existe. NO fija DB_VERSION."""
        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS version (
                        version INTEGER PRIMARY KEY,
                        fecha_actualizacion TEXT NOT NULL
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS ejecuciones (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        fecha TEXT NOT NULL,
                        duracion_total REAL DEFAULT 0,
                        agentes_total INTEGER DEFAULT 0,
                        completados INTEGER DEFAULT 0,
                        errores INTEGER DEFAULT 0,
                        cancelados INTEGER DEFAULT 0,
                        estado TEXT DEFAULT 'completada',
                        ejecutor TEXT DEFAULT '',
                        tags TEXT DEFAULT '',
                        notas TEXT DEFAULT ''
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS agentes_ejecucion (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ejecucion_id INTEGER NOT NULL,
                        agente_id TEXT NOT NULL,
                        nombre TEXT NOT NULL,
                        tipo TEXT NOT NULL,
                        estado TEXT NOT NULL,
                        duracion REAL DEFAULT 0,
                        dependencias TEXT DEFAULT '[]',
                        resultado TEXT DEFAULT '',
                        error TEXT DEFAULT '',
                        orden INTEGER DEFAULT 0,
                        progreso INTEGER DEFAULT 0,
                        FOREIGN KEY (ejecucion_id)
                            REFERENCES ejecuciones(id)
                            ON DELETE CASCADE
                    )
                ''')
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS auditoria (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp TEXT NOT NULL,
                        usuario TEXT DEFAULT '',
                        accion TEXT NOT NULL,
                        detalle TEXT DEFAULT '',
                        ip TEXT DEFAULT ''
                    )
                ''')
                logger.debug("Estructura base de la base de datos verificada")
        except sqlite3.Error as e:
            logger.error(f"Error inicializando base de datos: {e}")
            raise

    _init_db = init_db

    def migrar_db(self):
        """Aplica migraciones al esquema."""
        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT version FROM version ORDER BY version DESC LIMIT 1")
                result = cursor.fetchone()
                current_version = result[0] if result else 0

                if current_version >= DB_VERSION:
                    logger.debug(f"Base de datos ya está en versión {DB_VERSION}")
                    return

                logger.info(f"Migrando base de datos de versión {current_version} a {DB_VERSION}")

                # Migración 1 → 2
                if current_version < 2:
                    try:
                        columnas = self._obtener_columnas(conn, 'ejecuciones')
                        if 'estado' not in columnas:
                            cursor.execute("ALTER TABLE ejecuciones ADD COLUMN estado TEXT DEFAULT 'completada'")
                        if 'ejecutor' not in columnas:
                            cursor.execute("ALTER TABLE ejecuciones ADD COLUMN ejecutor TEXT DEFAULT ''")
                        if 'tags' not in columnas:
                            cursor.execute("ALTER TABLE ejecuciones ADD COLUMN tags TEXT DEFAULT ''")
                        if 'notas' not in columnas:
                            cursor.execute("ALTER TABLE ejecuciones ADD COLUMN notas TEXT DEFAULT ''")

                        columnas_agentes = self._obtener_columnas(conn, 'agentes_ejecucion')
                        if 'progreso' not in columnas_agentes:
                            cursor.execute("ALTER TABLE agentes_ejecucion ADD COLUMN progreso INTEGER DEFAULT 0")
                        if 'orden' not in columnas_agentes:
                            cursor.execute("ALTER TABLE agentes_ejecucion ADD COLUMN orden INTEGER DEFAULT 0")
                    except sqlite3.OperationalError as e:
                        logger.warning(f"Error en migración 1→2: {e}")

                # Migración 2 → 3
                if current_version < 3:
                    try:
                        cursor.execute('''
                            CREATE TABLE IF NOT EXISTS auditoria (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                timestamp TEXT NOT NULL,
                                usuario TEXT DEFAULT '',
                                accion TEXT NOT NULL,
                                detalle TEXT DEFAULT '',
                                ip TEXT DEFAULT ''
                            )
                        ''')
                        cursor.execute('''
                            CREATE INDEX IF NOT EXISTS idx_auditoria_timestamp
                            ON auditoria(timestamp DESC)
                        ''')
                    except sqlite3.OperationalError as e:
                        logger.warning(f"Error en migración 2→3: {e}")

                # Migración 3 → 4
                if current_version < 4:
                    try:
                        columnas = self._obtener_columnas(conn, 'ejecuciones')
                        if 'cancelados' not in columnas:
                            cursor.execute("ALTER TABLE ejecuciones ADD COLUMN cancelados INTEGER DEFAULT 0")
                            logger.info("✅ Migración 3→4: columna 'cancelados' añadida")
                        if 'agentes_total' not in columnas:
                            cursor.execute("ALTER TABLE ejecuciones ADD COLUMN agentes_total INTEGER DEFAULT 0")
                        if 'duracion_total' not in columnas:
                            cursor.execute("ALTER TABLE ejecuciones ADD COLUMN duracion_total REAL DEFAULT 0")
                    except sqlite3.OperationalError as e:
                        logger.warning(f"Error en migración 3→4: {e}")

                cursor.execute("DELETE FROM version")
                cursor.execute(
                    'INSERT INTO version (version, fecha_actualizacion) VALUES (?, ?)',
                    (DB_VERSION, datetime.now().isoformat())
                )
                logger.info(f"Base de datos migrada a versión {DB_VERSION}")
        except Exception as e:
            logger.error(f"Error en migración: {e}")
            raise

    _migrar_db = migrar_db

    def crear_indices(self):
        """Crea índices verificando que las columnas existan."""
        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                columnas_ejecuciones = self._obtener_columnas(conn, 'ejecuciones')
                columnas_agentes = self._obtener_columnas(conn, 'agentes_ejecucion')
                columnas_auditoria = self._obtener_columnas(conn, 'auditoria')

                tablas_columnas = {
                    'ejecuciones': columnas_ejecuciones,
                    'agentes_ejecucion': columnas_agentes,
                    'auditoria': columnas_auditoria,
                }

                for nombre_indice, (tabla, columna) in INDEX_DEFINITION.items():
                    columnas_tabla = tablas_columnas.get(tabla, set())
                    if columna in columnas_tabla:
                        try:
                            cursor.execute(
                                f"CREATE INDEX IF NOT EXISTS {nombre_indice} "
                                f"ON {tabla}({columna})"
                            )
                            logger.debug(f"Índice creado/verificado: {nombre_indice}")
                        except sqlite3.OperationalError as e:
                            logger.warning(f"Error creando índice {nombre_indice}: {e}")
                    else:
                        logger.warning(
                            f"⚠️ Columna '{columna}' no existe en '{tabla}', "
                            f"omitiendo índice {nombre_indice}"
                        )
        except Exception as e:
            logger.error(f"Error creando índices: {e}")

    _crear_indices = crear_indices


__all__ = ["SchemaManager"]