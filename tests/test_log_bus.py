# tests/test_log_bus.py
"""Bus de logs unificado (H4): buffer acotado, cursor, anti-recursión, GUI/web.

No se arranca Qt ni servidores reales: se usa el cliente de test de Flask y el
propio ``logging``.
"""
import logging

import pytest

from core.log_bus import (
    BusLogs,
    HandlerBus,
    instalar_handler,
    obtener_bus_logs,
    reset_bus_logs,
)


@pytest.fixture(autouse=True)
def _limpiar_bus():
    reset_bus_logs()
    yield
    reset_bus_logs()


# ============================================================
# BUFFER Y CURSOR
# ============================================================

def _record(mensaje: str, nivel=logging.INFO, nombre="test") -> logging.LogRecord:
    return logging.LogRecord(
        name=nombre, level=nivel, pathname=__file__, lineno=1,
        msg=mensaje, args=(), exc_info=None,
    )


def test_agregar_y_leer_por_cursor():
    bus = BusLogs()
    bus.agregar(_record("uno"))
    bus.agregar(_record("dos"))

    entradas, cursor = bus.desde(0)
    assert [e.mensaje for e in entradas] == ["uno", "dos"]
    assert cursor == 2

    # Sin novedades desde el cursor.
    entradas2, cursor2 = bus.desde(cursor)
    assert entradas2 == []
    assert cursor2 == cursor

    bus.agregar(_record("tres"))
    entradas3, cursor3 = bus.desde(cursor)
    assert [e.mensaje for e in entradas3] == ["tres"]
    assert cursor3 == 3


def test_buffer_acotado_no_crece_sin_limite():
    bus = BusLogs(max_entradas=3)
    for i in range(10):
        bus.agregar(_record(f"m{i}"))

    assert len(bus.ultimas(100)) == 3
    assert [e.mensaje for e in bus.ultimas(3)] == ["m7", "m8", "m9"]


def test_cursor_avanza_aunque_el_buffer_haya_rotado():
    bus = BusLogs(max_entradas=2)
    for i in range(5):
        bus.agregar(_record(f"m{i}"))

    entradas, cursor = bus.desde(0)
    assert [e.mensaje for e in entradas] == ["m3", "m4"]
    assert cursor == 5


def test_limite_de_lectura():
    bus = BusLogs()
    for i in range(10):
        bus.agregar(_record(f"m{i}"))

    entradas, cursor = bus.desde(0, limite=3)
    assert [e.mensaje for e in entradas] == ["m0", "m1", "m2"]
    assert cursor == 3


# ============================================================
# HANDLER
# ============================================================

def test_instalar_handler_captura_logs_y_es_idempotente():
    instalar_handler(nivel=logging.INFO)
    instalar_handler(nivel=logging.INFO)  # no debe duplicar

    logger = logging.getLogger("test.log_bus.emisor")
    logger.info("hola bus")

    bus = obtener_bus_logs()
    mensajes = [e.mensaje for e in bus.ultimas(50)]
    assert "hola bus" in mensajes

    handlers = [
        h for h in logging.getLogger().handlers if isinstance(h, HandlerBus)
    ]
    assert len(handlers) == 1


def test_el_bus_no_se_captura_a_si_mismo():
    bus = BusLogs()
    handler = HandlerBus(bus)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        logging.getLogger("core.log_bus").info("no debe aparecer")
        logging.getLogger("otro").info("sí debe aparecer")
    finally:
        root.removeHandler(handler)

    mensajes = [e.mensaje for e in bus.ultimas(50)]
    assert "no debe aparecer" not in mensajes
    assert "sí debe aparecer" in mensajes


def test_reset_desinstala_el_handler():
    instalar_handler()
    assert any(isinstance(h, HandlerBus) for h in logging.getLogger().handlers)

    reset_bus_logs()
    assert not any(isinstance(h, HandlerBus) for h in logging.getLogger().handlers)


# ============================================================
# WEB /api/logs
# ============================================================

def test_endpoint_web_logs_por_cursor():
    from web.app import ColaTrabajos, create_app

    instalar_handler()
    logging.getLogger("test.web").info("mensaje web")

    app = create_app(ColaTrabajos(), version="test")
    cliente = app.test_client()

    respuesta = cliente.get("/api/logs?cursor=0")
    assert respuesta.status_code == 200
    datos = respuesta.get_json()
    assert datos["ok"] is True
    assert any("mensaje web" in e["mensaje"] for e in datos["entradas"])
    assert datos["cursor"] >= 1

    # Con el cursor nuevo ya no hay novedades.
    respuesta2 = cliente.get(f"/api/logs?cursor={datos['cursor']}")
    assert respuesta2.get_json()["entradas"] == []
