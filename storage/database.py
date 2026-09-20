# storage/database.py - VERSIÓN COMPLETAMENTE REFACTORIZADA
"""
Gestor de persistencia SQLite para historial de ejecuciones.

CARACTERÍSTICAS PRINCIPALES:
- ✅ Esquema centralizado (SCHEMA_DEFINITION) - fuente única de verdad
- ✅ Verificación automática de TODAS las columnas al iniciar
- ✅ Migraciones seguras con versionado explícito
- ✅ Comparación de estados normalizada (case-insensitive)
- ✅ Validación de datos antes de insertar
- ✅ Método de diagnóstico para inspeccionar la BD
- ✅ Thread-safe con conexiones por hilo
- ✅ Transacciones atómicas con retry automático
- ✅ Compresión de JSONs grandes (GZIP)
- ✅ Modo WAL para mejor concurrencia
- ✅ Auditoría de operaciones críticas
- ✅ Backup y restauración con verificación de integridad

MEJORAS RESPECTO A VERSIÓN ANTERIOR:
- ✅ Lista centralizada de columnas requeridas (SCHEMA_DEFINITION)
- ✅ _verificar_esquema() ahora detecta cancelados, agentes_total, duracion_total
- ✅ Comparación de estados normalizada (_normalizar_estado)
- ✅ Método diagnostico() para inspeccionar estado de la BD
- ✅ Validación de datos en guardar_ejecucion()
- ✅ Manejo robusto de errores con logging estructurado
"""
import base64
import gzip
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any

# Configurar logger
logger = logging.getLogger(__name__)

# ============================================================
# CONSTANTES GLOBALES
# ============================================================
DEFAULT_DB_PATH = "agent_history.db"
DB_VERSION = 8  # ✅ FASE 5b: columna embedding en prompts_reescritos
MAX_RETRIES = 3
RETRY_DELAY = 0.1  # segundos
CONNECTION_TIMEOUT = 10.0  # segundos
MAX_JSON_SIZE = 10 * 1024 * 1024  # 10 MB
COMPRESSION_THRESHOLD = 1024  # 1 KB
CLEANUP_DAYS = 30  # Días a mantener historial
BATCH_SIZE = 100  # Tamaño de lote para inserción

# ============================================================
# ✅ DEFINICIÓN CENTRALIZADA DEL ESQUEMA
# ============================================================
# Fuente única de verdad: todas las columnas requeridas por tabla.
# Si una columna falta en la BD real, se añade automáticamente.
SCHEMA_DEFINITION = {
    "ejecuciones": {
        "fecha": "TEXT NOT NULL",
        "duracion_total": "REAL DEFAULT 0",
        "agentes_total": "INTEGER DEFAULT 0",
        "completados": "INTEGER DEFAULT 0",
        "errores": "INTEGER DEFAULT 0",
        "cancelados": "INTEGER DEFAULT 0",  # ✅ CRÍTICO: antes faltaba en verificación
        "estado": "TEXT DEFAULT 'completada'",
        "ejecutor": "TEXT DEFAULT ''",
        "tags": "TEXT DEFAULT ''",
        "notas": "TEXT DEFAULT ''",
    },
    "agentes_ejecucion": {
        "ejecucion_id": "INTEGER NOT NULL",
        "agente_id": "TEXT NOT NULL",
        "nombre": "TEXT NOT NULL",
        "descripcion": "TEXT DEFAULT ''",
        "tipo": "TEXT NOT NULL",
        "estado": "TEXT NOT NULL",
        "duracion": "REAL DEFAULT 0",
        "dependencias": "TEXT DEFAULT '[]'",
        "resultado": "TEXT DEFAULT ''",
        "error": "TEXT DEFAULT ''",
        "orden": "INTEGER DEFAULT 0",
        "progreso": "INTEGER DEFAULT 0",
        "prompt_usado": "TEXT DEFAULT ''",     # ✅ FASE 1
    },
    "auditoria": {
        "timestamp": "TEXT NOT NULL",
        "usuario": "TEXT DEFAULT ''",
        "accion": "TEXT NOT NULL",
        "detalle": "TEXT DEFAULT ''",
        "ip": "TEXT DEFAULT ''",
    },
}


def _definicion_alter(definicion: str) -> str:
    """Añade un DEFAULT a columnas NOT NULL para poder usar ALTER TABLE.

    SQLite rechaza ``ADD COLUMN x TEXT NOT NULL`` sobre una tabla con filas
    si no hay DEFAULT; si la columna falta en una BD existente, la
    reparación/migración fallaría y la columna nunca se crearía.
    """
    definicion = definicion.strip()
    upper = definicion.upper()
    if "NOT NULL" not in upper or "DEFAULT" in upper:
        return definicion
    if upper.startswith("INTEGER") or upper.startswith("REAL") or upper.startswith("NUMERIC"):
        return definicion + " DEFAULT 0"
    return definicion + " DEFAULT ''"

# Índices recomendados (nombre → (tabla, columna))
INDEX_DEFINITION = {
    "idx_ejecuciones_fecha": ("ejecuciones", "fecha"),
    "idx_ejecuciones_estado": ("ejecuciones", "estado"),
    "idx_agentes_ejecucion_ejecucion_id": ("agentes_ejecucion", "ejecucion_id"),
    "idx_agentes_ejecucion_nombre": ("agentes_ejecucion", "nombre"),
    "idx_agentes_ejecucion_estado": ("agentes_ejecucion", "estado"),
    "idx_agentes_ejecucion_tipo": ("agentes_ejecucion", "tipo"),
    "idx_auditoria_timestamp": ("auditoria", "timestamp"),
}

# ============================================================
# MODELOS DE DATOS
# ============================================================
@dataclass
class Ejecucion:
    """Modelo de una ejecución."""
    id: int | None = None
    fecha: str | None = None
    duracion_total: float = 0.0
    agentes_total: int = 0
    completados: int = 0
    errores: int = 0
    cancelados: int = 0
    estado: str = "completada"
    ejecutor: str = ""
    tags: str = ""
    notas: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class AgenteEjecucion:
    """Modelo de un agente en una ejecución."""
    id: int | None = None
    ejecucion_id: int = 0
    agente_id: str = ""
    nombre: str = ""
    tipo: str = ""
    estado: str = ""
    duracion: float = 0.0
    dependencias: list[str] = field(default_factory=list)
    resultado: dict | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        data = asdict(self)
        data['dependencias'] = json.dumps(data['dependencias'])
        if data['resultado']:
            data['resultado'] = json.dumps(data['resultado'])
        return data


# ============================================================
# CLASE PRINCIPAL: DATABASE
# ============================================================
class Database:
    """
    Gestor de persistencia SQLite para historial de ejecuciones.
    Thread-safe con conexiones por hilo.
    """

    # storage/database.py - SECCIÓN __init__ CORREGIDA

    def __init__(
        self,
        db_path: str = DEFAULT_DB_PATH
    ):
        """
        Inicializa el gestor de base de datos.

        Orden de inicialización:

            1. Crear/verificar estructura base
            2. ✅ Ejecutar migraciones pendientes (NUEVO)
            3. Verificar integridad del esquema
            4. Crear índices
            5. Marcar la base de datos como inicializada

        Args:
            db_path: Ruta al archivo SQLite.
        """
        self.db_path = db_path
        self._local = threading.local()
        self._lock = threading.RLock()
        self._initialized = False
        self._closed = False    # ← AÑADIR

        try:
            # ----------------------------------------------------------
            # 1. Crear estructura base
            # ----------------------------------------------------------
            self._init_db()

            # ----------------------------------------------------------
            # 2. ✅ MUY IMPORTANTE: Ejecutar migraciones ANTES de verificar
            # ----------------------------------------------------------
            self._migrar_db()  # ← AÑADIR ESTA LÍNEA

            # ----------------------------------------------------------
            # 3. Verificación adicional del esquema
            # ----------------------------------------------------------
            self._verificar_esquema()

            # ----------------------------------------------------------
            # 4. Crear/verificar índices
            # ----------------------------------------------------------
            self._crear_indices()

            # ----------------------------------------------------------
            # 5. ✅ Tablas del módulo de aprendizaje (opcional)
            # ----------------------------------------------------------
            self._aplicar_esquema_learning()

            self._initialized = True

            logger.info(
                f"Base de datos inicializada: "
                f"{db_path} (versión {DB_VERSION})"
            )

        except Exception as e:
            self._initialized = False

            logger.error(
                f"Error inicializando la base de datos "
                f"'{db_path}': {e}"
            )

            # Cerrar cualquier conexión parcialmente abierta
            try:
                self._close_connection()
            except Exception:
                pass

            raise

        

    # ============================================================
    # CONEXIONES (Thread-safe)
    # ============================================================
    def _get_connection(self) -> sqlite3.Connection:
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
                raise RuntimeError(f"Error al conectar a la base de datos: {e}") from e
        return self._local.connection

    def _close_connection(self):
        """Cierra la conexión del hilo actual."""
        if hasattr(self._local, 'connection') and self._local.connection:
            try:
                self._local.connection.close()
            except Exception:
                pass
            self._local.connection = None

    @contextmanager
    def _transaction(self, retries: int = MAX_RETRIES):
        """Context manager para transacciones con retry automático.

        Solo se reintenta el ``BEGIN IMMEDIATE`` cuando la BD está
        bloqueada; el cuerpo NUNCA se reintenta (podría duplicar efectos
        secundarios) y siempre se hace ROLLBACK si algo falla dentro.
        """
        conn = self._get_connection()
        last_error: sqlite3.OperationalError | None = None
        for attempt in range(retries):
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e) and attempt < retries - 1:
                    last_error = e
                    time.sleep(RETRY_DELAY * (attempt + 1))
                    logger.debug(f"Reintentando BEGIN de transacción ({attempt + 1}/{retries})")
                    continue
                raise

            try:
                yield conn
                conn.execute("COMMIT")
                return
            except Exception:
                if conn.in_transaction:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                raise

        if last_error is not None:
            raise last_error
        raise RuntimeError("No se pudo completar la transacción después de varios intentos")

    # ============================================================
    # UTILIDADES DE ESQUEMA
    # ============================================================
    def _obtener_columnas(self, conn: sqlite3.Connection, tabla: str) -> set:
        """Obtiene el conjunto de columnas de una tabla."""
        try:
            cursor = conn.cursor()
            cursor.execute(f"PRAGMA table_info({tabla})")
            return {row[1] for row in cursor.fetchall()}
        except sqlite3.OperationalError:
            return set()

    def _obtener_tablas(self, conn: sqlite3.Connection) -> set:
        """Obtiene el conjunto de tablas existentes."""
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
            return {row[0] for row in cursor.fetchall()}
        except sqlite3.OperationalError:
            return set()

    def _crear_tabla_ejecuciones(self, conn: sqlite3.Connection):
        """Crea la tabla ejecuciones con el esquema completo."""
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
        """Crea la tabla agentes_ejecucion con el esquema completo."""
        conn.execute('''
            CREATE TABLE agentes_ejecucion (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ejecucion_id INTEGER NOT NULL,
                agente_id TEXT NOT NULL,
                nombre TEXT NOT NULL,
                descripcion TEXT DEFAULT '',
                tipo TEXT NOT NULL,
                estado TEXT NOT NULL,
                duracion REAL DEFAULT 0,
                dependencias TEXT DEFAULT '[]',
                resultado TEXT DEFAULT '',
                error TEXT DEFAULT '',
                orden INTEGER DEFAULT 0,
                progreso INTEGER DEFAULT 0,
                prompt_usado TEXT DEFAULT '',
                FOREIGN KEY (ejecucion_id) REFERENCES ejecuciones(id) ON DELETE CASCADE
            )
        ''')

    # ============================================================
    # ✅ VERIFICACIÓN DE ESQUEMA (CORREGIDA Y COMPLETA)
    # ============================================================
    def _verificar_esquema(self):
        """
        Verifica que el esquema de la base de datos sea consistente.
        Si detecta columnas faltantes, las añade automáticamente.

        ✅ CORREGIDO: Ahora usa SCHEMA_DEFINITION como fuente única de verdad.
        Detecta TODAS las columnas faltantes, incluyendo 'cancelados'.
        """
        try:
            with self._transaction() as conn:
                cursor = conn.cursor()
                tablas_existentes = self._obtener_tablas(conn)

                # ── Verificar tabla 'version' ──
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

                # ── Verificar tablas principales ──
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

                # ── ✅ Verificar TODAS las columnas requeridas ──
                for tabla, columnas_requeridas in SCHEMA_DEFINITION.items():
                    if tabla not in self._obtener_tablas(conn):
                        continue  # Ya fue creada arriba

                    columnas_existentes = self._obtener_columnas(conn, tabla)

                    for columna, definicion in columnas_requeridas.items():
                        if columna not in columnas_existentes:
                            logger.warning(
                                f"Columna '{columna}' faltante en '{tabla}', añadiendo..."
                            )
                            try:
                                cursor.execute(
                                    f"ALTER TABLE {tabla} ADD COLUMN {columna} "
                                    f"{_definicion_alter(definicion)}"
                                )
                                logger.info(f"✅ Columna añadida: {tabla}.{columna}")
                            except sqlite3.OperationalError as e:
                                logger.error(f"Error añadiendo columna {tabla}.{columna}: {e}")

                logger.debug("Verificación de esquema completada")

        except Exception as e:
            logger.error(f"Error verificando esquema: {e}")
            raise

    # ============================================================
    # ✅ DIAGNÓSTICO DE LA BASE DE DATOS (NUEVO)
    # ============================================================
    def diagnostico(self) -> dict[str, Any]:
        """
        Retorna un diagnóstico completo del estado de la base de datos.
        Útil para debugging y para verificar que todo esté correcto.
        """
        resultado = {
            "ruta": self.db_path,
            "existe": os.path.exists(self.db_path),
            "tamaño_bytes": 0,
            "version": DB_VERSION,
            "tablas": {},
            "columnas_faltantes": [],
            "integridad": "unknown",
        }

        try:
            if resultado["existe"]:
                resultado["tamaño_bytes"] = os.path.getsize(self.db_path)

            with self._transaction() as conn:
                # Verificar integridad
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check")
                integrity_result = cursor.fetchone()
                resultado["integridad"] = integrity_result[0] if integrity_result else "unknown"

                # Listar tablas y columnas
                tablas_existentes = self._obtener_tablas(conn)
                for tabla in tablas_existentes:
                    if tabla.startswith("sqlite_"):
                        continue
                    columnas = self._obtener_columnas(conn, tabla)
                    resultado["tablas"][tabla] = sorted(columnas)

                    # Verificar columnas faltantes
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
    # REPARACIÓN DE ESQUEMA
    # ============================================================
    def reparar_esquema(self) -> dict[str, Any]:
        """
        Repara el esquema de la base de datos si está corrupto.
        Útil para recuperar bases de datos antiguas o dañadas.
        """
        resultado = {
            'exito': False,
            'columnas_añadidas': [],
            'tablas_creadas': [],
            'indices_creados': [],
            'errores': []
        }

        try:
            with self._transaction() as conn:
                cursor = conn.cursor()
                tablas_existentes = self._obtener_tablas(conn)

                # Crear tablas faltantes
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

                # ✅ Añadir columnas faltantes (usando SCHEMA_DEFINITION)
                for tabla, columnas_requeridas in SCHEMA_DEFINITION.items():
                    if tabla not in self._obtener_tablas(conn):
                        continue

                    columnas_existentes = self._obtener_columnas(conn, tabla)
                    for columna, definicion in columnas_requeridas.items():
                        if columna not in columnas_existentes:
                            try:
                                cursor.execute(
                                    f"ALTER TABLE {tabla} ADD COLUMN {columna} "
                                    f"{_definicion_alter(definicion)}"
                                )
                                resultado['columnas_añadidas'].append(f"{tabla}.{columna}")
                                logger.info(f"✅ Columna reparada: {tabla}.{columna}")
                            except sqlite3.OperationalError as e:
                                resultado['errores'].append(f"{tabla}.{columna}: {e}")

                # Recrear índices reutilizando la transacción abierta
                self._crear_indices(conn)
                resultado['indices_creados'].append('todos')

                resultado['exito'] = len(resultado['errores']) == 0
                logger.info(f"Reparación de esquema completada: {resultado}")

        except Exception as e:
            logger.error(f"Error reparando esquema: {e}")
            resultado['errores'].append(str(e))

        return resultado

    # ============================================================
    # INICIALIZACIÓN Y MIGRACIONES
    # ============================================================
    def _init_db(self):
        """
        Inicializa la estructura base de la base de datos.

        IMPORTANTE:
        - Este método solo crea tablas que no existan.
        - NO establece DB_VERSION en bases de datos existentes.
        - Las migraciones de versión son responsabilidad de _migrar_db().
        """
        try:
            with self._transaction() as conn:
                cursor = conn.cursor()

                # ======================================================
                # TABLA DE VERSIONES
                # ======================================================
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS version (
                        version INTEGER PRIMARY KEY,
                        fecha_actualizacion TEXT NOT NULL
                    )
                ''')

                # ======================================================
                # TABLA DE EJECUCIONES
                # ======================================================
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

                # ======================================================
                # TABLA DE AGENTES EN EJECUCIÓN
                # ======================================================
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS agentes_ejecucion (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ejecucion_id INTEGER NOT NULL,
                        agente_id TEXT NOT NULL,
                        nombre TEXT NOT NULL,
                        descripcion TEXT DEFAULT '',
                        tipo TEXT NOT NULL,
                        estado TEXT NOT NULL,
                        duracion REAL DEFAULT 0,
                        dependencias TEXT DEFAULT '[]',
                        resultado TEXT DEFAULT '',
                        error TEXT DEFAULT '',
                        orden INTEGER DEFAULT 0,
                        progreso INTEGER DEFAULT 0,
                        prompt_usado TEXT DEFAULT '',
                        FOREIGN KEY (ejecucion_id)
                            REFERENCES ejecuciones(id)
                            ON DELETE CASCADE
                    )
                ''')

                # ======================================================
                # TABLA DE AUDITORÍA
                # ======================================================
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

                # ======================================================
                # NO INSERTAMOS DB_VERSION AQUÍ
                # ======================================================
                #
                # _migrar_db() será quien determine si:
                #
                #   - es una BD nueva
                #   - es una BD antigua
                #   - necesita migraciones
                #   - ya está actualizada
                #
                # Esto evita marcar accidentalmente una BD antigua
                # como si ya estuviera en la versión actual.
                #
                logger.debug(
                    "Estructura base de la base de datos verificada"
                )

        except sqlite3.Error as e:
            logger.error(
                f"Error inicializando base de datos: {e}"
            )
            raise

    def _migrar_db(self):
        """Aplica migraciones al esquema de la base de datos."""
        try:
            with self._transaction() as conn:
                cursor = conn.cursor()

                # Obtener versión actual
                cursor.execute("SELECT version FROM version ORDER BY version DESC LIMIT 1")
                result = cursor.fetchone()
                current_version = result[0] if result else 0

                if current_version >= DB_VERSION:
                    logger.debug(f"Base de datos ya está en versión {DB_VERSION}")
                    return

                logger.info(f"Migrando base de datos de versión {current_version} a {DB_VERSION}")

                # Si alguna migración falla, NO se actualiza la versión: la
                # transacción hace rollback y la BD sigue en su versión
                # anterior (antes se marcaba v8 con el esquema incompleto).
                fallos: list[str] = []

                # Migración 1 → 2: Añadir columnas básicas
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
                        fallos.append(f"1→2: {e}")

                # Migración 2 → 3: Auditoría
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
                        fallos.append(f"2→3: {e}")

                # ✅ Migración 3 → 4: Asegurar columnas críticas (cancelados, etc.)
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
                        fallos.append(f"3→4: {e}")

                # ✅ Migración 4 → 5: columna prompt_usado en agentes_ejecucion
                #    (Fase 1 del sistema de feedback: persistir el prompt real que usó
                #    cada agente LLM para poder reescribirlo a partir del feedback del
                #    usuario en fases posteriores).
                if current_version < 5:
                    try:
                        columnas_agentes = self._obtener_columnas(conn, 'agentes_ejecucion')
                        if 'prompt_usado' not in columnas_agentes:
                            cursor.execute(
                                "ALTER TABLE agentes_ejecucion "
                                "ADD COLUMN prompt_usado TEXT DEFAULT ''"
                            )
                            logger.info("✅ Migración 4→5: columna 'prompt_usado' añadida")
                    except sqlite3.OperationalError as e:
                        logger.warning(f"Error en migración 4→5: {e}")
                        fallos.append(f"4→5: {e}")

                # ✅ Migración 5 → 6: columna descripcion en agentes_ejecucion
                if current_version < 6:
                    try:
                        columnas_agentes = self._obtener_columnas(conn, 'agentes_ejecucion')
                        if 'descripcion' not in columnas_agentes:
                            cursor.execute(
                                "ALTER TABLE agentes_ejecucion "
                                "ADD COLUMN descripcion TEXT DEFAULT ''"
                            )
                            logger.info("✅ Migración 5→6: columna 'descripcion' añadida")
                    except sqlite3.OperationalError as e:
                        logger.warning(f"Error en migración 5→6: {e}")
                        fallos.append(f"5→6: {e}")

                # ✅ Migración 6 → 7: A/B testing de reescrituras de prompt.
                #    - Añade columnas `estado` y `n_usos` a prompts_reescritos.
                #    - Crea tabla prompt_reescrito_usos para atribuir ejecuciones
                #      a versiones de prompt y calcular scores medios.
                if current_version < 7:
                    try:
                        # Columnas en prompts_reescritos
                        tablas = self._obtener_tablas(conn)
                        if 'prompts_reescritos' in tablas:
                            cols = self._obtener_columnas(conn, 'prompts_reescritos')
                            if 'estado' not in cols:
                                cursor.execute(
                                    "ALTER TABLE prompts_reescritos "
                                    "ADD COLUMN estado TEXT DEFAULT 'candidato'"
                                )
                                logger.info("✅ Migración 6→7: columna 'estado' añadida")
                            if 'n_usos' not in cols:
                                cursor.execute(
                                    "ALTER TABLE prompts_reescritos "
                                    "ADD COLUMN n_usos INTEGER DEFAULT 0"
                                )
                                logger.info("✅ Migración 6→7: columna 'n_usos' añadida")

                            # Backfill: los que estaban 'activo=1' pasan a estado='activo'
                            cursor.execute(
                                "UPDATE prompts_reescritos SET estado='activo' "
                                "WHERE activo=1 AND (estado IS NULL OR estado='candidato')"
                            )

                        # Tabla de usos
                        if 'prompt_reescrito_usos' not in tablas:
                            cursor.execute('''
                                CREATE TABLE prompt_reescrito_usos (
                                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                                    prompt_reescrito_id INTEGER NOT NULL,
                                    ejecucion_id INTEGER NOT NULL,
                                    score REAL,
                                    fecha TEXT NOT NULL,
                                    FOREIGN KEY (prompt_reescrito_id)
                                        REFERENCES prompts_reescritos(id)
                                        ON DELETE CASCADE
                                )
                            ''')
                            cursor.execute(
                                "CREATE INDEX IF NOT EXISTS idx_prompt_reescrito_usos_pr "
                                "ON prompt_reescrito_usos(prompt_reescrito_id)"
                            )
                            cursor.execute(
                                "CREATE INDEX IF NOT EXISTS idx_prompt_reescrito_usos_ej "
                                "ON prompt_reescrito_usos(ejecucion_id)"
                            )
                            logger.info("✅ Migración 6→7: tabla 'prompt_reescrito_usos' creada")
                    except sqlite3.OperationalError as e:
                        logger.warning(f"Error en migración 6→7: {e}")
                        fallos.append(f"6→7: {e}")

                # ✅ Migración 7 → 8: embeddings para matching semántico.
                #    Añade columnas embedding (BLOB) y embedding_model (TEXT) a
                #    prompts_reescritos, para poder buscar reescrituras por similitud
                #    coseno en lugar de por firma exacta.
                if current_version < 8:
                    try:
                        tablas = self._obtener_tablas(conn)
                        if 'prompts_reescritos' in tablas:
                            cols = self._obtener_columnas(conn, 'prompts_reescritos')
                            if 'embedding' not in cols:
                                cursor.execute(
                                    "ALTER TABLE prompts_reescritos ADD COLUMN embedding BLOB"
                                )
                                logger.info("✅ Migración 7→8: columna 'embedding' añadida")
                            if 'embedding_model' not in cols:
                                cursor.execute(
                                    "ALTER TABLE prompts_reescritos "
                                    "ADD COLUMN embedding_model TEXT DEFAULT ''"
                                )
                                logger.info("✅ Migración 7→8: columna 'embedding_model' añadida")
                            cursor.execute(
                                "CREATE INDEX IF NOT EXISTS idx_prompts_reescritos_embedding_model "
                                "ON prompts_reescritos(embedding_model)"
                            )
                    except sqlite3.OperationalError as e:
                        logger.warning(f"Error en migración 7→8: {e}")
                        fallos.append(f"7→8: {e}")

                if fallos:
                    raise sqlite3.OperationalError(
                        "Migraciones fallidas, no se actualiza la versión: "
                        + "; ".join(fallos)
                    )

                # Actualizar versión
                cursor.execute("DELETE FROM version")
                cursor.execute(
                    'INSERT INTO version (version, fecha_actualizacion) VALUES (?, ?)',
                    (DB_VERSION, datetime.now().isoformat())
                )
                logger.info(f"Base de datos migrada a versión {DB_VERSION}")

        except Exception as e:
            logger.error(f"Error en migración: {e}")
            raise

    def _crear_indices(self, conn: sqlite3.Connection | None = None):
        """
        Crea índices para optimizar consultas.
        Verifica que las columnas existan antes de crear índices.

        Si se pasa ``conn`` se reutiliza (y su transacción activa), evitando
        el BEGIN anidado que fallaba al llamar dentro de ``reparar_esquema``.
        """
        try:
            if conn is not None:
                self._crear_indices_en(conn)
            else:
                with self._transaction() as conexion:
                    self._crear_indices_en(conexion)
        except Exception as e:
            logger.error(f"Error creando índices: {e}")

    def _crear_indices_en(self, conn: sqlite3.Connection):
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

    def _aplicar_esquema_learning(self):
        """
        Crea las tablas del módulo de aprendizaje si existen los archivos.

        No falla si el módulo learning/ no está presente: la app sigue
        funcionando sin aprendizaje. Es deliberado.
        """
        try:
            from learning.schema import aplicar_esquema_learning
        except ImportError:
            logger.debug("Módulo learning/ no disponible, omitiendo")
            return

        try:
            with self._transaction() as conn:
                aplicar_esquema_learning(conn)
            logger.info("✅ Tablas de aprendizaje verificadas")
        except Exception as e:
            # No propagamos: la BD principal funciona aunque falle esto
            logger.warning(
                f"⚠️ No se pudieron crear tablas de aprendizaje: {e}",
                exc_info=True
            )

    # ============================================================
    # ✅ NORMALIZACIÓN DE ESTADOS
    # ============================================================
    @staticmethod
    def _normalizar_estado(estado: str) -> str:
        """
        Normaliza el estado de un agente para comparación consistente.
        Convierte variantes como 'completado', 'COMPLETADO', 'Completado'
        a una forma canónica.
        """
        if not estado:
            return "pendiente"

        estado_lower = estado.lower().strip()

        # Mapeo de variantes a forma canónica
        mapeo = {
            "completado": "Completado",
            "complete": "Completado",
            "completed": "Completado",
            "error": "Error",
            "failed": "Error",
            "fail": "Error",
            "cancelado": "Cancelado",
            "canceled": "Cancelado",
            "cancelled": "Cancelado",
            "pendiente": "Pendiente",
            "pending": "Pendiente",
            "ejecutando": "Ejecutando",
            "running": "Ejecutando",
        }

        return mapeo.get(estado_lower, estado)

    # ============================================================
    # LIMPIEZA DE REGISTROS ANTIGUOS
    # ============================================================
    def cleanup_old_records(
        self,
        days: int = CLEANUP_DAYS,
        force: bool = False
    ) -> int:
        """
        Elimina registros antiguos de ejecuciones,
        excepto los marcados como 'activa'.

        Si se eliminan más de 1000 registros, ejecuta VACUUM
        después de finalizar la transacción.

        Args:
            days: Antigüedad máxima en días.
            force: Si True, realiza la limpieza aunque haya menos
                de 10 registros antiguos.

        Returns:
            Número de registros eliminados.
        """
        try:
            if days < 0:
                logger.warning(
                    f"Valor inválido para days: {days}. Debe ser >= 0."
                )
                return 0

            cutoff = (
                datetime.now() - timedelta(days=days)
            ).isoformat()

            count = 0

            # ----------------------------------------------------------
            # 1. DELETE dentro de la transacción
            # ----------------------------------------------------------
            with self._transaction() as conn:
                cursor = conn.cursor()

                columnas = self._obtener_columnas(
                    conn,
                    'ejecuciones'
                )

                if 'estado' not in columnas:
                    logger.warning(
                        "Columna 'estado' no existe, omitiendo limpieza"
                    )
                    return 0

                cursor.execute(
                    '''
                    SELECT COUNT(*)
                    FROM ejecuciones
                    WHERE fecha < ?
                    AND estado != 'activa'
                    ''',
                    (cutoff,)
                )

                row = cursor.fetchone()
                count = row[0] if row else 0

                if count < 10 and not force:
                    logger.debug(
                        f"Limpieza automática: solo {count} "
                        "registros antiguos, omitiendo"
                    )
                    return 0

                if count > 0:
                    cursor.execute(
                        '''
                        DELETE FROM ejecuciones
                        WHERE fecha < ?
                        AND estado != 'activa'
                        ''',
                        (cutoff,)
                    )

                    # rowcount representa lo realmente eliminado.
                    if cursor.rowcount >= 0:
                        count = cursor.rowcount

                    logger.info(
                        f"Limpieza: {count} registros antiguos eliminados"
                    )

            # ----------------------------------------------------------
            # IMPORTANTE:
            # Aquí ya terminó self._transaction().
            # VACUUM se ejecuta FUERA de la transacción.
            # ----------------------------------------------------------
            if count > 1000:
                if self._ejecutar_vacuum():
                    logger.info(
                        "VACUUM ejecutado después de la limpieza"
                    )
                else:
                    logger.warning(
                        "Los registros fueron eliminados correctamente, "
                        "pero VACUUM no pudo ejecutarse"
                    )

            return count

        except sqlite3.Error as e:
            logger.warning(
                f"Error SQLite durante la limpieza: {e}"
            )
            return 0

        except Exception as e:
            logger.warning(
                f"Error en limpieza: {e}"
            )
            return 0  

    def _check_and_cleanup(self):
        """Verifica el tamaño de la BD y ejecuta limpieza si es necesario."""
        try:
            if not os.path.exists(self.db_path):
                return
            size = os.path.getsize(self.db_path)
            if size > 50 * 1024 * 1024:
                logger.info(f"Base de datos grande ({size / 1024 / 1024:.1f} MB), ejecutando limpieza")
                self.cleanup_old_records()
        except Exception as e:
            logger.warning(f"Error verificando tamaño: {e}")

    # ============================================================
    # ✅ OPERACIONES CRUD - EJECUCIONES (MEJORADAS)
    # ============================================================
    def guardar_ejecucion(
        self,
        agentes: list[dict],
        duracion_total: float,
        estado: str = "completada",
        tags: list[str] = None,
        notas: str = "",
        ejecutor: str = ""
    ) -> int:
        """
        Guarda una ejecución completa con todos sus agentes.

        ✅ MEJORADO:
        - Validación de datos de entrada
        - Conteo de estados normalizado (case-insensitive)
        - Logging detallado
        """
        # ── Validación de entrada ──
        if not agentes:
            raise ValueError("No hay agentes para guardar")

        for i, agente in enumerate(agentes):
            if not isinstance(agente, dict):
                raise ValueError(f"Agente {i} no es un diccionario")
            if 'nombre' not in agente:
                raise ValueError(f"Agente {i} sin campo 'nombre'")
            if 'estado' not in agente:
                raise ValueError(f"Agente {i} sin campo 'estado'")

        tags_str = ",".join(tags) if tags else ""

        try:
            with self._transaction() as conn:
                cursor = conn.cursor()

                # ✅ Conteo normalizado de estados (case-insensitive)
                completados = 0
                errores = 0
                cancelados = 0

                for a in agentes:
                    estado_norm = self._normalizar_estado(a.get('estado', ''))
                    if estado_norm == "Completado":
                        completados += 1
                    elif estado_norm == "Error":
                        errores += 1
                    elif estado_norm == "Cancelado":
                        cancelados += 1

                cursor.execute('''
                    INSERT INTO ejecuciones (
                        fecha, duracion_total, agentes_total,
                        completados, errores, cancelados,
                        estado, ejecutor, tags, notas
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    datetime.now().isoformat(),
                    float(duracion_total),
                    len(agentes),
                    completados,
                    errores,
                    cancelados,
                    estado,
                    ejecutor,
                    tags_str,
                    notas
                ))

                ejecucion_id = cursor.lastrowid

                # Insertar agentes
                self._insertar_agentes_lote(conn, ejecucion_id, agentes)

                # Auditoría
                self._registrar_auditoria(
                    conn,
                    accion="guardar_ejecucion",
                    detalle=(
                        f"Ejecución {ejecucion_id}: {len(agentes)} agentes, "
                        f"{duracion_total:.2f}s, "
                        f"✅{completados} ❌{errores} ⛔{cancelados}"
                    )
                )

                logger.info(
                    f"Ejecución guardada: ID={ejecucion_id}, "
                    f"{len(agentes)} agentes, "
                    f"✅{completados} ❌{errores} ⛔{cancelados}"
                )

                return ejecucion_id

        except sqlite3.Error as e:
            logger.error(f"Error guardando ejecución: {e}")
            raise RuntimeError(f"Error al guardar la ejecución: {e}") from e

    def _insertar_agentes_lote(
        self,
        conn: sqlite3.Connection,
        ejecucion_id: int,
        agentes: list[dict]
        ):
            """Inserta múltiples agentes de una ejecución."""
            cursor = conn.cursor()
            datos = []

            for orden, agente in enumerate(agentes):
                # Procesar dependencias
                dependencias = agente.get('dependencias', [])
                if isinstance(dependencias, str):
                    try:
                        dependencias = json.loads(dependencias)
                    except (json.JSONDecodeError, ValueError):
                        dependencias = []
                if not isinstance(dependencias, list):
                    dependencias = []

                # Procesar resultado
                resultado = agente.get('resultado', {})
                if isinstance(resultado, dict):
                    resultado_str = self._comprimir_json(resultado)
                elif resultado is None:
                    resultado_str = ''
                else:
                    resultado_str = self._comprimir_json({'resultado': str(resultado)})

                # ✅ Normalizar estado antes de guardar
                estado = self._normalizar_estado(agente.get('estado', 'Pendiente'))

                # ✅ FASE 1: prompt del agente LLM (vacío si no aplica)
                prompt_usado = str(agente.get('prompt_usado', '') or '')[:4000]

                datos.append((
                    ejecucion_id,
                    str(agente.get('id', '')),
                    str(agente.get('nombre', '')),
                    str(agente.get('descripcion', '') or '')[:1000],
                    str(agente.get('tipo', 'Desconocido')),
                    estado,
                    float(agente.get('duracion', 0.0) or 0.0),
                    json.dumps(dependencias, ensure_ascii=False),
                    resultado_str,
                    str(agente.get('error', '') or ''),
                    orden,
                    int(agente.get('progreso', 0) or 0),
                    prompt_usado,                        # ✅ FASE 1
                ))

            if datos:
                cursor.executemany('''
                    INSERT INTO agentes_ejecucion (
                        ejecucion_id, agente_id, nombre, descripcion, tipo, estado,
                        duracion, dependencias, resultado, error, orden, progreso,
                        prompt_usado
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', datos)

    def _comprimir_json(self, data: Any) -> str:
        """Comprime JSON si supera el umbral."""
        try:
            json_str = json.dumps(data, ensure_ascii=False, default=str)
            if len(json_str) <= COMPRESSION_THRESHOLD:
                return json_str
            compressed = gzip.compress(json_str.encode('utf-8'))
            return "GZIP:" + base64.b64encode(compressed).decode('ascii')
        except Exception as e:
            logger.warning(f"Error comprimiendo JSON: {e}")
            return json.dumps({'error': 'Error al serializar', 'detalle': str(e)})

    def _descomprimir_json(self, data: str) -> Any:
        """Descomprime JSON si está comprimido."""
        if not data or data == 'null' or data == '{}':
            return {}
        try:
            if data.startswith("GZIP:"):
                compressed = base64.b64decode(data[5:].encode('ascii'))
                decompressed = gzip.decompress(compressed).decode('utf-8')
                return json.loads(decompressed)
            else:
                return json.loads(data)
        except Exception as e:
            logger.warning(f"Error descomprimiendo JSON: {e}")
            return {'error': 'Error al deserializar', 'raw': data[:100]}

    # ============================================================
    # CONSULTAS
    # ============================================================
    def obtener_historial(
        self,
        limit: int = 20,
        offset: int = 0,
        estado: str = None
    ) -> list[dict]:
        """Obtiene el historial de ejecuciones."""
        try:
            with self._transaction() as conn:
                cursor = conn.cursor()
                query = 'SELECT * FROM ejecuciones WHERE 1=1'
                params = []

                if estado:
                    columnas = self._obtener_columnas(conn, 'ejecuciones')
                    if 'estado' in columnas:
                        query += " AND estado = ?"
                        params.append(estado)

                query += " ORDER BY fecha DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])

                cursor.execute(query, params)
                resultados = cursor.fetchall()
                return [dict(row) for row in resultados]

        except sqlite3.Error as e:
            logger.error(f"Error obteniendo historial: {e}")
            return []

    def obtener_ejecucion(self, ejecucion_id: int) -> dict | None:
        """Obtiene una ejecución específica por ID."""
        try:
            with self._transaction() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM ejecuciones WHERE id = ?', (ejecucion_id,))
                result = cursor.fetchone()
                if result:
                    return dict(result)
                return None
        except sqlite3.Error as e:
            logger.error(f"Error obteniendo ejecución {ejecucion_id}: {e}")
            return None

    def obtener_detalle_ejecucion(self, ejecucion_id: int) -> list[dict]:
        """Obtiene el detalle de agentes de una ejecución."""
        try:
            with self._transaction() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT * FROM agentes_ejecucion
                    WHERE ejecucion_id = ?
                    ORDER BY orden, id
                ''', (ejecucion_id,))

                resultados = []
                for row in cursor.fetchall():
                    r = dict(row)
                    try:
                        r['dependencias'] = json.loads(r['dependencias']) if r['dependencias'] else []
                    except (json.JSONDecodeError, ValueError):
                        r['dependencias'] = []
                    r['resultado'] = self._descomprimir_json(r.get('resultado', ''))
                    resultados.append(r)

                return resultados

        except sqlite3.Error as e:
            logger.error(f"Error obteniendo detalle de ejecución {ejecucion_id}: {e}")
            return []

    def obtener_estadisticas(self, dias: int = 30) -> dict:
        """Obtiene estadísticas agregadas de las últimas N días."""
        try:
            cutoff = (datetime.now() - timedelta(days=dias)).isoformat()
            with self._transaction() as conn:
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

                # Agrupar por estado
                por_estado = {}
                columnas = self._obtener_columnas(conn, 'ejecuciones')
                if 'estado' in columnas:
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

    def buscar_ejecuciones(
        self,
        texto: str = "",
        estado: str = None,
        desde: str = None,
        hasta: str = None,
        tags: list[str] = None,
        limit: int = 50
    ) -> list[dict]:
        """Busca ejecuciones con filtros."""
        try:
            with self._transaction() as conn:
                cursor = conn.cursor()
                query = "SELECT * FROM ejecuciones WHERE 1=1"
                params = []

                if texto:
                    query += " AND (notas LIKE ? OR tags LIKE ?)"
                    params.extend([f"%{texto}%", f"%{texto}%"])

                if estado:
                    columnas = self._obtener_columnas(conn, 'ejecuciones')
                    if 'estado' in columnas:
                        query += " AND estado = ?"
                        params.append(estado)

                if desde:
                    query += " AND fecha >= ?"
                    params.append(desde)

                if hasta:
                    query += " AND fecha <= ?"
                    params.append(hasta)

                if tags:
                    query += " AND tags GLOB ?"
                    params.append(f"*{','.join(tags)}*")

                query += " ORDER BY fecha DESC LIMIT ?"
                params.append(limit)

                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]

        except sqlite3.Error as e:
            logger.error(f"Error buscando ejecuciones: {e}")
            return []

    # ============================================================
    # OPERACIONES DE MANTENIMIENTO
    # ============================================================
    def eliminar_ejecucion(self, ejecucion_id: int) -> bool:
        """Elimina una ejecución y sus agentes asociados."""
        try:
            with self._transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM ejecuciones WHERE id = ?", (ejecucion_id,))
                if not cursor.fetchone():
                    return False

                cursor.execute("DELETE FROM ejecuciones WHERE id = ?", (ejecucion_id,))

                self._registrar_auditoria(
                    conn,
                    accion="eliminar_ejecucion",
                    detalle=f"Ejecución {ejecucion_id} eliminada"
                )
                logger.info(f"Ejecución {ejecucion_id} eliminada")
                return True

        except sqlite3.Error as e:
            logger.error(f"Error eliminando ejecución {ejecucion_id}: {e}")
            return False

    def limpiar_ejecuciones_antiguas(
        self,
        dias: int = 30
    ) -> int:
        """
        Elimina ejecuciones más antiguas que N días.

        La eliminación se realiza dentro de una transacción.
        VACUUM se ejecuta posteriormente mediante una conexión
        independiente para evitar el error:

            sqlite3.OperationalError:
            cannot VACUUM from within a transaction

        Args:
            dias: Número de días de antigüedad.

        Returns:
            Número de ejecuciones eliminadas.
        """
        try:
            if dias < 0:
                logger.warning(
                    f"Valor inválido para dias: {dias}. Debe ser >= 0."
                )
                return 0

            cutoff = (
                datetime.now() - timedelta(days=dias)
            ).isoformat()

            count = 0

            # ----------------------------------------------------------
            # 1. Eliminar dentro de la transacción
            # ----------------------------------------------------------
            with self._transaction() as conn:
                cursor = conn.cursor()

                cursor.execute(
                    '''
                    SELECT COUNT(*)
                    FROM ejecuciones
                    WHERE fecha < ?
                    ''',
                    (cutoff,)
                )

                row = cursor.fetchone()
                count = row[0] if row else 0

                if count > 0:
                    cursor.execute(
                        '''
                        DELETE FROM ejecuciones
                        WHERE fecha < ?
                        ''',
                        (cutoff,)
                    )

                    if cursor.rowcount >= 0:
                        count = cursor.rowcount

                    logger.info(
                        f"Limpiadas {count} ejecuciones antiguas "
                        f"(> {dias} días)"
                    )

            # ----------------------------------------------------------
            # 2. La transacción YA terminó.
            #    VACUUM se ejecuta fuera.
            # ----------------------------------------------------------
            if count > 0:
                if not self._ejecutar_vacuum():
                    logger.warning(
                        "Las ejecuciones antiguas fueron eliminadas, "
                        "pero VACUUM no pudo ejecutarse"
                    )

            return count

        except sqlite3.Error as e:
            logger.error(
                f"Error SQLite limpiando ejecuciones antiguas: {e}"
            )
            return 0

        except Exception as e:
            logger.error(
                f"Error limpiando ejecuciones antiguas: {e}"
            )
            return 0 

    # storage/database.py - MÉTODO compactar_db CORREGIDO

    def compactar_db(self) -> bool:
        """
        Compacta físicamente la base de datos SQLite mediante VACUUM.
        
        ✅ CORREGIDO: VACUUM se ejecuta FUERA de cualquier transacción.
        Se usa una conexión independiente para evitar:
            sqlite3.OperationalError: cannot VACUUM from within a transaction
        
        Returns:
            bool: True si la compactación fue exitosa.
        """
        try:
            # ✅ CORRECCIÓN: Ejecutar VACUUM fuera de self._transaction()
            return self._ejecutar_vacuum()

        except Exception as e:
            logger.error(
                f"Error compactando base de datos: {e}"
            )
            return False

    def _ejecutar_vacuum(self) -> bool:
        """
        Ejecuta VACUUM fuera de cualquier transacción activa.

        Se utiliza una conexión SQLite independiente para garantizar
        que VACUUM no se ejecute dentro de self._transaction().

        Returns:
            bool: True si VACUUM terminó correctamente, False en caso contrario.
        """
        vacuum_conn = None

        try:
            # VACUUM necesita acceso exclusivo a la base de datos.
            with self._lock:
                vacuum_conn = sqlite3.connect(
                    self.db_path,
                    timeout=CONNECTION_TIMEOUT,
                    isolation_level=None,      # autocommit
                    check_same_thread=False
                )

                # Esperar si temporalmente la BD está ocupada
                vacuum_conn.execute(
                    f"PRAGMA busy_timeout = {int(CONNECTION_TIMEOUT * 1000)}"
                )

                # Asegurarnos explícitamente de no estar en una transacción
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

    def verificar_integridad(self) -> tuple[bool, str]:
        """Verifica la integridad de la base de datos."""
        try:
            with self._transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA integrity_check")
                result = cursor.fetchone()
                if result and result[0] == "ok":
                    return True, "Base de datos íntegra"
                else:
                    return False, result[0] if result else "Error desconocido"
        except sqlite3.Error as e:
            return False, f"Error de SQLite: {e}"

    # ============================================================
    # EXPORTACIÓN E IMPORTACIÓN
    # ============================================================
    def exportar_json(self, ruta: str, ejecucion_id: int | None = None) -> bool:
        """Exporta datos a JSON."""
        try:
            if ejecucion_id:
                ejecucion = self.obtener_ejecucion(ejecucion_id)
                if not ejecucion:
                    return False
                agentes = self.obtener_detalle_ejecucion(ejecucion_id)
                data = {'ejecucion': ejecucion, 'agentes': agentes}
            else:
                historial = self.obtener_historial(limit=1000)
                data = {
                    'exportado': datetime.now().isoformat(),
                    'total': len(historial),
                    'ejecuciones': historial
                }

            with open(ruta, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)

            logger.info(f"Datos exportados a {ruta}")
            return True

        except Exception as e:
            logger.error(f"Error exportando datos: {e}")
            return False

    def importar_json(self, ruta: str) -> int:
        """Importa datos desde JSON."""
        try:
            with open(ruta, encoding='utf-8') as f:
                data = json.load(f)

            importados = 0

            if 'ejecuciones' in data:
                for ejec in data['ejecuciones']:
                    if 'agentes' in ejec:
                        agentes = ejec.pop('agentes', [])
                        self.guardar_ejecucion(agentes, ejec.get('duracion_total', 0))
                        importados += 1
            elif 'ejecucion' in data and 'agentes' in data:
                self.guardar_ejecucion(
                    data['agentes'],
                    data['ejecucion'].get('duracion_total', 0)
                )
                importados = 1

            logger.info(f"Importados {importados} registros desde {ruta}")
            return importados

        except Exception as e:
            logger.error(f"Error importando datos: {e}")
            return 0

    # ============================================================
    # AUDITORÍA
    # ============================================================
    def _registrar_auditoria(
        self,
        conn: sqlite3.Connection,
        accion: str,
        detalle: str = ""
    ):
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

    def obtener_auditoria(self, limit: int = 100) -> list[dict]:
        """Obtiene el log de auditoría."""
        try:
            with self._transaction() as conn:
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

    # ============================================================
    # ESTADÍSTICAS DE BASE DE DATOS
    # ============================================================
    def obtener_info_db(self) -> dict:
        """Obtiene información general de la base de datos."""
        try:
            with self._transaction() as conn:
                cursor = conn.cursor()
                size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

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
                    'ruta': self.db_path,
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

    # ============================================================
    # BACKUP Y RECUPERACIÓN
    # ============================================================
    def backup(self, ruta: str | None = None) -> str | None:
        """Crea un backup de la base de datos."""
        if not ruta:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            ruta = f"{self.db_path}.backup_{timestamp}"

        try:
            integro, mensaje = self.verificar_integridad()
            if not integro:
                logger.error(f"Backup abortado: la BD no está íntegra ({mensaje})")
                return None
            # API de backup de SQLite: copia consistente que incluye el WAL
            # (shutil.copy2 ignoraba -wal/-shm y podía dejar el backup a medias).
            origen = self._get_connection()
            destino = sqlite3.connect(ruta)
            try:
                origen.backup(destino)
            finally:
                destino.close()
            logger.info(f"Backup creado: {ruta}")
            return ruta
        except Exception as e:
            logger.error(f"Error creando backup: {e}")
            return None

    def restore_backup(self, ruta: str) -> bool:
        """Restaura un backup de la base de datos."""
        if not os.path.exists(ruta):
            logger.error(f"Backup no encontrado: {ruta}")
            return False

        try:
            self._close_connection()

            # Verificar integridad del backup (leyendo el resultado)
            conn = None
            try:
                conn = sqlite3.connect(ruta, timeout=1)
                fila = conn.execute("PRAGMA integrity_check").fetchone()
                if not fila or fila[0] != "ok":
                    logger.error(f"Backup corrupto: {fila[0] if fila else 'sin resultado'}")
                    return False
            except Exception as e:
                logger.error(f"Backup corrupto: {e}")
                return False
            finally:
                if conn is not None:
                    conn.close()

            # Backup de la base actual
            if os.path.exists(self.db_path):
                backup_path = f"{self.db_path}.before_restore"
                import shutil
                shutil.copy2(self.db_path, backup_path)
                logger.info(f"Backup de base actual creado: {backup_path}")

            # Restaurar
            import shutil
            shutil.copy2(ruta, self.db_path)
            self._initialized = False
            self._closed = False    # ← AÑADIR: permitir reabrir tras restore
            self._init_db()
            self._migrar_db()
            self._verificar_esquema()
            logger.info(f"Backup restaurado desde {ruta}")
            return True

        except Exception as e:
            logger.error(f"Error restaurando backup: {e}")
            return False

    def maintenance(self, days: int = CLEANUP_DAYS) -> dict:
        """Ejecuta mantenimiento completo de la base de datos."""
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

    # ============================================================
    # LIMPIEZA FINAL
    # ============================================================
    def close(self):
        """Cierra las conexiones a la base de datos (idempotente)."""
        if self._closed:
            return
        self._closed = True

        try:
            self._close_connection()
            logger.info("Conexiones a base de datos cerradas")
        except Exception as e:
            logger.warning(f"Error cerrando conexiones: {e}")

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass
