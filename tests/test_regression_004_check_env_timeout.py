"""Regresión 004 — ``--check-env --timeout N`` acota el comando completo.

Bug 1.17 del ROADMAP (punto 3.11):

    El ``timeout`` se pasaba a ``requests`` y solo acota CADA operación
    (connect + read), no el comando. Con dos endpoints y redirecciones, un
    ``--timeout 5`` podía tardar >10 s (se midió ``Respuesta 200 en 10949 ms``).

El arreglo impone un **presupuesto global** para el ping completo: cada
petición recibe solo el tiempo que queda y, agotado, no se prueba el siguiente
endpoint. Además se dejan de seguir redirecciones, porque cada salto es otra
petición que saldría del presupuesto.
"""
from __future__ import annotations

import pytest

import core.env_checker as ec


class _RespuestaFalsa:
    def __init__(self, status_code: int):
        self.status_code = status_code


@pytest.fixture
def requests_mock(monkeypatch):
    """Instala un ``requests.get`` falso y devuelve la lista de llamadas.

    ``respuestas`` se consume en orden; un elemento que sea ``Exception`` se
    **lanza** en vez de devolverse.
    """
    import requests

    llamadas: list[dict] = []

    def _instalar(respuestas=(), *, al_llamar=None):
        pendientes = list(respuestas)

        def fake_get(url, **kwargs):
            llamadas.append({"url": url, **kwargs})
            if al_llamar is not None:
                al_llamar(url)
            if pendientes:
                siguiente = pendientes.pop(0)
                if isinstance(siguiente, Exception):
                    raise siguiente
                return siguiente
            return _RespuestaFalsa(200)

        monkeypatch.setattr(requests, "get", fake_get)
        return llamadas

    return _instalar


@pytest.fixture
def reloj_controlable(monkeypatch):
    """Reloj monotónico controlado: permite simular tiempo sin dormir."""
    estado = {"t": 1000.0}
    monkeypatch.setattr(ec.time, "monotonic", lambda: estado["t"])
    return estado


# ---------------------------------------------------------------------------
# 1. El presupuesto global se respeta entre endpoints
# ---------------------------------------------------------------------------

def test_presupuesto_agotado_no_prueba_el_segundo_endpoint(
    requests_mock, reloj_controlable
):
    def _avanzar(_url):
        reloj_controlable["t"] += 5.0   # la primera petición agota el presupuesto

    solicitudes = requests_mock(
        [_RespuestaFalsa(404), _RespuestaFalsa(200)],  # 404 => probaría el segundo
        al_llamar=_avanzar,
    )

    exito, mensaje, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=5)

    assert exito is False
    assert detalles["error_tipo"] == "timeout"
    assert len(solicitudes) == 1, "no debe probar el segundo endpoint sin presupuesto"
    assert "Presupuesto" in mensaje


def test_sin_agotar_presupuesto_si_prueba_el_segundo_endpoint(requests_mock):
    llamadas = requests_mock([_RespuestaFalsa(404), _RespuestaFalsa(200)])

    exito, _, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=5)

    assert exito is True
    assert len(llamadas) == 2
    assert detalles["endpoint"] == "https://api.deepseek.com/"


# ---------------------------------------------------------------------------
# 2. Cada petición recibe solo el tiempo restante
# ---------------------------------------------------------------------------

def test_timeout_por_peticion_no_supera_el_presupuesto(requests_mock):
    llamadas = requests_mock([_RespuestaFalsa(200)])

    ec._ping_http_deepseek(None, "sk-x", timeout=5)

    assert 0 < llamadas[0]["timeout"] <= 5


def test_timeout_menor_se_propaga_tal_cual(requests_mock):
    llamadas = requests_mock([_RespuestaFalsa(200)])

    ec._ping_http_deepseek(None, "sk-x", timeout=0.5)

    assert 0 < llamadas[0]["timeout"] <= 0.5


# ---------------------------------------------------------------------------
# 3. No se siguen redirecciones (cada salto saldría del presupuesto)
# ---------------------------------------------------------------------------

def test_no_sigue_redirecciones(requests_mock):
    llamadas = requests_mock([_RespuestaFalsa(200)])

    ec._ping_http_deepseek(None, "sk-x", timeout=5)

    assert llamadas[0]["allow_redirects"] is False


def test_una_redireccion_cuenta_como_red_ok(requests_mock):
    """Un 3xx demuestra que hay red: no se sigue, pero se acepta como éxito."""
    requests_mock([_RespuestaFalsa(301)])

    exito, mensaje, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=5)

    assert exito is True
    assert detalles["status_code"] == 301
    assert "redirección" in mensaje


# ---------------------------------------------------------------------------
# 4. No-regresión: timeout <= 0 sigue llegando a requests (su ValueError)
# ---------------------------------------------------------------------------

def test_timeout_cero_sigue_pasando_a_requests(requests_mock):
    llamadas = requests_mock([ValueError("timeout must be positive")])

    exito, _, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=0)

    assert exito is False
    assert detalles["error_tipo"] == "valor_invalido"
    assert llamadas[0]["timeout"] == 0


# ---------------------------------------------------------------------------
# 5. Tope de reloj REAL (lo que requests no acota: DNS, cuerpo lento)
# ---------------------------------------------------------------------------

def test_el_ping_se_abandona_si_excede_el_presupuesto(monkeypatch):
    """Cubre la causa real: el resolver DNS puede bloquear ~20 s.

    El ``timeout`` de requests no lo acota, y un ``signal.setitimer`` tampoco
    (el handler de Python no corre mientras ``getaddrinfo`` está en C). Por eso
    el tope es "esperar como mucho N segundos y desistir".
    """
    import time as _time

    def _interno_lento(*_a, **_k):
        _time.sleep(5.0)
        return True, "nunca debería llegar", {}

    monkeypatch.setattr(ec, "_ping_http_deepseek_interno", _interno_lento)

    inicio = _time.monotonic()
    exito, mensaje, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=0.5)
    transcurrido = _time.monotonic() - inicio

    assert exito is False
    assert detalles["error_tipo"] == "timeout"
    assert "Presupuesto" in mensaje
    assert transcurrido < 3.0, f"tardó {transcurrido:.2f}s con presupuesto 0.5s"


def test_el_ping_devuelve_el_resultado_si_termina_a_tiempo(monkeypatch):
    monkeypatch.setattr(
        ec, "_ping_http_deepseek_interno",
        lambda *_a, **_k: (True, "ok", {"status_code": 200}),
    )

    exito, mensaje, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=2)

    assert exito is True
    assert detalles["status_code"] == 200


def test_excepcion_del_ping_se_propaga_al_hilo_principal(monkeypatch):
    def _explota(*_a, **_k):
        raise RuntimeError("boom interno")

    monkeypatch.setattr(ec, "_ping_http_deepseek_interno", _explota)

    with pytest.raises(RuntimeError, match="boom interno"):
        ec._ping_http_deepseek(None, "sk-x", timeout=2)
