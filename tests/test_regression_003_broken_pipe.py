"""Regresión 003 — ``run --json | jq`` no debe imprimir ``BrokenPipeError``.

Bug 1.18 del ROADMAP (punto 3.5):

    Si ``jq`` (o ``head``) cierra el pipe antes de que el proceso Python
    termine de escribir, la CLI imprimía un traceback de ``BrokenPipeError`` y
    devolvía un código de error engañoso. No había ningún manejo, ni en
    ``_configurar_logging`` ni en el ``print`` final.

Se cubren las tres piezas del arreglo:

  1. ``_StreamHandlerTolerante``: el handler de consola no propaga el EPIPE.
  2. ``_manejar_broken_pipe()``: neutraliza el pipe redirigiendo a ``os.devnull``.
  3. ``main()``: devuelve ``EXIT_OK`` en vez de propagar la excepción.

El último caso es **end-to-end**: abre un proceso real, cierra el extremo de
lectura del pipe y comprueba que no queda traceback ni código de error.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

import main

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class _StreamQueRompeElPipe:
    """Stream mínimo que falla como un pipe cerrado."""

    def __init__(self, error: Exception | None = None):
        self.error = error or BrokenPipeError(32, "Broken pipe")
        self.escrito = []

    def write(self, texto):
        self.escrito.append(texto)
        raise self.error

    def flush(self):
        raise self.error

    def fileno(self):
        raise ValueError("stream sin fileno real")


# ---------------------------------------------------------------------------
# 1. Handler tolerante
# ---------------------------------------------------------------------------

def test_handler_tolerante_ignora_broken_pipe():
    handler = main._StreamHandlerTolerante(_StreamQueRompeElPipe())

    handler.emit(logging.LogRecord(
        name="x", level=logging.INFO, pathname=__file__, lineno=1,
        msg="hola", args=(), exc_info=None,
    ))  # no debe propagar


def test_configurar_logging_instala_el_handler_tolerante(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    niveles = {n: logging.getLogger(n).level for n in ("weasyprint",)}
    try:
        main._configurar_logging(quiet=True)
        tolerantes = [
            h for h in logging.getLogger().handlers
            if isinstance(h, main._StreamHandlerTolerante)
        ]
        assert tolerantes, "la raíz debe tener un handler de consola tolerante"
    finally:
        for nombre, nivel in niveles.items():
            logging.getLogger(nombre).setLevel(nivel)


# ---------------------------------------------------------------------------
# 2. Neutralización del pipe
# ---------------------------------------------------------------------------

def test_manejar_broken_pipe_no_propaga(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdout", _StreamQueRompeElPipe())

    main._manejar_broken_pipe()  # no debe lanzar


def test_manejar_broken_pipe_tolera_stream_sin_fileno(monkeypatch):
    monkeypatch.setattr(sys, "stdout", _StreamQueRompeElPipe())
    monkeypatch.setattr(sys, "stderr", _StreamQueRompeElPipe())

    main._manejar_broken_pipe()


# ---------------------------------------------------------------------------
# 3. main() devuelve EXIT_OK
# ---------------------------------------------------------------------------

def test_main_devuelve_exit_ok_si_el_pipe_se_rompe(monkeypatch):
    llamado = {"manejado": False}

    def _rompe(args):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(main, "_ejecutar_run", _rompe)
    monkeypatch.setattr(main, "_manejar_broken_pipe",
                        lambda: llamado.update(manejado=True))
    monkeypatch.setattr(sys, "argv", ["agentes_visuales", "run", "--prompt", "x"])

    rc = main.main()

    assert rc == main.EXIT_OK
    assert llamado["manejado"] is True


def test_main_no_traga_otros_errores(monkeypatch):
    """Solo el EPIPE se neutraliza: un fallo real sigue propagándose."""
    def _explota(args):
        raise RuntimeError("fallo real")

    monkeypatch.setattr(main, "_ejecutar_run", _explota)
    monkeypatch.setattr(sys, "argv", ["agentes_visuales", "run", "--prompt", "x"])

    with pytest.raises(RuntimeError, match="fallo real"):
        main.main()


# ---------------------------------------------------------------------------
# 4. End-to-end: proceso real con el pipe cerrado
# ---------------------------------------------------------------------------

GUION = (
    "import sys, main\n"
    "main._configurar_logging(quiet=True)\n"
    "try:\n"
    "    for _ in range(20000):\n"
    "        print('x' * 100)\n"
    "except BrokenPipeError:\n"
    "    main._manejar_broken_pipe()\n"
    "    sys.exit(0)\n"
)


@pytest.mark.slow
def test_proceso_real_con_pipe_cerrado_no_deja_traceback():
    proc = subprocess.Popen(
        [sys.executable, "-c", GUION],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
    )
    try:
        assert proc.stdout is not None
        proc.stdout.read(10)
        proc.stdout.close()          # el hijo recibe EPIPE en la próxima escritura
        err = proc.stderr.read()
        rc = proc.wait(timeout=90)
    finally:
        proc.stderr.close()
        if proc.poll() is None:
            proc.kill()

    assert rc == 0, f"código de salida {rc}; stderr: {err[:400]!r}"
    assert b"BrokenPipeError" not in err
    assert b"Traceback" not in err
