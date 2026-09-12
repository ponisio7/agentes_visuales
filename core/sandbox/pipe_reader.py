# core/sandbox/pipe_reader.py
"""
Lectura no bloqueante de stdout/stderr del subproceso del sandbox.

DISEÑO:
    - select() no bloqueante mientras el hijo sigue vivo.
    - Cuando el hijo muere, drena los fds en modo bloqueante hasta EOF
      REAL (el kernel cierra el pipe al morir el hijo: os.read() devuelve
      b"" y salimos). Esto garantiza leer TODO lo que el hijo escribió
      antes de morir, sin ventanas de carrera.
    - Dueño único de r_out/r_err: los cierra siempre antes de salir.
"""
from __future__ import annotations

import errno
import os
import select
import time
from typing import TYPE_CHECKING, Optional, Tuple

from ._logging import _safe_log
from .models import SandboxError, SandboxTimeoutError
from .spawn import matar_grupo

if TYPE_CHECKING:
    from ..cancellation import CancellationToken
    from .temp_files import TempFileManager


def leer_pipes_no_bloqueante(
    r_out: int,
    r_err: int,
    pid: int,
    timeout: float,
    cancellation_token: Optional["CancellationToken"] = None,
    temp_manager: Optional["TempFileManager"] = None,
    script_path: Optional[str] = None,
) -> Tuple[bytes, bytes, int, float]:
    """
    Lee stdout/stderr del subproceso hasta que termine o haya timeout.

    Returns:
        Tuple[bytes, bytes, int, float]:
            (stdout_bytes, stderr_bytes, exit_code, elapsed)
    """
    buf_out = bytearray()
    buf_err = bytearray()
    inicio = time.time()
    exit_code: Optional[int] = None

    fd_abiertos = {r_out, r_err}
    ebadf_count = 0
    iteracion = 0

    try:
        while True:
            iteracion += 1

            # Refrescar el "touch" del script para que TempFileManager
            # no lo borre en ejecuciones largas.
            if script_path and temp_manager and (iteracion % 10 == 0):
                try:
                    temp_manager.touch(script_path)
                except Exception:
                    pass

            elapsed = time.time() - inicio

            # ── Timeout global ──
            if elapsed > timeout:
                if exit_code is None:
                    matar_grupo(pid)
                raise SandboxTimeoutError(f"Timeout ({timeout}s)")

            # ── Cancelación cooperativa ──
            if cancellation_token and cancellation_token.esta_cancelado():
                matar_grupo(pid)
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
                    # Otro waitpid (pytest-forked, etc.) ya reapeó al hijo.
                    # NO marcamos exit_code = -1 para no forzar cierre
                    # prematuro del pipe. El bucle termina cuando fd_abiertos
                    # se vacíe por EOF real.
                    pass

            # ── DRENADO BLOQUEANTE tras la muerte del hijo ──
            if exit_code is not None and fd_abiertos:
                for fd in list(fd_abiertos):
                    try:
                        os.set_blocking(fd, True)
                    except OSError:
                        fd_abiertos.discard(fd)
                        continue

                    try:
                        while True:
                            chunk = os.read(fd, 65536)
                            if not chunk:
                                break  # EOF real
                            if fd == r_out:
                                buf_out.extend(chunk)
                            else:
                                buf_err.extend(chunk)
                    except BlockingIOError:
                        break
                    except OSError:
                        pass

                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    fd_abiertos.discard(fd)

            # ── Fin: hijo reapeado y sin fds pendientes ──
            if exit_code is not None and not fd_abiertos:
                break

            # ── Sin fds pero hijo vivo: esperar un poco ──
            if not fd_abiertos:
                time.sleep(0.01)
                continue

            # ── select() no bloqueante mientras el hijo sigue vivo ──
            try:
                listos, _, _ = select.select(list(fd_abiertos), [], [], 0.05)
                ebadf_count = 0
            except (OSError, ValueError) as e:
                listos = []
                if isinstance(e, OSError) and e.errno == errno.EBADF:
                    ebadf_count += 1
                    for fd in list(fd_abiertos):
                        try:
                            os.fstat(fd)
                        except OSError:
                            fd_abiertos.discard(fd)
                            try:
                                os.close(fd)
                            except OSError:
                                pass
                    if not fd_abiertos and exit_code is not None:
                        break
                    if ebadf_count > 10:
                        matar_grupo(pid)
                        raise SandboxError(
                            "select() falla repetidamente con EBADF"
                        )
                    time.sleep(0.01)
                    continue
                continue

            for fd in listos:
                try:
                    chunk = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    chunk = b""

                if not chunk:
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

    finally:
        # Dueño único: cerrar cualquier fd remanente aquí.
        for fd in list(fd_abiertos):
            try:
                os.close(fd)
            except OSError:
                pass
        fd_abiertos.clear()

    elapsed = time.time() - inicio
    if exit_code is None:
        exit_code = -1
    return bytes(buf_out), bytes(buf_err), exit_code, elapsed


# Alias retrocompatible
_leer_pipes_no_bloqueante = leer_pipes_no_bloqueante


__all__ = ["leer_pipes_no_bloqueante", "_leer_pipes_no_bloqueante"]