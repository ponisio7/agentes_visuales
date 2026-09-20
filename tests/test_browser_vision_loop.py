# tests/test_browser_vision_loop.py
"""Bucle Browser + Visión (V3.8-5).

Sin Playwright y sin modelo real: el driver y la función de decisión se
inyectan. Lo que se prueba es el CICLO (percibir → razonar → actuar →
verificar), los límites duros, el allowlist, el kill switch y la traza con
``{accion, objetivo, razon, evidencia}``.
"""
import pytest

from core.browser_vision_loop import (
    BrowserVisionLoop,
    DriverNavegador,
    configuracion_vision_loop,
)

PNG = b"\x89PNG\r\n\x1a\n"


class _Reloj:
    """Reloj inyectable para probar el límite de tiempo sin dormir."""

    def __init__(self, t: float = 0.0):
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def avanzar(self, delta: float) -> None:
        self.t += float(delta)


class _Driver:
    """Driver falso: capturas finitas, acciones que pueden fallar."""

    def __init__(self, *, capturas=None, ok=True, contexto="texto visible"):
        self.capturas = capturas if capturas is not None else [PNG] * 100
        self.ok = ok
        self.contexto = contexto
        self.ejecutadas: list[dict] = []
        self.llamadas_captura = 0

    def capturar(self):
        self.llamadas_captura += 1
        indice = min(self.llamadas_captura - 1, len(self.capturas) - 1)
        return self.capturas[indice]

    def ejecutar(self, accion):
        self.ejecutadas.append(accion)
        return {"ok": self.ok, "detalle": "hecho"}

    def estado_texto(self):
        return self.contexto


def _hacer_driver(capturas=None, ok=True, contexto="texto visible"):
    """Devuelve (fachada, registrador) para poder inspeccionar lo ejecutado."""
    d = _Driver(capturas=capturas, ok=ok, contexto=contexto)
    fachada = DriverNavegador(capturar=d.capturar, ejecutar=d.ejecutar,
                              contexto=d.estado_texto)
    return fachada, d


def _driver(capturas=None, ok=True, contexto="texto visible") -> DriverNavegador:
    return _hacer_driver(capturas=capturas, ok=ok, contexto=contexto)[0]


def _decision(tipo="click", **extra):
    return {
        "razon": "porque sí",
        "evidencia": "se ve el botón",
        "objetivo": "pulsar el botón",
        "accion": {"tipo": tipo, "selector": "#b", **extra},
    }


@pytest.fixture(autouse=True)
def _vision_activa(monkeypatch):
    monkeypatch.setenv("AGENTES_VISION_HABILITADA", "1")


# ============================================================
# Guardrails previos
# ============================================================

def test_kill_switch_desactivado_no_arranca(monkeypatch):
    monkeypatch.delenv("AGENTES_VISION_HABILITADA", raising=False)
    fachada, driver = _hacer_driver()
    bucle = BrowserVisionLoop(fachada, None, instruccion="x",
                              decidir=lambda *a: pytest.fail("no debe decidir"))

    resultado = bucle.ejecutar()

    assert resultado["ok"] is False
    assert resultado["motivo"] == "vision_deshabilitada"
    assert resultado["capturas"] == 0
    assert resultado["trace"] == []
    assert driver.llamadas_captura == 0


def test_sin_driver_y_sin_instruccion():
    assert BrowserVisionLoop(None, None, instruccion="x").ejecutar()["motivo"] == (
        "sin_driver"
    )
    assert BrowserVisionLoop(_driver(), None, instruccion="  ").ejecutar()["motivo"] == (
        "sin_instruccion"
    )


def test_captura_no_disponible():
    driver = DriverNavegador(capturar=lambda: None, ejecutar=lambda a: {"ok": True})
    bucle = BrowserVisionLoop(driver, None, instruccion="x",
                              decidir=lambda *a: _decision())

    resultado = bucle.ejecutar()

    assert resultado["motivo"] == "captura_no_disponible"
    assert resultado["trace"] == []


# ============================================================
# Ciclo
# ============================================================

def test_objetivo_alcanzado_sin_accion():
    fachada, driver = _hacer_driver()
    bucle = BrowserVisionLoop(
        fachada, None, instruccion="lee el precio",
        decidir=lambda *a: {"razon": "ya está", "evidencia": "", "objetivo": "",
                            "accion": None},
    )

    resultado = bucle.ejecutar()

    assert resultado["ok"] is True
    assert resultado["motivo"] == "objetivo_alcanzado"
    assert resultado["capturas"] == 1
    assert resultado["pasos"] == 0
    assert driver.ejecutadas == []


def test_ciclo_percibir_actuar_verificar():
    fachada, driver = _hacer_driver()
    decisiones = [_decision(), {"razon": "", "evidencia": "", "objetivo": "",
                                "accion": None}]
    bucle = BrowserVisionLoop(
        fachada, None, instruccion="pulsa y comprueba",
        decidir=lambda *a: decisiones.pop(0),
    )

    resultado = bucle.ejecutar()

    assert resultado["ok"] is True
    assert resultado["motivo"] == "objetivo_alcanzado"
    # Una acción ejecutada y una captura POSTERIOR (verificación).
    assert resultado["capturas"] == 2
    assert resultado["pasos"] == 1
    assert driver.ejecutadas == [{"tipo": "click", "selector": "#b"}]
    paso = resultado["trace"][0]
    assert paso["accion"] == "click"
    assert paso["objetivo"] == "pulsar el botón"
    assert paso["razon"] == "porque sí"
    assert paso["evidencia"] == "se ve el botón"
    assert paso["ok"] is True
    assert paso["captura"] == 1


def test_la_instruccion_y_el_contexto_llegan_a_la_decision():
    visto = {}

    def _decidir(captura, instruccion, contexto):
        visto["captura"] = captura
        visto["instruccion"] = instruccion
        visto["contexto"] = contexto
        return {"accion": None, "razon": "", "evidencia": "", "objetivo": ""}

    BrowserVisionLoop(
        _driver(contexto="DOM de la página"),
        None,
        instruccion="busca empresas",
        decidir=_decidir,
    ).ejecutar()

    assert visto["captura"] == PNG
    assert visto["instruccion"] == "busca empresas"
    assert visto["contexto"] == "DOM de la página"


# ============================================================
# Límites duros
# ============================================================

def test_limite_de_pasos():
    bucle = BrowserVisionLoop(
        _driver(), None, instruccion="x", max_steps=4,
        decidir=lambda *a: _decision(),
    )

    resultado = bucle.ejecutar()

    assert resultado["motivo"] == "pasos_agotados"
    assert resultado["pasos"] == 4
    assert resultado["ok"] is False


def test_limite_de_capturas():
    bucle = BrowserVisionLoop(
        _driver(), None, instruccion="x", max_steps=50, max_capturas=2,
        decidir=lambda *a: _decision(),
    )

    resultado = bucle.ejecutar()

    assert resultado["motivo"] == "capturas_agotadas"
    assert resultado["capturas"] == 2
    assert resultado["pasos"] == 2


def test_limite_de_tiempo():
    reloj = _Reloj()

    def _capturar():
        reloj.avanzar(100)
        return PNG

    driver = DriverNavegador(capturar=_capturar, ejecutar=lambda a: {"ok": True})
    bucle = BrowserVisionLoop(
        driver, None, instruccion="x", max_steps=50, max_segundos=50,
        decidir=lambda *a: _decision(), ahora=reloj,
    )

    resultado = bucle.ejecutar()

    assert resultado["motivo"] == "tiempo_agotado"
    assert resultado["pasos"] == 1


def test_fallos_consecutivos_cortan_el_bucle():
    driver = _driver(ok=False)
    bucle = BrowserVisionLoop(
        driver, None, instruccion="x", max_steps=20, max_fallos_consecutivos=3,
        decidir=lambda *a: _decision(),
    )

    resultado = bucle.ejecutar()

    assert resultado["motivo"] == "fallos_consecutivos"
    assert resultado["pasos"] == 3
    assert all(not paso["ok"] for paso in resultado["trace"])


def test_limites_por_defecto():
    cfg = configuracion_vision_loop()
    assert cfg["max_steps"] == 20
    assert cfg["max_segundos"] == 120.0
    assert cfg["max_capturas"] == 30


def test_limites_desde_entorno(monkeypatch):
    monkeypatch.setenv("AGENTES_VISION_MAX_STEPS", "7")
    monkeypatch.setenv("AGENTES_VISION_MAX_CAPTURAS", "9")
    bucle = BrowserVisionLoop(_driver(), None, instruccion="x")

    assert bucle.max_steps == 7
    assert bucle.max_capturas == 9


# ============================================================
# Allowlist y cancelación
# ============================================================

def test_accion_fuera_del_allowlist_no_se_ejecuta():
    fachada, driver = _hacer_driver()
    bucle = BrowserVisionLoop(
        fachada, None, instruccion="x",
        decidir=lambda *a: _decision(tipo="borrar_todo"),
    )

    resultado = bucle.ejecutar()

    assert resultado["motivo"] == "accion_no_permitida:borrar_todo"
    assert resultado["pasos"] == 0
    assert driver.ejecutadas == []


def test_cancelacion_corta_el_bucle():
    class _TokenCancelado:
        def esta_cancelado(self):
            return True

    bucle = BrowserVisionLoop(
        _driver(), None, instruccion="x", cancellation_token=_TokenCancelado(),
        decidir=lambda *a: _decision(),
    )

    resultado = bucle.ejecutar()

    assert resultado["motivo"] == "cancelado"
    assert resultado["capturas"] == 0


def test_sin_decision_se_detiene():
    bucle = BrowserVisionLoop(_driver(), None, instruccion="x",
                              decidir=lambda *a: None)
    resultado = bucle.ejecutar()
    assert resultado["motivo"] == "sin_decision"


def test_error_de_decision_no_rompe():
    def _decidir(*a):
        raise RuntimeError("modelo caído")

    bucle = BrowserVisionLoop(_driver(), None, instruccion="x", decidir=_decidir)
    resultado = bucle.ejecutar()
    assert resultado["motivo"].startswith("error_decision:")


def test_error_de_captura_no_rompe():
    def _capturar():
        raise RuntimeError("sin navegador")

    driver = DriverNavegador(capturar=_capturar, ejecutar=lambda a: {"ok": True})
    bucle = BrowserVisionLoop(driver, None, instruccion="x",
                              decidir=lambda *a: _decision())
    assert bucle.ejecutar()["motivo"].startswith("error_captura:")


# ============================================================
# Integración con BrowserExecutor (opt-in, sin Playwright)
# ============================================================

def test_browser_ejecuta_accion_vision_deshabilitada(monkeypatch):
    monkeypatch.delenv("AGENTES_VISION_HABILITADA", raising=False)
    from core.executors.browser_executor import BrowserExecutor

    registros, cancelado = BrowserExecutor._ejecutar_acciones(
        page=None,
        acciones=[{"tipo": "vision", "instruccion": "haz algo"}],
        variables={},
        timeout_s=5,
        timeout_accion_ms=1000,
        agente=None,
        datos_extraidos={},
        screenshots=[],
    )

    assert cancelado is False
    assert len(registros) == 1
    assert registros[0]["tipo"] == "vision"
    assert registros[0]["ok"] is False
    assert "deshabilitada" in registros[0]["detalle"]


def test_vision_no_esta_en_el_vocabulario_del_vlm():
    from core.executors.browser_executor import ACCIONES_BUCLE, ACCIONES_VALIDAS
    from core.vision import ACCIONES_PERMITIDAS

    assert "vision" in ACCIONES_BUCLE
    assert "vision" not in ACCIONES_VALIDAS
    assert "vision" not in ACCIONES_PERMITIDAS, (
        "el VLM no debe poder proponer el bucle (evita recursión)"
    )


def test_crear_driver_playwright_tolera_una_pagina_rota():
    from core.browser_vision_loop import crear_driver_playwright

    class _PaginaRota:
        def screenshot(self, **kwargs):
            raise RuntimeError("sin página")

        def inner_text(self, *a, **k):
            raise RuntimeError("sin página")

    driver = crear_driver_playwright(_PaginaRota())

    assert driver.capturar() is None
    assert driver.estado_texto() == ""
