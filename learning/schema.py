"""
learning/schema.py
Extensiones de esquema SQLite para el módulo de aprendizaje.

Se aplican sobre la MISMA base de datos que ya usa storage/database.py.
No modifica ni toca las tablas existentes (ejecuciones, agentes_ejecucion):
solo añade tablas nuevas, así que es seguro correrlo sobre una BD en uso,
tantas veces como haga falta (todo es CREATE TABLE IF NOT EXISTS).

⚠️ IMPORTANTE: aplicar_esquema_learning() NO hace commit() porque el
llamador (Database._transaction) ya gestiona la transacción. Hacer commit
aquí provocaría:
    sqlite3.OperationalError: cannot commit - no transaction is active
"""
import logging
import sqlite3

logger = logging.getLogger(__name__)


SQL_CREAR_TABLAS = [
    """
    CREATE TABLE IF NOT EXISTS evaluaciones_llm (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ejecucion_id INTEGER NOT NULL,
        agente_ejecucion_id INTEGER,
        alcance TEXT NOT NULL DEFAULT 'agente',   -- 'agente' | 'plan'
        score REAL NOT NULL,                      -- recompensa normalizada 0.0-1.0
        justificacion TEXT DEFAULT '',
        modelo_evaluador TEXT DEFAULT '',
        fecha TEXT NOT NULL,
        FOREIGN KEY (ejecucion_id) REFERENCES ejecuciones(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback_usuario (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ejecucion_id INTEGER NOT NULL,
        agente_ejecucion_id INTEGER,
        alcance TEXT NOT NULL DEFAULT 'agente',
        score REAL NOT NULL,               -- -1.0 a 1.0 (pulgar abajo/arriba o escala)
        comentario TEXT DEFAULT '',
        fecha TEXT NOT NULL,
        FOREIGN KEY (ejecucion_id) REFERENCES ejecuciones(id) ON DELETE CASCADE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS modelos_entrenados (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,              -- 'predictor_fallos' | 'scorer_planes'
        version INTEGER NOT NULL,
        n_muestras_entrenamiento INTEGER DEFAULT 0,
        metricas TEXT DEFAULT '{}',        -- JSON con métricas (accuracy, etc.)
        ruta_archivo TEXT NOT NULL,
        fecha TEXT NOT NULL,
        activo INTEGER DEFAULT 1
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_eval_llm_ejecucion ON evaluaciones_llm(ejecucion_id)",
    "CREATE INDEX IF NOT EXISTS idx_eval_llm_agente ON evaluaciones_llm(agente_ejecucion_id)",
    "CREATE INDEX IF NOT EXISTS idx_feedback_ejecucion ON feedback_usuario(ejecucion_id)",
    """
    CREATE TABLE IF NOT EXISTS reparaciones_plan (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        problema TEXT NOT NULL,
        tipo TEXT NOT NULL,                    -- estrategia de recuperación
        fecha TEXT NOT NULL,
        -- ✅ H7: traza completa de la reparación
        ejecucion_id INTEGER,
        intento INTEGER DEFAULT 0,
        agente TEXT DEFAULT '',
        error TEXT DEFAULT '',
        estrategia TEXT DEFAULT '',
        plan_firma TEXT DEFAULT '',
        resultado TEXT DEFAULT '',
        exito INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reparaciones_tipo ON reparaciones_plan(tipo)",
    "CREATE INDEX IF NOT EXISTS idx_reparaciones_ejecucion "
    "ON reparaciones_plan(ejecucion_id)",
    """
    CREATE TABLE IF NOT EXISTS prompts_reescritos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        firma TEXT NOT NULL,
        prompt_original TEXT NOT NULL,
        prompt_nuevo TEXT NOT NULL,
        feedback_id INTEGER NOT NULL,
        razon TEXT DEFAULT '',
        fecha TEXT NOT NULL,
        activo INTEGER DEFAULT 1,
        estado TEXT DEFAULT 'candidato',
        n_usos INTEGER DEFAULT 0,
        embedding BLOB,                       -- ✅ FASE 5b: vector del prompt_original
        embedding_model TEXT DEFAULT '',      -- ✅ FASE 5b: nombre del modelo usado
        FOREIGN KEY (feedback_id) REFERENCES feedback_usuario(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_prompts_reescritos_embedding_model "
    "ON prompts_reescritos(embedding_model)",
    "CREATE INDEX IF NOT EXISTS idx_prompts_reescritos_firma "
    "ON prompts_reescritos(firma, activo)",
    "CREATE INDEX IF NOT EXISTS idx_prompts_reescritos_estado "
    "ON prompts_reescritos(firma, estado)",
    # ✅ FASE 4a: tabla de usos para A/B testing
    """
    CREATE TABLE IF NOT EXISTS prompt_reescrito_usos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        prompt_reescrito_id INTEGER NOT NULL,
        ejecucion_id INTEGER NOT NULL,
        score REAL,                          -- relleno después por el aprendizaje
        fecha TEXT NOT NULL,
        motivo TEXT DEFAULT '',              -- ✅ H2: por qué se usó esa variante
        FOREIGN KEY (prompt_reescrito_id)
            REFERENCES prompts_reescritos(id)
            ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_prompt_reescrito_usos_pr "
    "ON prompt_reescrito_usos(prompt_reescrito_id)",
    "CREATE INDEX IF NOT EXISTS idx_prompt_reescrito_usos_ej "
    "ON prompt_reescrito_usos(ejecucion_id)",
    # ✅ V4.0-AB: brazo de CONTROL del A/B.
    #
    # El A/B comparaba el candidato contra `_score_global()` (la media de TODOS
    # los scores registrados) cuando no había un `activo`. Eso no es un control:
    # es una población mezclada (escalas de agente y de plan, filas huérfanas,
    # filas sintéticas). Como además nunca existía un `activo` (nadie promocionaba
    # nunca), TODA decisión se tomaba contra esa media y el sistema quedaba en un
    # punto muerto: el candidato siempre «empataba a la baja» o empeoraba.
    #
    # Aquí se registra la puntuación de las ejecuciones que usaron el prompt
    # ORIGINAL de una firma, es decir, el brazo de control real. Así el candidato
    # se compara contra «lo que ya hacía el prompt sin reescribir», que es la
    # comparación que la promoción debe exigir.
    """
    CREATE TABLE IF NOT EXISTS prompt_reescrito_baseline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        firma TEXT NOT NULL,
        ejecucion_id INTEGER NOT NULL,
        score REAL,                          -- relleno después por el aprendizaje
        fecha TEXT NOT NULL,
        motivo TEXT DEFAULT ''
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_prompt_baseline_firma "
    "ON prompt_reescrito_baseline(firma, score)",
    "CREATE INDEX IF NOT EXISTS idx_prompt_baseline_ejecucion "
    "ON prompt_reescrito_baseline(ejecucion_id)",
]


def aplicar_esquema_learning(conn: sqlite3.Connection) -> None:
    """
    Crea las tablas del módulo de aprendizaje si no existen. Idempotente.

    ⚠️ NO hace commit() — el llamador (Database._transaction) lo hace.
    """
    for sql in SQL_CREAR_TABLAS:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            # p. ej. un índice sobre una columna que falta en una BD legada:
            # no debe impedir aplicar el resto del esquema.
            logger.warning(f"No se pudo aplicar una pieza del esquema learning: {e}")
    logger.debug("Esquema de aprendizaje aplicado (sin commit)")
