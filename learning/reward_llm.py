import json
import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

PROMPT_SISTEMA_EVALUADOR = """
Eres un evaluador de ejecuciones de agentes. 
Evalúa la calidad del resultado final en base al objetivo original.
Responde SOLO con JSON válido: {"score": 0.0-1.0, "justificacion": "texto breve"}
"""

# Motivo devuelto cuando el evaluador no tiene cliente LLM. Es un fallo de
# CONFIGURACIÓN, no de la respuesta del modelo, y debe poder distinguirse en
# los logs y en la justificación (ver 1.14 del ROADMAP).
MOTIVO_SIN_CLIENTE = (
    "Evaluador sin cliente LLM: aprendizaje por refuerzo degradado (score neutro)"
)


def resolver_cliente_compartido():
    """Resuelve el cliente LLM compartido del proceso, o ``None`` si no puede.

    Se importa en tiempo de llamada a propósito: ``core.llm_client`` importa
    partes del núcleo, y hacerlo a nivel de módulo acoplaría ``learning`` al
    núcleo ya en el import.
    """
    try:
        from core.llm_client import obtener_llm_client_compartido

        return obtener_llm_client_compartido()
    except Exception as e:  # ImportError, falta de API key, etc.
        logger.warning(f"No se pudo resolver el cliente LLM compartido: {e}")
        return None


@dataclass
class Evaluacion:
    """Resultado de la evaluación de un agente o plan."""
    score: float
    justificacion: str
    modelo: str | None = None

class EvaluadorLLM:
    def __init__(
        self,
        llm_client,
        modelo: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 500,   # ✅ subido de 200: thinking + JSON cabe
    ):
        self.llm_client = llm_client
        self.modelo = modelo
        self.temperature = temperature
        self.max_tokens = max_tokens

    def set_llm_client(self, llm_client) -> None:
        """Inyecta el cliente LLM (p. ej. tras ``obtener_llm_client_compartido()``).

        Necesario porque ``LearningEngine`` es un singleton perezoso: si la
        primera construcción ocurre sin cliente, el evaluador se queda sin él
        para todo el proceso (bug 1.14 del ROADMAP).
        """
        self.llm_client = llm_client

    def _cliente(self):
        """Devuelve el cliente, resolviéndolo de forma perezosa si falta."""
        if self.llm_client is None:
            self.llm_client = resolver_cliente_compartido()
        return self.llm_client

    def evaluar(self, objetivo: str, resultado: str, traza: str = "") -> Evaluacion:
        cliente = self._cliente()
        if cliente is None:
            # Fallo de CONFIGURACIÓN, no de la respuesta del modelo. Antes esto
            # caía en el `except` genérico y producía el críptico
            # "'NoneType' object has no attribute 'chat'" en nivel WARNING:
            # el aprendizaje quedaba degradado en silencio. Ahora es un ERROR
            # explícito y accionable.
            logger.error(
                "Evaluador LLM SIN CLIENTE: el aprendizaje por refuerzo queda "
                "degradado (score neutro 0.5). Causa habitual: se construyó "
                "LearningEngine/EvaluadorLLM sin llm_client. Pasa "
                "llm_client=obtener_llm_client_compartido() o llama a "
                "EvaluadorLLM.set_llm_client()."
            )
            return Evaluacion(
                score=0.5, justificacion=MOTIVO_SIN_CLIENTE, modelo=self.modelo
            )

        prompt = f"""
OBJETIVO ORIGINAL:
{objetivo}

RESULTADO OBTENIDO:
{resultado[:8000]}

TRAZA (opcional):
{traza[:4000]}

Evalúa de 0.0 a 1.0 qué tan bien se cumplió el objetivo.
Responde SOLO JSON: {{"score": <float>, "justificacion": "<por qué>"}}
"""
        try:
            kwargs = dict(
                prompt=prompt,
                system_prompt=PROMPT_SISTEMA_EVALUADOR,
                temperature=self.temperature,
                max_tokens=self.max_tokens,
                # ✅ CRÍTICO: sin thinking el LLM responde directo el JSON.
                # Con thinking activo, gasta tokens "pensando" y trunca la
                # respuesta antes de emitir el JSON.
                reasoning_effort="low",
                thinking_enabled=False,
            )
            if self.modelo:
                kwargs["model"] = self.modelo  # tu LLMClient usa 'model'
            respuesta = cliente.chat(**kwargs)
            data = self._parsear_json(respuesta)
            if not isinstance(data, dict):
                raise ValueError(f"El evaluador no devolvió un objeto JSON: {type(data)}")
            score = max(0.0, min(1.0, float(data.get("score", 0.5))))
            justificacion = str(data.get("justificacion", ""))[:500]
        except Exception as e:
            # Si el evaluador falla (API caída, JSON mal formado, etc.)
            # no bloqueamos el flujo: usamos un score neutro y seguimos.
            # ✅ Logueamos el raw para diagnosticar por qué falla.
            raw = locals().get("respuesta", "(sin respuesta)")
            logger.warning(
                f"Evaluador no disponible: {e}\n"
                f"Respuesta cruda ({len(raw) if isinstance(raw, str) else 0} chars): "
                f"{raw[:500] if isinstance(raw, str) else raw}"
            )
            score, justificacion = 0.5, f"Evaluador no disponible: {e}"

        return Evaluacion(score=score, justificacion=justificacion, modelo=self.modelo)

    @staticmethod
    def _parsear_json(texto: str) -> dict:
        """
        Extrae el JSON del evaluador con 4 estrategias de recuperación.

        Estrategias (en orden):
          1. JSON directo.
          2. Extraer el primer {...} balanceado.
          3. Reparar comas finales y comillas simples.
          4. Último recurso: regex sobre "score" y "justificacion"
             (recupera JSON truncado como `{"score": 0.8, "justificaci`).
        """
        if not texto or not texto.strip():
            raise ValueError("Respuesta vacía del evaluador")

        t = texto.strip()

        # Limpiar fences markdown
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
        t = t.strip()

        # ── Estrategia 1: JSON directo ──
        try:
            return json.loads(t)
        except json.JSONDecodeError:
            pass

        # ── Estrategia 2: primer {...} balanceado ──
        inicio = t.find("{")
        fin = t.rfind("}")
        candidato = None
        if inicio != -1 and fin != -1 and fin > inicio:
            candidato = t[inicio:fin + 1]
            try:
                return json.loads(candidato)
            except json.JSONDecodeError:
                pass

        # ── Estrategia 3: reparar comas finales y comillas simples ──
        if candidato:
            reparado = re.sub(r",\s*}", "}", candidato)
            reparado = re.sub(r",\s*]", "]", reparado)
            reparado = re.sub(r"'([^']*)'", r'"\1"', reparado)
            try:
                return json.loads(reparado)
            except json.JSONDecodeError:
                pass

        # ── Estrategia 4: regex de último recurso ──
        # Recupera score y justificacion incluso de JSON truncado.
        match_score = re.search(r'"score"\s*:\s*([\d.]+)', t)
        match_just = re.search(r'"justificacion"\s*:\s*"([^"]*)', t)
        if match_score:
            return {
                "score": float(match_score.group(1)),
                "justificacion": (
                    match_just.group(1) if match_just else "(sin justificación)"
                ),
            }

        raise ValueError(f"No se pudo parsear JSON: {t[:200]}")
