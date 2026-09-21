# learning/self_critique.py
"""
SelfCritic — cierra el ciclo de auto-crítica del LLM (V4.0, punto 4).

El hueco que tapa:

  - El ``EvaluadorLLM`` ya puntuaba cada resultado (agente y plan) **después**
    de ejecutarlo... y ahí se acababa todo: el juicio se guardaba en
    ``evaluaciones_llm`` y no disparaba nada.
  - La única vía para reescribir un prompt era el feedback del **usuario**
    (score ≤ 0 + comentario), que es manual y esporádico.
  - Con el A/B ya arreglado (V4.0-AB) sí existe un sitio seguro donde probar
    una reescritura automática: se guarda como ``candidato`` y solo se promueve
    si mejora al incumbent *medido*.

Qué hace este módulo:

  1. Lee las evaluaciones del LLM de una ejecución.
  2. Selecciona las que **merecen** crítica (score por debajo del umbral y con
     justificación) — no todo resultado mediocre debe reescribir un prompt.
  3. Crea la crítica y delega en ``FeedbackProcessor.procesar_critica``, el
     camino ÚNICO de reescritura que comparte con el feedback humano.
  4. Respeta el presupuesto y un tope por ejecución, y **no gasta** una llamada
     al LLM si esa firma ya tiene un candidato esperando al A/B.

Unificado de verdad: el disparador cambia (usuario o LLM) y el alcance cambia
(agente o plan), pero el destino es siempre el mismo ``prompts_reescritos`` con
estado ``candidato``, que el A/B mide y promueve.

Freno explícito (por qué no reescribe siempre): reescribir es una llamada al
LLM, y una reescritura sin control degrada el sistema en silencio. Aquí se
aplican cuatro límites: umbral de score, presupuesto, tope por ejecución y
deduplicación por firma con candidato pendiente.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Por debajo de esto el evaluador está diciendo que el resultado es malo.
UMBRAL_CRITICA = 0.4
# Como mucho, una reescritura automática por ejecución. Evita que una ejecución
# desastrosa dispare cinco llamadas al LLM a reescribir.
MAX_POR_EJECUCION = 1
# Interruptor de entorno (opt-out): AGENTES_AUTOCRITICA=0.
VAR_ENTORNO = "AGENTES_AUTOCRITICA"


def autocritica_habilitada() -> bool:
    """``True`` salvo que ``AGENTES_AUTOCRITICA`` esté a 0/false/no."""
    valor = os.environ.get(VAR_ENTORNO)
    if valor is None:
        return True
    return str(valor).strip().lower() not in ("0", "false", "no", "off")


# ============================================================
# Datos
# ============================================================
@dataclass
class Critica:
    """Una evaluación del LLM que puede justificar una reescritura."""
    ejecucion_id: int
    alcance: str
    score: float
    justificacion: str
    agente_ejecucion_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ejecucion_id": self.ejecucion_id,
            "alcance": self.alcance,
            "score": round(float(self.score), 3),
            "justificacion": self.justificacion[:300],
            "agente_ejecucion_id": self.agente_ejecucion_id,
        }


@dataclass
class ResultadoAutocritica:
    ejecucion_id: int
    revisadas: int = 0
    reescrituras: list[int] = field(default_factory=list)
    saltadas: list[str] = field(default_factory=list)

    @property
    def reescribio(self) -> bool:
        return bool(self.reescrituras)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ejecucion_id": self.ejecucion_id,
            "revisadas": self.revisadas,
            "reescrituras": list(self.reescrituras),
            "saltadas": list(self.saltadas),
        }


# ============================================================
# SelfCritic
# ============================================================
class SelfCritic:
    """Convierte la auto-evaluación del LLM en candidatos de prompt.

    Args:
        db_path: BD de aprendizaje.
        feedback_processor: ``FeedbackProcessor`` ya construido (necesita LLM).
        umbral: score por debajo del cual el resultado merece crítica.
        max_por_ejecucion: tope de reescrituras automáticas por ejecución.
        presupuesto: ``BudgetManager`` opcional. Si está agotado, no se gasta
            una llamada de reescritura: es exactamente la información de
            «cuándo merece la pena» que aporta V3.8-2.
    """

    def __init__(
        self,
        db_path: str,
        feedback_processor,
        *,
        umbral: float = UMBRAL_CRITICA,
        max_por_ejecucion: int = MAX_POR_EJECUCION,
        presupuesto: Any = None,
    ):
        self.db_path = str(db_path)
        self.feedback_processor = feedback_processor
        self.umbral = float(umbral)
        self.max_por_ejecucion = max(0, int(max_por_ejecucion))
        self.presupuesto = presupuesto

    # ------------------------------------------------------------
    # Lectura
    # ------------------------------------------------------------
    def criticas_de(self, ejecucion_id: int) -> list[Critica]:
        """Evaluaciones del LLM de esa ejecución, de peor a mejor score."""
        criticas: list[Critica] = []
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")
                filas = conn.execute(
                    """SELECT ejecucion_id, alcance, score, justificacion,
                              agente_ejecucion_id
                       FROM evaluaciones_llm
                       WHERE ejecucion_id = ?
                       ORDER BY score ASC, id ASC""",
                    (ejecucion_id,),
                ).fetchall()
        except Exception as e:
            logger.debug(f"SelfCritic: no se pudieron leer evaluaciones: {e}")
            return []

        for fila in filas:
            try:
                score = float(fila["score"])
            except (TypeError, ValueError):
                continue
            criticas.append(
                Critica(
                    ejecucion_id=int(fila["ejecucion_id"]),
                    alcance=str(fila["alcance"] or "agente"),
                    score=score,
                    justificacion=str(fila["justificacion"] or "").strip(),
                    agente_ejecucion_id=fila["agente_ejecucion_id"],
                )
            )
        return criticas

    # ------------------------------------------------------------
    # Decisión
    # ------------------------------------------------------------
    def _firma_de_la_critica(self, critica: Critica) -> str:
        """Firma del prompt objetivo, para deduplicar contra el A/B."""
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")
                agente = self.feedback_processor._agente_relevante(
                    conn,
                    {
                        "agente_ejecucion_id": critica.agente_ejecucion_id,
                        "ejecucion_id": critica.ejecucion_id,
                    },
                )
                if not agente:
                    return ""
                crudo = self.feedback_processor._prompt_crudo(
                    agente["prompt_usado"]
                )
                return self.feedback_processor._firmar(crudo) if crudo else ""
        except Exception as e:
            logger.debug(f"SelfCritic: no se pudo calcular la firma: {e}")
            return ""

    def _ya_tiene_candidato(self, firma: str) -> bool:
        """¿Esa firma ya tiene un candidato esperando al A/B?

        Si lo tiene, otra reescritura solo tiraría una llamada al LLM y
        sustituiría al candidato que aún se está midiendo.
        """
        if not firma:
            return False
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                fila = conn.execute(
                    """SELECT 1 FROM prompts_reescritos
                       WHERE firma = ? AND estado = 'candidato' LIMIT 1""",
                    (firma,),
                ).fetchone()
            return fila is not None
        except Exception as e:
            logger.debug(f"SelfCritic: no se pudo consultar candidatos: {e}")
            return False

    def merece_reescritura(self, critica: Critica) -> tuple[bool, str]:
        """``(merece, motivo)``. El motivo se registra cuando NO merece."""
        if critica.score >= self.umbral:
            return False, f"score {critica.score:.2f} ≥ umbral {self.umbral:.2f}"
        if not critica.justificacion:
            return False, "sin justificación del evaluador"
        if self.presupuesto is not None:
            try:
                if self.presupuesto.agotado():
                    return False, "presupuesto agotado"
            except Exception as e:
                logger.debug(f"SelfCritic: presupuesto no consultable: {e}")
        firma = self._firma_de_la_critica(critica)
        if self._ya_tiene_candidato(firma):
            return False, "la firma ya tiene un candidato pendiente del A/B"
        return True, ""

    # ------------------------------------------------------------
    # Acción
    # ------------------------------------------------------------
    def _registrar_critica(self, critica: Critica, comentario: str) -> int:
        """Anota la auto-crítica como feedback de origen LLM.

        Se usa ``feedback_usuario`` porque es la tabla a la que apunta la FK de
        ``prompts_reescritos``. El marcador es ``alcance='auto_critica'``: deja
        auditoría del origen y, a propósito, **no** entra en ``exito_real``,
        porque el HistoricalIndexer solo lee ``alcance='plan'``.

        Ese aislamiento no es cosmético: el evaluador LLM puntúa en 0–1 (donde
        0.6 es mediocre) y el feedback del usuario en −1–1 (donde 0.6 es
        claramente positivo). Si la crítica se colara como feedback de usuario,
        un 0.6 mediocre se leería como veredicto positivo y podría convertir una
        ejecución fallida en «éxito» del corpus de retrieval.
        """
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                cur = conn.execute(
                    """INSERT INTO feedback_usuario
                       (ejecucion_id, agente_ejecucion_id, alcance, score,
                        comentario, fecha)
                       VALUES (?, ?, 'auto_critica', ?, ?, ?)""",
                    (
                        critica.ejecucion_id,
                        critica.agente_ejecucion_id,
                        float(critica.score),
                        comentario[:2000],
                        _ahora(),
                    ),
                )
                conn.commit()
                return int(cur.lastrowid or 0)
        except Exception as e:
            logger.debug(f"SelfCritic: no se pudo registrar la crítica: {e}")
            return 0

    def revisar_ejecucion(self, ejecucion_id: int) -> ResultadoAutocritica:
        """Revisa la ejecución y reescribe como mucho ``max_por_ejecucion``.

        Nunca lanza: si algo falla, se registra y se sigue.
        """
        resultado = ResultadoAutocritica(ejecucion_id=ejecucion_id)
        if not autocritica_habilitada():
            resultado.saltadas.append(f"auto-crítica desactivada ({VAR_ENTORNO})")
            return resultado
        if self.max_por_ejecucion <= 0:
            resultado.saltadas.append("tope por ejecución a 0")
            return resultado

        criticas = self.criticas_de(ejecucion_id)
        resultado.revisadas = len(criticas)

        for critica in criticas:
            if len(resultado.reescrituras) >= self.max_por_ejecucion:
                resultado.saltadas.append("tope de reescrituras por ejecución")
                break
            merece, motivo = self.merece_reescritura(critica)
            if not merece:
                resultado.saltadas.append(f"{critica.alcance}: {motivo}")
                continue

            comentario = (
                f"Auto-crítica del evaluador (score {critica.score:.2f}, "
                f"alcance {critica.alcance}): {critica.justificacion}"
            )
            feedback_id = self._registrar_critica(critica, comentario)
            if not feedback_id:
                resultado.saltadas.append("no se pudo registrar la crítica")
                continue

            salida = self.feedback_processor.procesar_critica(
                ejecucion_id=critica.ejecucion_id,
                comentario=comentario,
                agente_ejecucion_id=critica.agente_ejecucion_id,
                feedback_id=feedback_id,
                origen="auto-critica",
            )
            if salida.get("procesado"):
                resultado.reescrituras.append(int(salida["prompt_id"]))
                logger.info(
                    "🧠 Auto-crítica: prompt %s reescrito desde la evaluación "
                    "del LLM (score %.2f)", salida["prompt_id"], critica.score
                )
            else:
                resultado.saltadas.append(
                    f"{critica.alcance}: {salida.get('razon') or salida.get('error')}"
                )
        return resultado


def _ahora() -> str:
    from datetime import datetime

    return datetime.now().isoformat()


def obtener_self_critic(
    db_path: str,
    llm_client=None,
    *,
    umbral: float = UMBRAL_CRITICA,
    max_por_ejecucion: int = MAX_POR_EJECUCION,
    presupuesto: Any = None,
) -> SelfCritic | None:
    """Fábrica perezosa: ``None`` si no hay LLM o falta algo. Nunca lanza."""
    try:
        if llm_client is None:
            from core.llm_client import obtener_llm_client_compartido

            llm_client = obtener_llm_client_compartido()
        if llm_client is None:
            return None
        from .feedback_processor import FeedbackProcessor

        return SelfCritic(
            db_path,
            FeedbackProcessor(db_path, llm_client),
            umbral=umbral,
            max_por_ejecucion=max_por_ejecucion,
            presupuesto=presupuesto,
        )
    except Exception as e:
        logger.debug(f"SelfCritic no disponible: {e}")
        return None


__all__ = [
    "SelfCritic",
    "Critica",
    "ResultadoAutocritica",
    "obtener_self_critic",
    "autocritica_habilitada",
    "UMBRAL_CRITICA",
    "MAX_POR_EJECUCION",
    "VAR_ENTORNO",
]
