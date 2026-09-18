# core/executors/dispatcher.py
"""
Dispatcher de ejecutores por tipo de agente.

AgentExecutor ya NO contiene la lógica de cada tipo: solo enruta
al ejecutor correspondiente.
"""

import logging
import time

from core.agent import Agente, TipoAgente
from core.cancellation import CancellationToken

from .file_executor import FileExecutor
from .http_executor import HTTPExecutor
from .llm_executor import LLMExecutor
from .loop_executor import LoopExecutor
from .python_executor import PythonExecutor
from .shell_executor import ShellExecutor

logger = logging.getLogger(__name__)


class AgentExecutor:
    """Ejecuta agentes delegando en el ejecutor específico por tipo."""

    EXECUTORES = {
        TipoAgente.PYTHON: PythonExecutor,
        TipoAgente.SHELL: ShellExecutor,
        TipoAgente.HTTP: HTTPExecutor,
        TipoAgente.LLM: LLMExecutor,
        TipoAgente.FILE: FileExecutor,
        TipoAgente.LOOP: LoopExecutor,
    }

    @staticmethod
    def _actualizar_progreso(agente: Agente, progreso: int, mensaje: str = ""):
        """Helper compartido. Delega en el ejecutor específico si está disponible."""
        agente.progreso = min(100, max(0, progreso))
        if mensaje:
            agente.mensaje = mensaje
        if hasattr(agente, '_bridge') and agente._bridge is not None:
            try:
                agente._bridge.agente_actualizado.emit(agente.id)
            except Exception as e:
                logger.debug(f"Error emitiendo señal de progreso: {e}")

    @classmethod
    def ejecutar(
        cls,
        agente: Agente,
        contexto: dict = None,
        cancellation_token: CancellationToken | None = None
    ) -> tuple[bool, str, dict]:
        """
        Ejecuta un agente según su tipo.
        """
        contexto = contexto or {}
        start_time = time.time()

        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de iniciar", {'error': 'cancelled'}

        try:
            es_valido, mensaje_error = agente.validar_configuracion()
            if not es_valido:
                return False, f"Configuración inválida: {mensaje_error}", {
                    'error': mensaje_error
                }

            tipo = agente.tipo
            ejecutor = cls.EXECUTORES.get(tipo)
            if ejecutor is None:
                return False, f"Tipo de agente no soportado: {tipo}", {}

            return ejecutor.ejecutar(agente, contexto, cancellation_token)

        except Exception as e:
            execution_time = time.time() - start_time
            logger.exception(f"Error en ejecución de {agente.nombre}")
            return False, f"Error crítico: {str(e)}", {
                'error': str(e),
                'tipo': type(e).__name__,
                'duracion': execution_time
            }

    # ── Helpers públicos que antes estaban en el monolito ──

    @classmethod
    def probar_loop(cls, agente: Agente, items):
        return LoopExecutor.probar_loop(agente, items)

    @classmethod
    def clear_http_cache(cls):
        from .http_executor import get_http_cache
        get_http_cache().clear()

    @classmethod
    def get_http_cache_stats(cls) -> dict:
        from .http_executor import get_http_cache
        return get_http_cache().get_stats()

    @classmethod
    def get_status(cls) -> dict:
        return {
            'http_cache': cls.get_http_cache_stats(),
            'rate_limiter': 'active',
        }


# ============================================================
# LIMPIEZA AL FINALIZAR
# ============================================================

import atexit


@atexit.register
def _cleanup_executor():
    try:
        AgentExecutor.clear_http_cache()
    except Exception:
        pass
