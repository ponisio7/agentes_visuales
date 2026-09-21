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
       El resto de las veces se usa el INCUMBENTE: el `activo` si existe y,
       si no, el prompt ORIGINAL. Esto es la corrección V4.0-AB: antes, con
       solo un candidato y ningún activo, se usaba el candidato al 100 %, lo
       que eliminaba el brazo de control y hacía imposible comparar.
    2. Cada uso se registra en `prompt_reescrito_usos`; los usos del prompt
       original (control) se registran en `prompt_reescrito_baseline`.
    3. Cuando el aprendizaje evalúa el plan, se actualiza el score de ambas.
    4. Tras MIN_USOS_PARA_DECIDIR usos puntuados, se compara contra una
       referencia de CONTROL válida:
         - activo con evidencia suficiente (A/B clásico), o
         - baseline de la misma firma (prompt original, arranque en frío).
       Sin ninguna de las dos NO se decide: se devuelve 'espera'. La media
       global NO es un control y ya no se usa para decidir.
    5. Con control válido:
         - candidato mejora por ≥ MARGEN_PROMOCION → PROMOTE.
         - candidato empeora por ≥ MARGEN_PROMOCION → DISCARD.
         - empate → sigue acumulando hasta MAX_USOS_SIN_DECISION.
    6. Si tras MAX_USOS_SIN_DECISION no hay decisión clara → DISCARD.

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
MIN_USOS_BASELINE_PARA_COMPARAR = 3  # mínimo de usos del prompt original (control)


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

    def _conectar(self) -> sqlite3.Connection:
        """Conexión auxiliar homogénea.

        Activa ``foreign_keys`` para que los borrados en cascada funcionen de
        verdad (si no, al limpiar `ejecuciones` quedaban filas huérfanas en
        `prompt_reescrito_usos` que luego contaminaban las estadísticas).
        """
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        try:
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error:
            pass
        return conn

    # ------------------------------------------------------------
    # 1. Elección de variante (runtime, llamado desde el builder)
    # ------------------------------------------------------------
    @staticmethod
    def elegir_variante(
        prompt_activo: str | None = None,
        prompt_id_activo: int | None = None,
        prompt_candidato: str | None = None,
        prompt_id_candidato: int | None = None,
        prompt_incumbente: str | None = None,
        prompt_id_incumbente: int | None = 0,
    ):
        """
        Decide qué prompt usar en esta ejecución.

        El «incumbente» es la versión que se usa cuando NO se explora: el
        `activo` si existe y, si no, el prompt ORIGINAL (id 0). Es lo que
        convierte esto en un A/B de verdad.

        Reglas (corregidas en V4.0-AB):
        - candidato + incumbente → candidato con probabilidad
          PROBABILIDAD_CANDIDATO; si no, el incumbente.
        - solo incumbente (activo y/o original) → incumbente.
        - solo candidato y sin incumbente → candidato (no hay alternativa).
        - ninguno → (None, None).

        ⚠️ La regla antigua «solo candidato → candidato al 100 %» se ha
        eliminado: impedía registrar el brazo de control y, como nunca había
        `activo`, dejaba el A/B sin referencia válida para siempre.

        Devuelve: (prompt_usado, prompt_id_usado)
        """
        if prompt_activo:
            referencia, referencia_id = prompt_activo, prompt_id_activo
        elif prompt_incumbente:
            referencia, referencia_id = prompt_incumbente, prompt_id_incumbente
        else:
            referencia, referencia_id = None, None

        if prompt_candidato and referencia:
            if random.random() < PROBABILIDAD_CANDIDATO:
                return prompt_candidato, prompt_id_candidato
            return referencia, referencia_id

        if referencia:
            return referencia, referencia_id

        if prompt_candidato:
            return prompt_candidato, prompt_id_candidato

        return None, None

    # ------------------------------------------------------------
    # 2. Registro de uso (background, tras la ejecución)
    # ------------------------------------------------------------
    def registrar_uso(
        self, prompt_id: int | None, ejecucion_id: int, motivo: str = ""
    ) -> bool:
        """
        Registra que una ejecución usó una versión concreta de prompt.

        ``motivo`` (H2) explica POR QUÉ se eligió esa variante (similitud,
        firma coincidente, solape léxico...). Si la columna no existe en una
        BD antigua, se reintenta sin ella para no perder el registro.

        Idempotente: si ya existe la pareja (prompt_id, ejecucion_id),
        no duplica.
        """
        if not prompt_id or not ejecucion_id:
            return False
        try:
            with closing(self._conectar()) as conn:
                existe = conn.execute(
                    """SELECT 1 FROM prompt_reescrito_usos
                       WHERE prompt_reescrito_id = ? AND ejecucion_id = ?""",
                    (prompt_id, ejecucion_id),
                ).fetchone()
                if existe:
                    return True
                try:
                    conn.execute(
                        """INSERT INTO prompt_reescrito_usos
                           (prompt_reescrito_id, ejecucion_id, score, fecha, motivo)
                           VALUES (?, ?, NULL, ?, ?)""",
                        (prompt_id, ejecucion_id, datetime.now().isoformat(), motivo),
                    )
                except sqlite3.OperationalError:
                    # BD antigua sin la columna 'motivo'.
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

    def registrar_uso_baseline(
        self, firma: str, ejecucion_id: int, motivo: str = ""
    ) -> bool:
        """
        Registra el uso del prompt ORIGINAL para una firma (brazo de control).

        Se llama cuando el builder eligió el incumbente (id 0) y la firma tiene
        al menos una reescritura candidateable: sin esto el candidato no tiene
        contra qué compararse y la promoción es imposible en arranque en frío.

        Idempotente por (firma, ejecucion_id).
        """
        if not firma or not ejecucion_id:
            return False
        try:
            with closing(self._conectar()) as conn:
                existe = conn.execute(
                    """SELECT 1 FROM prompt_reescrito_baseline
                       WHERE firma = ? AND ejecucion_id = ?""",
                    (firma, ejecucion_id),
                ).fetchone()
                if existe:
                    return True
                conn.execute(
                    """INSERT INTO prompt_reescrito_baseline
                       (firma, ejecucion_id, score, fecha, motivo)
                       VALUES (?, ?, NULL, ?, ?)""",
                    (firma, ejecucion_id, datetime.now().isoformat(), motivo),
                )
                conn.commit()
            return True
        except Exception as e:
            logger.debug(f"registrar_uso_baseline falló: {e}")
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
        Actualiza el score de todos los usos asociados a esa ejecución,
        tanto del brazo candidato/activo como del brazo de control
        (prompt original).

        Se llama desde execution_recorder._worker() cuando el
        EvaluadorLLM ha puntuado el plan.

        Si no había usos registrados para esa ejecución, no hace nada.
        """
        if ejecucion_id is None or score is None:
            return False
        try:
            with closing(self._conectar()) as conn:
                cur = conn.execute(
                    """UPDATE prompt_reescrito_usos
                       SET score = ?
                       WHERE ejecucion_id = ? AND score IS NULL""",
                    (score, ejecucion_id),
                )
                cur_base = conn.execute(
                    """UPDATE prompt_reescrito_baseline
                       SET score = ?
                       WHERE ejecucion_id = ? AND score IS NULL""",
                    (score, ejecucion_id),
                )
                conn.commit()
                total = (cur.rowcount or 0) + (cur_base.rowcount or 0)
                if total > 0:
                    logger.debug(
                        f"actualizar_score: {cur.rowcount} usos + "
                        f"{cur_base.rowcount} baseline para "
                        f"ejecucion_id={ejecucion_id} con score={score}"
                    )
                return total > 0
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
            with closing(self._conectar()) as conn:
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
                stats_base = self._stats_baseline(conn, firma)

                # ¿Hay suficiente evidencia sobre el candidato?
                if stats_cand["usos"] < MIN_USOS_PARA_DECIDIR:
                    logger.debug(
                        f"AB: candidato {candidato['id']} acumula "
                        f"{stats_cand['usos']}/{MIN_USOS_PARA_DECIDIR} usos"
                    )
                    return "espera"

                score_cand = (
                    stats_cand["score"] if stats_cand["score"] is not None else 0.5
                )

                # ── Referencia de control (V4.0-AB) ──
                # Solo dos referencias son legítimas:
                #   1. el `activo` de la misma firma (A/B clásico), o
                #   2. el baseline de la misma firma: lo que rendía el prompt
                #      ORIGINAL (arranque en frío, cuando aún no hay activo).
                # La media global NO es un control (mezcla escalas, firmas y
                # filas huérfanas) y ya no se usa para decidir.
                score_ref = None
                tipo_ref = ""
                if activo and stats_act["usos"] >= MIN_USOS_ACTIVO_PARA_COMPARAR:
                    score_ref = (
                        stats_act["score"] if stats_act["score"] is not None else 0.5
                    )
                    tipo_ref = f"activo id={activo['id']}"
                elif stats_base["usos"] >= MIN_USOS_BASELINE_PARA_COMPARAR:
                    score_ref = stats_base["score"]
                    tipo_ref = f"baseline prompt original ({stats_base['usos']} usos)"

                if score_ref is None:
                    logger.info(
                        f"AB: candidato {candidato['id']} sin control válido "
                        f"(activo={stats_act['usos']} usos, "
                        f"baseline={stats_base['usos']} usos) → espera"
                    )
                    return "espera"

                delta = score_cand - score_ref
                logger.info(
                    f"AB: candidato {candidato['id']} vs {tipo_ref} "
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

        El JOIN con `prompts_reescritos` descarta filas huérfanas (usos cuya
        versión ya no existe) que, si no, inflarían las estadísticas.
        """
        row = conn.execute(
            """SELECT COUNT(*) AS usos, AVG(u.score) AS score
               FROM prompt_reescrito_usos u
               JOIN prompts_reescritos p ON p.id = u.prompt_reescrito_id
               WHERE u.prompt_reescrito_id = ? AND u.score IS NOT NULL""",
            (prompt_id,),
        ).fetchone()
        usos = row["usos"] or 0
        return {
            "usos": usos,
            "score": row["score"] if usos > 0 else None,
        }

    @staticmethod
    def _stats_baseline(conn: sqlite3.Connection, firma: str) -> dict:
        """Media de score del brazo de control (prompt original) de una firma."""
        row = conn.execute(
            """SELECT COUNT(*) AS usos, AVG(score) AS score
               FROM prompt_reescrito_baseline
               WHERE firma = ? AND score IS NOT NULL""",
            (firma,),
        ).fetchone()
        usos = row["usos"] or 0
        return {
            "usos": usos,
            "score": row["score"] if usos > 0 else None,
        }

    @staticmethod
    def _score_global(conn: sqlite3.Connection) -> float:
        """Media global de los scores con versión existente. Solo diagnóstico.

        ⚠️ No usar como referencia de decisión: no es un control.
        """
        row = conn.execute(
            """SELECT AVG(u.score) AS s
               FROM prompt_reescrito_usos u
               JOIN prompts_reescritos p ON p.id = u.prompt_reescrito_id
               WHERE u.score IS NOT NULL"""
        ).fetchone()
        return float(row["s"]) if row and row["s"] is not None else 0.5

    @staticmethod
    def limpiar_huerfanos(db_path: str) -> dict:
        """Borra usos A/B cuya versión de prompt ya no existe.

        Mantenimiento: las conexiones antiguas abrían SQLite con
        ``foreign_keys=OFF``, así que los borrados en cascada dejaban filas
        huérfanas que contaminaban las estadísticas. Devuelve el recuento.
        """
        borrados = {"usos": 0}
        try:
            conn = sqlite3.connect(str(db_path), timeout=10)
            try:
                conn.execute("PRAGMA foreign_keys=ON")
                cur = conn.execute(
                    """DELETE FROM prompt_reescrito_usos
                       WHERE prompt_reescrito_id NOT IN
                             (SELECT id FROM prompts_reescritos)"""
                )
                borrados["usos"] = cur.rowcount or 0
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.debug(f"limpiar_huerfanos falló: {e}")
        return borrados

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
