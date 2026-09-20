# tests/test_http_executor_recursos.py
"""Regresión: la sesión HTTP debe cerrarse siempre.

Bug encontrado en la auditoría de v3.1: ``HTTPExecutor.ejecutar`` creaba un
``requests.Session`` por petición y solo lo cerraba en las rutas de
cancelación/timeout. En la ruta de éxito la sesión quedaba abierta y su pool
de conexiones (sockets/fds) se liberaba únicamente cuando el GC recogía el
objeto; en procesos de larga duración eso acumula descriptores.
"""
import json
import uuid

import pytest

from core.agent import Agente, TipoAgente
from core.executors import http_executor as modulo


def _url_unica() -> str:
    """URL distinta por test para no chocar con la caché HTTP global."""
    return f"https://example.test/{uuid.uuid4().hex}"


class _RespuestaFalsa:
    def __init__(self, url: str):
        self.status_code = 200
        self.url = url
        self.headers = {"Content-Type": "application/json"}
        self.content = json.dumps({"ok": True}).encode("utf-8")
        self.text = json.dumps({"ok": True})

        class _Elapsed:
            @staticmethod
            def total_seconds() -> float:
                return 0.01

        self.elapsed = _Elapsed()

    def json(self):
        return {"ok": True}


class _SesionFalsa:
    """Sesión falsa que registra si se cerró."""

    def __init__(self):
        self.cerrada = False

    def mount(self, *_a, **_k):
        pass

    def get(self, url, **_k):
        return _RespuestaFalsa(url)

    def close(self):
        self.cerrada = True


@pytest.fixture
def sesiones(monkeypatch):
    """Sustituye requests.Session y devuelve la lista de sesiones creadas."""
    creadas: list[_SesionFalsa] = []

    def _fabrica():
        sesion = _SesionFalsa()
        creadas.append(sesion)
        return sesion

    monkeypatch.setattr(modulo.requests, "Session", _fabrica)
    return creadas


def _agente_http(url: str) -> Agente:
    return Agente(
        nombre="Peticion",
        tipo=TipoAgente.HTTP,
        url_http=url,
        metodo_http="GET",
        timeout_http=5,
    )


def test_sesion_se_cierra_en_exito(sesiones):
    exito, mensaje, _ = modulo.HTTPExecutor.ejecutar(_agente_http(_url_unica()), {})

    assert exito is True, mensaje
    assert len(sesiones) == 1
    assert sesiones[0].cerrada is True, "la sesión HTTP quedó abierta"


def test_sesion_se_cierra_tras_error_de_conexion(sesiones, monkeypatch):
    def _get_que_falla(*_a, **_k):
        raise modulo.requests.exceptions.ConnectionError("sin red")

    monkeypatch.setattr(_SesionFalsa, "get", _get_que_falla)

    exito, _, resultado = modulo.HTTPExecutor.ejecutar(_agente_http(_url_unica()), {})

    assert exito is False
    assert resultado["error"] == "connection_error"
    assert sesiones[0].cerrada is True, "la sesión HTTP quedó abierta tras el error"
