# core/sandbox/python_sandbox.py
"""
Clase principal PythonSandbox - Ejecuta código Python en un subproceso aislado.

Este módulo es la FACHADA pública del sandbox. La implementación real vive
en módulos especializados:

    - script_builder.py  → escapado + construcción del script
    - spawn.py           → posix_spawn + setsid + matar_grupo
    - pipe_reader.py     → lectura no bloqueante de stdout/stderr
    - runner.py          → orquestación de una ejecución completa
    - temp_files.py      → gestión de archivos temporales
    - cache.py           → caché de resultados

La clase PythonSandbox mantiene:
    - Los singletons internos (_temp_manager, _cache).
    - La API pública de ejecución: ejecutar(), clear_cache(), get_cache_stats(),
      cleanup_temp_files(), get_status().
    - El registro atexit para limpieza.
"""
from __future__ import annotations

import atexit
import hashlib
import threading
from typing import Dict, Optional, Tuple

from ..cancellation import CancellationToken
from ._logging import _safe_log
from .cache import SandboxCache
from .constants import (
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT,
    MIN_MEMORY_LIMIT_MB,
    MAX_FILE_SIZE,
    DANGEROUS_PATTERNS,
)
from .models import (
    SandboxError,
    SandboxResourceError,
    SandboxResult,
    SandboxSecurityError,
    SandboxTimeoutError,
)
from .runner import ejecutar_script
from .script_builder import escapar_codigo, construir_script
from .temp_files import MAX_TEMP_FILES, TempFileManager


class PythonSandbox:
    """
    Ejecuta código Python en un subproceso aislado.

    Características:
    - Escapado de código para prevenir inyección
    - Timeout configurable
    - Gestión automática de archivos temporales
    - Caché de resultados (opcional)
    - Límites de recursos (opcional, Linux/macOS)
    - posix_spawn + setsid (sin fork) para evitar segfaults en py3.13
    - Lectura thread-safe de stdout/stderr con drenado hasta EOF
    """

    _temp_manager: Optional[TempFileManager] = None
    _cache: Optional[SandboxCache] = None
    _class_lock = threading.RLock()

    # ============================================================
    # SINGLETONS INTERNOS
    # ============================================================

    @classmethod
    def _get_temp_manager(cls) -> TempFileManager:
        with cls._class_lock:
            if cls._temp_manager is None:
                cls._temp_manager = TempFileManager()
            return cls._temp_manager

    @classmethod
    def _get_cache(cls) -> SandboxCache:
        with cls._class_lock:
            if cls._cache is None:
                cls._cache = SandboxCache()
            return cls._cache

    @classmethod
    def clear_cache(cls) -> None:
        with cls._class_lock:
            if cls._cache:
                cls._cache.clear()

    @classmethod
    def get_cache_stats(cls) -> Dict[str, int]:
        with cls._class_lock:
            if cls._cache:
                return cls._cache.get_stats()
            return {"size": 0, "hits": 0, "misses": 0, "hit_ratio": 0}

    @classmethod
    def cleanup_temp_files(cls) -> None:
        with cls._class_lock:
            if cls._temp_manager:
                cls._temp_manager.cleanup_all()

    # ============================================================
    # API PÚBLICA DE EJECUCIÓN
    # ============================================================

    @staticmethod
    def ejecutar(
        codigo: str,
        contexto: Dict,
        timeout: int = DEFAULT_TIMEOUT,
        use_cache: bool = False,
        memory_limit_mb: Optional[int] = None,
        cancellation_token: Optional["CancellationToken"] = None,
    ) -> Tuple[bool, str, Dict]:
        """
        Ejecuta código Python en un subproceso aislado.

        Returns:
            Tuple[bool, str, Dict]: (éxito, mensaje, resultado)
        """
        # ── Validación de entrada ──
        if not codigo or not codigo.strip():
            return False, "Código vacío", {}

        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {"error": "cancelled"}

        # ── Acotar timeout ──
        if timeout < 1:
            timeout = 1
        elif timeout > MAX_TIMEOUT:
            timeout = MAX_TIMEOUT
            _safe_log("warning", f"Timeout ajustado a {MAX_TIMEOUT}s (máximo permitido)")

        # ── Cache ──
        if use_cache:
            cache = PythonSandbox._get_cache()
            cached_result = cache.get(codigo, contexto)
            if cached_result:
                _safe_log(
                    "debug",
                    "Resultado cacheado para código hash %s",
                    hashlib.sha256(codigo.encode()).hexdigest()[:8],
                )
                return cached_result.success, cached_result.message, cached_result.result

        # ── Ajustar límite de memoria ──
        if memory_limit_mb and 0 < memory_limit_mb < MIN_MEMORY_LIMIT_MB:
            _safe_log(
                "warning",
                "memory_limit_mb=%s por debajo del mínimo recomendado (%s MB); "
                "se ajusta a %s MB",
                memory_limit_mb,
                MIN_MEMORY_LIMIT_MB,
                MIN_MEMORY_LIMIT_MB,
            )
            memory_limit_mb = MIN_MEMORY_LIMIT_MB

        try:
            codigo_escapado = escapar_codigo(codigo)
            script = construir_script(codigo_escapado, contexto, memory_limit_mb)
            result = ejecutar_script(
                script,
                contexto,
                timeout,
                PythonSandbox._get_temp_manager(),
                memory_limit_mb,
                cancellation_token,
            )

            if use_cache and result.success:
                cache = PythonSandbox._get_cache()
                cache.put(codigo, contexto, result)

            return result.success, result.message, result.result

        except SandboxTimeoutError as e:
            _safe_log("warning", "Timeout en ejecución de código: %s", e)
            return False, f"⏱️ Timeout: {e}", {"error": "timeout", "detail": str(e)}
        except SandboxSecurityError as e:
            _safe_log("error", "Error de seguridad en sandbox: %s", e)
            return False, f"🔒 Error de seguridad: {e}", {"error": "security", "detail": str(e)}
        except SandboxResourceError as e:
            _safe_log("error", "Error de recursos en sandbox: %s", e)
            return False, f"📦 Error de recursos: {e}", {"error": "resource", "detail": str(e)}
        except SandboxError as e:
            _safe_log("error", "Error en sandbox: %s", e)
            return False, f"Error: {e}", {"error": "sandbox", "detail": str(e)}
        except Exception as e:
            _safe_log("exception", "Error inesperado en sandbox: %s", e)
            return False, f"Error inesperado: {str(e)}", {"error": "unexpected", "detail": str(e)}

    # ============================================================
    # DIAGNÓSTICO
    # ============================================================

    @classmethod
    def get_status(cls) -> Dict:
        """Obtiene el estado del sandbox para diagnóstico."""
        from .spawn import has_posix_spawn_setsid
        cache_stats = cls.get_cache_stats()
        return {
            "cache": cache_stats,
            "temp_files": (
                len(cls._get_temp_manager()._temp_files)
                if cls._temp_manager else 0
            ),
            "max_temp_files": MAX_TEMP_FILES,
            "cache_max_size": 100,
            "cache_ttl": 300,
            "posix_spawn_setsid": has_posix_spawn_setsid(),
        }


# ============================================================
# LIMPIEZA AL FINALIZAR LA APLICACIÓN
# ============================================================

@atexit.register
def _cleanup_sandbox() -> None:
    """Limpia recursos del sandbox al finalizar la aplicación."""
    try:
        if PythonSandbox._temp_manager:
            PythonSandbox._temp_manager.stop()
            PythonSandbox._temp_manager.cleanup_all()
        PythonSandbox.clear_cache()
    except Exception:
        pass


__all__ = [
    "PythonSandbox",
    "MAX_FILE_SIZE",
    "DEFAULT_TIMEOUT",
    "MAX_TIMEOUT",
    "MIN_MEMORY_LIMIT_MB",
    "DANGEROUS_PATTERNS",
]