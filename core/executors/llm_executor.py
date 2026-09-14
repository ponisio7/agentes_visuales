# core/executors/llm_executor.py
"""Ejecutor de agentes LLM (DeepSeek)."""

import json
import time
import logging
import threading
from typing import Dict, Tuple, Optional

from core.agent import Agente
from core.cancellation import CancellationToken

from .helpers import limpiar_fences_markdown, parsear_json_robusto, es_resultado_sospechoso
from .content_extractor import variables_disponibles, sustituir_variables
from .cache import RateLimiter

logger = logging.getLogger(__name__)

MIN_TOKENS_SEGUROS = 4000

_rate_limiter: Optional[RateLimiter] = None
_class_lock = threading.RLock()


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    with _class_lock:
        if _rate_limiter is None:
            _rate_limiter = RateLimiter()
        return _rate_limiter


class LLMExecutor:
    """Llama a la API de DeepSeek con el prompt del agente."""

    @staticmethod
    def actualizar_progreso(agente, progreso, mensaje=""):
        agente.progreso = min(100, max(0, progreso))
        if mensaje:
            agente.mensaje = mensaje
        if hasattr(agente, '_bridge') and agente._bridge is not None:
            try:
                agente._bridge.agente_actualizado.emit(agente.id)
            except Exception:
                pass

    @classmethod
    def ejecutar(
        cls,
        agente: Agente,
        contexto: Dict,
        cancellation_token: Optional[CancellationToken] = None
    ) -> Tuple[bool, str, Dict]:
        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {'error': 'cancelled'}

        cls.actualizar_progreso(agente, 20, "Preparando prompt LLM...")

        if not agente.prompt_llm:
            cls.actualizar_progreso(agente, 100, "Prompt vacío")
            return False, "No hay prompt definido", {}

        try:
            import openai
        except ImportError:
            cls.actualizar_progreso(agente, 100, "openai no instalado")
            return False, "openai no instalado. Ejecuta: pip install openai", {}

        variables = variables_disponibles(agente, contexto)
        prompt_procesado = sustituir_variables(agente.prompt_llm, variables)
        se_sustituyo_algo = prompt_procesado != agente.prompt_llm

        try:
            from core.llm_client import LLMClient
            _cliente_llm = LLMClient()
            api_key = _cliente_llm.api_key
            base_url = _cliente_llm.base_url
        except Exception as e:
            cls.actualizar_progreso(agente, 100, "Error cargando config LLM")
            return False, (
                f"No se pudo inicializar el cliente LLM: {e}\n"
                f"Verifica la configuración de DEEPSEEK_API_KEY.\n"
                f"Prueba: python main.py --check-env"
            ), {'error': 'llm_client_init', 'detalle': str(e)}

        if not api_key:
            cls.actualizar_progreso(agente, 100, "Falta DEEPSEEK_API_KEY")
            return False, (
                "No se encontró DEEPSEEK_API_KEY en ninguna fuente.\n"
                "Prueba: python main.py --check-env"
            ), {'error': 'missing_api_key'}

        modelo = getattr(agente, 'modelo_llm', 'deepseek-v4-flash')
        reasoning_effort = getattr(agente, 'reasoning_effort_llm', 'low') or 'low'
        thinking_enabled = bool(getattr(agente, 'thinking_enabled_llm', False))

        max_tokens_efectivos = int(
            getattr(agente, 'max_tokens_llm', MIN_TOKENS_SEGUROS) or MIN_TOKENS_SEGUROS
        )
        if max_tokens_efectivos < MIN_TOKENS_SEGUROS:
            logger.warning(
                f"⚠️ [{agente.nombre}] max_tokens={max_tokens_efectivos} es bajo "
                f"para un modelo con thinking. Subiendo a {MIN_TOKENS_SEGUROS}."
            )
            max_tokens_efectivos = MIN_TOKENS_SEGUROS

        cls.actualizar_progreso(agente, 40, f"Consultando {modelo}...")

        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de llamar a la API", {'error': 'cancelled'}

        try:
            rate_limiter = get_rate_limiter()
            rate_limiter.wait()

            client = openai.OpenAI(api_key=api_key, base_url=base_url)

            messages = [
                {
                    "role": "system",
                    "content": (
                        "Eres un asistente útil y preciso. "
                        "Responde directamente con lo que se te pide, sin preámbulos."
                    )
                },
                {"role": "user", "content": prompt_procesado}
            ]

            if contexto and not se_sustituyo_algo:
                contexto_visible = {
                    k: v for k, v in contexto.items()
                    if k not in ('llm_base_url', 'llm_api_key', 'openai_api_key')
                }
                if contexto_visible:
                    messages[1]["content"] += (
                        "\n\nContexto adicional:\n" +
                        json.dumps(
                            contexto_visible, indent=2, default=str,
                            ensure_ascii=False
                        )
                    )

            cls.actualizar_progreso(agente, 60, "Esperando respuesta...")

            start_time = time.time()
            response = None
            error = None
            completed = threading.Event()

            def hacer_llamada():
                nonlocal response, error
                try:
                    response = client.chat.completions.create(
                        model=modelo,
                        messages=messages,
                        temperature=agente.temperatura_llm,
                        max_tokens=max_tokens_efectivos,
                        reasoning_effort=reasoning_effort,
                        stream=False,
                        extra_body={
                            "thinking": {
                                "type": "enabled" if thinking_enabled else "disabled"
                            }
                        },
                        timeout=60
                    )
                except Exception as e:
                    error = e
                finally:
                    completed.set()

            thread = threading.Thread(target=hacer_llamada, daemon=True)
            thread.start()

            timeout = 60
            inicio = time.time()
            while not completed.is_set():
                if cancellation_token and cancellation_token.esta_cancelado():
                    cls.actualizar_progreso(agente, 100, "⛔ Cancelado por usuario")
                    return False, (
                        "Cancelado por usuario durante la llamada LLM"
                    ), {
                        'error': 'cancelled', 'modelo': modelo,
                        'tiempo_espera': time.time() - inicio
                    }
                if time.time() - inicio > timeout + 5:
                    cls.actualizar_progreso(agente, 100, "⏱️ Timeout")
                    return False, f"Timeout en llamada LLM ({timeout}s)", {
                        'error': 'timeout', 'modelo': modelo,
                        'tiempo_espera': timeout
                    }
                completed.wait(0.1)

            if error:
                raise error
            if response is None or not response.choices:
                raise openai.APIError("No se recibió respuesta de la API")

            elapsed_time = time.time() - start_time
            respuesta = response.choices[0].message.content if response.choices else ""

            if not respuesta or not respuesta.strip():
                reasoning = getattr(
                    response.choices[0].message, 'reasoning_content', None
                )
                if reasoning:
                    tokens_info = {}
                    if response.usage:
                        tokens_info = {
                            'prompt': response.usage.prompt_tokens,
                            'completion': response.usage.completion_tokens,
                            'total': response.usage.total_tokens
                        }
                    return False, (
                        f"⚠️ LLM solo devolvió razonamiento (no respuesta final). "
                        f"Aumenta max_tokens (actual: {agente.max_tokens_llm}). "
                        f"Tokens usados: {tokens_info.get('total', '?')}"
                    ), {
                        'error': 'only_reasoning', 'modelo': modelo,
                        'razonamiento_preview': reasoning[:200],
                        'tokens_uso': tokens_info
                    }
                else:
                    return False, (
                        f"LLM devolvió respuesta vacía (modelo: {modelo}, "
                        f"tokens: {response.usage.total_tokens if response.usage else '?'})"
                    ), {'error': 'empty_response', 'modelo': modelo}

            prompt_lower = prompt_procesado.lower()
            pide_generar = any(
                kw in prompt_lower for kw in (
                    "genera", "escribe", "redacta", "crea", "mensaje",
                    "texto", "resumen", "explica", "describe", "elabora",
                    "lista", "enumera", "traduce"
                )
            )

            if pide_generar and len(respuesta.strip()) < 3:
                tokens_usados = response.usage.total_tokens if response.usage else None
                return False, (
                    f"⚠️ El LLM devolvió una respuesta truncada: '{respuesta}'\n"
                    f"Esto ocurre cuando max_tokens es demasiado bajo "
                    f"(actual: {agente.max_tokens_llm}) o el modelo gastó "
                    f"tokens en razonamiento.\n"
                    f"Sugerencia: aumenta max_tokens a 500 o más."
                ), {
                    'error': 'truncated_response', 'modelo': modelo,
                    'respuesta_truncada': respuesta,
                    'max_tokens': agente.max_tokens_llm,
                    'tokens_usados': tokens_usados
                }

            palabras_thinking = [
                "We need", "The user", "I need to", "Let me", "First,",
                "Okay,", "Alright,", "Hmm,", "So,", "Now,"
            ]
            respuesta_lower = respuesta.strip()[:50].lower()
            es_razonamiento = any(
                respuesta_lower.startswith(p.lower()) for p in palabras_thinking
            )
            if es_razonamiento and len(respuesta) > 300:
                logger.warning(
                    f"⚠️ LLM parece haber devuelto razonamiento en lugar "
                    f"de respuesta. Modelo: {modelo}, Longitud: {len(respuesta)}, "
                    f"Inicio: {respuesta[:100]}"
                )

            cls.actualizar_progreso(agente, 90, "Procesando respuesta...")

            respuesta_limpia = limpiar_fences_markdown(respuesta)
            json_auto = parsear_json_robusto(respuesta_limpia)

            if json_auto is None and respuesta_limpia.strip().startswith(('{', '[')):
                error_msg = (
                    f"La respuesta del LLM parece ser un JSON truncado "
                    f"({len(respuesta_limpia)} caracteres recibidos). "
                    f"Aumenta 'max_tokens_llm' (actual: {agente.max_tokens_llm}) "
                    f"o desactiva el modo 'thinking' en este agente."
                )
                logger.error(f"[{agente.nombre}] {error_msg}")
                cls.actualizar_progreso(agente, 100, "❌ JSON truncado")
                return False, error_msg, {
                    'error': 'truncated_json', 'modelo': modelo,
                    'raw_response': respuesta[:500],
                    'total_length': len(respuesta),
                    'max_tokens_actual': agente.max_tokens_llm,
                }

            resultado = {
                "modelo": modelo,
                "prompt": prompt_procesado,
                "respuesta": respuesta,
                "respuesta_limpia": respuesta_limpia,
                "json": json_auto,
                "_av_respuesta": respuesta,
                "_av_respuesta_limpia": respuesta_limpia,
                "_av_json": json_auto,
                "tiempo_respuesta": elapsed_time,
                "tokens_uso": {
                    "prompt": response.usage.prompt_tokens if response.usage else 0,
                    "completion": response.usage.completion_tokens if response.usage else 0,
                    "total": response.usage.total_tokens if response.usage else 0
                }
            }

            es_sospechoso, razon = es_resultado_sospechoso(
                json_auto if json_auto is not None else respuesta_limpia
            )
            if es_sospechoso and len(respuesta_limpia) < 20:
                logger.warning(f"[{agente.nombre}] Resultado sospechoso: {razon}")

            cls.actualizar_progreso(agente, 100, "LLM respondió")
            preview = respuesta_limpia[:200] if len(respuesta_limpia) > 20 else respuesta[:200]
            return True, f"DeepSeek respondió: {preview}...", resultado

        except openai.APIError as e:
            cls.actualizar_progreso(agente, 100, f"Error API: {str(e)[:50]}")
            return False, f"Error de API de DeepSeek: {str(e)}", {
                'error': 'api_error', 'detalle': str(e)
            }
        except openai.APIConnectionError as e:
            cls.actualizar_progreso(agente, 100, "Error de conexión")
            return False, f"Error de conexión con DeepSeek: {str(e)}", {
                'error': 'connection_error', 'detalle': str(e)
            }
        except openai.APITimeoutError as e:
            cls.actualizar_progreso(agente, 100, "Timeout")
            return False, f"Timeout en DeepSeek: {str(e)}", {
                'error': 'timeout', 'detalle': str(e)
            }
        except openai.RateLimitError as e:
            cls.actualizar_progreso(agente, 100, "Rate limit")
            return False, f"Rate limit excedido en DeepSeek: {str(e)}", {
                'error': 'rate_limit', 'detalle': str(e)
            }
        except Exception as e:
            cls.actualizar_progreso(agente, 100, f"Error: {str(e)[:50]}")
            return False, f"Error en DeepSeek: {str(e)}", {
                'error': 'unexpected', 'detalle': str(e)
            }
