# core/executors/llm_executor.py
"""Ejecutor de agentes LLM (DeepSeek)."""

import json
import logging
import threading
import time

from core.agent import Agente
from core.cancellation import CancellationToken
from core.utils import es_valor_placeholder

from .cache import RateLimiter
from .content_extractor import sustituir_variables, variables_disponibles
from .helpers import es_resultado_sospechoso, limpiar_fences_markdown, parsear_json_robusto

logger = logging.getLogger(__name__)

MIN_TOKENS_SEGUROS = 4000

_rate_limiter: RateLimiter | None = None
_class_lock = threading.RLock()


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    with _class_lock:
        if _rate_limiter is None:
            _rate_limiter = RateLimiter()
        return _rate_limiter


# ============================================================
# TROCEADO DE PROMPTS GRANDES Y FUSIÓN DE RESPUESTAS
# ============================================================

# A partir de este tamaño, el prompt se manda en varias llamadas. El límite
# real es la salida del modelo: traducir ~20k chars no cabe en max_tokens.
MAX_PROMPT_CHARS = 12000
# Tiempo máximo de cada llamada: crece con el tamaño del prompt.
TIMEOUT_MIN = 60
TIMEOUT_MAX = 300
# Una variable sustituida mayor que esto justifica trocear por dependencia.
MIN_CHARS_VARIABLE_GRANDE = 4000
MARCADOR_OMITIDO = "[contenido omitido: se procesa en otra llamada]"

def json_util(datos) -> bool:
    """¿El JSON recibido aporta algo (no es None ni solo relleno)?"""
    if datos is None:
        return False
    return not es_valor_placeholder(datos)


def _fusionar_valor(a, b):
    """Combina dos valores del mismo campo de dos respuestas."""
    a_vacio, b_vacio = es_valor_placeholder(a), es_valor_placeholder(b)
    if b_vacio and not a_vacio:
        return a
    if a_vacio and not b_vacio:
        return b
    if isinstance(a, dict) and isinstance(b, dict):
        return fusionar_json(a, b)
    if isinstance(a, list) and isinstance(b, list):
        return _fusionar_listas(a, b)
    return a


def _fusionar_listas(a: list, b: list) -> list:
    """Une listas sin duplicar items (por su JSON) y sin rellenos."""
    resultado = []
    vistos = set()
    for item in list(a) + list(b):
        if es_valor_placeholder(item):
            continue
        try:
            clave = json.dumps(item, sort_keys=True, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            clave = str(item)
        if clave in vistos:
            continue
        vistos.add(clave)
        resultado.append(item)
    return resultado


def fusionar_json(a, b):
    """Fusiona dos respuestas JSON (misma estructura) en una sola."""
    if a is None:
        return b
    if b is None:
        return a
    if isinstance(a, dict) and isinstance(b, dict):
        fusion = dict(a)
        for clave, valor_b in b.items():
            fusion[clave] = (
                _fusionar_valor(fusion[clave], valor_b) if clave in fusion else valor_b
            )
        return fusion
    if isinstance(a, list) and isinstance(b, list):
        return _fusionar_listas(a, b)
    return _fusionar_valor(a, b)


def planificar_chunks(prompt_plantilla: str, variables: dict,
                      max_chars: int = MAX_PROMPT_CHARS,
                      min_grande: int = MIN_CHARS_VARIABLE_GRANDE) -> list[str]:
    """
    Si el prompt es grande, lo parte en una llamada por variable sustituida
    grande (manteniendo la instrucción completa en todas). Devuelve [] si no
    hace falta o no se puede trocear.
    """
    import re as _re

    claves = _re.findall(r"\{\s*([^{}\s]+)\s*\}", prompt_plantilla or "")
    presentes = [k for k in dict.fromkeys(claves) if k in (variables or {})]
    grandes = [k for k in presentes if len(str(variables[k])) >= min_grande]

    prompt_completo = sustituir_variables(prompt_plantilla, variables)
    if len(prompt_completo) <= max_chars or not grandes:
        return []

    if len(grandes) >= 2:
        # Una llamada por variable grande (la instrucción se repite entera).
        chunks = []
        for clave in grandes:
            variables_chunk = dict(variables)
            for otra in grandes:
                if otra != clave:
                    variables_chunk[otra] = MARCADOR_OMITIDO
            chunks.append(sustituir_variables(prompt_plantilla, variables_chunk))
        return chunks

    # Una sola variable grande: si es una lista JSON, se parte por items.
    clave = grandes[0]
    try:
        items = json.loads(variables[clave])
    except (TypeError, ValueError):
        return []
    if not isinstance(items, list) or len(items) < 2:
        return []

    chunks = []
    for grupo in _agrupar_items(items, max_chars // 2):
        variables_chunk = dict(variables)
        variables_chunk[clave] = json.dumps(grupo, ensure_ascii=False, default=str)
        chunks.append(sustituir_variables(prompt_plantilla, variables_chunk))
    return chunks


def _agrupar_items(items: list, max_chars_grupo: int) -> list[list]:
    """Agrupa items por tamaño aproximado de su JSON."""
    grupos: list[list] = []
    actual: list = []
    tam = 0
    for item in items:
        try:
            peso = len(json.dumps(item, ensure_ascii=False, default=str))
        except (TypeError, ValueError):
            peso = len(str(item))
        if actual and tam + peso > max_chars_grupo:
            grupos.append(actual)
            actual, tam = [], 0
        actual.append(item)
        tam += peso
    if actual:
        grupos.append(actual)
    return grupos


def timeout_para_prompt(contenido: str, agente=None) -> int:
    """Timeout de la llamada LLM: crece con el tamaño del prompt."""
    configurado = getattr(agente, 'timeout_llm', None) if agente is not None else None
    if configurado:
        try:
            return max(30, int(configurado))
        except (TypeError, ValueError):
            pass
    return max(TIMEOUT_MIN, min(TIMEOUT_MAX, 30 + len(contenido or "") // 300))


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
    def _llamar_una_vez(
        cls,
        agente,
        client,
        modelo: str,
        contenido_usuario: str,
        contexto: dict,
        se_sustituyo_algo: bool,
        temperatura: float,
        max_tokens: int,
        reasoning_effort: str,
        thinking_enabled: bool,
        cancellation_token: CancellationToken | None = None,
    ) -> tuple[bool, str, dict]:
        """
        Una única llamada al modelo, con cancelación y timeout.

        Devuelve (ok, mensaje, datos) con 'respuesta', 'finish_reason',
        'elapsed' y 'tokens' cuando va bien.
        """
        timeout_llamada = timeout_para_prompt(contenido_usuario, agente)

        messages = [
            {
                "role": "system",
                "content": (
                    "Eres un asistente útil y preciso. "
                    "Responde directamente con lo que se te pide, sin preámbulos."
                )
            },
            {"role": "user", "content": contenido_usuario},
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
                        contexto_visible, indent=2, default=str, ensure_ascii=False
                    )
                )

        response = None
        error = None
        completed = threading.Event()

        def hacer_llamada():
            nonlocal response, error
            try:
                response = client.chat.completions.create(
                    model=modelo,
                    messages=messages,
                    temperature=temperatura,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                    stream=False,
                    extra_body={
                        "thinking": {
                            "type": "enabled" if thinking_enabled else "disabled"
                        }
                    },
                    timeout=timeout_llamada,
                )
            except Exception as e:
                error = e
            finally:
                completed.set()

        start_time = time.time()
        thread = threading.Thread(target=hacer_llamada, daemon=True)
        thread.start()

        timeout = timeout_llamada
        while not completed.is_set():
            if cancellation_token and cancellation_token.esta_cancelado():
                return False, "Cancelado por usuario durante la llamada LLM", {
                    'error': 'cancelled', 'tiempo_espera': time.time() - start_time
                }
            if time.time() - start_time > timeout + 5:
                return False, f"Timeout en llamada LLM ({timeout}s)", {
                    'error': 'timeout', 'tiempo_espera': timeout
                }
            completed.wait(0.1)

        if error:
            raise error
        if response is None or not response.choices:
            raise RuntimeError("No se recibió respuesta de la API")

        eleccion = response.choices[0]
        respuesta = eleccion.message.content or ""
        elapsed = time.time() - start_time
        finish_reason = getattr(eleccion, "finish_reason", None)
        tokens = {
            'prompt': response.usage.prompt_tokens if response.usage else 0,
            'completion': response.usage.completion_tokens if response.usage else 0,
            'total': response.usage.total_tokens if response.usage else 0,
        }

        if not respuesta.strip():
            reasoning = getattr(eleccion.message, "reasoning_content", None)
            if reasoning:
                return False, (
                    "⚠️ LLM solo devolvió razonamiento (no respuesta final). "
                    f"Aumenta max_tokens (actual: {getattr(agente, 'max_tokens_llm', '?')}) "
                    f"o divide el paso."
                ), {
                    'error': 'only_reasoning', 'modelo': modelo,
                    'razonamiento_preview': reasoning[:200], 'tokens_uso': tokens,
                    'finish_reason': finish_reason,
                }
            return False, (
                f"LLM devolvió respuesta vacía (modelo: {modelo}, "
                f"tokens: {tokens.get('total', '?')})"
            ), {'error': 'empty_response', 'modelo': modelo,
                'finish_reason': finish_reason}

        return True, "", {
            'respuesta': respuesta, 'finish_reason': finish_reason,
            'elapsed': elapsed, 'tokens': tokens,
        }

    @classmethod
    def ejecutar(
        cls,
        agente: Agente,
        contexto: dict,
        cancellation_token: CancellationToken | None = None
    ) -> tuple[bool, str, dict]:
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

        modelo = getattr(agente, 'modelo_llm', 'deepseek-v4-pro')
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

            chunks = planificar_chunks(agente.prompt_llm, variables)
            if chunks:
                logger.info(
                    f"[{agente.nombre}] prompt de {len(prompt_procesado)} chars -> "
                    f"{len(chunks)} llamadas (una por dependencia grande)"
                )
                llamadas = []
                for i, chunk in enumerate(chunks, 1):
                    cabecera = (
                        f"(Parte {i} de {len(chunks)}. Responde SOLO con el JSON de los "
                        f"datos de esta parte, con el mismo esquema; donde falte "
                        f"contenido usa \"Sin contenido\".)\n\n"
                    )
                    llamadas.append(cabecera + chunk)
            else:
                llamadas = [prompt_procesado]

            pide_json = "json" in (agente.prompt_llm or "").lower()

            respuestas: list[str] = []
            json_fusionado = None
            finish_reasons: list = []
            tokens_total = {"prompt": 0, "completion": 0, "total": 0}
            tiempo_total = 0.0

            for idx, contenido in enumerate(llamadas, 1):
                cls.actualizar_progreso(
                    agente,
                    min(85, 40 + int(40 * idx / max(1, len(llamadas)))),
                    f"Consultando {modelo} ({idx}/{len(llamadas)})...",
                )
                ok, mensaje, datos = cls._llamar_una_vez(
                    agente=agente,
                    client=client,
                    modelo=modelo,
                    contenido_usuario=contenido,
                    contexto=contexto,
                    se_sustituyo_algo=se_sustituyo_algo,
                    temperatura=agente.temperatura_llm,
                    max_tokens=max_tokens_efectivos,
                    reasoning_effort=reasoning_effort,
                    thinking_enabled=thinking_enabled,
                    cancellation_token=cancellation_token,
                )
                if not ok:
                    if len(llamadas) > 1:
                        mensaje = f"[parte {idx}/{len(llamadas)}] {mensaje}"
                    cls.actualizar_progreso(agente, 100, mensaje[:60])
                    return False, mensaje, {
                        **datos, 'modelo': modelo, 'chunks': len(llamadas),
                    }

                respuesta_chunk = datos["respuesta"]
                respuestas.append(respuesta_chunk)
                finish_reasons.append(datos.get("finish_reason"))
                for clave in tokens_total:
                    tokens_total[clave] += datos.get("tokens", {}).get(clave, 0)
                tiempo_total += datos.get("elapsed", 0.0)

                json_chunk = parsear_json_robusto(
                    limpiar_fences_markdown(respuesta_chunk)
                )
                if datos.get("finish_reason") == "length" and not json_util(json_chunk):
                    cls.actualizar_progreso(agente, 100, "❌ Respuesta cortada")
                    return False, (
                        f"⚠️ La respuesta del LLM se cortó por max_tokens "
                        f"({agente.max_tokens_llm}) antes de completar el JSON "
                        f"(parte {idx}/{len(llamadas)}). Sube max_tokens o divide el paso."
                    ), {
                        'error': 'truncated_response', 'modelo': modelo,
                        'finish_reason': finish_reasons + [datos.get("finish_reason")],
                        'chunks': len(llamadas),
                    }
                if json_chunk is not None and not json_util(json_chunk):
                    cls.actualizar_progreso(agente, 100, "❌ JSON de plantilla")
                    return False, (
                        "⚠️ El LLM devolvió solo la plantilla (valores vacíos o '...') "
                        f"en la parte {idx}/{len(llamadas)}; no hay contenido que usar."
                    ), {
                        'error': 'json_placeholder', 'modelo': modelo,
                        'chunks': len(llamadas), 'json': json_chunk,
                    }
                if json_chunk is None and pide_json:
                    cls.actualizar_progreso(agente, 100, "❌ Sin JSON")
                    return False, (
                        f"⚠️ El LLM no devolvió JSON válido en la parte "
                        f"{idx}/{len(llamadas)}. Revisa el prompt."
                    ), {
                        'error': 'json_no_parseable', 'modelo': modelo,
                        'chunks': len(llamadas), 'raw_response': respuesta_chunk[:500],
                    }
                if json_chunk is not None:
                    json_fusionado = fusionar_json(json_fusionado, json_chunk)

            if "length" in [fr for fr in finish_reasons if fr] and not json_util(json_fusionado):
                cls.actualizar_progreso(agente, 100, "❌ Respuesta cortada")
                return False, (
                    f"⚠️ La respuesta del LLM se cortó por max_tokens "
                    f"({agente.max_tokens_llm}) antes de completar el JSON."
                ), {
                    'error': 'truncated_response', 'modelo': modelo,
                    'finish_reason': finish_reasons, 'chunks': len(llamadas),
                }

            if pide_json and not json_util(json_fusionado):
                cls.actualizar_progreso(agente, 100, "❌ JSON no utilizable")
                return False, (
                    "⚠️ El LLM no devolvió un JSON utilizable (vacío, plantilla o "
                    "sin contenido). El paso no puede continuar."
                ), {
                    'error': 'json_no_utilizable', 'modelo': modelo,
                    'chunks': len(llamadas),
                    'raw_response': (respuestas[-1] if respuestas else "")[:500],
                }

            if len(llamadas) == 1:
                respuesta = respuestas[0]
                respuesta_limpia = limpiar_fences_markdown(respuesta)
            else:
                respuesta = (
                    json.dumps(json_fusionado, ensure_ascii=False, indent=2)
                    if json_fusionado is not None else "\n\n".join(respuestas)
                )
                respuesta_limpia = respuesta

            json_auto = json_fusionado

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

            palabras_thinking = [
                "We need", "The user", "I need to", "Let me", "First,",
                "Okay,", "Alright,", "Hmm,", "So,", "Now,"
            ]
            respuesta_lower = respuesta.strip()[:50].lower()
            if any(respuesta_lower.startswith(p.lower()) for p in palabras_thinking) \
                    and len(respuesta) > 300:
                logger.warning(
                    f"⚠️ LLM parece haber devuelto razonamiento en lugar de "
                    f"respuesta. Modelo: {modelo}, Longitud: {len(respuesta)}"
                )

            resultado = {
                "modelo": modelo,
                "prompt": prompt_procesado,
                "respuesta": respuesta,
                "respuesta_limpia": respuesta_limpia,
                "json": json_auto,
                "_av_respuesta": respuesta,
                "_av_respuesta_limpia": respuesta_limpia,
                "_av_json": json_auto,
                "tiempo_respuesta": tiempo_total,
                "tokens_uso": tokens_total,
            }
            if len(llamadas) > 1:
                resultado["chunks"] = len(llamadas)
                resultado["finish_reasons"] = finish_reasons

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
