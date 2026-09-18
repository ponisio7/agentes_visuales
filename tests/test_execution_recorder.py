# tests/test_execution_recorder.py
"""Pruebas de los helpers de snapshot de ``core/execution_recorder.py``.

El registro de aprendizaje corre en un hilo en segundo plano y NO debe leer
el scheduler vivo (mutado por la GUI/workers). Por eso se construye un
snapshot antes de lanzar el hilo; aquí se verifica que ese snapshot sea
correcto, esté deduplicado y que el resumen del plan elija al agente final.
"""

from types import SimpleNamespace

from core.agent import Agente, EstadoAgente, TipoAgente
from core.execution_recorder import (
    _construir_resumen_plan,
    _construir_snapshot,
    _snapshot_de_agente,
)


class TestSnapshotDeAgente:
    def test_snapshot_basico(self):
        agente = Agente(
            nombre="A1",
            descripcion="desc",
            tipo=TipoAgente.PYTHON,
            dependencias_ids=["d1"],
            estado=EstadoAgente.COMPLETADO,
            resultado={"clave": "valor"},
        )
        snap = _snapshot_de_agente(agente)

        assert snap is not None
        assert snap["nombre"] == "A1"
        assert snap["estado_real"] == "Completado"
        assert snap["descripcion"] == "desc"
        assert snap["proxy"].dependencias_ids == ["d1"]
        assert snap["proxy"].resultado["__tipo__"] == "dict"

    def test_snapshot_sin_resultado(self):
        agente = Agente(nombre="A2")
        snap = _snapshot_de_agente(agente)
        assert snap is not None
        assert snap["proxy"].resultado["__tipo__"] == "None"
        assert snap["estado_real"] == "Pendiente"

    def test_snapshot_dependencias_nombres_usa_ids_si_faltan(self):
        agente = Agente(nombre="A3", dependencias_ids=["d1", "d2"])
        snap = _snapshot_de_agente(agente)
        assert snap["proxy"].dependencias_nombres == ["d1", "d2"]


class TestConstruirSnapshot:
    def test_deduplica_por_id(self):
        agente = Agente(nombre="A1")
        scheduler = SimpleNamespace(
            _agentes_plan_original=[agente],
            agentes={agente.id: agente},
        )
        snapshot = _construir_snapshot(scheduler)
        assert len(snapshot) == 1
        assert snapshot[0]["nombre"] == "A1"

    def test_incluye_plan_original_y_agentes(self):
        plan = Agente(nombre="Plan")
        vivo = Agente(nombre="Vivo")
        scheduler = SimpleNamespace(
            _agentes_plan_original=[plan],
            agentes={vivo.id: vivo},
        )
        snapshot = _construir_snapshot(scheduler)
        nombres = {s["nombre"] for s in snapshot}
        assert nombres == {"Plan", "Vivo"}

    def test_sin_agentes(self):
        scheduler = SimpleNamespace(_agentes_plan_original=[], agentes={})
        assert _construir_snapshot(scheduler) == []


class TestResumenPlan:
    def test_usa_el_agente_final(self):
        primero = Agente(nombre="A1", resultado="resultado primero")
        final = Agente(
            nombre="A2",
            dependencias_ids=[primero.id],
            resultado="resultado final",
        )
        plan = SimpleNamespace(agentes_generados=[primero, final])
        scheduler = SimpleNamespace(agentes={})

        assert _construir_resumen_plan(scheduler, plan) == "resultado final"

    def test_agente_final_sin_resultado(self):
        final = Agente(nombre="A1")
        plan = SimpleNamespace(agentes_generados=[final])
        scheduler = SimpleNamespace(agentes={})

        resumen = _construir_resumen_plan(scheduler, plan)
        assert "sin resultado" in resumen

    def test_fallback_al_scheduler(self):
        ultimo = Agente(nombre="Ultimo", resultado="desde scheduler")
        scheduler = SimpleNamespace(agentes={ultimo.id: ultimo})

        assert _construir_resumen_plan(scheduler, None) == "desde scheduler"

    def test_sin_resultados(self):
        scheduler = SimpleNamespace(agentes={})
        assert _construir_resumen_plan(scheduler, None) == "(sin resultado del plan)"

    def test_plan_sin_atributo_agentes_generados(self):
        ultimo = Agente(nombre="Ultimo", resultado="ok")
        scheduler = SimpleNamespace(agentes={ultimo.id: ultimo})
        plan = SimpleNamespace()  # sin agentes_generados

        assert _construir_resumen_plan(scheduler, plan) == "ok"
