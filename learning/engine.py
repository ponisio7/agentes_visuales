"""
learning/engine.py
Punto de entrada único del módulo de aprendizaje. Une minería de datos,
evaluación por LLM y los dos modelos online en una API sencilla para
usar desde ProblemSolver / Scheduler / la UI, sin que esos módulos
tengan que saber nada de scikit-learn ni de SQL.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

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


# ============================================================
# RETRIEVAL DE CASOS SIMILARES (H8)
# ============================================================

UMBRAL_RETRIEVAL_DEFAULT = 0.85
MAX_CASOS_RETRIEVAL = 3
MAX_CANDIDATOS_RETRIEVAL = 1000


def configuracion_retrieval() -> dict:
    """Umbral y número de casos, configurables por entorno.

    - ``AGENTES_RETRIEVAL_UMBRAL`` (por defecto 0.85, conservador)
    - ``AGENTES_RETRIEVAL_MAX`` (por defecto 3, acotado a 1..3)
    """
    try:
        umbral = float(os.environ.get("AGENTES_RETRIEVAL_UMBRAL", ""))
    except (TypeError, ValueError):
        umbral = UMBRAL_RETRIEVAL_DEFAULT
    if not (0.0 < umbral <= 1.0):
        umbral = UMBRAL_RETRIEVAL_DEFAULT

    try:
        max_casos = int(os.environ.get("AGENTES_RETRIEVAL_MAX", ""))
    except (TypeError, ValueError):
        max_casos = MAX_CASOS_RETRIEVAL
    max_casos = min(max(max_casos, 1), MAX_CASOS_RETRIEVAL)
    return {"umbral": umbral, "max_casos": max_casos}


def _resumen_plan_json(plan_json: str) -> dict:
    """Resumen seguro del plan guardado (H5) para el prompt."""
    import json

    if not plan_json:
        return {"titulo": "", "pasos": 0, "tipos": [], "pasos_detalle": []}
    try:
        datos = json.loads(plan_json)
    except (json.JSONDecodeError, TypeError):
        return {"titulo": "", "pasos": 0, "tipos": [], "pasos_detalle": []}

    pasos = datos.get("pasos") or []
    detalle = [
        {
            "nombre": str(p.get("nombre", ""))[:60],
            "tipo": str(p.get("tipo_agente", "")),
            "critico": bool(p.get("es_critico", False)),
            "contrato": bool(p.get("tiene_contrato", False)),
        }
        for p in pasos
        if isinstance(p, dict)
    ]
    return {
        "titulo": str(datos.get("titulo", ""))[:120],
        "pasos": len(detalle),
        "tipos": sorted({d["tipo"] for d in detalle if d["tipo"]}),
        "pasos_detalle": detalle,
    }


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

    # ------------------------------------------------------------------
    # RETRIEVAL DE CASOS SIMILARES (H8)
    # ------------------------------------------------------------------
    def obtener_casos_similares(
        self,
        problema: str,
        max_casos: int | None = None,
        umbral: float | None = None,
    ) -> list[dict]:
        """Casos ANTERIORMENTE EXITOSOS parecidos al problema actual.

        Solo busca entre ejecuciones con ``aceptada = 1`` (éxito real, no
        «los agentes terminaron»). Reutiliza el ``EmbeddingMatcher`` del
        proyecto; si no está disponible o no hay casos, devuelve ``[]``.

        Umbral conservador por defecto (0.85) y 1..3 casos.
        """
        if not problema or not problema.strip():
            return []
        cfg = configuracion_retrieval()
        umbral = cfg["umbral"] if umbral is None else float(umbral)
        max_casos = cfg["max_casos"] if max_casos is None else int(max_casos)
        max_casos = min(max(max_casos, 1), MAX_CASOS_RETRIEVAL)

        try:
            from .embedding_matcher import obtener_matcher

            matcher = obtener_matcher()
            vector_bytes = matcher.calcular(problema)
            if vector_bytes is None:
                return []
            vec_consulta = np.frombuffer(vector_bytes, dtype=np.float32)
        except Exception as e:
            logger.debug(f"Retrieval: embeddings no disponibles: {e}")
            return []

        candidatos = self._cargar_candidatos_exitosos(matcher.modelo)
        if not candidatos:
            return []

        puntuados = []
        for fila in candidatos:
            try:
                vec_cand = np.frombuffer(fila["problema_embedding"], dtype=np.float32)
            except (TypeError, ValueError):
                continue
            if vec_cand.shape != vec_consulta.shape:
                continue
            sim = float(np.dot(vec_consulta, vec_cand))
            if sim >= umbral:
                puntuados.append((sim, fila))

        puntuados.sort(key=lambda par: par[0], reverse=True)

        casos = []
        for sim, fila in puntuados[:max_casos]:
            plan = _resumen_plan_json(fila.get("plan_json") or "")
            casos.append({
                "ejecucion_id": fila.get("id"),
                "problema": (fila.get("problema") or "")[:400],
                "similitud": round(sim, 4),
                "titulo": plan["titulo"],
                "tipos": plan["tipos"],
                "pasos": plan["pasos_detalle"],
                "resultado": (fila.get("resultado") or "")[:400],
                "score": self._score_plan(fila.get("id")),
                "fecha": fila.get("fecha") or "",
            })

        if casos:
            logger.info(
                f"🧠 Retrieval: {len(casos)} caso(s) exitosos similares "
                f"(umbral={umbral}, mejor={casos[0]['similitud']})"
            )
        return casos

    def _cargar_candidatos_exitosos(self, modelo: str) -> list[dict]:
        """Ejecuciones exitosas con embedding del problema del mismo modelo."""
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")
                columnas = {
                    r[1] for r in conn.execute("PRAGMA table_info(ejecuciones)")
                }
                if not {"problema_embedding", "aceptada"} <= columnas:
                    return []
                filas = conn.execute(
                    """SELECT id, problema, plan_json, resultado, fecha,
                              problema_embedding
                       FROM ejecuciones
                       WHERE aceptada = 1
                         AND problema_embedding IS NOT NULL
                         AND problema_embedding_model = ?
                         AND problema IS NOT NULL AND problema != ''
                       ORDER BY id DESC
                       LIMIT ?""",
                    (modelo, MAX_CANDIDATOS_RETRIEVAL),
                ).fetchall()
                return [dict(r) for r in filas]
        except Exception as e:
            logger.debug(f"Retrieval: no se pudieron cargar candidatos: {e}")
            return []

    def _score_plan(self, ejecucion_id: int | None) -> float | None:
        """Score LLM del plan (si existe), como señal de calidad del caso."""
        if ejecucion_id is None:
            return None
        try:
            with closing(sqlite3.connect(self.db_path, timeout=5)) as conn:
                fila = conn.execute(
                    """SELECT MAX(score) FROM evaluaciones_llm
                       WHERE ejecucion_id = ? AND alcance = 'plan'""",
                    (ejecucion_id,),
                ).fetchone()
                if fila and fila[0] is not None:
                    return float(fila[0])
        except Exception:
            pass
        return None

    def formatear_casos_para_prompt(self, casos: list[dict]) -> str:
        """Bloque de prompt con los casos similares, SIN presentarlos como verdad."""
        if not casos:
            return ""
        lineas = [
            "ENFOQUES UTILIZADOS ANTERIORMENTE EN PROBLEMAS SIMILARES:",
            "Estos casos se resolvieron con éxito antes. Evalúa si son",
            "aplicables al problema actual; NO los copies ciegamente y NO",
            "asumas que el mismo enfoque funcionará aquí.",
            "",
        ]
        for i, caso in enumerate(casos, 1):
            score = (
                f"{caso['score']:.2f}" if caso.get("score") is not None else "n/d"
            )
            tipos = ", ".join(caso.get("tipos") or []) or "n/d"
            lineas.append(
                f"CASO {i} (similitud {caso['similitud']}, score {score}):"
            )
            if caso.get("problema"):
                lineas.append(f"  · problema: {caso['problema']}")
            lineas.append(f"  · plan: {caso.get('titulo') or '(sin título)'}")
            lineas.append(f"  · tipos de agente: {tipos}")
            for paso in (caso.get("pasos") or [])[:6]:
                sufijo = []
                if paso.get("critico"):
                    sufijo.append("crítico")
                if paso.get("contrato"):
                    sufijo.append("con contrato")
                extra = f" [{', '.join(sufijo)}]" if sufijo else ""
                lineas.append(f"      - [{paso.get('tipo')}] {paso.get('nombre')}{extra}")
            if caso.get("resultado"):
                lineas.append(f"  · resultado/evidencia: {caso['resultado'][:200]}")
            lineas.append("")
        return "\n".join(lineas)

    def obtener_casos_para_prompt(
        self,
        problema: str,
        max_casos: int | None = None,
        umbral: float | None = None,
    ) -> str:
        """Bloque listo para el prompt (o '' si no hay casos fiables)."""
        try:
            casos = self.obtener_casos_similares(
                problema, max_casos=max_casos, umbral=umbral
            )
            return self.formatear_casos_para_prompt(casos)
        except Exception as e:
            logger.debug(f"obtener_casos_para_prompt falló: {e}")
            return ""
