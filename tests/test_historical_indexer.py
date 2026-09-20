# tests/test_historical_indexer.py
"""Reindexado del histórico y éxito real (V3.8-4).

Se prueba con una BD temporal y un matcher FALSO (no se carga
sentence-transformers). El punto central: las ejecuciones antiguas se
marcaron ``aceptada=1`` por defecto, así que el «éxito» histórico era falso;
el éxito real compuesto las reclasifica y el retrieval deja de usarlas.
"""
import hashlib
import json
import sqlite3
from contextlib import closing

import numpy as np
import pytest

from learning.historical_indexer import (
    MOTIVO_ACEPTADA_SIN_ERRORES,
    MOTIVO_EJECUCION_INCOMPLETA,
    MOTIVO_FEEDBACK_NEGATIVO,
    MOTIVO_FEEDBACK_POSITIVO,
    MOTIVO_SIN_PROBLEMA,
    HistoricalIndexer,
)
from learning.schema import aplicar_esquema_learning
from storage.database import Database


class _MatcherFalso:
    """Embedding determinista de 4 dimensiones, sin modelo real."""

    modelo = "modelo-falso"

    def calcular(self, texto: str) -> bytes | None:
        if not texto:
            return None
        digest = hashlib.sha256(texto.encode("utf-8")).digest()
        return np.frombuffer(digest[:16], dtype=np.float32).copy().tobytes()


class _MatcherNulo:
    modelo = "modelo-falso"

    def calcular(self, texto: str) -> bytes | None:
        return None


@pytest.fixture
def db_path(tmp_path):
    ruta = str(tmp_path / "historico.db")
    Database(db_path=ruta)
    with closing(sqlite3.connect(ruta)) as conn:
        aplicar_esquema_learning(conn)
        conn.commit()
    return ruta


def _insertar(
    ruta: str,
    *,
    problema: str = "problema de prueba",
    aceptada: int = 1,
    errores: int = 0,
    cancelados: int = 0,
    estado: str = "completada",
    completados: int = 1,
    agentes_total: int = 1,
    plan_json: str = "",
    resultado: str = "",
    motivo_fallo: str = "",
    embedding: bytes | None = None,
    embedding_model: str = "",
) -> int:
    with closing(sqlite3.connect(ruta)) as conn:
        cursor = conn.execute(
            """INSERT INTO ejecuciones (
                   fecha, duracion_total, agentes_total, completados, errores,
                   cancelados, estado, problema, plan_json, resultado, aceptada,
                   motivo_fallo, problema_embedding, problema_embedding_model)
               VALUES ('2026-01-01', 1.5, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agentes_total, completados, errores, cancelados, estado,
                problema, plan_json, resultado, aceptada, motivo_fallo,
                embedding, embedding_model,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _insertar_feedback(ruta: str, ejecucion_id: int, score: float, *,
                       alcance: str = "plan", comentario: str = "") -> None:
    with closing(sqlite3.connect(ruta)) as conn:
        conn.execute(
            """INSERT INTO feedback_usuario
                   (ejecucion_id, alcance, score, comentario, fecha)
               VALUES (?, ?, ?, ?, '2026-01-01')""",
            (ejecucion_id, alcance, score, comentario),
        )
        conn.commit()


def _fila(ruta: str, ejecucion_id: int) -> dict:
    with closing(sqlite3.connect(ruta)) as conn:
        conn.row_factory = sqlite3.Row
        return dict(conn.execute(
            "SELECT * FROM ejecuciones WHERE id = ?", (ejecucion_id,)
        ).fetchone())


# ============================================================
# Migración / esquema
# ============================================================

def test_la_migracion_crea_las_columnas_de_exito_real(db_path):
    with closing(sqlite3.connect(db_path)) as conn:
        columnas = {
            f[1] for f in conn.execute("PRAGMA table_info(ejecuciones)")
        }
    assert {"exito_real", "exito_real_motivo", "indexado_fecha"} <= columnas
    assert {"llamadas_llm", "tokens_total", "coste"} <= columnas


# ============================================================
# Éxito real (sin tocar el modelo)
# ============================================================

def test_aceptada_sin_errores_es_exito(db_path):
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())
    fila = {"problema": "p", "aceptada": 1, "errores": 0, "cancelados": 0,
            "estado": "completada", "completados": 1, "agentes_total": 1}

    exito, motivo = indexer.calcular_exito_real(fila)

    assert exito is True
    assert motivo == MOTIVO_ACEPTADA_SIN_ERRORES


def test_antigua_aceptada_con_errores_deja_de_ser_exito(db_path):
    """El bug que motivó V3.8-4: aceptada=1 por defecto con errores."""
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())
    fila = {"problema": "p", "aceptada": 1, "errores": 2, "cancelados": 0,
            "estado": "completada", "completados": 1, "agentes_total": 3}

    exito, motivo = indexer.calcular_exito_real(fila)

    assert exito is False
    assert motivo == MOTIVO_EJECUCION_INCOMPLETA


def test_feedback_negativo_veta_el_exito(db_path):
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())
    fila = {"problema": "p", "aceptada": 1, "errores": 0, "cancelados": 0,
            "estado": "completada", "completados": 1, "agentes_total": 1}

    exito, motivo = indexer.calcular_exito_real(
        fila, [{"alcance": "plan", "score": -1.0}]
    )

    assert exito is False
    assert motivo == MOTIVO_FEEDBACK_NEGATIVO


def test_feedback_positivo_confirma_el_exito(db_path):
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())
    fila = {"problema": "p", "aceptada": 0, "errores": 1, "cancelados": 0,
            "estado": "fallida", "completados": 0, "agentes_total": 1}

    exito, motivo = indexer.calcular_exito_real(
        fila, [{"alcance": "plan", "score": 1.0}]
    )

    assert exito is True
    assert motivo == MOTIVO_FEEDBACK_POSITIVO


def test_sin_problema_es_indeterminado(db_path):
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())
    assert indexer.calcular_exito_real({"problema": "  "}) == (
        None, MOTIVO_SIN_PROBLEMA
    )


def test_estado_fallido_es_fallo(db_path):
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())
    fila = {"problema": "p", "aceptada": 1, "errores": 0, "cancelados": 0,
            "estado": "fallida", "completados": 1, "agentes_total": 1}
    exito, _ = indexer.calcular_exito_real(fila)
    assert exito is False


def test_aceptada_cero_es_fallo(db_path):
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())
    fila = {"problema": "p", "aceptada": 0, "errores": 0, "cancelados": 0,
            "estado": "completada", "completados": 1, "agentes_total": 1}
    exito, _ = indexer.calcular_exito_real(fila)
    assert exito is False


# ============================================================
# Reindexado
# ============================================================

def test_reindexar_guarda_embedding_y_exito_real(db_path):
    fila_id = _insertar(db_path, problema="busca empresas en Valencia")
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())

    resumen = indexer.reindexar()

    assert resumen["indexadas"] == 1
    assert resumen["embeddings_calculados"] == 1
    assert resumen["exito_real_positivos"] == 1

    fila = _fila(db_path, fila_id)
    assert fila["problema_embedding"] is not None
    assert fila["problema_embedding_model"] == "modelo-falso"
    assert fila["exito_real"] == 1
    assert fila["exito_real_motivo"] == MOTIVO_ACEPTADA_SIN_ERRORES
    assert fila["indexado_fecha"]


def test_reindexar_reclasifica_las_antiguas(db_path):
    buena = _insertar(db_path, problema="caso bueno")
    mala = _insertar(db_path, problema="caso malo", errores=1, completados=0,
                     agentes_total=2)
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())

    resumen = indexer.reindexar()

    assert resumen["exito_real_positivos"] == 1
    assert resumen["exito_real_negativos"] == 1
    assert _fila(db_path, buena)["exito_real"] == 1
    assert _fila(db_path, mala)["exito_real"] == 0


def test_reindexar_dry_run_no_escribe(db_path):
    fila_id = _insertar(db_path, problema="no escribir")
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())

    resumen = indexer.reindexar(dry_run=True)

    assert resumen["dry_run"] is True
    assert resumen["revisadas"] == 1
    assert resumen["indexadas"] == 0
    fila = _fila(db_path, fila_id)
    assert fila["problema_embedding"] is None
    assert fila["exito_real"] is None


def test_reindexar_no_borra_un_embedding_existente(db_path):
    previo = b"\x00\x01\x02\x03"
    fila_id = _insertar(db_path, problema="con embedding", embedding=previo,
                        embedding_model="modelo-falso")
    # Fuerza el reindexado del éxito real aunque ya tenga embedding.
    indexer = HistoricalIndexer(db_path, matcher=_MatcherNulo())

    indexer.reindexar(forzar=True)

    fila = _fila(db_path, fila_id)
    assert fila["problema_embedding"] == previo
    assert fila["exito_real"] == 1


def test_candidatos_solo_devuelve_lo_pendiente(db_path):
    _insertar(db_path, problema="uno")
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())
    assert len(indexer.candidatos()) == 1

    indexer.reindexar()

    assert indexer.candidatos() == []
    # Con ``forzar`` vuelve a aparecer.
    assert len(indexer.candidatos(forzar=True)) == 1


def test_reindexar_sin_embeddings_sigue_calculando_exito_real(db_path):
    fila_id = _insertar(db_path, problema="sin modelo", errores=1)
    indexer = HistoricalIndexer(db_path, matcher=_MatcherNulo())

    resumen = indexer.reindexar()

    assert resumen["embeddings_no_disponibles"] == 1
    fila = _fila(db_path, fila_id)
    assert fila["problema_embedding"] is None
    assert fila["exito_real"] == 0, "el éxito real no depende del embedding"


def test_estadisticas(db_path):
    _insertar(db_path, problema="a")
    _insertar(db_path, problema="b", errores=1)
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())
    indexer.reindexar()

    stats = indexer.estadisticas()

    assert stats["total_con_problema"] == 2
    assert stats["con_embedding"] == 2
    assert stats["exito_real_positivo"] == 1
    assert stats["exito_real_negativo"] == 1


# ============================================================
# CaseRecord
# ============================================================

def test_construir_case_reune_el_contexto(db_path):
    plan = json.dumps({
        "titulo": "Informe de empresas",
        "pasos": [
            {"nombre": "Buscar", "tipo_agente": "Browser", "es_critico": False,
             "tiene_contrato": False},
            {"nombre": "Excel", "tipo_agente": "File", "es_critico": True,
             "tiene_contrato": True},
        ],
    })
    fila_id = _insertar(
        db_path, problema="empresas de Valencia", plan_json=plan,
        resultado="24 empresas", errores=0,
    )
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute(
            """INSERT INTO agentes_ejecucion
                   (ejecucion_id, agente_id, nombre, tipo, estado, orden)
               VALUES (?, 'a1', 'Buscar', 'Browser', 'completado', 0)""",
            (fila_id,),
        )
        conn.execute(
            """INSERT INTO reparaciones_plan
                   (problema, tipo, fecha, ejecucion_id, intento, exito)
               VALUES ('p', 'correccion_puntual', '2026-01-01', ?, 1, 0)""",
            (fila_id,),
        )
        conn.commit()
    _insertar_feedback(db_path, fila_id, -1.0, comentario="faltaban emails")

    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())
    case = indexer.construir_case(fila_id)

    assert case.objetivo == "Informe de empresas"
    assert case.tipos == ["Browser", "File"]
    assert case.contrato["pasos_criticos"] == ["Excel"]
    assert case.contrato["pasos_con_contrato"] == ["Excel"]
    assert case.intentos_plan_b == 1
    assert case.errores == 0
    assert len(case.agentes) == 1
    assert case.exito_real is False  # feedback negativo manda
    assert any("faltaban emails" in leccion for leccion in case.lecciones)

    datos = case.to_dict()
    assert json.loads(json.dumps(datos, default=str))["ejecucion_id"] == fila_id


def test_construir_case_inexistente_devuelve_none(db_path):
    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())
    assert indexer.construir_case(99999) is None


# ============================================================
# El retrieval prefiere el éxito real
# ============================================================

def test_retrieval_excluye_los_casos_reclasificados(db_path):
    """``_cargar_candidatos_exitosos`` debe usar exito_real, no aceptada."""
    from learning.engine import LearningEngine

    vector = _MatcherFalso().calcular("x")
    # A: éxito real → entra.
    id_ok = _insertar(db_path, problema="ok", embedding=vector,
                      embedding_model="modelo-falso")
    # B: éxito real 0 con aceptada=1 → NO entra.
    _insertar(db_path, problema="malo", errores=1, embedding=vector,
              embedding_model="modelo-falso")
    # C: ya evaluada como indeterminada (motivo puesto) → NO entra.
    id_c = _insertar(db_path, problema="dudoso", embedding=vector,
                     embedding_model="modelo-falso")
    # D: legado sin indexar (motivo vacío) → fallback aceptada=1 → entra.
    id_d = _insertar(db_path, problema="legado", embedding=vector,
                     embedding_model="modelo-falso")

    indexer = HistoricalIndexer(db_path, matcher=_MatcherFalso())
    indexer.reindexar(forzar=True)
    with closing(sqlite3.connect(db_path)) as conn:
        conn.execute("UPDATE ejecuciones SET exito_real = 0 WHERE id = ?", (id_c,))
        conn.commit()

    engine = object.__new__(LearningEngine)
    engine.db_path = db_path
    ids = {f["id"] for f in engine._cargar_candidatos_exitosos("modelo-falso")}

    assert id_ok in ids
    assert id_d in ids
    assert id_c not in ids
    assert len(ids) == 2
