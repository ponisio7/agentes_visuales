"""
learning/engine.py
Punto de entrada único del módulo de aprendizaje. Une minería de datos,
evaluación por LLM y los dos modelos online en una API sencilla para
usar desde ProblemSolver / Scheduler / la UI, sin que esos módulos
tengan que saber nada de scikit-learn ni de SQL.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from .dataset import minar_dataset
from .feature_extraction import (
    extraer_features_agente,
    extraer_features_plan,
)
from .models import (
    MUESTRAS_MINIMAS_ENTRENAMIENTO_INICIAL,
    FailurePredictor,
    PlanScorer,
)
from .reward_llm import EvaluadorLLM
from .schema import aplicar_esquema_learning

logger = logging.getLogger(__name__)


class LearningEngine:
    """
    Uso típico:

        engine = LearningEngine(db_path="historial.db", llm_client=llm_client)
        engine.reentrenar_desde_historial()   # una vez al arrancar la app

        # --- antes de ejecutar un agente ---
        riesgo = engine.predecir_riesgo_agente(agente)

        # --- después de ejecutar un agente ---
        engine.registrar_resultado_agente(...)

        # --- eligiendo entre planes candidatos ---
        mejor = engine.elegir_mejor_plan([plan_a, plan_b])

        # --- tras ejecutar el plan completo ---
        engine.registrar_resultado_plan(...)
    """

    def __init__(
        self,
        db_path: str,
        llm_client,
        ruta_modelos: str | None = None,
        modelo_evaluador: str | None = None,
    ):
        self.db_path = str(db_path)
        self.llm_client = llm_client
        self.evaluador = EvaluadorLLM(
            llm_client, modelo=modelo_evaluador
        )

        if ruta_modelos is None:
            ruta_modelos = Path(self.db_path).parent / "learning_models"
        self.failure_predictor = FailurePredictor(Path(ruta_modelos))
        self.plan_scorer = PlanScorer(Path(ruta_modelos))

        # Crear tablas (idempotente). Usamos una conexión independiente
        # porque este engine puede invocarse antes de que Database esté
        # inicializado.
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                aplicar_esquema_learning(conn)
                conn.commit()
        except Exception as e:
            logger.warning(f"No se pudo aplicar esquema de learning: {e}")

    def registrar_reparacion_plan(self, problema: str, tipo: str) -> None:
        """
        Registra que un plan fue reparado (porque el LLM no cumplió un
        requisito explícito). Alimenta la minería de lecciones: si un
        tipo de reparación se repite, se genera una lección que va al
        prompt para que el LLM aprenda a no necesitar la reparación.
        """
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute(
                    """INSERT INTO reparaciones_plan (problema, tipo, fecha)
                    VALUES (?, ?, ?)""",
                    (problema[:500], tipo, datetime.now().isoformat()),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"No se pudo registrar reparación: {e}")

    # ------------------------------------------------------------------
    # ENTRENAMIENTO / MINERÍA
    # ------------------------------------------------------------------
    def reentrenar_desde_historial(self) -> dict[str, int]:
        """
        Vuelca todo el historial disponible al FailurePredictor.
        Llamar al arrancar la app o desde un botón "🧠 Reentrenar".
        """
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")
                features, y_exito, _y_reward = minar_dataset(
                    conn,
                    minimo_muestras=MUESTRAS_MINIMAS_ENTRENAMIENTO_INICIAL,
                )
        except Exception as e:
            logger.warning(f"Error minando historial: {e}")
            return {"agentes_minados": 0, "entrenado": 0}

        if not features:
            logger.info("Sin datos suficientes para reentrenar")
            return {"agentes_minados": 0, "entrenado": 0}

        self.failure_predictor.entrenar_inicial(features, y_exito)
        return {
            "agentes_minados": len(features),
            "entrenado": int(self.failure_predictor._entrenado),
        }

    # ------------------------------------------------------------------
    # PREDICCIÓN (antes de ejecutar)
    # ------------------------------------------------------------------
    def predecir_riesgo_agente(self, agente: Any) -> dict[str, Any]:
        try:
            features = extraer_features_agente(agente)
            resultado = self.failure_predictor.predecir(features)
            return {
                "probabilidad_exito": resultado.probabilidad_exito,
                "confianza": resultado.confianza,
                "n_muestras": resultado.n_muestras_vistas,
            }
        except Exception as e:
            logger.debug(f"predecir_riesgo_agente falló: {e}")
            return {
                "probabilidad_exito": 0.5,
                "confianza": "baja",
                "n_muestras": 0,
            }

    def puntuar_plan(self, plan: Any) -> dict[str, Any]:
        try:
            features = extraer_features_plan(plan)
            score = self.plan_scorer.puntuar(features)
            return {"score_esperado": score, "n_muestras": self.plan_scorer.n_muestras}
        except Exception as e:
            logger.debug(f"puntuar_plan falló: {e}")
            return {"score_esperado": 0.5, "n_muestras": 0}

    def elegir_mejor_plan(self, planes: list[Any]) -> Any:
        if not planes:
            return None
        if self.plan_scorer.n_muestras == 0:
            return planes[0]
        try:
            return max(
                planes,
                key=lambda p: self.puntuar_plan(p)["score_esperado"],
            )
        except Exception as e:
            logger.debug(f"elegir_mejor_plan falló: {e}")
            return planes[0]

    # ------------------------------------------------------------------
    # REFUERZO (después de ejecutar)
    # ------------------------------------------------------------------
    def registrar_resultado_agente(
        self,
        ejecucion_id: int,
        agente_ejecucion_id: int,
        agente: Any,
        tarea: str,
        resultado_texto: str,
        estado_real: str,
    ) -> dict[str, Any]:
        """
        Llamar justo después de que un agente termina de ejecutarse.
        1) Pide al LLM que evalúe el resultado (esa es la recompensa).
        2) Guarda la evaluación en la BD.
        3) Actualiza el FailurePredictor de forma incremental.
        """
        evaluacion = self.evaluador.evaluar(tarea, resultado_texto)

        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute(
                    """INSERT INTO evaluaciones_llm
                       (ejecucion_id, agente_ejecucion_id, alcance, score,
                        justificacion, modelo_evaluador, fecha)
                       VALUES (?, ?, 'agente', ?, ?, ?, ?)""",
                    (
                        ejecucion_id,
                        agente_ejecucion_id,
                        evaluacion.score,
                        evaluacion.justificacion,
                        evaluacion.modelo,
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"No se pudo guardar evaluación: {e}")

        try:
            features = extraer_features_agente(agente)
            etiqueta = 1 if (estado_real or "").lower() in (
                "completado", "completada", "exito", "éxito", "ok"
            ) else 0
            self.failure_predictor.actualizar(features, etiqueta)
        except Exception as e:
            logger.debug(f"No se pudo actualizar predictor: {e}")

        return {
            "score_llm": evaluacion.score,
            "justificacion": evaluacion.justificacion,
        }

    def registrar_resultado_plan(
        self,
        ejecucion_id: int,
        plan: Any,
        tarea: str,
        resultado_texto: str,
    ) -> dict[str, Any]:
        """Evalúa y refuerza a nivel de PLAN completo."""
        evaluacion = self.evaluador.evaluar(tarea, resultado_texto)

        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                conn.execute(
                    """INSERT INTO evaluaciones_llm
                       (ejecucion_id, agente_ejecucion_id, alcance, score,
                        justificacion, modelo_evaluador, fecha)
                       VALUES (?, NULL, 'plan', ?, ?, ?, ?)""",
                    (
                        ejecucion_id,
                        evaluacion.score,
                        evaluacion.justificacion,
                        evaluacion.modelo,
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"No se pudo guardar evaluación del plan: {e}")

        try:
            features = extraer_features_plan(plan)
            if self.plan_scorer.n_muestras == 0:
                self.plan_scorer.entrenar_inicial([features], [evaluacion.score])
            else:
                self.plan_scorer.actualizar(features, evaluacion.score)
        except Exception as e:
            logger.debug(f"No se pudo actualizar PlanScorer: {e}")

        return {
            "score_llm": evaluacion.score,
            "justificacion": evaluacion.justificacion,
        }

    def obtener_lecciones_para_prompt(self, max_lecciones: int = 12) -> str:
        """
        Devuelve un bloque de texto con lecciones aprendidas, listo para
        añadir al system prompt del ProblemSolver.

        Si no hay datos suficientes, devuelve cadena vacía.
        No lanza excepciones: si falla, devuelve "".
        """
        try:
            from .lessons import ExtractorLecciones
            extractor = ExtractorLecciones(self.db_path)
            lecciones = extractor.extraer(max_lecciones=max_lecciones)
            if not lecciones:
                return ""
            logger.info(
                f"🧠 ExtractorLecciones: {len(lecciones)} lecciones "
                f"para el prompt"
            )
            return extractor.formatear_para_prompt(lecciones)
        except Exception as e:
            logger.debug(f"obtener_lecciones_para_prompt falló: {e}")
            return ""
