# core/executors/http_executor.py
"""Ejecutor de agentes HTTP."""

import json
import time
import logging
import threading
from typing import Dict, Tuple, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from core.agent import Agente
from core.cancellation import CancellationToken

from .security import (
    MAX_HTTP_BODY_SIZE, ALLOWED_HTTP_METHODS, validar_url
)
from .content_extractor import variables_disponibles, sustituir_variables
from .cache import HTTPCache, RateLimiter

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Agentes-Visuales/1.0"

# Singletons a nivel de módulo
_http_cache: Optional[HTTPCache] = None
_rate_limiter: Optional[RateLimiter] = None
_class_lock = threading.RLock()


def get_http_cache() -> HTTPCache:
    global _http_cache
    with _class_lock:
        if _http_cache is None:
            _http_cache = HTTPCache()
        return _http_cache


def get_rate_limiter() -> RateLimiter:
    global _rate_limiter
    with _class_lock:
        if _rate_limiter is None:
            _rate_limiter = RateLimiter()
        return _rate_limiter


class HTTPExecutor:
    """Realiza peticiones HTTP con caché, rate limiting y cancelación."""

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
            return False, "Cancelado antes de ejecutar", {
                'error': 'cancelled', 'cancelled': True,
                'status_code': None, 'headers': {}, 'body': '',
                'json': None, 'url': '', 'elapsed': 0
            }

        cls.actualizar_progreso(agente, 10, "Preparando petición HTTP...")

        variables = variables_disponibles(agente, contexto)

        url_raw = agente.url_http or ""
        url = sustituir_variables(url_raw, variables)

        if not url or not url.strip():
            cls.actualizar_progreso(agente, 100, "URL vacía")
            return False, "No hay URL definida", {
                'error': 'empty_url', 'status_code': None, 'headers': {},
                'body': '', 'json': None, 'url': '', 'elapsed': 0
            }

        if not validar_url(url):
            cls.actualizar_progreso(agente, 100, "URL inválida")
            return False, f"URL inválida: {url}", {
                'error': 'invalid_url', 'status_code': None, 'headers': {},
                'body': '', 'json': None, 'url': url[:100], 'elapsed': 0
            }

        metodo = agente.metodo_http.upper() if agente.metodo_http else "GET"
        if metodo not in ALLOWED_HTTP_METHODS:
            cls.actualizar_progreso(agente, 100, f"Método no soportado: {metodo}")
            return False, f"Método HTTP no soportado: {metodo}", {
                'error': 'unsupported_method', 'status_code': None,
                'headers': {}, 'body': '', 'json': None,
                'url': url[:100], 'elapsed': 0
            }

        headers = {}
        if agente.headers_http:
            for k, v in agente.headers_http.items():
                if k and v:
                    try:
                        headers[k] = sustituir_variables(str(v), variables)
                    except Exception as e:
                        logger.warning(f"Error sustituyendo header '{k}': {e}")
                        headers[k] = str(v)

        if 'User-Agent' not in headers:
            headers['User-Agent'] = DEFAULT_USER_AGENT
        if 'Accept' not in headers:
            headers['Accept'] = 'application/json, */*'

        body_texto_raw = agente.body_http or ""
        body_texto = sustituir_variables(body_texto_raw, variables)

        body = None
        content_type = headers.get('Content-Type', '')

        if body_texto and metodo in ("POST", "PUT", "PATCH"):
            if 'application/json' in content_type.lower() or body_texto.strip().startswith(('{', '[')):
                try:
                    body = json.loads(body_texto)
                except json.JSONDecodeError:
                    body = body_texto
                    if 'Content-Type' not in headers:
                        headers['Content-Type'] = 'text/plain'
            else:
                body = body_texto
                if 'Content-Type' not in headers:
                    headers['Content-Type'] = 'text/plain'

        timeout = getattr(agente, 'timeout_http', 30)
        if timeout < 1:
            timeout = 30
            logger.warning(f"Timeout HTTP inválido ({agente.timeout_http}), usando 30s")

        cls.actualizar_progreso(agente, 30, f"{metodo} {url[:60]}...")

        http_cache = get_http_cache()
        cached_result = http_cache.get(url, metodo, headers, body)
        if cached_result is not None:
            cls.actualizar_progreso(agente, 100, "✅ Respuesta desde caché")
            return True, "Respuesta desde caché", cached_result

        rate_limiter = get_rate_limiter()
        rate_limiter.wait()

        cls.actualizar_progreso(agente, 50, "Enviando petición...")

        session = None
        response = None
        error = None
        completed = threading.Event()
        cancelar_peticion = None

        try:
            session = requests.Session()
            retry = Retry(
                total=2, backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
            )
            adapter = HTTPAdapter(max_retries=retry)
            session.mount('http://', adapter)
            session.mount('https://', adapter)

            def cancelar_peticion(token):
                nonlocal session
                try:
                    if session:
                        session.close()
                except Exception as e:
                    logger.warning(f"Error cancelando petición HTTP: {e}")

            if cancellation_token:
                cancellation_token.agregar_callback(cancelar_peticion)

            def hacer_peticion():
                nonlocal response, error
                try:
                    kwargs = {
                        'headers': headers, 'timeout': timeout,
                        'allow_redirects': True, 'verify': True,
                    }
                    if metodo in ("POST", "PUT", "PATCH"):
                        if isinstance(body, dict):
                            kwargs['json'] = body
                        else:
                            kwargs['data'] = body

                    if metodo == "GET":
                        response = session.get(url, **kwargs)
                    elif metodo == "POST":
                        response = session.post(url, **kwargs)
                    elif metodo == "PUT":
                        response = session.put(url, **kwargs)
                    elif metodo == "DELETE":
                        response = session.delete(url, **kwargs)
                    elif metodo == "PATCH":
                        response = session.patch(url, **kwargs)
                    elif metodo == "HEAD":
                        response = session.head(url, **kwargs)
                    elif metodo == "OPTIONS":
                        response = session.options(url, **kwargs)
                except Exception as e:
                    error = e
                finally:
                    completed.set()

            thread = threading.Thread(target=hacer_peticion, daemon=True)
            thread.start()

            inicio = time.time()
            while not completed.is_set():
                if cancellation_token and cancellation_token.esta_cancelado():
                    session.close()
                    cls.actualizar_progreso(agente, 100, "⛔ Cancelado por usuario")
                    return False, "Cancelado por usuario", {
                        'error': 'cancelled', 'cancelled': True,
                        'status_code': None, 'headers': {}, 'body': '',
                        'json': None, 'url': url[:100],
                        'elapsed': time.time() - inicio
                    }
                if time.time() - inicio > timeout + 5:
                    session.close()
                    cls.actualizar_progreso(agente, 100, "⏱️ Timeout")
                    return False, f"Timeout en petición HTTP ({timeout}s)", {
                        'error': 'timeout', 'status_code': None, 'headers': {},
                        'body': '', 'json': None, 'url': url[:100],
                        'elapsed': timeout
                    }
                completed.wait(0.05)

            if error:
                raise error
            if response is None:
                raise requests.exceptions.RequestException("No se recibió respuesta")

        except requests.exceptions.Timeout:
            cls.actualizar_progreso(agente, 100, "⏱️ Timeout")
            return False, f"Timeout en petición HTTP ({timeout}s)", {
                'error': 'timeout', 'status_code': None, 'headers': {},
                'body': '', 'json': None, 'url': url[:100], 'elapsed': timeout
            }
        except requests.exceptions.ConnectionError as e:
            cls.actualizar_progreso(agente, 100, "❌ Error de conexión")
            return False, f"Error de conexión: {url}", {
                'error': 'connection_error', 'status_code': None,
                'headers': {}, 'body': '', 'json': None,
                'url': url[:100], 'elapsed': 0, 'detail': str(e)
            }
        except requests.exceptions.SSLError as e:
            cls.actualizar_progreso(agente, 100, "❌ Error SSL")
            return False, f"Error SSL en: {url}", {
                'error': 'ssl_error', 'status_code': None, 'headers': {},
                'body': '', 'json': None, 'url': url[:100],
                'elapsed': 0, 'detail': str(e)
            }
        except requests.exceptions.TooManyRedirects as e:
            cls.actualizar_progreso(agente, 100, "❌ Demasiadas redirecciones")
            return False, f"Demasiadas redirecciones en: {url}", {
                'error': 'too_many_redirects', 'status_code': None,
                'headers': {}, 'body': '', 'json': None,
                'url': url[:100], 'elapsed': 0, 'detail': str(e)
            }
        except requests.exceptions.RequestException as e:
            cls.actualizar_progreso(agente, 100, f"❌ Error: {str(e)[:50]}")
            return False, f"Error en petición HTTP: {str(e)}", {
                'error': 'request_exception', 'status_code': None,
                'headers': {}, 'body': '', 'json': None,
                'url': url[:100], 'elapsed': 0, 'detail': str(e)
            }
        except Exception as e:
            cls.actualizar_progreso(agente, 100, f"❌ Error inesperado: {str(e)[:50]}")
            logger.exception(f"Error inesperado en HTTP: {e}")
            return False, f"Error inesperado en HTTP: {str(e)}", {
                'error': 'unexpected', 'status_code': None, 'headers': {},
                'body': '', 'json': None, 'url': url[:100],
                'elapsed': 0, 'detail': str(e)
            }
        finally:
            if cancellation_token and cancelar_peticion:
                cancellation_token.eliminar_callback(cancelar_peticion)

        cls.actualizar_progreso(agente, 80, f"📥 Procesando respuesta {response.status_code}...")

        body_size = len(response.content)
        body_truncated = body_size > MAX_HTTP_BODY_SIZE

        try:
            body_text = response.text
        except UnicodeDecodeError:
            import base64
            body_text = (
                f"[BINARY DATA: "
                f"{base64.b64encode(response.content).decode('ascii')[:100]}...]"
            )

        if body_truncated:
            body_text = body_text[:10000] + "\n... (truncado)"

        json_data = None
        if response.headers.get('Content-Type', '').startswith('application/json'):
            try:
                json_data = response.json()
            except (ValueError, json.JSONDecodeError):
                try:
                    json_data = json.loads(body_text[:10000])
                except (ValueError, json.JSONDecodeError):
                    pass

        resultado = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": body_text,
            "body_truncated": body_truncated,
            "body_size": body_size,
            "json": json_data,
            "url": response.url,
            "elapsed": response.elapsed.total_seconds(),
            "error": None,
        }

        if 200 <= response.status_code < 400:
            http_cache.put(url, metodo, headers, body, resultado)

        cls.actualizar_progreso(agente, 100, f"✅ Completado (status: {response.status_code})")

        if 200 <= response.status_code < 300:
            mensaje = f"Petición exitosa (status: {response.status_code})"
            if json_data is not None:
                if isinstance(json_data, dict):
                    claves = list(json_data.keys())[:3]
                    mensaje += f" - JSON claves: {claves}"
                elif isinstance(json_data, list):
                    mensaje += f" - JSON array: {len(json_data)} items"
            return True, mensaje, resultado
        else:
            mensaje = f"Petición falló (status: {response.status_code})"
            if json_data is not None and isinstance(json_data, dict):
                error_msg = (
                    json_data.get('error') or
                    json_data.get('message') or
                    json_data.get('detail')
                )
                if error_msg:
                    mensaje += f" - {error_msg[:100]}"
            return False, mensaje, resultado