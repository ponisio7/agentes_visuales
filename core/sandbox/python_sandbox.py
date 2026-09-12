# core/sandbox/python_sandbox.py
"""
Clase principal PythonSandbox - Ejecuta código Python en un subproceso aislado.

IMPORTANTE (Python 3.13 + extensiones nativas):
    NO usar subprocess.Popen(start_new_session=True) desde hilos.
    Esa combinación fuerza fork()+exec() en CPython, lo que puede
    producir segfault si hay extensiones nativas cargadas (PyQt6,
    NumPy, Pandas) y algún hilo mantiene locks internos.

    Este módulo usa os.posix_spawn() con setsid=True, que invoca
    POSIX_SPAWN_SETSID sin fork() clásico. Se conserva la capacidad
    de matar el árbol completo con os.killpg().
"""

import subprocess
import tempfile
import os
import json
import sys
import platform
import time
import hashlib
import threading
import re
import atexit
import select
import signal
from typing import Dict, Tuple, Optional, Any, List
from pathlib import Path

from ..cancellation import obtener_gestor_cancelacion, CancellationToken
from .models import (
    SandboxResult,
    SandboxError,
    SandboxTimeoutError,
    SandboxSecurityError,
    SandboxResourceError,
)
from .temp_files import TempFileManager, MAX_TEMP_FILES
from .cache import SandboxCache, CACHE_MAX_SIZE, CACHE_TTL
import logging

logger = logging.getLogger(__name__)

# ============================================================
# CONSTANTES DE SEGURIDAD
# ============================================================

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 3600  # 1 hora máximo
MIN_MEMORY_LIMIT_MB = 64  # Mínimo razonable: cubre el arranque del intérprete hijo

# Caracteres peligrosos a escapar en el código del usuario
DANGEROUS_PATTERNS = [
    (r'"""', '\\"\\"\\"'),
    (r"'''", "\\'\\'\\'"),
    (r'`', '\\`'),
    (r'\$', '\\$'),
]


# ============================================================
# CLASE PRINCIPAL: PYTHON SANDBOX
# ============================================================

class PythonSandbox:
    """
    Ejecuta código Python en un subproceso aislado.

    Características:
    - Escapado de código para prevenir inyección
    - Timeout configurable
    - Gestión automática de archivos temporales
    - Caché de resultados (opcional)
    - Límites de recursos (opcional)
    - Logging detallado
    - posix_spawn con setsid (sin fork) para evitar segfaults en py3.13
    """

    _temp_manager: Optional[TempFileManager] = None
    _cache: Optional[SandboxCache] = None
    _class_lock = threading.RLock()
    _tempfile_lock = threading.Lock()   # protege mkstemp + escritura del script

    # ── Diagnóstico: ¿posix_spawn con setsid disponible? ──
    _HAS_POSIX_SPAWN_SETSID = (
        hasattr(os, "posix_spawn")
        and hasattr(os, "POSIX_SPAWN_SETSID")
        and platform.system() != "Windows"
    )

    @classmethod
    def _get_temp_manager(cls) -> TempFileManager:
        """Obtiene el gestor de archivos temporales (singleton)."""
        with cls._class_lock:
            if cls._temp_manager is None:
                cls._temp_manager = TempFileManager()
            return cls._temp_manager

    @classmethod
    def _get_cache(cls) -> SandboxCache:
        """Obtiene el caché de resultados (singleton)."""
        with cls._class_lock:
            if cls._cache is None:
                cls._cache = SandboxCache()
            return cls._cache

    @classmethod
    def clear_cache(cls):
        """Limpia el caché de resultados."""
        with cls._class_lock:
            if cls._cache:
                cls._cache.clear()

    @classmethod
    def get_cache_stats(cls) -> Dict[str, int]:
        """Obtiene estadísticas del caché."""
        with cls._class_lock:
            if cls._cache:
                return cls._cache.get_stats()
            return {'size': 0, 'hits': 0, 'misses': 0, 'hit_ratio': 0}

    @classmethod
    def cleanup_temp_files(cls):
        """Limpia todos los archivos temporales."""
        with cls._class_lock:
            if cls._temp_manager:
                cls._temp_manager.cleanup_all()

    # ============================================================
    # MÉTODO PRINCIPAL DE EJECUCIÓN
    # ============================================================

    @staticmethod
    def ejecutar(
        codigo: str,
        contexto: Dict,
        timeout: int = DEFAULT_TIMEOUT,
        use_cache: bool = False,
        memory_limit_mb: Optional[int] = None,
        cancellation_token: Optional['CancellationToken'] = None
    ) -> Tuple[bool, str, Dict]:
        """
        Ejecuta código Python en un subproceso aislado.

        Args:
            codigo: Código Python a ejecutar
            contexto: Diccionario con datos de contexto
            timeout: Tiempo máximo en segundos (1-3600)
            use_cache: Si se debe usar caché de resultados
            memory_limit_mb: Límite de memoria en MB (Linux/macOS)
            cancellation_token: Token de cancelación (opcional)

        Returns:
            Tuple[bool, str, Dict]: (éxito, mensaje, resultado)

        Raises:
            SandboxError: Si ocurre un error grave
        """
        if not codigo or not codigo.strip():
            return False, "Código vacío", {}

        # ── Verificar cancelación ──
        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {'error': 'cancelled'}

        if timeout < 1:
            timeout = 1
        elif timeout > MAX_TIMEOUT:
            timeout = MAX_TIMEOUT
            logger.warning(f"Timeout ajustado a {MAX_TIMEOUT}s (máximo permitido)")

        if use_cache:
            cache = PythonSandbox._get_cache()
            cached_result = cache.get(codigo, contexto)
            if cached_result:
                logger.debug(f"Resultado cacheado para código hash {hashlib.sha256(codigo.encode()).hexdigest()[:8]}")
                return cached_result.success, cached_result.message, cached_result.result

        start_time = time.time()

        if memory_limit_mb and 0 < memory_limit_mb < MIN_MEMORY_LIMIT_MB:
            logger.warning(
                "memory_limit_mb=%s por debajo del mínimo recomendado (%s MB); "
                "se ajusta a %s MB para evitar fallos espurios en el arranque "
                "del intérprete hijo",
                memory_limit_mb, MIN_MEMORY_LIMIT_MB, MIN_MEMORY_LIMIT_MB
            )
            memory_limit_mb = MIN_MEMORY_LIMIT_MB

        try:
            codigo_escapado = PythonSandbox._escapar_codigo(codigo)
            script = PythonSandbox._construir_script(codigo_escapado, contexto, memory_limit_mb)
            result = PythonSandbox._ejecutar_script(
                script, contexto, timeout, memory_limit_mb, cancellation_token
            )

            if use_cache and result.success:
                cache = PythonSandbox._get_cache()
                cache.put(codigo, contexto, result)

            return result.success, result.message, result.result

        except SandboxTimeoutError as e:
            logger.warning("Timeout en ejecución de código: %s", e)
            return False, f"⏱️ Timeout: {e}", {'error': 'timeout', 'detail': str(e)}
        except SandboxSecurityError as e:
            logger.error("Error de seguridad en sandbox: %s", e)
            return False, f"🔒 Error de seguridad: {e}", {'error': 'security', 'detail': str(e)}
        except SandboxResourceError as e:
            logger.error("Error de recursos en sandbox: %s", e)
            return False, f"📦 Error de recursos: {e}", {'error': 'resource', 'detail': str(e)}
        except SandboxError as e:
            logger.error("Error en sandbox: %s", e)
            return False, f"Error: {e}", {'error': 'sandbox', 'detail': str(e)}
        except Exception as e:
            logger.exception("Error inesperado en sandbox: %s", e)
            return False, f"Error inesperado: {str(e)}", {'error': 'unexpected', 'detail': str(e)}

    # ============================================================
    # ESCAPADO DE CÓDIGO
    # ============================================================

    @staticmethod
    def _escapar_codigo(codigo: str) -> str:
        """
        Escapa el código del usuario para prevenir inyección.

        ✅ Solo elimina caracteres de control (nulos).
        ✅ No rompe docstrings ni f-strings.
        ✅ Advertencia de palabras peligrosas con contexto:
           - eval( / exec( / __import__  → WARNING
           - compile(                    → WARNING solo si NO es re.compile(
        """
        # Eliminar caracteres de control nulos
        codigo_escapado = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', codigo)

        # Keywords realmente peligrosas
        for keyword in ('eval(', 'exec(', '__import__'):
            if keyword in codigo:
                logger.warning(
                    "Código contiene palabra clave potencialmente peligrosa: %s",
                    keyword
                )

        # compile( es legítimo como re.compile(...), así que solo
        # advertimos si aparece sin el prefijo 're.'
        if 'compile(' in codigo and not re.search(r'\bre\.compile\s*\(', codigo):
            logger.warning(
                "Código contiene 'compile(' sin prefijo 're.' "
                "(posible uso de compile() built-in)"
            )

        return codigo_escapado

    # ============================================================
    # CONSTRUCCIÓN DEL SCRIPT
    # ============================================================

    @staticmethod
    def _construir_script(codigo_escapado: str, contexto: Dict, memory_limit_mb: Optional[int] = None) -> str:
        """
        Construye el script completo a ejecutar.
        Usa globals() para capturar 'resultado'.
        Inyecta automáticamente las variables del Loop (item, indice, total).
        El límite de memoria (RLIMIT_AS) se aplica DENTRO de este script hijo.

        Args:
            codigo_escapado: Código del usuario escapado
            contexto: Contexto para la ejecución
            memory_limit_mb: Límite de memoria en MB a aplicar en el hijo
                (solo Linux/macOS; en Windows se ignora)

        Returns:
            str: Script completo
        """
        try:
            contexto_json = json.dumps(contexto, default=str, ensure_ascii=False)
            # Reemplazar valores JSON por valores Python válidos
            contexto_json = contexto_json.replace('true', 'True').replace('false', 'False').replace('null', 'None')
        except Exception as e:
            logger.warning(f"⚠️ Contexto no serializable directamente: {e}")
            contexto_simplificado = {}
            for k, v in contexto.items():
                try:
                    json.dumps(v, default=str)
                    contexto_simplificado[k] = v
                except Exception as e2:
                    logger.warning(
                        f"   → clave '{k}' (tipo {type(v).__name__}) no serializable: {e2}. "
                        f"Se convierte a str(): {str(v)[:200]}"
                    )
                    contexto_simplificado[k] = str(v)
            contexto_json = json.dumps(contexto_simplificado, default=str, ensure_ascii=False)

        # ── Bloque de límite de memoria (aplicado en el hijo) ──
        memoria_bloque = ""
        if memory_limit_mb and memory_limit_mb > 0 and platform.system() != "Windows":
            limit_bytes = memory_limit_mb * 1024 * 1024
            memoria_bloque = f'''
# ============================================================
# LÍMITE DE MEMORIA (RLIMIT_AS aplicado aquí, en el hijo)
# ============================================================
try:
    import resource as _resource_mod
    _resource_mod.setrlimit(_resource_mod.RLIMIT_AS, ({limit_bytes}, {limit_bytes}))
    try:
        _resource_mod.setrlimit(_resource_mod.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass
except (ValueError, OSError, ImportError) as _mem_err:
    print("__ERROR__" + json.dumps({{
        'error': f"No se pudo aplicar el limite de memoria: {{_mem_err}}",
        'tipo': 'ResourceError'
    }}, ensure_ascii=False))
    sys.exit(1)
'''
        elif memory_limit_mb and memory_limit_mb > 0:
            logger.warning(
                "memory_limit_mb=%s ignorado en Windows "
                "(resource.setrlimit no disponible)",
                memory_limit_mb
            )

        script = f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script generado automáticamente por el sandbox.
NO MODIFICAR MANUALMENTE.
"""

import json
import sys
import traceback
import time
from datetime import datetime
{memoria_bloque}
# ============================================================
# CONTEXTO PROPORCIONADO
# ============================================================
contexto = {contexto_json}

# ============================================================
# ✅ INYECCIÓN AUTOMÁTICA DE VARIABLES DEL LOOP
# (Necesario para loops: item, indice, total)
# ============================================================
item = contexto.get('item')
indice = contexto.get('indice', 0)
total = contexto.get('total', 0)

# ============================================================
# CÓDIGO DEL USUARIO
# NOTA: El código ha sido escapado para prevenir inyección.
# ============================================================
{codigo_escapado}

# ============================================================
# CAPTURA DE RESULTADO
# ============================================================
def _capturar_resultado():
    """Captura el resultado de la ejecución del código del usuario."""
    try:
        # Buscar 'resultado' en globals() - el código del usuario está aquí
        if 'resultado' in globals():
            resultado_final = globals()['resultado']
        else:
            # Buscar cualquier variable no mágica en globals()
            resultado_final = {{
                k: v for k, v in globals().items()
                if not k.startswith('_')
                and not callable(v)
                and type(v).__name__ != 'module'
                and k not in ['contexto', 'json', 'sys', 'traceback',
                              'datetime', 'time', 'item', 'indice', 'total',
                              'resultado_final', '_capturar_resultado']
            }}

        # Si no hay resultado, crear uno por defecto
        if not resultado_final:
            resultado_final = {{'status': 'ok', 'mensaje': 'Código ejecutado correctamente'}}

        # Serializar resultado (convertir tipos no serializables)
        try:
            resultado_serializado = json.loads(json.dumps(resultado_final, default=str))
        except Exception as e_ser:
            # Identificar qué clave específica falla, para no perder el detalle
            claves_problematicas = {{}}
            if isinstance(resultado_final, dict):
                for k, v in resultado_final.items():
                    try:
                        json.dumps(v, default=str)
                    except Exception as e2:
                        claves_problematicas[k] = {{
                            'tipo': type(v).__name__,
                            'error': str(e2),
                            'valor': str(v)[:200]
                        }}
            resultado_serializado = {{
                'error': 'Resultado no serializable',
                'error_original': str(e_ser),
                'tipo': type(resultado_final).__name__,
                'claves_problematicas': claves_problematicas,
                'contenido': str(resultado_final)[:1000]
            }}

        return resultado_serializado

    except Exception as e:
        return {{
            'error': str(e),
            'tipo': type(e).__name__,
            'traceback': traceback.format_exc()
        }}

# ============================================================
# EJECUCIÓN PRINCIPAL
# ============================================================
if __name__ == "__main__":
    try:
        resultado_final = _capturar_resultado()
        print("__RESULT__" + json.dumps(resultado_final, ensure_ascii=False))
    except Exception as e:
        print("__ERROR__" + json.dumps({{
            'error': str(e),
            'tipo': type(e).__name__,
            'traceback': traceback.format_exc()
        }}, ensure_ascii=False))
        sys.exit(1)
'''

        return script

    # ============================================================
    # SPAWN SIN FORK (posix_spawn + POSIX_SPAWN_SETSID)
    # ============================================================

    @staticmethod
    def _spawn_sandbox_proceso(
        script_path: str,
        env: dict,
        cwd: str,
    ) -> Tuple[int, int, int]:
        """
        Lanza el subproceso del sandbox con la mejor estrategia disponible:

        1. os.posix_spawn con setsid=True (si POSIX_SPAWN_SETSID está
           disponible) → NO usa fork, seguro bajo hilos.
        2. Fallback: subprocess.Popen con preexec_fn=os.setsid → usa
           fork, pero llama a setsid() directamente en C (más seguro
           que start_new_session=True en Python 3.13).

        Args:
            script_path: Ruta al script Python a ejecutar
            env: Variables de entorno para el hijo
            cwd: Directorio de trabajo del hijo

        Returns:
            Tuple[int, int, int]: (pid, fd_stdout, fd_stderr)

        Raises:
            SandboxError: Si no se puede lanzar el proceso
        """
        # ── Estrategia 1: posix_spawn con setsid (ideal) ──
        if PythonSandbox._HAS_POSIX_SPAWN_SETSID:
            return PythonSandbox._spawn_via_posix_spawn(script_path, env)

        # ── Estrategia 2: subprocess + preexec_fn=os.setsid (fallback) ──
        return PythonSandbox._spawn_via_subprocess_preexec(script_path, env, cwd)

    @staticmethod
    def _spawn_via_posix_spawn(script_path: str, env: dict) -> Tuple[int, int, int]:
        """Spawnea el sandbox usando os.posix_spawn + POSIX_SPAWN_SETSID."""
        env_list = [f"{k}={v}" for k, v in env.items()]

        r_out, w_out = os.pipe()
        r_err, w_err = os.pipe()

        file_actions = [
            (os.POSIX_SPAWN_DUP2, w_out, 1),
            (os.POSIX_SPAWN_DUP2, w_err, 2),
        ]

        argv = [sys.executable, script_path]

        try:
            pid = os.posix_spawn(
                sys.executable,
                argv,
                env_list,
                file_actions=file_actions,
                setsid=True,
            )
        except OSError as e:
            for fd in (r_out, w_out, r_err, w_err):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise SandboxError(f"No se pudo lanzar sandbox (posix_spawn): {e}") from e

        os.close(w_out)
        os.close(w_err)
        os.set_blocking(r_out, False)
        os.set_blocking(r_err, False)

        return pid, r_out, r_err

    @staticmethod
    def _spawn_via_subprocess_preexec(
        script_path: str,
        env: dict,
        cwd: str,
    ) -> Tuple[int, int, int]:
        """
        Fallback: subprocess.Popen con preexec_fn=os.setsid.
        Usa fork internamente, pero setsid() se ejecuta directamente en C
        (más seguro que start_new_session=True en Python 3.13).

        Devuelve (pid, fd_stdout, fd_stderr) creando tuberías manuales
        para no depender de proceso.stdout.fileno() (que es un BufferedReader
        con su propio lock y puede colisionar con el bucle de lectura).
        """
        r_out, w_out = os.pipe()
        r_err, w_err = os.pipe()

        try:
            proceso = subprocess.Popen(
                [sys.executable, script_path],
                stdout=w_out,
                stderr=w_err,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                env=env,
                close_fds=True,
                preexec_fn=os.setsid,   # ← fallback: setsid() en C
            )
        except OSError as e:
            for fd in (r_out, w_out, r_err, w_err):
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise SandboxError(f"No se pudo lanzar sandbox (subprocess): {e}") from e

        # El padre cierra los extremos de escritura
        os.close(w_out)
        os.close(w_err)

        os.set_blocking(r_out, False)
        os.set_blocking(r_err, False)

        return proceso.pid, r_out, r_err

    @staticmethod
    def _leer_pipes_no_bloqueante(
        r_out: int,
        r_err: int,
        pid: int,
        timeout: float,
        cancellation_token: Optional['CancellationToken'] = None,
    ) -> Tuple[bytes, bytes, int, float]:
        """
        Lee stdout/stderr del subproceso hasta que termine o haya timeout.
        Usa select() para no bloquear y poder chequear cancelación.

        Returns:
            Tuple[bytes, bytes, int, float]:
                (stdout_bytes, stderr_bytes, exit_code, elapsed)
        """
        buf_out = bytearray()
        buf_err = bytearray()
        inicio = time.time()
        exit_code: Optional[int] = None

        fd_abiertos = {r_out, r_err}

        while True:
            elapsed = time.time() - inicio

            # ── Timeout global ──
            if elapsed > timeout:
                _matar_grupo(pid)
                raise SandboxTimeoutError(f"Timeout ({timeout}s)")

            # ── Cancelación cooperativa ──
            if cancellation_token and cancellation_token.esta_cancelado():
                _matar_grupo(pid)
                raise SandboxTimeoutError("Cancelado por usuario")

            # ── Reap del hijo (no bloqueante) ──
            if exit_code is None:
                try:
                    wpid, status = os.waitpid(pid, os.WNOHANG)
                    if wpid == pid:
                        if os.WIFEXITED(status):
                            exit_code = os.WEXITSTATUS(status)
                        elif os.WIFSIGNALED(status):
                            exit_code = -os.WTERMSIG(status)
                        else:
                            exit_code = -1
                except ChildProcessError:
                    # Ya reapeado (raro, pero posible)
                    if exit_code is None:
                        exit_code = -1

            # ── Si terminó y no quedan fds, salir ──
            if exit_code is not None and not fd_abiertos:
                break

            # ── Leer lo disponible ──
            if fd_abiertos:
                try:
                    listos, _, _ = select.select(list(fd_abiertos), [], [], 0.05)
                except (OSError, ValueError):
                    listos = []

                for fd in listos:
                    try:
                        chunk = os.read(fd, 65536)
                    except BlockingIOError:
                        continue
                    except OSError:
                        chunk = b""

                    if not chunk:
                        # EOF: cerrar este fd
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                        fd_abiertos.discard(fd)
                    else:
                        if fd == r_out:
                            buf_out.extend(chunk)
                        else:
                            buf_err.extend(chunk)
            else:
                # Sin fds pero el hijo aún no ha sido reapeado: esperar
                time.sleep(0.01)

        elapsed = time.time() - inicio
        if exit_code is None:
            exit_code = -1
        return bytes(buf_out), bytes(buf_err), exit_code, elapsed

    # ============================================================
    # EJECUCIÓN DEL SCRIPT
    # ============================================================

    @staticmethod
    def _ejecutar_script(
        script: str,
        contexto: Dict,
        timeout: int,
        memory_limit_mb: Optional[int] = None,
        cancellation_token: Optional['CancellationToken'] = None
    ) -> SandboxResult:
        """
        Ejecuta el script en un subproceso con soporte para cancelación
        y límite de memoria real (Linux/macOS via resource.setrlimit).

        Usa os.posix_spawn con setsid en lugar de subprocess.Popen con
        start_new_session=True. Motivo: subprocess fuerza fork()+exec()
        cuando se pasa start_new_session, y eso produce segfaults en
        Python 3.13 con hilos + extensiones nativas.

        Args:
            script: Script completo a ejecutar
            contexto: Contexto (para logging)
            timeout: Timeout en segundos
            memory_limit_mb: Límite de memoria opcional (solo Linux/macOS)
            cancellation_token: Token de cancelación (opcional)

        Returns:
            SandboxResult: Resultado de la ejecución

        Raises:
            SandboxTimeoutError: Si se excede el timeout o se cancela
            SandboxError: Si ocurre otro error
        """
        script_path = None
        temp_manager = PythonSandbox._get_temp_manager()

        try:
            # ── Crear archivo temporal usando mkstemp (thread-safe) ──
            with PythonSandbox._tempfile_lock:
                fd, script_path = tempfile.mkstemp(suffix='.py', prefix='agent_')
                try:
                    with os.fdopen(fd, 'w', encoding='utf-8') as f:
                        f.write(script)
                        f.flush()
                except Exception:
                    try:
                        os.unlink(script_path)
                    except OSError:
                        pass
                    raise

            temp_manager.register(script_path)

            # ── Preparar entorno ──
            if platform.system() == "Windows":
                env = {k: v for k, v in os.environ.items()
                       if k in ['PATH', 'SYSTEMROOT', 'TEMP', 'APPDATA', 'USERPROFILE']}
                cwd = tempfile.gettempdir()
            else:
                env = {
                    'PATH': '/usr/local/bin:/usr/bin:/bin',
                    'HOME': os.path.expanduser('~'),
                    'LANG': 'en_US.UTF-8',
                    'PYTHONUNBUFFERED': '1',
                    'PYTHONDONTWRITEBYTECODE': '1',
                }
                cwd = tempfile.gettempdir()

            start_time = time.time()

            # ── Lanzar proceso SIN fork (posix_spawn + setsid) ──
            pid, r_out, r_err = PythonSandbox._spawn_sandbox_proceso(
                script_path, env, cwd
            )

            logger.debug(
                "Sandbox lanzado vía posix_spawn (sin fork): pid=%s script=%s",
                pid, script_path,
            )

            # ── Leer stdout/stderr hasta fin o timeout ──
            try:
                stdout_bytes, stderr_bytes, result_code, execution_time = (
                    PythonSandbox._leer_pipes_no_bloqueante(
                        r_out, r_err, pid, timeout, cancellation_token
                    )
                )
            finally:
                # Asegurar cierre de descriptores (idempotente)
                for fd in (r_out, r_err):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

            stdout = stdout_bytes.decode('utf-8', errors='replace').strip()
            stderr = stderr_bytes.decode('utf-8', errors='replace').strip()

            # Guardar debug si hay stderr
            if stderr:
                logger.debug("stderr del sandbox: %s", stderr[:500])
                try:
                    debug_dir = os.path.join(os.getcwd(), "logs")
                    os.makedirs(debug_dir, exist_ok=True)
                    debug_path = os.path.join(debug_dir, "sandbox_debug.log")
                    with open(debug_path, "a", encoding="utf-8") as f:
                        f.write("=" * 80 + "\n")
                        f.write("=== SANDBOX DEBUG ===\n")
                        f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                        f.write(f"Script path: {script_path}\n")
                        f.write(f"stderr:\n{stderr}\n")
                        f.write(f"stdout:\n{stdout}\n")
                        f.write("=" * 80 + "\n\n")
                except Exception:
                    pass

            # ── Detectar MemoryError explícitamente (límite de memoria) ──
            if memory_limit_mb and (
                "MemoryError" in stderr
                or (result_code is not None and result_code < 0)
            ):
                raise SandboxResourceError(
                    f"Límite de memoria excedido ({memory_limit_mb} MB)"
                )

            # ── Parsear resultado ──
            for line in stdout.split('\n'):
                if line.startswith('__RESULT__'):
                    try:
                        resultado = json.loads(line[len('__RESULT__'):])
                        mensaje = (
                            stdout.replace(line, '').strip()
                            or "Código ejecutado correctamente"
                        )
                        return SandboxResult(
                            success=True,
                            message=mensaje,
                            result=resultado,
                            execution_time=execution_time,
                            exit_code=result_code,
                            stdout=stdout,
                            stderr=stderr
                        )
                    except json.JSONDecodeError as e:
                        logger.warning("Error parseando resultado: %s", e)
                        return SandboxResult(
                            success=False,
                            message=f"Error parseando resultado: {e}",
                            result={'stdout': stdout, 'error': str(e)},
                            execution_time=execution_time,
                            exit_code=result_code,
                            stdout=stdout,
                            stderr=stderr
                        )

                elif line.startswith('__ERROR__'):
                    try:
                        error_data = json.loads(line[len('__ERROR__'):])
                        return SandboxResult(
                            success=False,
                            message=error_data.get('error', 'Error desconocido'),
                            result=error_data,
                            execution_time=execution_time,
                            exit_code=result_code,
                            stdout=stdout,
                            stderr=stderr
                        )
                    except Exception:
                        return SandboxResult(
                            success=False,
                            message=f"Error en ejecución: {stderr or stdout}",
                            result={'stderr': stderr, 'stdout': stdout},
                            execution_time=execution_time,
                            exit_code=result_code,
                            stdout=stdout,
                            stderr=stderr
                        )

            # ── Fallback ──
            if result_code == 0:
                try:
                    if stdout:
                        resultado = json.loads(stdout)
                        return SandboxResult(
                            success=True,
                            message="Código ejecutado correctamente",
                            result=resultado,
                            execution_time=execution_time,
                            exit_code=result_code,
                            stdout=stdout,
                            stderr=stderr
                        )
                except Exception:
                    pass

                return SandboxResult(
                    success=True,
                    message=stdout or "Código ejecutado correctamente",
                    result={'salida': stdout},
                    execution_time=execution_time,
                    exit_code=result_code,
                    stdout=stdout,
                    stderr=stderr
                )

            return SandboxResult(
                success=False,
                message=stderr or stdout or "Error desconocido",
                result={'stderr': stderr, 'stdout': stdout},
                execution_time=execution_time,
                exit_code=result_code,
                stdout=stdout,
                stderr=stderr
            )

        except SandboxTimeoutError:
            raise
        except SandboxResourceError:
            raise
        except subprocess.TimeoutExpired as e:
            raise SandboxTimeoutError(f"El código excedió {timeout}s") from e
        except subprocess.SubprocessError as e:
            raise SandboxError(f"Error en subproceso: {e}") from e
        except Exception as e:
            raise SandboxError(f"Error inesperado: {e}") from e
        finally:
            if script_path and os.path.exists(script_path):
                temp_manager.unregister(script_path)

    # ============================================================
    # MÉTODOS DE UTILIDAD PARA PRUEBAS
    # ============================================================

    @classmethod
    def probar_ejecucion(cls, codigo: str, contexto: Dict = None) -> Tuple[bool, str, Dict]:
        """
        Método de prueba para verificar la ejecución de código.

        Args:
            codigo: Código a probar
            contexto: Contexto opcional

        Returns:
            Tuple[bool, str, Dict]: (éxito, mensaje, resultado)
        """
        contexto = contexto or {}
        return cls.ejecutar(codigo, contexto, timeout=10)

    @classmethod
    def probar_loop(cls, codigo_por_item: str, items: List[Any]) -> Tuple[bool, str, Dict]:
        """
        Prueba un código de loop con items de ejemplo.

        Args:
            codigo_por_item: Código a ejecutar por cada item
            items: Lista de items de prueba

        Returns:
            Tuple[bool, str, Dict]: Resultado de la prueba
        """
        if not codigo_por_item:
            return False, "Código vacío", {}

        resultados = []
        errores = 0

        for idx, item in enumerate(items):
            contexto = {
                'item': item,
                'indice': idx,
                'total': len(items)
            }

            exito, mensaje, resultado = cls.ejecutar(codigo_por_item, contexto, timeout=10)

            resultados.append({
                'indice': idx,
                'item': item,
                'exito': exito,
                'mensaje': mensaje,
                'resultado': resultado
            })

            if not exito:
                errores += 1

        return errores == 0, f"Prueba completada: {len(items) - errores} éxitos, {errores} errores", {
            'total_items': len(items),
            'errores': errores,
            'exitos': len(items) - errores,
            'resultados': resultados
        }

    @classmethod
    def diagnosticar_contexto(cls, contexto: Dict) -> Dict:
        """
        Analiza un contexto y reporta si es serializable a JSON y, si no,
        qué claves fallan y por qué.

        Args:
            contexto: Diccionario de contexto a analizar

        Returns:
            Dict con 'serializable' (bool), 'detalle_claves' y, si aplica,
            'claves_problematicas' con el tipo y error de cada una.
        """
        detalle_claves = {}
        for key, value in contexto.items():
            info = {'tipo': type(value).__name__}
            if isinstance(value, dict):
                info['claves'] = list(value.keys())[:5]
            elif isinstance(value, list):
                info['items'] = len(value)
                if value:
                    info['tipo_primer_item'] = type(value[0]).__name__
            detalle_claves[key] = info

        try:
            json.dumps(contexto, default=str)
            return {'serializable': True, 'detalle_claves': detalle_claves}
        except Exception as e:
            claves_problematicas = {}
            for key, value in contexto.items():
                try:
                    json.dumps({key: value}, default=str)
                except Exception as e2:
                    claves_problematicas[key] = {
                        'tipo': type(value).__name__,
                        'error': str(e2),
                        'valor': str(value)[:200]
                    }
            logger.error(f"❌ Contexto no serializable: {e}")
            return {
                'serializable': False,
                'error': str(e),
                'detalle_claves': detalle_claves,
                'claves_problematicas': claves_problematicas
            }

    @classmethod
    def get_status(cls) -> Dict:
        """
        Obtiene el estado del sandbox.

        Returns:
            Dict: Estado del sandbox
        """
        cache_stats = cls.get_cache_stats()
        return {
            'cache': cache_stats,
            'temp_files': len(cls._get_temp_manager()._temp_files) if cls._temp_manager else 0,
            'max_temp_files': MAX_TEMP_FILES,
            'cache_max_size': CACHE_MAX_SIZE,
            'cache_ttl': CACHE_TTL,
            'posix_spawn_setsid': cls._HAS_POSIX_SPAWN_SETSID,
        }


# ============================================================
# UTILIDADES INTERNAS DE PROCESOS
# ============================================================

def _matar_grupo(pid: int) -> None:
    """
    Mata el grupo de procesos del sandbox de forma limpia.
    Usa SIGTERM primero, luego SIGKILL si no muere.
    """
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    # Dar margen para que termine limpiamente
    deadline = time.time() + 0.3
    while time.time() < deadline:
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                return
        except ChildProcessError:
            return
        time.sleep(0.02)
    # Forzar
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


# ============================================================
# LIMPIEZA AL FINALIZAR LA APLICACIÓN
# ============================================================

@atexit.register
def _cleanup_sandbox():
    """Limpia recursos del sandbox al finalizar la aplicación."""
    try:
        if PythonSandbox._temp_manager:
            PythonSandbox._temp_manager.stop()
            PythonSandbox._temp_manager.cleanup_all()
        PythonSandbox.clear_cache()
    except Exception:
        pass