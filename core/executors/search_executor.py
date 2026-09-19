# core/executors/search_executor.py
"""
Ejecutor de agentes Search: búsqueda web sin API key.

Usa DuckDuckGo a través de la librería ``duckduckgo-search`` (renombrada a
``ddgs`` en versiones recientes; el import es tolerante a ambos nombres).

Contrato de salida (siempre presente, también en error):

    {
        "query": str,                 # consulta ejecutada
        "resultados": [               # lista de resultados
            {"title": str, "href": str, "body": str},
            ...
        ],
        "total": int,                 # número de resultados devueltos
        "error": str | None,          # None si todo fue bien
        "duracion": float,            # segundos
    }

Es un executor genérico: no conoce ningún dominio concreto. La consulta
puede contener referencias a dependencias con la sintaxis ``{Agente.clave}``,
que se sustituyen antes de buscar (igual que en los agentes HTTP/Shell).
"""

import logging
import time

from core.agent import Agente
from core.cancellation import CancellationToken

from .content_extractor import sustituir_variables, variables_disponibles

logger = logging.getLogger(__name__)

# El paquete se renombró: se acepta cualquiera de los dos nombres.
try:  # pragma: no cover - depende del entorno
    from ddgs import DDGS
except ImportError:  # pragma: no cover
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        DDGS = None

DEFAULT_REGION = "wt-wt"
DEFAULT_MAX_RESULTADOS = 5
DEFAULT_TIMEOUT = 30
MAX_RESULTADOS = 50


def _resultado_vacio(query: str, error: str | None = None, duracion: float = 0.0) -> dict:
    """Construye el contrato de salida vacío (con error opcional)."""
    return {
        "query": query,
        "resultados": [],
        "total": 0,
        "error": error,
        "duracion": duracion,
    }


def _soporta_backend() -> bool:
    """¿La versión instalada acepta el parámetro 'backend' en text()?"""
    if DDGS is None:
        return False
    try:
        import inspect
        return "backend" in inspect.signature(DDGS.text).parameters
    except (TypeError, ValueError):
        return False


class SearchExecutor:
    """Busca en la web y devuelve una lista de resultados."""

    @staticmethod
    def actualizar_progreso(agente, progreso: int, mensaje: str = ""):
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
        contexto: dict,
        cancellation_token: CancellationToken | None = None
    ) -> tuple[bool, str, dict]:
        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", _resultado_vacio(
                getattr(agente, 'query_search', '') or '', error='cancelled'
            )

        inicio = time.time()

        if DDGS is None:
            cls.actualizar_progreso(agente, 100, "Search no disponible")
            return False, (
                "La búsqueda web no está disponible: instala "
                "'duckduckgo-search' (pip install duckduckgo-search)"
            ), _resultado_vacio('', error='search_unavailable')

        # ── Consulta (con sustitución de {Dependencia.clave}) ──
        variables = variables_disponibles(agente, contexto)
        query = sustituir_variables(getattr(agente, 'query_search', '') or "", variables)
        query = query.strip()

        if not query:
            cls.actualizar_progreso(agente, 100, "Consulta vacía")
            return False, "Search: 'query_search' está vacía", _resultado_vacio(
                '', error='empty_query'
            )

        region = (getattr(agente, 'region_search', '') or DEFAULT_REGION).strip()
        try:
            max_resultados = int(getattr(agente, 'max_resultados_search', None) or DEFAULT_MAX_RESULTADOS)
        except (TypeError, ValueError):
            max_resultados = DEFAULT_MAX_RESULTADOS
        max_resultados = max(1, min(max_resultados, MAX_RESULTADOS))

        try:
            timeout = int(getattr(agente, 'timeout_search', None) or DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_TIMEOUT
        timeout = max(1, timeout)

        cls.actualizar_progreso(agente, 20, f"Buscando: {query[:60]}...")
        logger.info(f"Search '{agente.nombre}': query={query!r} max={max_resultados} region={region!r}")

        resultados: list[dict] = []
        error: str | None = None

        # Algunos backends de DuckDuckGo pueden no estar accesibles desde
        # una red concreta (DNS/proxies) y devuelven 0 resultados sin error.
        # Se intenta el backend automático y, si no da nada, el backend
        # 'html' (el endpoint clásico de DuckDuckGo). Todo genérico.
        backends: list[str | None] = [None]
        if _soporta_backend():
            backends = ["auto", "html"]

        for backend in backends:
            try:
                with DDGS(timeout=timeout) as buscador:
                    kwargs: dict = {
                        "region": region or None,
                        "max_results": max_resultados,
                    }
                    if backend:
                        kwargs["backend"] = backend
                    crudos = buscador.text(query, **kwargs) or []
                for item in crudos:
                    if not isinstance(item, dict):
                        continue
                    resultados.append({
                        "title": str(item.get("title", "") or ""),
                        "href": str(item.get("href", "") or item.get("url", "") or ""),
                        "body": str(item.get("body", "") or item.get("snippet", "") or ""),
                    })
                error = None
                if resultados:
                    break
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                logger.warning(
                    f"Search '{agente.nombre}' falló con backend={backend!r}: {error}"
                )

        duracion = time.time() - inicio

        if cancellation_token and cancellation_token.esta_cancelado():
            cls.actualizar_progreso(agente, 100, "Cancelado")
            return False, "Cancelado durante la búsqueda", {
                "query": query, "resultados": resultados,
                "total": len(resultados), "error": "cancelled",
                "duracion": duracion,
            }

        if error is not None:
            cls.actualizar_progreso(agente, 100, "Error en la búsqueda")
            return False, f"Search falló: {error}", {
                "query": query, "resultados": resultados,
                "total": len(resultados), "error": error,
                "duracion": duracion,
            }

        cls.actualizar_progreso(agente, 100, f"{len(resultados)} resultados")
        return True, f"Search: {len(resultados)} resultados para '{query[:60]}'", {
            "query": query,
            "resultados": resultados,
            "total": len(resultados),
            "error": None,
            "duracion": duracion,
        }
