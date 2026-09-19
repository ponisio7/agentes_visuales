# tests/test_urls_plantilla.py
"""
Tests de URLs con plantilla ({Agente.clave}).

El planificador referencia a menudo la URL de una dependencia, p. ej.
"{Buscar.resultados[0].href}". Esa URL no se puede validar de forma
estática y debe resolverse en tiempo de ejecución.
"""

import pytest

from core.agent import Agente, AgenteValidator, TipoAgente
from core.executors import browser_executor
from core.executors.browser_executor import BrowserExecutor


def test_validar_url_plantilla_browser():
    valido, _ = AgenteValidator.validar_url("{Buscar.resultados[0].href}")

    assert valido is True


def test_validar_url_plantilla_http():
    agente = Agente(nombre="ObtenerDatos", tipo=TipoAgente.HTTP,
                    url_http="{Fuente.items[0].url}")

    assert agente.validar_configuracion() == (True, "")


def test_validar_url_sigue_rechazando_no_urls():
    assert AgenteValidator.validar_url("no-es-una-url")[0] is False
    assert AgenteValidator.validar_url("")[0] is False
    assert AgenteValidator.validar_url("ftp://ejemplo.test")[0] is False


def test_agente_browser_con_url_plantilla_es_valido():
    agente = Agente(
        nombre="ExtraerNoticiasUcrania",
        tipo=TipoAgente.BROWSER,
        url_browser="{BuscarNoticiasUcrania.resultados[0].href}",
        acciones_browser=[{"tipo": "extraer", "selector": "body", "nombre": "texto"}],
    )

    assert agente.validar_configuracion() == (True, "")


def test_browser_url_sin_resolver_devuelve_error_claro(monkeypatch):
    """Si el contexto no tiene la dependencia, la URL queda con llaves."""
    monkeypatch.setattr(browser_executor, "PLAYWRIGHT_DISPONIBLE", True)
    agente = Agente(
        nombre="ExtraerNoticias",
        tipo=TipoAgente.BROWSER,
        url_browser="{Buscar.resultados[0].href}",
    )

    ok, mensaje, resultado = BrowserExecutor.ejecutar(agente, {})

    assert ok is False
    assert resultado["error"] == "unresolved_url"
    assert "sin resolver" in mensaje
