# storage/database/schema.py
"""Definición centralizada del esquema de la base de datos.

Fuente única de verdad: todas las columnas requeridas por tabla.
Si una columna falta en la BD real, se añade automáticamente.
"""

# ============================================================
# CONSTANTES GLOBALES
# ============================================================
DEFAULT_DB_PATH = "agent_history.db"
DB_VERSION = 4
MAX_RETRIES = 3
RETRY_DELAY = 0.1
CONNECTION_TIMEOUT = 10.0
MAX_JSON_SIZE = 10 * 1024 * 1024      # 10 MB
COMPRESSION_THRESHOLD = 1024          # 1 KB
CLEANUP_DAYS = 30
BATCH_SIZE = 100

# ============================================================
# DEFINICIÓN CENTRALIZADA DEL ESQUEMA
# ============================================================
SCHEMA_DEFINITION = {
    "ejecuciones": {
        "fecha": "TEXT NOT NULL",
        "duracion_total": "REAL DEFAULT 0",
        "agentes_total": "INTEGER DEFAULT 0",
        "completados": "INTEGER DEFAULT 0",
        "errores": "INTEGER DEFAULT 0",
        "cancelados": "INTEGER DEFAULT 0",
        "estado": "TEXT DEFAULT 'completada'",
        "ejecutor": "TEXT DEFAULT ''",
        "tags": "TEXT DEFAULT ''",
        "notas": "TEXT DEFAULT ''",
    },
    "agentes_ejecucion": {
        "ejecucion_id": "INTEGER NOT NULL",
        "agente_id": "TEXT NOT NULL",
        "nombre": "TEXT NOT NULL",
        "tipo": "TEXT NOT NULL",
        "estado": "TEXT NOT NULL",
        "duracion": "REAL DEFAULT 0",
        "dependencias": "TEXT DEFAULT '[]'",
        "resultado": "TEXT DEFAULT ''",
        "error": "TEXT DEFAULT ''",
        "orden": "INTEGER DEFAULT 0",
        "progreso": "INTEGER DEFAULT 0",
    },
    "auditoria": {
        "timestamp": "TEXT NOT NULL",
        "usuario": "TEXT DEFAULT ''",
        "accion": "TEXT NOT NULL",
        "detalle": "TEXT DEFAULT ''",
        "ip": "TEXT DEFAULT ''",
    },
}

INDEX_DEFINITION = {
    "idx_ejecuciones_fecha": ("ejecuciones", "fecha"),
    "idx_ejecuciones_estado": ("ejecuciones", "estado"),
    "idx_agentes_ejecucion_ejecucion_id": ("agentes_ejecucion", "ejecucion_id"),
    "idx_agentes_ejecucion_nombre": ("agentes_ejecucion", "nombre"),
    "idx_agentes_ejecucion_estado": ("agentes_ejecucion", "estado"),
    "idx_agentes_ejecucion_tipo": ("agentes_ejecucion", "tipo"),
    "idx_auditoria_timestamp": ("auditoria", "timestamp"),
}