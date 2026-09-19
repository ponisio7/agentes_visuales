# tests/test_search_executor.py
"""
Tests unitarios del SearchExecutor.

No tocan la red: el cliente DDGS se sustituye por un doble controlable.
"""

import pytest

from core.agent import Agente, TipoAgente
from core.cancellation import CancellationToken
from core.executors import search_executor
from core.executors.search_executor import SearchExecutor


class _FakeDDGS:
    """Doble de DDGS: devuelve resultados según el backend."""

    #: {backend: [resultados]} o {None: [...]}
    resultados: dict = {}
    #: Si se define, text() lanza esta excepción.
    excepcion: Exception | None = None
    #: Última consulta recibida (para comprobar la sustitución de variables).
    ultima_query: str | None = None

    def __init__(self, timeout=None, **kwargs):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def text(self, keywords, region=None, safesearch="moderate",
             timelimit=None, backend="auto", max_results=None):
        type(self).ultima_query = keywords
        if type(self).excepcion is not None:
            raise type(self).excepcion
        if backend in type(self).resultados:
            return list(type(self).resultados[backend])
        return list(type(self).resultados.get(None, []))


@pytest.fixture(autouse=True)
def _fake_ddgs(monkeypatch):
    _FakeDDGS.resultados = {}
    _FakeDDGS.excepcion = None
    _FakeDDGS.ultima_query = None
    monkeypatch.setattr(search_executor, "DDGS", _FakeDDGS)
    return _FakeDDGS


def _agente(**kwargs) -> Agente:
    base = dict(nombre="BuscarFuentes", tipo=TipoAgente.SEARCH, query_search="python")
    base.update(kwargs)
    return Agente(**base)


def test_busqueda_ok_devuelve_contrato():
    _FakeDDGS.resultados = {None: [
        {"title": "Python", "href": "https://python.org", "body": "lenguaje"},
        {"title": "Docs", "href": "https://docs.python.org", "body": "documentación"},
    ]}

    ok, mensaje, resultado = SearchExecutor.ejecutar(_agente(), {})

    assert ok is True
    assert resultado["query"] == "python"
    assert resultado["total"] == 2
    assert resultado["error"] is None
    assert set(resultado.keys()) == {"query", "resultados", "total", "error", "duracion"}
    assert resultado["resultados"][0] == {
        "title": "Python", "href": "https://python.org", "body": "lenguaje",
    }


def test_query_vacia_no_llama_al_buscador():
    ok, mensaje, resultado = SearchExecutor.ejecutar(_agente(query_search="   "), {})

    assert ok is False
    assert resultado["error"] == "empty_query"
    assert resultado["total"] == 0
    assert _FakeDDGS.ultima_query is None


def test_sin_libreria_disponible(monkeypatch):
    monkeypatch.setattr(search_executor, "DDGS", None)

    ok, mensaje, resultado = SearchExecutor.ejecutar(_agente(), {})

    assert ok is False
    assert resultado["error"] == "search_unavailable"
    assert "duckduckgo" in mensaje.lower()


def test_error_del_buscador_se_captura():
    _FakeDDGS.excepcion = RuntimeError("boom")

    ok, mensaje, resultado = SearchExecutor.ejecutar(_agente(), {})

    assert ok is False
    assert "boom" in (resultado["error"] or "")
    assert resultado["total"] == 0
    assert resultado["resultados"] == []


def test_cancelado_antes_de_ejecutar():
    token = CancellationToken()
    token.cancelar()

    ok, mensaje, resultado = SearchExecutor.ejecutar(_agente(), {}, token)

    assert ok is False
    assert resultado["error"] == "cancelled"


def test_fallback_al_backend_html():
    """Si el backend automático no da resultados, se reintenta con 'html'."""
    _FakeDDGS.resultados = {
        "auto": [],
        "html": [{"title": "T", "href": "https://ejemplo.com", "body": "b"}],
    }

    ok, mensaje, resultado = SearchExecutor.ejecutar(_agente(), {})

    assert ok is True
    assert resultado["total"] == 1
    assert resultado["resultados"][0]["href"] == "https://ejemplo.com"


def test_sustituye_variables_en_la_query():
    _FakeDDGS.resultados = {None: [{"title": "T", "href": "https://x.com", "body": "b"}]}
    contexto = {"Fuente": {"tema": "energía solar"}}

    ok, mensaje, resultado = SearchExecutor.ejecutar(
        _agente(query_search="{Fuente.tema} precios"), contexto
    )

    assert ok is True
    assert _FakeDDGS.ultima_query == "energía solar precios"


def test_max_resultados_se_limita_al_rango():
    capturado = {}

    class _FakeLimite(_FakeDDGS):
        def text(self, keywords, region=None, safesearch="moderate",
                 timelimit=None, backend="auto", max_results=None):
            capturado["max"] = max_results
            return []

    search_executor.DDGS = _FakeLimite
    SearchExecutor.ejecutar(_agente(max_resultados_search=999), {})

    assert capturado["max"] == search_executor.MAX_RESULTADOS
