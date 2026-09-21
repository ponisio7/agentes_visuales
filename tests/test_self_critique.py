# tests/test_self_critique.py
"""
Tests del ciclo de auto-crítica (V4.0, punto 4).

Lo que se fija:

  - una evaluación del LLM por debajo del umbral se convierte en CANDIDATO de
    reescritura, por el mismo camino que el feedback del usuario;
  - no se gasta una llamada al LLM cuando no merece la pena (score bueno, sin
    justificación, presupuesto agotado o firma que ya tiene candidato);
  - los topes se respetan (tope por ejecución y opt-out por entorno);
  - la crítica se atribuye al AGENTE correcto, no al último LLM;
  - la fila de auditoría usa `alcance='auto_critica'` y **no** contamina
    `exito_real` del HistoricalIndexer.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from core.budget_manager import BudgetManager
from learning.feedback_processor import FeedbackProcessor
from learning.self_critique import SelfCritic, autocritica_habilitada


# ------------------------------------------------------------
# Dobles y utilidades
# ------------------------------------------------------------
class _LLMFalso:
    """Devuelve siempre una reescritura válida y cuenta las llamadas."""

    def __init__(self, respuesta: str | None = None):
        self.llamadas = 0
        self._respuesta = respuesta or json.dumps({
            "prompt_nuevo": "PROMPT MEJORADO",
            "razon": "el evaluador detectó salida incompleta",
        })

    def chat(self, **kwargs):
        self.llamadas += 1
        return self._respuesta


@pytest.fixture(autouse=True)
def _sin_embeddings(monkeypatch):
    """Los tests no deben descargar modelos de HuggingFace ni tocar la red."""
    import learning.embedding_matcher as em

    class _MatcherNulo:
        modelo = "test"

        def calcular(self, _texto):
            return None

    monkeypatch.setattr(em, "obtener_matcher", lambda *a, **k: _MatcherNulo())


@pytest.fixture()
def db(tmp_path):
    from storage.database import Database

    ruta = tmp_path / "self.db"
    Database(str(ruta)).close()
    with sqlite3.connect(str(ruta), timeout=10) as conn:
        conn.execute(
            "INSERT INTO ejecuciones (id, fecha, duracion_total, agentes_total, "
            "completados, errores, cancelados, estado, aceptada, problema) "
            "VALUES (1, '2026-01-01T00:00:00', 1.0, 2, 2, 0, 0, 'completada', 1, "
            "'escribe un cuento')"
        )
        # Nota: prompt_usado lleva un endurecimiento real (saltos de línea de
        # verdad); _prompt_crudo lo recorta antes de firmar.
        conn.execute(
            """INSERT INTO agentes_ejecucion
               (id, ejecucion_id, agente_id, nombre, tipo, estado, dependencias,
                orden, prompt_usado)
               VALUES (10, 1, 'aaa', 'Redactar', 'LLM', 'Completado', '[]', 0, ?)""",
            ("INSTRUCCIONES CRÍTICAS:\nTAREA:\nescribe un cuento",),
        )
        conn.execute(
            """INSERT INTO agentes_ejecucion
               (id, ejecucion_id, agente_id, nombre, tipo, estado, dependencias,
                orden, prompt_usado)
               VALUES (11, 1, 'bbb', 'Revisar', 'LLM', 'Completado', '[]', 1,
                       'revisa y corrige el informe')"""
        )
        conn.execute(
            "INSERT INTO agentes_ejecucion (id, ejecucion_id, agente_id, nombre, "
            "tipo, estado, dependencias, orden) VALUES (12, 1, 'ccc', 'Guardar', "
            "'File', 'Completado', '[]', 2)"
        )
        conn.commit()
    return str(ruta)


def _evaluacion(db: str, score: float, *, alcance="agente", agente_id=10,
                justificacion="la salida está incompleta"):
    with sqlite3.connect(db, timeout=10) as conn:
        conn.execute(
            """INSERT INTO evaluaciones_llm
               (ejecucion_id, agente_ejecucion_id, alcance, score, justificacion,
                modelo_evaluador, fecha)
               VALUES (1, ?, ?, ?, ?, 'fake', '2026-01-01T00:00:00')""",
            (None if alcance == "plan" else agente_id, alcance, score, justificacion),
        )
        conn.commit()


def _critic(db: str, llm=None, **kwargs):
    llm = llm or _LLMFalso()
    return SelfCritic(db, FeedbackProcessor(db, llm), **kwargs), llm


def _reescrituras(db: str):
    with sqlite3.connect(db, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM prompts_reescritos")]


def _feedback(db: str):
    with sqlite3.connect(db, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute("SELECT * FROM feedback_usuario")]


# ------------------------------------------------------------
# 1. El camino feliz
# ------------------------------------------------------------
def test_una_evaluacion_baja_se_convierte_en_candidato(db):
    _evaluacion(db, 0.1)
    critico, llm = _critic(db)

    resultado = critico.revisar_ejecucion(1)

    assert resultado.reescribio is True
    assert llm.llamadas == 1
    reescrituras = _reescrituras(db)
    assert len(reescrituras) == 1
    assert reescrituras[0]["estado"] == "candidato"
    assert reescrituras[0]["prompt_nuevo"] == "PROMPT MEJORADO"
    assert reescrituras[0]["razon"].startswith("[auto-critica]")


def test_la_critica_queda_auditada_con_su_origen(db):
    _evaluacion(db, 0.1)
    critico, _ = _critic(db)
    critico.revisar_ejecucion(1)

    filas = _feedback(db)
    assert len(filas) == 1
    # El marcador 'auto_critica' es lo que evita confundirla con feedback humano.
    assert filas[0]["alcance"] == "auto_critica"
    assert "Auto-crítica del evaluador" in filas[0]["comentario"]


def test_una_evaluacion_de_plan_tambien_dispara_reescritura(db):
    _evaluacion(db, 0.2, alcance="plan", agente_id=None)
    critico, llm = _critic(db)

    resultado = critico.revisar_ejecucion(1)

    assert resultado.reescribio is True
    assert llm.llamadas == 1


# ------------------------------------------------------------
# 2. Cuándo NO merece la pena
# ------------------------------------------------------------
def test_no_reescribe_si_el_score_es_bueno(db):
    _evaluacion(db, 0.9)
    critico, llm = _critic(db)

    resultado = critico.revisar_ejecucion(1)

    assert resultado.reescribio is False
    assert llm.llamadas == 0
    assert any("umbral" in s for s in resultado.saltadas)


def test_no_reescribe_sin_justificacion(db):
    _evaluacion(db, 0.1, justificacion="")
    critico, llm = _critic(db)

    resultado = critico.revisar_ejecucion(1)

    assert resultado.reescribio is False
    assert llm.llamadas == 0
    assert any("justificación" in s for s in resultado.saltadas)


def test_no_gasta_llm_si_la_firma_ya_tiene_candidato(db):
    """Deduplicación contra el A/B: no se pisa al candidato que se está midiendo."""
    _evaluacion(db, 0.1)
    with sqlite3.connect(db, timeout=10) as conn:
        conn.execute(
            """INSERT INTO prompts_reescritos
               (firma, prompt_original, prompt_nuevo, feedback_id, razon,
                fecha, activo, estado, n_usos)
               VALUES (?, 'escribe un cuento', 'ya reescrito', 0, 'previa',
                       '2026-01-01T00:00:00', 1, 'candidato', 0)""",
            (FeedbackProcessor._firmar("escribe un cuento"),),
        )
        conn.commit()
    critico, llm = _critic(db)

    resultado = critico.revisar_ejecucion(1)

    assert resultado.reescribio is False
    assert llm.llamadas == 0
    assert any("candidato" in s for s in resultado.saltadas)


def test_respeta_el_presupuesto_agotado(db):
    _evaluacion(db, 0.1)
    presupuesto = BudgetManager(max_llamadas=0)
    presupuesto.iniciar()
    critico, llm = _critic(db, presupuesto=presupuesto)

    resultado = critico.revisar_ejecucion(1)

    assert resultado.reescribio is False
    assert llm.llamadas == 0
    assert any("presupuesto" in s for s in resultado.saltadas)


def test_tope_de_reescrituras_por_ejecucion(db):
    _evaluacion(db, 0.1, agente_id=10)
    _evaluacion(db, 0.2, agente_id=11)
    critico, llm = _critic(db, max_por_ejecucion=1)

    resultado = critico.revisar_ejecucion(1)

    assert len(resultado.reescrituras) == 1
    assert llm.llamadas == 1
    assert any("tope" in s for s in resultado.saltadas)


def test_se_puede_desactivar_por_entorno(db, monkeypatch):
    _evaluacion(db, 0.1)
    monkeypatch.setenv("AGENTES_AUTOCRITICA", "0")
    assert autocritica_habilitada() is False
    critico, llm = _critic(db)

    resultado = critico.revisar_ejecucion(1)

    assert resultado.reescribio is False
    assert llm.llamadas == 0
    assert "desactivada" in resultado.saltadas[0]


def test_sin_evaluaciones_no_hace_nada(db):
    critico, llm = _critic(db)
    resultado = critico.revisar_ejecucion(1)
    assert resultado.revisadas == 0
    assert llm.llamadas == 0


# ------------------------------------------------------------
# 3. Atribución y efecto sobre el histórico
# ------------------------------------------------------------
def test_atribuye_la_critica_al_agente_correcto(db):
    """El id 11 es 'Revisar'; la reescritura debe partir de SU prompt."""
    _evaluacion(db, 0.1, agente_id=11)
    critico, _ = _critic(db)

    critico.revisar_ejecucion(1)

    reescritura = _reescrituras(db)[0]
    assert reescritura["prompt_original"] == "revisa y corrige el informe"
    assert reescritura["firma"] == FeedbackProcessor._firmar(
        "revisa y corrige el informe"
    )


def test_la_auto_critica_no_contamina_exito_real(db):
    """`exito_real` solo mira feedback con alcance='plan' (el del usuario).

    El riesgo real no es de signo, es de ESCALA: el evaluador LLM puntúa 0–1
    (donde 0.6 es mediocre) y el feedback de usuario puntúa −1–1 (donde 0.6 es
    claramente positivo). Si la auto-crítica entrara con alcance='plan', un
    0.6 mediocre se leería como veredicto POSITIVO del usuario y podría
    convertir una ejecución fallida en «éxito» del corpus de retrieval.
    """
    # Ejecución 2: fallida por columnas duras (errores=1), no por feedback.
    with sqlite3.connect(db, timeout=10) as conn:
        conn.execute(
            "INSERT INTO ejecuciones (id, fecha, duracion_total, agentes_total, "
            "completados, errores, cancelados, estado, aceptada, problema) "
            "VALUES (2, '2026-01-01T00:00:00', 1.0, 2, 1, 1, 0, 'fallida', 0, "
            "'escribe un cuento')"
        )
        conn.execute(
            """INSERT INTO feedback_usuario
               (ejecucion_id, agente_ejecucion_id, alcance, score, comentario, fecha)
               VALUES (2, 10, 'auto_critica', 0.6, 'mediocre según el evaluador',
                       '2026-01-01T00:00:00')"""
        )
        conn.commit()

    from learning.historical_indexer import HistoricalIndexer

    indice = HistoricalIndexer(db_path=db)
    with sqlite3.connect(db, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        fila = dict(conn.execute("SELECT * FROM ejecuciones WHERE id = 2").fetchone())
        feedback = [dict(r) for r in conn.execute(
            "SELECT * FROM feedback_usuario WHERE ejecucion_id = 2"
        )]

    # Con el marcador, la crítica del LLM se ignora: manda la verdad dura.
    exito, _ = indice.calcular_exito_real(fila, feedback)
    assert exito is False

    # Si se colara como feedback de usuario, el 0.6 mediocre la volvería «éxito».
    como_usuario = [dict(feedback[0], alcance="plan")]
    exito_colado, motivo = indice.calcular_exito_real(fila, como_usuario)
    assert exito_colado is True
    assert motivo == "feedback_positivo"


# ------------------------------------------------------------
# 4. Unificación con el feedback del usuario
# ------------------------------------------------------------
def test_procesar_feedback_delega_en_el_mismo_camino(db):
    with sqlite3.connect(db, timeout=10) as conn:
        cur = conn.execute(
            """INSERT INTO feedback_usuario
               (ejecucion_id, agente_ejecucion_id, alcance, score, comentario, fecha)
               VALUES (1, 11, 'plan', -1.0, 'no era lo que pedí', '2026-01-01T00:00:00')"""
        )
        feedback_id = cur.lastrowid
        conn.commit()

    llm = _LLMFalso()
    processor = FeedbackProcessor(db, llm)
    salida = processor.procesar_feedback(feedback_id)

    assert salida["procesado"] is True
    assert llm.llamadas == 1
    reescritura = _reescrituras(db)[0]
    assert reescritura["razon"].startswith("[usuario]")
    # Mismo destino que la auto-crítica: candidato medible por el A/B.
    assert reescritura["estado"] == "candidato"


def test_el_feedback_positivo_sigue_sin_reescribir(db):
    with sqlite3.connect(db, timeout=10) as conn:
        cur = conn.execute(
            """INSERT INTO feedback_usuario
               (ejecucion_id, agente_ejecucion_id, alcance, score, comentario, fecha)
               VALUES (1, 11, 'plan', 1.0, 'perfecto', '2026-01-01T00:00:00')"""
        )
        feedback_id = cur.lastrowid
        conn.commit()

    llm = _LLMFalso()
    salida = FeedbackProcessor(db, llm).procesar_feedback(feedback_id)

    assert salida["procesado"] is False
    assert llm.llamadas == 0
    assert _reescrituras(db) == []
