# learning/prompt_ab_evaluator.py
"""
PromptABEvaluator: A/B testing de reescrituras de prompt.

Contexto:
  Cuando el FeedbackProcessor produce una reescritura, se guarda en
  `prompts_reescritos` con estado='candidato'. Hasta ahora el builder
  la usaba al 100%, sin validación. Eso es riesgo: si la reescritura
  empeora el prompt, el sistema degrada en silencio.

Solución:
  Este módulo implementa un A/B testing ligero:
    1. El candidato se explora con probabilidad PROBABILIDAD_CANDIDATO.
    2. Cada uso se registra en `prompt_reescrito_usos`.
    3. Cuando el aprendizaje evalúa el plan, se actualiza el score.
    4. Tras MIN_USOS_PARA_DECIDIR usos, se compara contra el activo:
         - candidato mejora por ≥ MARGEN_PROMOCION → PROMOTE.
         - candidato empeora por ≥ MARGEN_PROMOCION → DISCARD.
         - empate → sigue acumulando hasta MAX_USOS_SIN_DECISION.
    5. Si tras MAX_USOS_SIN_DECISION no hay decisión clara → DISCARD.

Todo falla en silencio (log a debug) para no romper la ejecución.
"""
from __future__ import annotations

import logging
import random
import sqlite3
from contextlib import closing
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================
# PARÁMETROS DEL A/B TESTING
# ============================================================
PROBABILIDAD_CANDIDATO = 0.20      # 20% de las ejecuciones prueban el candidato
MIN_USOS_PARA_DECIDIR = 5          # mínimo de usos con score antes de decidir
MARGEN_PROMOCION = 0.05            # mejora/empeora mínima para decidir
MAX_USOS_SIN_DECISION = 20         # si tras N usos no se decide, descartar
MIN_USOS_ACTIVO_PARA_COMPARAR = 3  # mínimo de usos del activo para comparar


class PromptABEvaluator:
    """
    Gestiona el ciclo de vida de candidatos: exploración, registro de
    usos, evaluación y promoción/descarte.

    Uso típico (runtime, desde builder._kwargs_llm):
        evaluator = PromptABEvaluator(db_path)
        prompt, prompt_id, estado = evaluator.elegir_variante(firma, activo, candidato)

    Uso típico (background, desde execution_recorder._worker):
        evaluator.registrar_uso(prompt_id, ejecucion_id)
        evaluator.actualizar_score(prompt_id, ejecucion_id, score)
        evaluator.evaluar_candidato(firma)
    """

    def __init__(self, db_path: str):
        self.db_path = str(db_path)

    # ------------------------------------------------------------
    # 1. Elección de variante (runtime, llamado desde el builder)
    # ------------------------------------------------------------
    @staticmethod
    def elegir_variante(
        prompt_activo: str | None,
        prompt_id_activo: int | None,
        prompt_candidato: str | None,
        prompt_id_candidato: int | None,
    ):
        """
        Decide qué prompt usar en esta ejecución.

        - Si hay activo y candidato: elige el candidato con probabilidad
          PROBABILIDAD_CANDIDATO, si no, el activo.
        - Si solo hay activo: usa el activo.
        - Si solo hay candidato: usa el candidato (no hay nada mejor).
        - Si no hay ninguno: devuelve (None, None).

        Devuelve: (prompt_usado, prompt_id_usado)
        """
        if prompt_activo and prompt_candidato:
            if random.random() < PROBABILIDAD_CANDIDATO:
                return prompt_candidato, prompt_id_candidato
            return prompt_activo, prompt_id_activo

        if prompt_activo:
            return prompt_activo, prompt_id_activo

        if prompt_candidato:
            return prompt_candidato, prompt_id_candidato

        return None, None

    # ------------------------------------------------------------
    # 2. Registro de uso (background, tras la ejecución)
    # ------------------------------------------------------------
    def registrar_uso(self, prompt_id: int | None, ejecucion_id: int) -> bool:
        """
        Registra que una ejecución usó una versión concreta de prompt.
        Idempotente: si ya existe la pareja (prompt_id, ejecucion_id),
        no duplica.
        """
        if not prompt_id or not ejecucion_id:
            return False
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                existe = conn.execute(
                    """SELECT 1 FROM prompt_reescrito_usos
                       WHERE prompt_reescrito_id = ? AND ejecucion_id = ?""",
                    (prompt_id, ejecucion_id),
                ).fetchone()
                if existe:
                    return True
                conn.execute(
                    """INSERT INTO prompt_reescrito_usos
                       (prompt_reescrito_id, ejecucion_id, score, fecha)
                       VALUES (?, ?, NULL, ?)""",
                    (prompt_id, ejecucion_id, datetime.now().isoformat()),
                )
                # Incrementar el contador en prompts_reescritos.
                conn.execute(
                    "UPDATE prompts_reescritos SET n_usos = n_usos + 1 WHERE id = ?",
                    (prompt_id,),
                )
                conn.commit()
            return True
        except Exception as e:
            logger.debug(f"registrar_uso falló: {e}")
            return False

    # ------------------------------------------------------------
    # 3. Actualización de score (background, tras el aprendizaje)
    # ------------------------------------------------------------
    def actualizar_score(
        self,
        ejecucion_id: int,
        score: float,
    ) -> bool:
        """
        Actualiza el score de todos los usos asociados a esa ejecución.

        Se llama desde execution_recorder._worker() cuando el
        EvaluadorLLM ha puntuado el plan.

        Si no había usos registrados para esa ejecución, no hace nada.
        """
        if ejecucion_id is None or score is None:
            return False
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                cur = conn.execute(
                    """UPDATE prompt_reescrito_usos
                       SET score = ?
                       WHERE ejecucion_id = ? AND score IS NULL""",
                    (score, ejecucion_id),
                )
                conn.commit()
                if cur.rowcount > 0:
                    logger.debug(
                        f"actualizar_score: {cur.rowcount} filas actualizadas "
                        f"para ejecucion_id={ejecucion_id} con score={score}"
                    )
                return cur.rowcount > 0
        except Exception as e:
            logger.debug(f"actualizar_score falló: {e}")
            return False

    # ------------------------------------------------------------
    # 4. Evaluación de candidato (background)
    # ------------------------------------------------------------
    def evaluar_candidato(self, firma: str) -> str | None:
        """
        Decide si promover, descartar o dejar en espera al candidato
        de la firma indicada.

        Devuelve: 'promovido' | 'descartado' | 'espera' | None (sin candidato).
        """
        if not firma:
            return None
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")

                activo = conn.execute(
                    """SELECT id, prompt_nuevo, estado
                       FROM prompts_reescritos
                       WHERE firma = ? AND estado = 'activo'
                       ORDER BY fecha DESC LIMIT 1""",
                    (firma,),
                ).fetchone()
                candidato = conn.execute(
                    """SELECT id, prompt_nuevo, n_usos
                       FROM prompts_reescritos
                       WHERE firma = ? AND estado = 'candidato'
                       ORDER BY fecha DESC LIMIT 1""",
                    (firma,),
                ).fetchone()

                if candidato is None:
                    return None

                stats_cand = self._stats_version(conn, candidato["id"])
                stats_act = (
                    self._stats_version(conn, activo["id"])
                    if activo
                    else {"usos": 0, "score": None}
                )

                # ¿Hay suficiente evidencia sobre el candidato?
                if stats_cand["usos"] < MIN_USOS_PARA_DECIDIR:
                    logger.debug(
                        f"AB: candidato {candidato['id']} acumula "
                        f"{stats_cand['usos']}/{MIN_USOS_PARA_DECIDIR} usos"
                    )
                    return "espera"

                score_cand = stats_cand["score"] if stats_cand["score"] is not None else 0.5

                # Sin activo con datos suficientes → usar media global
                if (not activo) or (stats_act["usos"] < MIN_USOS_ACTIVO_PARA_COMPARAR):
                    score_ref = self._score_global(conn)
                    logger.debug(
                        f"AB: activo sin datos suficientes, comparando "
                        f"con global ({score_ref:.3f})"
                    )
                else:
                    score_ref = stats_act["score"] if stats_act["score"] is not None else 0.5

                delta = score_cand - score_ref
                logger.info(
                    f"AB: candidato {candidato['id']} vs activo "
                    f"(score {score_cand:.3f} vs {score_ref:.3f}, "
                    f"delta {delta:+.3f}, usos={stats_cand['usos']})"
                )

                # Decisión
                if delta >= MARGEN_PROMOCION:
                    self._promover(conn, candidato["id"])
                    return "promovido"
                elif delta <= -MARGEN_PROMOCION:
                    self._descartar(conn, candidato["id"])
                    return "descartado"
                else:
                    # Empate. Si ya se han acumulado demasiados usos, descartar.
                    if stats_cand["usos"] >= MAX_USOS_SIN_DECISION:
                        logger.info(
                            f"AB: candidato {candidato['id']} sin mejora "
                            f"clara tras {stats_cand['usos']} usos → descartado"
                        )
                        self._descartar(conn, candidato["id"])
                        return "descartado"
                    return "espera"
        except Exception as e:
            logger.debug(f"evaluar_candidato falló: {e}")
            return None

    # ------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------
    @staticmethod
    def _stats_version(conn: sqlite3.Connection, prompt_id: int) -> dict:
        """
        Calcula la media de score y número de usos con score válido
        para una versión concreta.
        """
        row = conn.execute(
            """SELECT COUNT(*) AS usos, AVG(score) AS score
               FROM prompt_reescrito_usos
               WHERE prompt_reescrito_id = ? AND score IS NOT NULL""",
            (prompt_id,),
        ).fetchone()
        usos = row["usos"] or 0
        return {
            "usos": usos,
            "score": row["score"] if usos > 0 else None,
        }

    @staticmethod
    def _score_global(conn: sqlite3.Connection) -> float:
        """Media global de todos los scores registrados. Fallback 0.5."""
        row = conn.execute(
            "SELECT AVG(score) AS s FROM prompt_reescrito_usos WHERE score IS NOT NULL"
        ).fetchone()
        return float(row["s"]) if row and row["s"] is not None else 0.5

    @staticmethod
    def _promover(conn: sqlite3.Connection, prompt_id: int):
        """
        Promueve un candidato a activo. El activo anterior pasa a
        descartado. Mantiene `activo` sincronizado con `estado` para
        compatibilidad con consultar_reescritura.
        """
        # Sacar la firma del candidato
        firma_row = conn.execute(
            "SELECT firma FROM prompts_reescritos WHERE id = ?",
            (prompt_id,),
        ).fetchone()
        if not firma_row:
            return
        firma = firma_row["firma"]

        # Descartar el activo anterior de la misma firma
        conn.execute(
            """UPDATE prompts_reescritos
               SET estado = 'descartado', activo = 0
               WHERE firma = ? AND estado = 'activo' AND id != ?""",
            (firma, prompt_id),
        )
        # Promover el candidato
        conn.execute(
            """UPDATE prompts_reescritos
               SET estado = 'activo', activo = 1
               WHERE id = ?""",
            (prompt_id,),
        )
        conn.commit()
        logger.info(f"✅ AB: candidato {prompt_id} PROMOVIDO a activo")

    @staticmethod
    def _descartar(conn: sqlite3.Connection, prompt_id: int):
        """Descarta un candidato."""
        conn.execute(
            """UPDATE prompts_reescritos
               SET estado = 'descartado', activo = 0
               WHERE id = ?""",
            (prompt_id,),
        )
        conn.commit()
        logger.info(f"❌ AB: candidato {prompt_id} DESCARTADO")
