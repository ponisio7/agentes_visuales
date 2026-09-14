"""
learning/reward_llm.py
Genera la señal de recompensa mediante auto-evaluación de un LLM: se le
muestra al modelo la tarea que debía cumplir un agente (o un plan
completo) y el resultado que produjo, y se le pide una puntuación
0.0-1.0 con una justificación breve.

Esta es la única fuente de recompensa: no hay calificación manual del
usuario ni heurísticas automáticas aparte del propio 'estado' que ya
guardaba la BD (ese se usa solo como etiqueta dura para el
FailurePredictor).
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import json
import re
import logging

logger = logging.getLogger(__name__)


@dataclass
class Evaluacion:
    score: float          # 0.0 (muy malo) - 1.0 (excelente)
    justificacion: str
    modelo: str


PROMPT_SISTEMA_EVALUADOR = (
    "Eres un evaluador estricto de resultados de agentes automatizados. "
    "Te doy la tarea que debía cumplir un agente (o un plan completo) y "
    "el resultado que produjo. Devuelve SOLO un JSON con este formato "
    'exacto: {"score": <numero entre 0.0 y 1.0>, "justificacion": '
    '"<una frase breve>"}\n'
    "IMPORTANTE: usa TODA la escala, no solo 0, 0.5 y 1. "
    "Guía de calibración:\n"
    "  0.0  = resultado vacío, error, o irrelevante\n"
    "  0.2  = intentó pero falló en lo esencial\n"
    "  0.4  = parcialmente correcto pero con defectos graves\n"
    "  0.5  = cumplió a medias, sin destacar\n"
    "  0.6  = correcto pero mejorable\n"
    "  0.8  = correcto y útil, con detalles menores\n"
    "  1.0  = perfecto, cumplió exactamente lo pedido\n"
    "No devuelvas nada más que el JSON, sin texto adicional ni backticks."
)


class EvaluadorLLM:
    """
    Envuelve el LLMClient que ya existe en el proyecto (core/llm_client).
    No depende de una clase concreta: solo necesita un objeto con un
    método .chat(prompt=..., system_prompt=..., temperature=...,
    max_tokens=...).
    """

    def __init__(
        self,
        llm_client,
        modelo: Optional[str] = None,
        temperature: float = 0.0,
        max_tokens: int = 200,
    ):
        self.llm_client = llm_client
        self.modelo = modelo
        self.temperature = temperature
        self.max_tokens = max_tokens

    def evaluar(self, tarea: str, resultado: str) -> Evaluacion:
        prompt = (
            f"TAREA DEL AGENTE:\n{tarea}\n\n"
            f"RESULTADO PRODUCIDO:\n{resultado[:4000]}\n"
        )
        try:
            kwargs = dict(
                prompt=prompt,
                system_prompt=PROMPT_SISTEMA_EVALUADOR,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
            )
            if self.modelo:
                kwargs["model"] = self.modelo  # tu LLMClient usa 'model'
            respuesta = self.llm_client.chat(**kwargs)
            data = self._parsear_json(respuesta)
            score = max(0.0, min(1.0, float(data.get("score", 0.5))))
            justificacion = str(data.get("justificacion", ""))[:500]
        except Exception as e:
            # Si el evaluador falla (API caída, JSON mal formado, etc.)
            # no bloqueamos el flujo: usamos un score neutro y seguimos.
            logger.debug(f"Evaluador no disponible: {e}")
            score, justificacion = 0.5, f"Evaluador no disponible: {e}"

        return Evaluacion(
            score=score,
            justificacion=justificacion,
            modelo=self.modelo or "desconocido",
        )

    @staticmethod
    def _parsear_json(texto: str) -> dict:
        texto = texto.strip()
        texto = re.sub(r"^```json\s*|```\s*$", "", texto).strip()
        match = re.search(r"\{.*\}", texto, re.DOTALL)
        if match:
            texto = match.group(0)
        return json.loads(texto)