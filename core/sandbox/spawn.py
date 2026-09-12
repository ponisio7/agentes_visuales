# core/sandbox/spawn.py
"""
Lanzamiento de subprocesos del sandbox con posix_spawn + setsid.

IMPORTANTE (Python 3.13 + extensiones nativas):
    NO usar subprocess.Popen(start_new_session=True) desde hilos.
    Esa combinación fuerza fork()+exec() en CPython, lo que puede
    producir segfault si hay extensiones nativas cargadas (PyQt6,
    NumPy, Pandas) y algún hilo mantiene locks internos.

    Este módulo usa os.posix_spawn() con setsid=True, que invoca
    POSIX_SPAWN_SETSID sin fork() clásico. Se conserva la capacidad
    de matar el árbol completo con os.killpg().

FIX CRÍTICO EN file_actions:
    Cerrar SIEMPRE 0, 1 y 2 ANTES de duplicar w_out → 1 y w_err → 2.
    Bajo concurrencia, os.pipe() puede reutilizar los fds 0/1/2 (si el
    padre los tenía cerrados o si otro hilo los liberó), y entonces el
    DUP2 pisa el fd equivocado: el hijo acaba escribiendo en stderr
    o en otro pipe, y el padre lee stdout vacío. Cerrar 0/1/2 primero
    elimina la colisión de forma determinista.
"""
from __future__ import annotations

import errno
import os
import platform
import signal
import sys
import threading
import time
from typing import Optional, Tuple

from ._logging import _safe_log
from .models import SandboxError


# Serializa os.pipe() + os.posix_spawn bajo concurrencia alta: dos hilos
# pueden crear pipes simultáneamente y el kernel reutiliza números de fd,
# provocando que uno apunte a un fd que el otro ya cerró.
_spawn_lock = threading.Lock()

# Probe cacheado: ¿os.posix_spawn soporta setsid=True en este sistema?
_HAS_POSIX_SPAWN_SETSID: Optional[bool] = None


def _probe_posix_spawn_setsid() -> bool:
    """
    Prueba de forma real si os.posix_spawn soporta setsid=True.

    NO basta con hasattr(os, 'POSIX_SPAWN_SETSID') porque esa constante
    no está expuesta en CPython aunque el kwarg setsid=True funcione.
    Se ejecuta una vez y se cachea en _HAS_POSIX_SPAWN_SETSID.
    """
    if platform.system() == "Windows" or not hasattr(os, "posix_spawn"):
        return False
    try:
        r, w = os.pipe()
        try:
            pid = os.posix_spawn(
                sys.executable,
                [sys.executable, "-c", "pass"],
                os.environ,
                file_actions=[(os.POSIX_SPAWN_DUP2, w, 1)],
                setsid=True,
            )
            os.close(w)
            os.waitpid(pid, 0)
            return True
        finally:
            for fd in (r, w):
                try:
                    os.close(fd)
                except OSError:
                    pass
    except (OSError, TypeError, ValueError):
        return False


def has_posix_spawn_setsid() -> bool:
    """Retorna True si posix_spawn+setsid está disponible (con caché)."""
    global _HAS_POSIX_SPAWN_SETSID
    if _HAS_POSIX_SPAWN_SETSID is None:
        _HAS_POSIX_SPAWN_SETSID = _probe_posix_spawn_setsid()
    return _HAS_POSIX_SPAWN_SETSID


def spawn_sandbox_proceso(
    script_path: str,
    env: dict,
) -> Tuple[int, int, int]:
    """
    Lanza el subproceso del sandbox usando exclusivamente posix_spawn
    + setsid=True. Falla explícitamente si no está disponible para
    evitar segfaults por fork().

    Returns:
        Tuple[int, int, int]: (pid, fd_stdout, fd_stderr)

    Raises:
        SandboxError: Si no se puede lanzar el proceso.
    """
    if not has_posix_spawn_setsid():
        raise SandboxError(
            "posix_spawn+setsid no disponible; no se puede lanzar sandbox "
            "de forma segura bajo hilos con extensiones nativas. "
            "Requiere Python >= 3.11 en Linux/macOS."
        )

    argv = [sys.executable, script_path]
    intentos = 3

    with _spawn_lock:
        for intento in range(intentos):
            r_out, w_out = os.pipe()
            r_err, w_err = os.pipe()

            # Orden IMPORTANTE (ver docstring del módulo):
            file_actions = [
                (os.POSIX_SPAWN_CLOSE, 0),
                (os.POSIX_SPAWN_CLOSE, 1),
                (os.POSIX_SPAWN_CLOSE, 2),
                (os.POSIX_SPAWN_DUP2, w_out, 1),
                (os.POSIX_SPAWN_DUP2, w_err, 2),
                (os.POSIX_SPAWN_CLOSE, r_out),
                (os.POSIX_SPAWN_CLOSE, r_err),
                (os.POSIX_SPAWN_CLOSE, w_out),
                (os.POSIX_SPAWN_CLOSE, w_err),
            ]

            try:
                pid = os.posix_spawn(
                    sys.executable,
                    argv,
                    env,
                    file_actions=file_actions,
                    setsid=True,
                )
            except OSError as e:
                for fd in (r_out, w_out, r_err, w_err):
                    try:
                        os.close(fd)
                    except OSError:
                        pass

                if (
                    e.errno in (errno.EMFILE, errno.ENFILE, errno.EAGAIN)
                    and intento < intentos - 1
                ):
                    time.sleep(0.01)
                    continue
                raise SandboxError(
                    f"No se pudo lanzar sandbox (posix_spawn): {e}"
                ) from e

            # Padre cierra los extremos de escritura (ya duplicados en el hijo)
            os.close(w_out)
            os.close(w_err)
            os.set_blocking(r_out, False)
            os.set_blocking(r_err, False)
            return pid, r_out, r_err

    raise SandboxError("No se pudo lanzar sandbox tras reintentos")


def matar_grupo(pid: int) -> None:
    """
    Mata el grupo de procesos del sandbox de forma limpia.
    Usa SIGTERM primero, luego SIGKILL si no muere.
    """
    try:
        os.killpg(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return

    deadline = time.time() + 0.3
    while time.time() < deadline:
        try:
            wpid, _ = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                return
        except ChildProcessError:
            return
        time.sleep(0.02)

    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        os.waitpid(pid, 0)
    except ChildProcessError:
        pass


# Alias retrocompatible con el nombre que se usaba antes
_matar_grupo = matar_grupo


__all__ = [
    "spawn_sandbox_proceso",
    "matar_grupo",
    "_matar_grupo",
    "has_posix_spawn_setsid",
    "_spawn_lock",
]