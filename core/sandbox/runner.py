# core/sandbox/runner.py
"""
Orquestador de una ejecución del sandbox.

Responsabilidades:
    - Crear el archivo temporal con el script.
    - Registrar el archivo en TempFileManager.
    - Lanzar el subproceso (spawn.spawn_sandbox_proceso).
    - Leer stdout/stderr (pipe_reader.leer_pipes_no_bloqueante).
    - Parsear la salida y devolver un SandboxResult.
    - Guardar debug de stderr en logs/.
    - Desregistrar el archivo temporal en finally.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import tempfile
import threading
import time
from typing import TYPE_CHECKING, Dict, Optional

from ._logging import _safe_log
from .models import (
    SandboxError,
    SandboxResourceError,
    SandboxResult,
    SandboxTimeoutError,
)
from .pipe_reader import leer_pipes_no_bloqueante
from .spawn import spawn_sandbox_proceso
from .temp_files import TempFileManager

if TYPE_CHECKING:
    from ..cancellation import CancellationToken


# Lock para serializar mkstemp (evita colisiones de nombres en alta concurrencia)
_tempfile_lock = threading.Lock()


def ejecutar_script(
    script: str,
    contexto: Dict,
    timeout: int,
    temp_manager: TempFileManager,
    memory_limit_mb: Optional[int] = None,
    cancellation_token: Optional["CancellationToken"] = None,
) -> SandboxResult:
    """
    Ejecuta el script en un subproceso con soporte para cancelación
    y límite de memoria real (Linux/macOS via resource.setrlimit).
    """
    script_path: Optional[str] = None

    try:
        # ── Crear archivo temporal (thread-safe) ──
        with _tempfile_lock:
            fd, script_path = tempfile.mkstemp(suffix=".py", prefix="agent_")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(script)
                    f.flush()
            except Exception:
                try:
                    os.unlink(script_path)
                except OSError:
                    pass
                raise

        temp_manager.register(script_path)

        # ── Entorno del hijo ──
        # Heredar el entorno completo (VIRTUAL_ENV, LD_LIBRARY_PATH,
        # PYTHONPATH, etc.) para evitar conflictos de ABI o imports
        # al arrancar el intérprete del .venv.
        if platform.system() == "Windows":
            env = {
                k: v
                for k, v in os.environ.items()
                if k in ("PATH", "SYSTEMROOT", "TEMP", "APPDATA", "USERPROFILE")
            }
        else:
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            env["PYTHONDONTWRITEBYTECODE"] = "1"

        # ── Lanzar subproceso (posix_spawn + setsid) ──
        pid, r_out, r_err = spawn_sandbox_proceso(script_path, env)

        _safe_log(
            "debug",
            "Sandbox lanzado vía posix_spawn: pid=%s script=%s",
            pid, script_path,
        )

        # ── Leer stdout/stderr ──
        stdout_bytes, stderr_bytes, result_code, execution_time = (
            leer_pipes_no_bloqueante(
                r_out, r_err, pid, timeout,
                cancellation_token, temp_manager, script_path,
            )
        )

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        # ── Guardar debug si hay stderr ──
        if stderr:
            _safe_log("debug", "stderr del sandbox: %s", stderr[:500])
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

        # ── Detectar MemoryError explícito (límite de memoria) ──
        if memory_limit_mb and (
            "MemoryError" in stderr
            or (result_code is not None and result_code < 0)
        ):
            raise SandboxResourceError(
                f"Límite de memoria excedido ({memory_limit_mb} MB)"
            )

        # ── Parsear resultado ──
        for line in stdout.split("\n"):
            if line.startswith("__RESULT__"):
                try:
                    resultado = json.loads(line[len("__RESULT__"):])
                    mensaje = (
                        stdout.replace(line, "").strip()
                        or "Código ejecutado correctamente"
                    )
                    return SandboxResult(
                        success=True,
                        message=mensaje,
                        result=resultado,
                        execution_time=execution_time,
                        exit_code=result_code,
                        stdout=stdout,
                        stderr=stderr,
                    )
                except json.JSONDecodeError as e:
                    _safe_log("warning", "Error parseando resultado: %s", e)
                    return SandboxResult(
                        success=False,
                        message=f"Error parseando resultado: {e}",
                        result={"stdout": stdout, "error": str(e)},
                        execution_time=execution_time,
                        exit_code=result_code,
                        stdout=stdout,
                        stderr=stderr,
                    )

            elif line.startswith("__ERROR__"):
                try:
                    error_data = json.loads(line[len("__ERROR__"):])
                    return SandboxResult(
                        success=False,
                        message=error_data.get("error", "Error desconocido"),
                        result=error_data,
                        execution_time=execution_time,
                        exit_code=result_code,
                        stdout=stdout,
                        stderr=stderr,
                    )
                except Exception:
                    return SandboxResult(
                        success=False,
                        message=f"Error en ejecución: {stderr or stdout}",
                        result={"stderr": stderr, "stdout": stdout},
                        execution_time=execution_time,
                        exit_code=result_code,
                        stdout=stdout,
                        stderr=stderr,
                    )

        # ── Fallback: sin marcador __RESULT__ / __ERROR__ ──
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
                        stderr=stderr,
                    )
            except Exception:
                pass

            return SandboxResult(
                success=True,
                message=stdout or "Código ejecutado correctamente",
                result={"salida": stdout},
                execution_time=execution_time,
                exit_code=result_code,
                stdout=stdout,
                stderr=stderr,
            )

        return SandboxResult(
            success=False,
            message=stderr or stdout or "Error desconocido",
            result={"stderr": stderr, "stdout": stdout},
            execution_time=execution_time,
            exit_code=result_code,
            stdout=stdout,
            stderr=stderr,
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


# Alias retrocompatible
_ejecutar_script = ejecutar_script


__all__ = ["ejecutar_script", "_ejecutar_script"]