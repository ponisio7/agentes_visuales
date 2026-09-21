"""Regresión 011 — persistir la ejecución: fallo visible, sin ids inventados.

Bug 2.5 del ROADMAP (punto 4.2):

    ``registrar_ejecucion_en_aprendizaje`` capturaba cualquier excepción al
    guardar y devolvía ``None`` con un simple *warning*: fallo silencioso. La
    GUI lo tapaba con ``_obtener_ultimo_ejecucion_id_fallback``, que hacía
    ``SELECT MAX(id) FROM ejecuciones``. Si de verdad hubiera fallado el
    registro, ese id sería el de **otra** ejecución y el feedback humano
    quedaría colgado de la fila equivocada: peor que no pedirlo.

Estado real medido: en los logs del 15 al 21 de septiembre (6 días de uso) el
fallo **no ocurrió ni una vez** — cero apariciones de «No se pudo guardar la
ejecución» y de «fallback: usando último id». El parche era código muerto que
además introducía un riesgo de atribución errónea.

Arreglo: el fallo al persistir se registra como ERROR (accionable) y el
llamador NO inventa un id; si no hay id real, no se pide feedback.
"""
from __future__ import annotations

import inspect
import logging

import pytest

from core.execution_recorder import registrar_ejecucion_en_aprendizaje


class _DBQueFalla:
    db_path = "/tmp/inexistente/agent_history.db"

    def guardar_ejecucion(self, *a, **k):
        raise OSError("disco lleno")


class _DBQueFunciona:
    db_path = ":memory:"

    def guardar_ejecucion(self, *a, **k):
        return 4242


class _SchedulerFalso:
    agentes: dict = {}
    _agentes_plan_original: list = []
    presupuesto = None

    def obtener_resultado_aceptacion(self):
        return {"aceptada": True, "motivos": []}


# ---------------------------------------------------------------------------
# 1. El fallo al persistir es VISIBLE
# ---------------------------------------------------------------------------

def test_si_no_se_puede_guardar_se_loguea_error(caplog):
    caplog.set_level(logging.ERROR, logger="core.execution_recorder")

    resultado = registrar_ejecucion_en_aprendizaje(
        scheduler=_SchedulerFalso(), db=_DBQueFalla(), plan=None,
        problema="crear saludo.txt", duracion_total=1.0,
    )

    assert resultado is None
    assert "No se pudo guardar la ejecución" in caplog.text
    # Accionable: dice que NO se pedirá feedback y por qué ruta mirar.
    assert "no se inventa un id" in caplog.text
    assert "disco lleno" in caplog.text


def test_un_registro_correcto_no_loguea_error(caplog):
    caplog.set_level(logging.ERROR, logger="core.execution_recorder")

    resultado = registrar_ejecucion_en_aprendizaje(
        scheduler=_SchedulerFalso(), db=_DBQueFunciona(), plan=None,
        problema="crear saludo.txt", duracion_total=1.0,
    )

    assert resultado == 4242
    assert "No se pudo guardar la ejecución" not in caplog.text


# ---------------------------------------------------------------------------
# 2. La GUI ya NO inventa un id
# ---------------------------------------------------------------------------

def test_la_gui_no_usa_select_max_id():
    """Guarda contra la reintroducción del parche de atribución errónea.

    Se analiza el AST y se miran solo los literales de cadena: así un
    comentario que *mencione* la consulta (para explicar por qué no se usa) no
    hace fallar la comprobación.
    """
    import ast

    import ui.simple_main_window as ventana

    fuente = inspect.getsource(ventana)
    arbol = ast.parse(fuente)

    consultas_max = [
        nodo.value
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.Constant)
        and isinstance(nodo.value, str)
        and "SELECT" in nodo.value.upper()
        and "MAX(ID)" in nodo.value.upper()
    ]

    assert consultas_max == [], f"consulta MAX(id) reintroducida: {consultas_max}"
    assert "_obtener_ultimo_ejecucion_id_fallback" not in fuente


class _VentanaFalsa:
    _ultimo_plan = object()
    _ultimo_problema = "crear saludo.txt"
    _tiempo_inicio_ejecucion = None
    scheduler = object()
    db = object()

    def __init__(self):
        self.logs: list[str] = []

    def _log(self, mensaje, *a, **k):
        self.logs.append(mensaje)


def test_si_el_registro_falla_no_se_devuelve_ningun_id(monkeypatch):
    import ui.simple_main_window as ventana

    monkeypatch.setattr(
        ventana, "registrar_ejecucion_en_aprendizaje", lambda **k: None
    )
    falsa = _VentanaFalsa()

    resultado = ventana.SimpleMainWindow._persistir_ejecucion_si_hay_plan(falsa)

    assert resultado is None, "no debe inventarse un id"
    assert not any("ejecución guardada" in m for m in falsa.logs)


def test_si_el_registro_funciona_se_devuelve_el_id_real(monkeypatch):
    import ui.simple_main_window as ventana

    monkeypatch.setattr(
        ventana, "registrar_ejecucion_en_aprendizaje", lambda **k: 777
    )
    falsa = _VentanaFalsa()

    resultado = ventana.SimpleMainWindow._persistir_ejecucion_si_hay_plan(falsa)

    assert resultado == 777
    assert any("ejecución guardada" in m for m in falsa.logs)


def test_sin_plan_no_se_persiste(monkeypatch):
    import ui.simple_main_window as ventana

    falsa = _VentanaFalsa()
    falsa._ultimo_plan = None

    assert ventana.SimpleMainWindow._persistir_ejecucion_si_hay_plan(falsa) is None


@pytest.mark.parametrize("excepcion", [RuntimeError("boom"), OSError("io")])
def test_una_excepcion_del_registro_no_escapa(monkeypatch, excepcion):
    import ui.simple_main_window as ventana

    def _explota(**k):
        raise excepcion

    monkeypatch.setattr(ventana, "registrar_ejecucion_en_aprendizaje", _explota)
    falsa = _VentanaFalsa()

    assert ventana.SimpleMainWindow._persistir_ejecucion_si_hay_plan(falsa) is None
