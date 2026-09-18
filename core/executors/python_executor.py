# core/executors/python_executor.py
"""Ejecutor de agentes Python."""

import logging
import time

from core.agent import Agente
from core.cancellation import CancellationToken
from core.sandbox import PythonSandbox, SandboxError

from .helpers import es_resultado_sospechoso
from .security import MAX_CODIGO_LENGTH

logger = logging.getLogger(__name__)


class PythonExecutor:
    """Ejecuta código Python en un subproceso aislado."""

    @staticmethod
    def actualizar_progreso(agente: Agente, progreso: int, mensaje: str = ""):
        """Actualiza progreso (helper inyectado desde el dispatcher)."""
        # Se sobreescribe en dispatcher; aquí solo un fallback
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
            return False, "Cancelado antes de ejecutar", {'error': 'cancelled'}

        cls.actualizar_progreso(agente, 20, "Preparando entorno Python...")

        codigo = agente.codigo_python or ""

        if not codigo.strip():
            cls.actualizar_progreso(agente, 50, "Simulando ejecución...")
            duracion = max(0.01, float(agente.duracion or 0.5))
            for _ in range(int(min(duracion, 5) * 10)):
                if cancellation_token and cancellation_token.esta_cancelado():
                    return False, "Cancelado durante simulación", {'error': 'cancelled'}
                time.sleep(0.1)
            cls.actualizar_progreso(agente, 100, "Simulación completada")
            return True, f"Simulado {duracion:.2f}s", {
                "simulado": True,
                "duracion": duracion,
                "nombre": agente.nombre
            }

        if len(codigo) > MAX_CODIGO_LENGTH:
            cls.actualizar_progreso(agente, 100, "Código demasiado largo")
            return False, f"Código demasiado largo (máx {MAX_CODIGO_LENGTH//1024}KB)", {
                'error': 'code_too_long'
            }

        timeout = getattr(agente, 'timeout_python', 30)
        cls.actualizar_progreso(agente, 40, "Ejecutando código Python...")

        try:
            exito, mensaje, resultado = PythonSandbox.ejecutar(
                codigo,
                contexto,
                timeout=timeout,
                cancellation_token=cancellation_token,
                memory_limit_mb=getattr(agente, 'memory_limit_mb', None),
            )

            if exito:
                sospechoso, razon = es_resultado_sospechoso(resultado)
                if sospechoso:
                    logger.warning(
                        f"[{agente.nombre}] Resultado sospechoso Python: "
                        f"{razon} -> {str(resultado)[:200]}"
                    )
                    mensaje += f" (⚠️ sospechoso: {razon})"

            cls.actualizar_progreso(
                agente, 100,
                "Código ejecutado correctamente" if exito else f"Error: {mensaje[:50]}"
            )
            return exito, mensaje, resultado

        except SandboxError as e:
            cls.actualizar_progreso(agente, 100, f"Error en sandbox: {str(e)[:50]}")
            return False, f"Error en sandbox: {str(e)}", {'error': str(e)}
