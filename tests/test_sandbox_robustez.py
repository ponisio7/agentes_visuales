# tests/test_sandbox_robustez.py
"""Pruebas de regresión de robustez del sandbox.

Antes se usaba ``Popen(stdout=PIPE, stderr=PIPE)`` y se esperaba con
``poll()`` sin drenar: un hijo que escribía más de ~64 KB se bloqueaba
escribiendo, el sandbox lo mataba por falso timeout y se perdía el
resultado. Ahora stdout/stderr van a ficheros temporales, por lo que una
salida grande debe completar correctamente.
"""

import logging

import pytest

from core.sandbox import PythonSandbox


@pytest.fixture
def silenciar_logs_sandbox():
    """Evita que un stderr enorme inunde la salida de pytest.

    El sandbox registra el stderr filtrado con ``logger.error``; con un
    stderr de cientos de KB la salida de consola se vuelve inservible.
    """
    logger = logging.getLogger("core.sandbox")
    nivel_previo = logger.level
    logger.setLevel(logging.CRITICAL)
    yield
    logger.setLevel(nivel_previo)


class TestSalidaGrandeNoBloquea:
    def test_stdout_mayor_que_el_buffer_de_tuberia(self):
        codigo = """
linea = "x" * 40
for i in range(20000):
    print(f"linea {i} " + linea)
resultado = {'ok': True, 'lineas': 20000}
"""
        exito, mensaje, resultado = PythonSandbox.ejecutar(
            codigo, {}, timeout=60, use_cache=False
        )

        assert exito is True, mensaje
        assert resultado.get("ok") is True
        assert resultado.get("lineas") == 20000

    def test_stderr_mayor_que_el_buffer_de_tuberia(self, silenciar_logs_sandbox):
        codigo = """
import sys
linea = "e" * 40
for i in range(20000):
    print(f"error {i} " + linea, file=sys.stderr)
resultado = {'ok': True}
"""
        exito, mensaje, resultado = PythonSandbox.ejecutar(
            codigo, {}, timeout=60, use_cache=False
        )

        assert exito is True, mensaje
        assert resultado.get("ok") is True

    def test_stdout_y_stderr_grandes_a_la_vez(self, silenciar_logs_sandbox):
        codigo = """
import sys
for i in range(20000):
    print(f"out {i}")
    print(f"err {i}", file=sys.stderr)
resultado = {'ok': True}
"""
        exito, mensaje, resultado = PythonSandbox.ejecutar(
            codigo, {}, timeout=60, use_cache=False
        )

        assert exito is True, mensaje
        assert resultado.get("ok") is True


class TestRecursosCerrados:
    def test_ficheros_temporales_se_cierran(self, monkeypatch):
        """Invariante: stdout/stderr temporales se cierran al terminar.

        ``PythonSandbox.ejecutar`` los cierra en el ``finally`` del bloque
        principal. Este test mantiene la garantía bajo vigilancia: al
        conservar referencias fuertes a los ficheros creados, un cierre
        basado solo en el recolector de basura no bastaría para pasarlo, así
        que si alguien elimina ese cierre el descriptor quedaría abierto.
        """
        import tempfile as tempfile_mod

        real = tempfile_mod.TemporaryFile
        creados = []

        def _fabrica(*args, **kwargs):
            fichero = real(*args, **kwargs)
            creados.append(fichero)
            return fichero

        monkeypatch.setattr(tempfile_mod, "TemporaryFile", _fabrica)

        exito, mensaje, resultado = PythonSandbox.ejecutar(
            "resultado = {'ok': True}", {}, timeout=30, use_cache=False
        )

        assert exito is True, mensaje
        assert resultado.get("ok") is True
        assert len(creados) >= 2, "no se crearon los dos ficheros temporales"
        abiertos = [f for f in creados if not f.closed]
        assert abiertos == [], f"quedaron {len(abiertos)} ficheros temporales abiertos"
