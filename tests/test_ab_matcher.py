# tests/test_ab_matcher.py
"""Matcher A/B conservador (H2).

Regla fundamental: un prompt de la tarea A NUNCA debe recibir la reescritura
de la tarea B. No se carga el modelo real de embeddings: se sustituye
``calcular`` por vectores controlados, de modo que el test es determinista y
no necesita red ni torch.
"""
import math
import sqlite3

import numpy as np

from learning.embedding_matcher import (
    EmbeddingMatcher,
    compatible_por_intencion,
    configuracion,
    solape_lexico,
)

# ============================================================
# COMPATIBILIDAD DE INTENCIÓN (pura)
# ============================================================

def test_solape_lexico_tareas_distintas_es_bajo():
    a = "escribe un cuento de terror sobre un dragon"
    b = "receta de albondigas frias con tomate"
    assert solape_lexico(a, b) < 0.2


def test_solape_lexico_misma_tarea_es_alto():
    a = "escribe un cuento corto original sobre un dragon"
    b = "escribe un cuento corto en espanol sobre un dragon"
    assert solape_lexico(a, b) >= 0.5


def test_intencion_incompatible_entre_tareas():
    consulta = "receta de albondigas frias con tomate"
    candidato = {
        "prompt_original": "escribe un cuento de terror sobre un dragon",
        "firma": "cuento-terror-dragon",
    }
    ok, motivo = compatible_por_intencion(consulta, candidato, similitud=0.99)
    assert ok is False
    assert "incompatible" in motivo


def test_intencion_compatible_por_solape():
    consulta = "escribe un cuento corto sobre un dragon y un caballero"
    candidato = {
        "prompt_original": "escribe un cuento corto original sobre un dragon",
        "firma": "otra-firma",
    }
    ok, motivo = compatible_por_intencion(consulta, candidato, similitud=0.90)
    assert ok is True
    assert "solape" in motivo


def test_configuracion_umbral_configurable(monkeypatch):
    monkeypatch.setenv("AGENTES_AB_UMBRAL", "0.95")
    monkeypatch.setenv("AGENTES_AB_MIN_SOLAPE", "0.7")
    cfg = configuracion()
    assert cfg["umbral"] == 0.95
    assert cfg["min_solape"] == 0.7


# ============================================================
# BUSCAR_MATCH CON VECTORES CONTROLADOS
# ============================================================

def _vector(similitud: float) -> np.ndarray:
    """Vector unitario con coseno exacto ``similitud`` respecto a [1, 0]."""
    return np.array([similitud, math.sqrt(max(0.0, 1 - similitud ** 2))], dtype=np.float32)


class _MatcherControlado(EmbeddingMatcher):
    """Matcher cuya consulta devuelve un vector fijo."""

    def __init__(self, vector_consulta: np.ndarray):
        super().__init__(modelo="fake-model")
        self._vector_consulta = vector_consulta

    def calcular(self, texto: str) -> bytes | None:  # noqa: D102
        return self._vector_consulta.astype(np.float32).tobytes()


def _db_con_candidatos(tmp_path, candidatos: list[dict]) -> str:
    db = tmp_path / "hist.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE prompts_reescritos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                firma TEXT NOT NULL,
                prompt_original TEXT NOT NULL,
                prompt_nuevo TEXT NOT NULL,
                estado TEXT DEFAULT 'candidato',
                n_usos INTEGER DEFAULT 0,
                embedding BLOB,
                embedding_model TEXT DEFAULT ''
            )
        """)
        for c in candidatos:
            conn.execute(
                """INSERT INTO prompts_reescritos
                   (firma, prompt_original, prompt_nuevo, estado, n_usos,
                    embedding, embedding_model)
                   VALUES (?, ?, ?, ?, 0, ?, 'fake-model')""",
                (
                    c["firma"],
                    c["prompt_original"],
                    c["prompt_nuevo"],
                    c.get("estado", "activo"),
                    c["vector"].astype(np.float32).tobytes(),
                ),
            )
        conn.commit()
    return str(db)


def test_match_de_la_misma_tarea(tmp_path):
    consulta = "escribe un cuento corto original sobre un dragon"
    cand = {
        "firma": "misma",
        "prompt_original": "escribe un cuento corto original sobre un dragon",
        "prompt_nuevo": "PROMPT MEJORADO",
        "vector": _vector(0.99),
    }
    db = _db_con_candidatos(tmp_path, [cand])
    matcher = _MatcherControlado(_vector(1.0))

    # La firma real no coincide con 'misma', pero el solape léxico es total.
    match = matcher.buscar_match(db, consulta)

    assert match is not None
    assert match["prompt"] == "PROMPT MEJORADO"
    assert match["id"] == 1
    assert "solape" in match["motivo"] or "firma" in match["motivo"]


def test_no_aplica_reescritura_de_otra_tarea(tmp_path):
    """El caso albóndigas → cuento de terror: similitud alta pero otra tarea."""
    consulta = "receta de albondigas frias con tomate"
    cand = {
        "firma": "cuento-terror-dragon",
        "prompt_original": "escribe un cuento de terror sobre un dragon",
        "prompt_nuevo": "PROMPT DE CUENTO DE TERROR",
        "vector": _vector(0.99),
    }
    db = _db_con_candidatos(tmp_path, [cand])
    matcher = _MatcherControlado(_vector(1.0))

    assert matcher.buscar_match(db, consulta) is None


def test_umbral_conservador_descarta_similitud_media(tmp_path):
    consulta = "escribe un cuento corto original sobre un dragon"
    cand = {
        "firma": "x",
        "prompt_original": "escribe un cuento corto sobre un dinosaurio",
        "prompt_nuevo": "OTRO",
        "vector": _vector(0.80),  # por debajo del umbral 0.85
    }
    db = _db_con_candidatos(tmp_path, [cand])
    matcher = _MatcherControlado(_vector(1.0))

    assert matcher.buscar_match(db, consulta) is None


def test_duda_entre_dos_tareas_no_aplica_nada(tmp_path):
    consulta = "escribe un cuento corto original sobre un dragon"
    candidatos = [
        {
            "firma": "a",
            "prompt_original": "escribe un cuento corto original sobre un dragon",
            "prompt_nuevo": "A",
            "vector": _vector(0.95),
        },
        {
            "firma": "b",
            "prompt_original": "escribe un cuento corto original sobre un dinosaurio",
            "prompt_nuevo": "B",
            "vector": _vector(0.94),  # dentro del margen de duda
        },
    ]
    db = _db_con_candidatos(tmp_path, candidatos)
    matcher = _MatcherControlado(_vector(1.0))

    assert matcher.buscar_match(db, consulta) is None


def test_umbral_explicito_se_respeta(tmp_path):
    consulta = "escribe un cuento corto original sobre un dragon"
    cand = {
        "firma": "x",
        "prompt_original": consulta,
        "prompt_nuevo": "OK",
        "vector": _vector(0.80),
    }
    db = _db_con_candidatos(tmp_path, [cand])
    matcher = _MatcherControlado(_vector(1.0))

    assert matcher.buscar_match(db, consulta, umbral=0.75) is not None
    assert matcher.buscar_match(db, consulta, umbral=0.95) is None


def test_calcular_vacio_no_busca(tmp_path):
    matcher = _MatcherControlado(_vector(1.0))
    assert matcher.buscar_match(str(tmp_path / "no.db"), "   ") is None


# ============================================================
# REGISTRO DEL MOTIVO (H2)
# ============================================================

def test_registrar_uso_guarda_el_motivo(tmp_path):
    from learning.prompt_ab_evaluator import PromptABEvaluator

    db = tmp_path / "hist.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE prompts_reescritos (id INTEGER PRIMARY KEY, n_usos INTEGER DEFAULT 0)"
        )
        conn.execute(
            """CREATE TABLE prompt_reescrito_usos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                prompt_reescrito_id INTEGER,
                ejecucion_id INTEGER,
                score REAL,
                fecha TEXT,
                motivo TEXT DEFAULT ''
            )"""
        )
        conn.execute("INSERT INTO prompts_reescritos (id, n_usos) VALUES (7, 0)")
        conn.commit()

    evaluator = PromptABEvaluator(str(db))
    assert evaluator.registrar_uso(7, 99, motivo="solape léxico 0.75") is True

    with sqlite3.connect(db) as conn:
        fila = conn.execute(
            "SELECT motivo FROM prompt_reescrito_usos WHERE prompt_reescrito_id=7"
        ).fetchone()
        n_usos = conn.execute(
            "SELECT n_usos FROM prompts_reescritos WHERE id=7"
        ).fetchone()[0]

    assert fila[0] == "solape léxico 0.75"
    assert n_usos == 1
    # Idempotente
    assert evaluator.registrar_uso(7, 99, motivo="otra vez") is True
    with sqlite3.connect(db) as conn:
        total = conn.execute("SELECT COUNT(*) FROM prompt_reescrito_usos").fetchone()[0]
    assert total == 1


def test_builder_registra_el_motivo_de_la_variante(monkeypatch, tmp_path):
    """El builder anota POR QUÉ usó una reescritura (H2)."""
    import logging

    import learning.embedding_matcher as em
    from core.problem_solver.builder import PlanBuilder
    from core.problem_solver.code_corrector import PythonCodeCorrector
    from core.problem_solver.validator import PlanValidator

    class _MatcherFalso:
        def buscar_match(self, db_path, prompt, estados_validos=()):
            if "activo" not in estados_validos:
                return None
            return {
                "id": 7,
                "similitud": 0.9,
                "estado": "activo",
                "prompt": "PROMPT REESCRITO",
                "n_usos": 3,
                "prompt_original": prompt,
                "firma": "f",
                "motivo": "solape léxico 0.80",
            }

    monkeypatch.setattr(em, "obtener_matcher", lambda *a, **k: _MatcherFalso())

    log = logging.getLogger("test.ab.builder")
    log.addHandler(logging.NullHandler())
    validador = PlanValidator(log)
    builder = PlanBuilder(log, PythonCodeCorrector(), validador)
    plan = builder.construir_plan("x", {
        "titulo": "t",
        "pasos": [{
            "orden": 1, "nombre": "Analizar", "tipo": "LLM",
            "configuracion": {"prompt": "analiza el cuento del dragon"},
        }],
    })

    agente = builder.generar_agentes(plan)[0]
    assert agente.prompt_reescrito_id == 7
    assert "solape" in agente.prompt_reescrito_motivo
    assert agente.prompt_llm.endswith("PROMPT REESCRITO") or "PROMPT REESCRITO" in agente.prompt_llm
