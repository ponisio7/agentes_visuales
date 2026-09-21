"""Regresión 002 — ``weasyprint.progress`` no debe contaminar los logs.

Bug 1.16 del ROADMAP (punto 3.6):

    ``weasyprint`` no estaba en la lista de loggers silenciados de
    ``main._configurar_logging``, pese a que ``file_executor`` lo usa para
    generar PDF. Su logger ``progress`` escribe una línea por cada recurso
    externo resuelto y tapa la información útil.

El test comprueba el **nivel efectivo** del logger hijo real
(``weasyprint.progress``), no solo la tupla de nombres: un silenciado que no
alcance al hijo no serviría de nada.
"""
from __future__ import annotations

import logging

import pytest

import main


@pytest.fixture
def logging_aislado(tmp_path, monkeypatch):
    """Ejecuta ``_configurar_logging`` sin ensuciar el ``logs/`` del repo."""
    monkeypatch.chdir(tmp_path)
    niveles_previos = {
        nombre: logging.getLogger(nombre).level
        for nombre in ("weasyprint", "weasyprint.progress")
    }
    yield
    for nombre, nivel in niveles_previos.items():
        logging.getLogger(nombre).setLevel(nivel)


def test_weasyprint_queda_silenciado(logging_aislado):
    main._configurar_logging(quiet=True)

    assert logging.getLogger("weasyprint").level == logging.WARNING


def test_weasyprint_progress_queda_silenciado_de_verdad(logging_aislado):
    """El logger ruidoso es el hijo: debe heredar el WARNING del padre."""
    main._configurar_logging(quiet=True)

    assert logging.getLogger("weasyprint.progress").getEffectiveLevel() == (
        logging.WARNING
    )


def test_weasyprint_progress_no_emite_en_nivel_info(logging_aislado, caplog):
    """Prueba de comportamiento: un INFO de progress ya no se registra."""
    main._configurar_logging(quiet=True)

    with caplog.at_level(logging.INFO):
        logging.getLogger("weasyprint.progress").info("Step 1 - resuelto")

    assert "Step 1 - resuelto" not in caplog.text


def test_los_loggers_ya_silenciados_siguen_silenciados(logging_aislado):
    """No-regresión del resto de la lista (httpx2 se arregló en su día)."""
    main._configurar_logging(quiet=True)

    for nombre in ("httpx2", "httpx", "urllib3", "openai", "charset_normalizer"):
        assert logging.getLogger(nombre).level == logging.WARNING, nombre
