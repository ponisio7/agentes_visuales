# tests/test_retrieval_casos.py
"""Retrieval de casos similares exitosos (H8).

Solo se buscan ejecuciones con éxito REAL (``aceptada = 1``), con umbral
conservador y como máximo 3 casos. No se carga el modelo real de embeddings:
se sustituye ``obtener_matcher`` por vectores controlados.
"""
import math
import sqlite3

import numpy as np
import pytest

from learning.embedding_matcher import EmbeddingMatcher
from learning.engine import (
    MAX_CASOS_RETRIEVAL,
    LearningEngine,
    configuracion_retrieval,
)


def _vector(similitud: float) -> np.ndarray:
    return np.array(
        [similitud, math.sqrt(max(0.0, 1 - similitud ** 2))], dtype=np.float32
    )


class _MatcherFalso(EmbeddingMatcher):
    def __init__(self, vector):
        super().__init__(modelo="fake-model")
        self._vector = vector

    def calcular(self, texto):  # noqa: D102
        return self._vector.astype(np.float32).tobytes()


@pytest.fixture
def motor(tmp_path, monkeypatch):
    db = tmp_path / "hist.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE ejecuciones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fecha TEXT, estado TEXT, problema TEXT, plan_json TEXT,
                resultado TEXT, aceptada INTEGER DEFAULT 1,
                motivo_fallo TEXT DEFAULT '',
                problema_embedding BLOB, problema_embedding_model TEXT DEFAULT ''
            )
        """)
        conn.commit()

    monkeypatch.setattr(
        "learning.embedding_matcher.obtener_matcher",
        lambda *a, **k: _MatcherFalso(_vector(1.0)),
    )
    return LearningEngine(
        db_path=str(db),
        llm_client=object(),
        ruta_modelos=str(tmp_path / "modelos"),
    ), db


def _insertar(db, *, problema, similitud, aceptada=1, modelo="fake-model",
              plan_json='{"titulo": "Plan cuento", "pasos": [{"nombre": "Escribir", '
                        '"tipo_agente": "File", "es_critico": true, "tiene_contrato": true}]}',
              resultado="documento.docx con imagen"):
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO ejecuciones
               (fecha, estado, problema, plan_json, resultado, aceptada,
                problema_embedding, problema_embedding_model)
               VALUES ('2026-01-01', ?, ?, ?, ?, ?, ?, ?)""",
            ("completada" if aceptada else "fallida", problema, plan_json,
             resultado, aceptada,
             _vector(similitud).astype(np.float32).tobytes(), modelo),
        )
        conn.commit()


# ============================================================
# CONFIGURACIÓN
# ============================================================

def test_configuracion_retrieval_por_entorno(monkeypatch):
    monkeypatch.setenv("AGENTES_RETRIEVAL_UMBRAL", "0.9")
    monkeypatch.setenv("AGENTES_RETRIEVAL_MAX", "2")
    cfg = configuracion_retrieval()
    assert cfg["umbral"] == 0.9
    assert cfg["max_casos"] == 2


def test_configuracion_retrieval_acota_el_maximo(monkeypatch):
    monkeypatch.setenv("AGENTES_RETRIEVAL_MAX", "99")
    assert configuracion_retrieval()["max_casos"] == MAX_CASOS_RETRIEVAL
    monkeypatch.setenv("AGENTES_RETRIEVAL_MAX", "0")
    assert configuracion_retrieval()["max_casos"] == 1


# ============================================================
# BÚSQUEDA
# ============================================================

def test_solo_devuelve_casos_exitosos(motor):
    engine, db = motor
    _insertar(db, problema="cuento con imagen", similitud=0.99, aceptada=1)
    _insertar(db, problema="cuento fallido", similitud=0.99, aceptada=0)

    casos = engine.obtener_casos_similares("cuento con imagen")

    assert len(casos) == 1
    assert casos[0]["problema"] == "cuento con imagen"


def test_umbral_conservador_descarta_parecidos_lejanos(motor):
    engine, db = motor
    _insertar(db, problema="cuento muy parecido", similitud=0.99)
    _insertar(db, problema="cuento algo parecido", similitud=0.80)

    casos = engine.obtener_casos_similares("cuento con imagen")

    assert [c["problema"] for c in casos] == ["cuento muy parecido"]


def test_ignora_otro_modelo_de_embedding(motor):
    engine, db = motor
    _insertar(db, problema="otro modelo", similitud=0.99, modelo="otro-modelo")

    assert engine.obtener_casos_similares("cuento con imagen") == []


def test_devuelve_como_maximo_tres_casos(motor):
    engine, db = motor
    for i in range(6):
        _insertar(db, problema=f"cuento {i}", similitud=0.99 - i * 0.001)

    casos = engine.obtener_casos_similares("cuento con imagen")

    assert len(casos) == MAX_CASOS_RETRIEVAL
    # Ordenados por similitud descendente.
    assert casos[0]["similitud"] >= casos[-1]["similitud"]


def test_caso_incluye_plan_tipos_y_resultado(motor):
    engine, db = motor
    _insertar(db, problema="cuento con imagen", similitud=0.99)

    caso = engine.obtener_casos_similares("cuento con imagen")[0]

    assert caso["titulo"] == "Plan cuento"
    assert caso["tipos"] == ["File"]
    assert caso["pasos"][0]["critico"] is True
    assert caso["pasos"][0]["contrato"] is True
    assert "documento.docx" in caso["resultado"]


def test_sin_problema_no_busca(motor):
    engine, _ = motor
    assert engine.obtener_casos_similares("") == []


# ============================================================
# PROMPT
# ============================================================

def test_prompt_de_casos_no_presenta_el_caso_como_verdad(motor):
    engine, db = motor
    _insertar(db, problema="cuento con imagen", similitud=0.99)

    bloque = engine.obtener_casos_para_prompt("cuento con imagen")

    assert "ENFOQUES UTILIZADOS ANTERIORMENTE" in bloque
    assert "NO los copies ciegamente" in bloque
    assert "Evalúa si son" in bloque
    assert "aplicables al problema actual" in bloque
    assert "cuento con imagen" in bloque
    assert "tipos de agente: File" in bloque


def test_prompt_vacio_sin_casos(motor):
    engine, _ = motor
    assert engine.obtener_casos_para_prompt("un problema sin historial") == ""


def test_formatear_casos_recorta_resultados_largos():
    engine = object.__new__(LearningEngine)
    bloque = LearningEngine.formatear_casos_para_prompt(engine, [{
        "problema": "p", "similitud": 0.9, "titulo": "t", "tipos": ["Python"],
        "pasos": [{"nombre": "A", "tipo": "Python"}],
        "resultado": "x" * 5000, "score": 0.8,
    }])
    assert len(bloque) < 2000
