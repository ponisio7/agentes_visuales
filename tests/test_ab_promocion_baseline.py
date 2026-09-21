# tests/test_ab_promocion_baseline.py
"""
Regresión del bug de promoción A/B (V4.0-AB).

Qué se arregla y qué se fija aquí:

1. **Nunca se promocionaba nada.** La referencia era `_score_global()` (media
   de TODOS los scores registrados) siempre que no hubiera un `activo`; y como
   nadie promocionaba nunca, no había `activo` nunca. Punto muerto.
2. **Sin brazo de control.** Con «solo candidato», `elegir_variante` usaba el
   candidato al 100 %: no se registraba ningún uso del prompt original, así que
   no existía contra qué comparar.
3. **El `activo` se destruía.** `_guardar_reescritura` ponía a `descartado` el
   `activo` de la firma al crear un candidato nuevo.
4. **Estadísticas contaminadas por filas huérfanas** (usos cuya versión de
   prompt ya no existe, porque las conexiones antiguas abrían SQLite con
   `foreign_keys=OFF`).

Estos tests usan SIEMPRE una BD temporal y abortan si alguien apunta a la de
producción (misma red de seguridad que `test_ab_sintetico`).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from learning.prompt_ab_evaluator import (
    MIN_USOS_BASELINE_PARA_COMPARAR,
    MIN_USOS_PARA_DECIDIR,
    PromptABEvaluator,
)

DB_PRODUCCION = Path("agent_history.db").resolve()


def _validar_db_no_produccion(db_path) -> None:
    if Path(db_path).resolve() == DB_PRODUCCION:
        raise AssertionError(
            "test_ab_promocion_baseline no puede tocar la BD de producción; "
            "usa una BD temporal (tmp_path)."
        )


@pytest.fixture()
def db(tmp_path):
    """BD con el esquema completo (base + learning), sin datos."""
    _validar_db_no_produccion(tmp_path / "x.db")
    from storage.database import Database

    ruta = tmp_path / "ab.db"
    Database(str(ruta)).close()
    return str(ruta)


# ------------------------------------------------------------
# Helpers de escenario
# ------------------------------------------------------------
def _firma(nombre: str) -> str:
    return f"firma_{nombre}"


def _crear_version(db: str, firma: str, estado: str, nombre: str) -> int:
    """Inserta una versión de prompt y devuelve su id."""
    with sqlite3.connect(db, timeout=10) as conn:
        cur = conn.execute(
            """INSERT INTO prompts_reescritos
               (firma, prompt_original, prompt_nuevo, feedback_id, razon,
                fecha, activo, estado, n_usos)
               VALUES (?, ?, ?, 0, 'test', ?, ?, ?, 0)""",
            (
                firma,
                f"original {nombre}",
                f"reescrito {nombre}",
                datetime.now().isoformat(),
                1 if estado == "activo" else 0,
                estado,
            ),
        )
        conn.commit()
        return cur.lastrowid


def _usos(db: str, prompt_id: int, score: float, n: int, base_ejec: int = 1000) -> None:
    with sqlite3.connect(db, timeout=10) as conn:
        for i in range(n):
            conn.execute(
                """INSERT INTO prompt_reescrito_usos
                   (prompt_reescrito_id, ejecucion_id, score, fecha)
                   VALUES (?, ?, ?, ?)""",
                (prompt_id, base_ejec + i, score, datetime.now().isoformat()),
            )
        conn.commit()


def _baseline(db: str, firma: str, score: float, n: int, base_ejec: int = 5000) -> None:
    with sqlite3.connect(db, timeout=10) as conn:
        for i in range(n):
            conn.execute(
                """INSERT INTO prompt_reescrito_baseline
                   (firma, ejecucion_id, score, fecha, motivo)
                   VALUES (?, ?, ?, ?, 'control')""",
                (firma, base_ejec + i, score, datetime.now().isoformat()),
            )
        conn.commit()


def _estado(db: str, prompt_id: int) -> str:
    with sqlite3.connect(db, timeout=10) as conn:
        return conn.execute(
            "SELECT estado FROM prompts_reescritos WHERE id = ?", (prompt_id,)
        ).fetchone()[0]


def _feedback(db: str) -> int:
    """Feedback real: satisfacer la FK de prompts_reescritos.feedback_id."""
    with sqlite3.connect(db, timeout=10) as conn:
        cur = conn.execute(
            "INSERT INTO feedback_usuario (ejecucion_id, score, fecha) VALUES (1, -1.0, ?)",
            (datetime.now().isoformat(),),
        )
        conn.commit()
        return cur.lastrowid


# ------------------------------------------------------------
# 1. Arranque en frío: el baseline es un control válido
# ------------------------------------------------------------
def test_arranque_en_frio_promueve_contra_baseline(db):
    """Sin ningún `activo`, el prompt original es la referencia legítima."""
    firma = _firma("frio")
    cand = _crear_version(db, firma, "candidato", "cand")

    _baseline(db, firma, 0.50, MIN_USOS_BASELINE_PARA_COMPARAR)
    _usos(db, cand, 0.80, MIN_USOS_PARA_DECIDIR)

    decision = PromptABEvaluator(db).evaluar_candidato(firma)

    assert decision == "promovido"
    assert _estado(db, cand) == "activo"


def test_arranque_en_frio_descarta_si_empeora_contra_baseline(db):
    firma = _firma("frio_peor")
    cand = _crear_version(db, firma, "candidato", "cand")

    _baseline(db, firma, 0.70, MIN_USOS_BASELINE_PARA_COMPARAR)
    _usos(db, cand, 0.20, MIN_USOS_PARA_DECIDIR)

    assert PromptABEvaluator(db).evaluar_candidato(firma) == "descartado"
    assert _estado(db, cand) == "descartado"


# ------------------------------------------------------------
# 2. Sin control válido NO se decide (nunca contra la media global)
# ------------------------------------------------------------
def test_sin_control_no_decide_y_no_descarta(db):
    """El bug original: sin `activo` se comparaba contra la media global."""
    firma = _firma("sin_control")
    cand = _crear_version(db, firma, "candidato", "cand")
    _usos(db, cand, 0.90, MIN_USOS_PARA_DECIDIR)

    # Envenenar la media global con OTRA firma muy alta: si el evaluador la
    # usara como referencia, el candidato saldría "descartado".
    otra = _crear_version(db, _firma("otra"), "candidato", "otra")
    _usos(db, otra, 0.05, 50, base_ejec=7000)

    decision = PromptABEvaluator(db).evaluar_candidato(firma)

    assert decision == "espera"
    assert _estado(db, cand) == "candidato"  # ni promovido ni descartado


def test_filas_huerfanas_no_cuentan_como_evidencia(db):
    """Un uso que apunta a un prompt inexistente no es evidencia de nada."""
    firma = _firma("huerfanas")
    _crear_version(db, firma, "candidato", "cand")

    with sqlite3.connect(db, timeout=10) as conn:
        for i in range(MIN_USOS_PARA_DECIDIR):
            conn.execute(
                """INSERT INTO prompt_reescrito_usos
                   (prompt_reescrito_id, ejecucion_id, score, fecha)
                   VALUES (999999, ?, 1.0, ?)""",
                (800000 + i, datetime.now().isoformat()),
            )
        conn.commit()

    # Sin baseline y sin usos propios suficientes → espera, pase lo que pase.
    assert PromptABEvaluator(db).evaluar_candidato(firma) == "espera"

    # Y la media global ignora los huérfanos.
    with sqlite3.connect(db, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        assert PromptABEvaluator._score_global(conn) == 0.5

    # El mantenimiento los borra.
    assert PromptABEvaluator.limpiar_huerfanos(db)["usos"] == MIN_USOS_PARA_DECIDIR
    with sqlite3.connect(db, timeout=10) as conn:
        quedan = conn.execute("SELECT COUNT(*) FROM prompt_reescrito_usos").fetchone()[0]
    assert quedan == 0


# ------------------------------------------------------------
# 3. El activo tiene prioridad sobre el baseline
# ------------------------------------------------------------
def test_el_activo_tiene_prioridad_sobre_el_baseline(db):
    firma = _firma("prioridad")
    activo = _crear_version(db, firma, "activo", "act")
    cand = _crear_version(db, firma, "candidato", "cand")

    # Baseline altísimo: si se usara, el candidato se descartaría.
    _baseline(db, firma, 0.95, MIN_USOS_BASELINE_PARA_COMPARAR)
    _usos(db, activo, 0.30, MIN_USOS_PARA_DECIDIR, base_ejec=2000)
    _usos(db, cand, 0.50, MIN_USOS_PARA_DECIDIR)

    assert PromptABEvaluator(db).evaluar_candidato(firma) == "promovido"
    assert _estado(db, cand) == "activo"
    assert _estado(db, activo) == "descartado"


# ------------------------------------------------------------
# 4. Explotación/exploración: sin activo, el 80 % usa el original
# ------------------------------------------------------------
def test_solo_candidato_usa_el_incumbente_la_mayoria_de_las_veces(monkeypatch):
    import learning.prompt_ab_evaluator as ab

    # Explora (p < 0.20) → candidato.
    monkeypatch.setattr(ab.random, "random", lambda: 0.10)
    assert ab.PromptABEvaluator.elegir_variante(
        prompt_candidato="CAND", prompt_id_candidato=9,
        prompt_incumbente="ORIG", prompt_id_incumbente=0,
    ) == ("CAND", 9)

    # No explora → prompt ORIGINAL (id 0 = brazo de control).
    monkeypatch.setattr(ab.random, "random", lambda: 0.90)
    assert ab.PromptABEvaluator.elegir_variante(
        prompt_candidato="CAND", prompt_id_candidato=9,
        prompt_incumbente="ORIG", prompt_id_incumbente=0,
    ) == ("ORIG", 0)


def test_con_activo_el_incumbente_es_el_activo(monkeypatch):
    import learning.prompt_ab_evaluator as ab

    monkeypatch.setattr(ab.random, "random", lambda: 0.90)
    assert ab.PromptABEvaluator.elegir_variante(
        prompt_activo="ACT", prompt_id_activo=5,
        prompt_candidato="CAND", prompt_id_candidato=9,
        prompt_incumbente="ORIG", prompt_id_incumbente=0,
    ) == ("ACT", 5)


def test_sin_incumbente_se_conserva_el_comportamiento_antiguo(monkeypatch):
    """Sin nada contra lo que competir, el candidato es la única opción."""
    import learning.prompt_ab_evaluator as ab

    monkeypatch.setattr(ab.random, "random", lambda: 0.99)
    assert ab.PromptABEvaluator.elegir_variante(
        prompt_candidato="CAND", prompt_id_candidato=9,
    ) == ("CAND", 9)


# ------------------------------------------------------------
# 5. El score del aprendizaje rellena también el brazo de control
# ------------------------------------------------------------
def test_actualizar_score_rellena_usos_y_baseline(db):
    firma = _firma("score")
    cand = _crear_version(db, firma, "candidato", "cand")
    ev = PromptABEvaluator(db)

    ev.registrar_uso(cand, 4242, motivo="test")
    ev.registrar_uso_baseline(firma, 4242, motivo="test")
    assert ev.registrar_uso_baseline(firma, 4242) is True  # idempotente

    assert ev.actualizar_score(4242, 0.66) is True

    with sqlite3.connect(db, timeout=10) as conn:
        s_uso = conn.execute(
            "SELECT score FROM prompt_reescrito_usos WHERE ejecucion_id=4242"
        ).fetchone()[0]
        n_base = conn.execute(
            "SELECT COUNT(*) FROM prompt_reescrito_baseline WHERE ejecucion_id=4242"
        ).fetchone()[0]
        s_base = conn.execute(
            "SELECT score FROM prompt_reescrito_baseline WHERE ejecucion_id=4242"
        ).fetchone()[0]
    assert s_uso == 0.66
    assert (n_base, s_base) == (1, 0.66)


# ------------------------------------------------------------
# 6. Un candidato nuevo ya no tira al activo promocionado
# ------------------------------------------------------------
def test_guardar_reescritura_conserva_el_activo(db, monkeypatch):
    import learning.embedding_matcher as em
    from learning.feedback_processor import FeedbackProcessor

    class _MatcherNulo:
        modelo = "test"

        def calcular(self, _texto):
            return None

    monkeypatch.setattr(em, "obtener_matcher", lambda *a, **k: _MatcherNulo())

    texto = "escribe un cuento del dragon"
    firma = FeedbackProcessor._firmar(texto)
    activo = _crear_version(db, firma, "activo", "act")

    processor = FeedbackProcessor(db, llm_client=None)
    nuevo = processor._guardar_reescritura(
        prompt_original=texto,
        prompt_nuevo="reescrito nuevo",
        razon="test",
        feedback_id=_feedback(db),
    )

    assert nuevo is not None
    assert _estado(db, activo) == "activo"       # el incumbent sobrevive
    assert _estado(db, nuevo) == "candidato"     # y el nuevo se mide contra él


def test_candidato_anterior_se_supera(db, monkeypatch):
    """Solo un candidato medible a la vez: el anterior se descarta."""
    import learning.embedding_matcher as em
    from learning.feedback_processor import FeedbackProcessor

    class _MatcherNulo:
        modelo = "test"

        def calcular(self, _texto):
            return None

    monkeypatch.setattr(em, "obtener_matcher", lambda *a, **k: _MatcherNulo())

    texto = "escribe un cuento del dragon"
    firma = FeedbackProcessor._firmar(texto)
    viejo = _crear_version(db, firma, "candidato", "viejo")

    processor = FeedbackProcessor(db, llm_client=None)
    nuevo = processor._guardar_reescritura(
        prompt_original=texto, prompt_nuevo="nuevo",
        razon="t", feedback_id=_feedback(db),
    )

    assert nuevo is not None
    assert _estado(db, viejo) == "descartado"
    assert _estado(db, nuevo) == "candidato"


# ------------------------------------------------------------
# 7. El builder expone la firma y usa el original como control
# ------------------------------------------------------------
def _construir_agente_llm(monkeypatch, match_candidato, valor_random):
    import learning.embedding_matcher as em
    import learning.prompt_ab_evaluator as ab
    from core.problem_solver.builder import PlanBuilder
    from core.problem_solver.code_corrector import PythonCodeCorrector
    from core.problem_solver.validator import PlanValidator

    class _MatcherFalso:
        def buscar_match(self, db_path, prompt, estados_validos=()):
            if "candidato" not in estados_validos:
                return None
            return match_candidato

    monkeypatch.setattr(em, "obtener_matcher", lambda *a, **k: _MatcherFalso())
    monkeypatch.setattr(ab.random, "random", lambda: valor_random)

    builder = PlanBuilder(
        __import__("logging").getLogger("test.ab.control"),
        PythonCodeCorrector(),
        PlanValidator(__import__("logging").getLogger("test.ab.control")),
    )
    plan = builder.construir_plan("x", {
        "titulo": "t",
        "pasos": [{
            "orden": 1, "nombre": "Generar", "tipo": "LLM",
            "configuracion": {"prompt": "escribe un cuento del dragon"},
        }],
    })
    return builder.generar_agentes(plan)[0]


_MATCH = {
    "id": 42,
    "similitud": 0.91,
    "estado": "candidato",
    "prompt": "PROMPT CANDIDATO",
    "n_usos": 0,
    "prompt_original": "escribe un cuento del dragon",
    "firma": "firma_del_cuento",
    "motivo": "solape léxico 0.80",
}


def test_builder_usa_el_original_como_brazo_de_control(monkeypatch):
    agente = _construir_agente_llm(monkeypatch, _MATCH, valor_random=0.99)

    assert agente.prompt_reescrito_id == 0
    assert agente.prompt_firma == "firma_del_cuento"
    assert "control A/B" in agente.prompt_reescrito_motivo
    assert "PROMPT CANDIDATO" not in agente.prompt_llm


def test_builder_explora_el_candidato_y_conserva_la_firma(monkeypatch):
    agente = _construir_agente_llm(monkeypatch, _MATCH, valor_random=0.01)

    assert agente.prompt_reescrito_id == 42
    assert agente.prompt_firma == "firma_del_cuento"
    assert "PROMPT CANDIDATO" in agente.prompt_llm
