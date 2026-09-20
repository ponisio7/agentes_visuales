# tests/test_scheduler_refactor.py
"""Refactor del Scheduler (V3.8-6): responsabilidades extraídas.

Se prueban las funciones puras que salieron de ``core/scheduler.py`` y que el
orquestador siga delegando en ellas sin cambiar el comportamiento.
"""
import pytest

from core import dependency_manager as dm
from core.acceptance_manager import calcular_aceptacion
from core.agent import Agente, EstadoAgente, TipoAgente
from core.execution_log import resumir_error, resumir_resultado
from core.scheduler import Scheduler


def _agente(nombre, tipo=TipoAgente.PYTHON, **kwargs) -> Agente:
    return Agente(nombre=nombre, tipo=tipo, **kwargs)


# ============================================================
# dependency_manager
# ============================================================

def test_resolver_dependencias_resuelve_y_avisa():
    a = _agente("A")
    b = _agente("B", dependencias_nombres=["A", "Z"])
    agentes = {a.id: a, b.id: b}

    no_encontradas = dm.resolver_dependencias(agentes)

    assert b.dependencias_ids == [a.id]
    assert b.dependencias_nombres == []
    assert no_encontradas == [("B", "Z")]


def test_detectar_ciclos():
    a = _agente("A", dependencias_nombres=["B"])
    b = _agente("B", dependencias_nombres=["A"])
    agentes = {a.id: a, b.id: b}
    dm.resolver_dependencias(agentes)

    hay, ciclos = dm.detectar_ciclos(agentes)

    assert hay is True
    assert ciclos


def test_sin_ciclos():
    a = _agente("A")
    b = _agente("B", dependencias_nombres=["A"])
    agentes = {a.id: a, b.id: b}
    dm.resolver_dependencias(agentes)

    assert dm.detectar_ciclos(agentes) == (False, [])


def test_validar_fuentes_loop():
    fuente = _agente("Fuente")
    loop_ok = _agente(
        "Loop", tipo=TipoAgente.LOOP,
        dependencias_nombres=["Fuente"], fuente_items="Fuente.items",
    )
    loop_mal = _agente(
        "LoopMal", tipo=TipoAgente.LOOP,
        dependencias_nombres=["Fuente"], fuente_items="Otra.items",
    )
    agentes = {fuente.id: fuente, loop_ok.id: loop_ok}
    dm.resolver_dependencias(agentes)

    valido, errores = dm.validar_fuentes_loop(agentes)
    assert valido is True
    assert errores == []

    agentes[loop_mal.id] = loop_mal
    dm.resolver_dependencias(agentes)
    valido, errores = dm.validar_fuentes_loop(agentes)
    assert valido is False
    assert any("Otra" in e for e in errores)


def test_dependencias_pendientes_y_fallidas():
    a = _agente("A")
    b = _agente("B", dependencias_nombres=["A"])
    agentes = {a.id: a, b.id: b}
    dm.resolver_dependencias(agentes)

    assert dm.obtener_dependencias_pendientes(b, agentes) == [a.id]
    assert dm.tiene_dependencias_fallidas(b, agentes) is False

    a.estado = EstadoAgente.COMPLETADO
    assert dm.obtener_dependencias_pendientes(b, agentes) == []
    assert dm.tiene_dependencias_fallidas(b, agentes) is False

    a.estado = EstadoAgente.ERROR
    assert dm.tiene_dependencias_fallidas(b, agentes) is True


# ============================================================
# acceptance_manager
# ============================================================

def test_aceptacion_sin_fallos():
    a = _agente("A")
    a.estado = EstadoAgente.COMPLETADO

    resultado = calcular_aceptacion({a.id: a})

    assert resultado["aceptada"] is True
    assert resultado["motivos"] == []
    assert resultado["resumen"] == "aceptada"


def test_aceptacion_rechaza_agente_fallido():
    a = _agente("A")
    a.estado = EstadoAgente.ERROR
    a.mensaje = "boom"

    resultado = calcular_aceptacion({a.id: a})

    assert resultado["aceptada"] is False
    assert "boom" in resultado["motivos"][0]


def test_aceptacion_rechaza_verificacion_no_aceptada():
    a = _agente("A", es_critico=True)
    a.estado = EstadoAgente.COMPLETADO
    verificacion = {
        "aceptado": False,
        "motivos": ["falta la imagen"],
        "comprobaciones": [{"no_verificable": True}],
    }

    resultado = calcular_aceptacion({a.id: a}, {a.id: verificacion})

    assert resultado["aceptada"] is False
    assert "falta la imagen" in resultado["motivos"][0]
    assert resultado["no_verificables"] == 1
    assert resultado["verificada"] is True
    assert resultado["pasos"][0]["nombre"] == "A"


def test_aceptacion_rechaza_critico_sin_verificacion():
    a = _agente("A", es_critico=True)
    a.estado = EstadoAgente.COMPLETADO

    resultado = calcular_aceptacion({a.id: a})

    assert resultado["aceptada"] is False
    assert "sin verificación" in resultado["motivos"][0]


def test_aceptacion_expone_parada_dura_y_presupuesto():
    class _Presupuesto:
        def resumen(self):
            return {"consumo": {"llamadas": 2}, "agotado": False}

    a = _agente("A")
    a.estado = EstadoAgente.COMPLETADO
    parada = {"codigo": "API_KEY_MISSING", "motivo": "falta la key"}

    resultado = calcular_aceptacion(
        {a.id: a}, parada_dura=parada, presupuesto=_Presupuesto()
    )

    assert resultado["parada_dura"] == parada
    assert resultado["presupuesto"]["consumo"]["llamadas"] == 2


def test_presupuesto_roto_no_rompe_la_aceptacion():
    class _PresupuestoRoto:
        def resumen(self):
            raise RuntimeError("boom")

    a = _agente("A")
    a.estado = EstadoAgente.COMPLETADO
    resultado = calcular_aceptacion({a.id: a}, presupuesto=_PresupuestoRoto())
    assert "presupuesto" not in resultado
    assert resultado["aceptada"] is True


# ============================================================
# execution_log
# ============================================================

def test_resumir_resultado_http():
    a = _agente("A", tipo=TipoAgente.HTTP)
    texto = resumir_resultado(a, {"status_code": 200, "url": "https://x.test/a",
                                  "json": {"data": [1, 2]}})
    assert "HTTP 200" in texto
    assert "data" in texto


def test_resumir_resultado_llm_con_tokens():
    a = _agente("A", tipo=TipoAgente.LLM)
    texto = resumir_resultado(a, {"respuesta": "hola", "tokens_uso": {"total": 12}})
    assert "hola" in texto
    assert "12 tokens" in texto


def test_resumir_resultado_none():
    a = _agente("A")
    assert resumir_resultado(a, None) == "sin resultado"


def test_resumir_resultado_trunca():
    a = _agente("A", tipo=TipoAgente.PYTHON)
    texto = resumir_resultado(a, "x" * 500, max_len=20)
    assert texto == "x" * 20 + "..."


def test_resumir_error_incluye_detalles():
    a = _agente("A")
    texto = resumir_error(a, "falló", {"stderr": "traceback", "status_code": 500})
    assert "falló" in texto
    assert "traceback" in texto
    assert "HTTP 500" in texto


def test_resumir_error_trunca():
    a = _agente("A")
    texto = resumir_error(a, "x" * 500, None, max_len=30)
    assert len(texto) == 33


# ============================================================
# El Scheduler delega (sin cambiar la API)
# ============================================================

@pytest.fixture
def scheduler():
    s = Scheduler(max_concurrent=1)
    yield s
    try:
        s.detener()
    except Exception:
        pass
    s._executor.shutdown(wait=True, cancel_futures=True)


def test_scheduler_delega_dependencias(scheduler):
    a = _agente("A")
    b = _agente("B", dependencias_nombres=["A"])
    scheduler.agregar_agentes([a, b])

    scheduler.resolver_dependencias()

    assert b.dependencias_ids == [a.id]
    assert scheduler.detectar_ciclos() == (False, [])
    assert scheduler._obtener_dependencias_pendientes(b) == [a.id]
    assert scheduler._tiene_dependencias_fallidas(b) is False


def test_scheduler_delega_logs(scheduler):
    a = _agente("A")
    assert scheduler._resumir_resultado_log(a, None) == "sin resultado"
    assert "falló" in scheduler._resumir_error_log(a, "falló", None)


def test_scheduler_delega_aceptacion(scheduler):
    a = _agente("A")
    a.estado = EstadoAgente.ERROR
    a.mensaje = "boom"
    scheduler.agregar_agentes([a])

    resultado = scheduler.obtener_resultado_aceptacion()

    assert resultado["aceptada"] is False
    assert "presupuesto" in resultado
