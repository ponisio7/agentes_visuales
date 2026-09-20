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


class TestEstadoHonesto:
    """H6: la ejecución se guarda como 'fallida' si no pasa la aceptación."""

    class _DbFalso:
        def __init__(self):
            self.db_path = ":memory:"
            self.estado_guardado = None

        def guardar_ejecucion(self, agentes, duracion_total, estado="completada", **kwargs):
            self.estado_guardado = estado
            return 123

    class _SchedulerFalso:
        def __init__(self, aceptada: bool):
            self._aceptada = aceptada
            self.agentes = {}

        def obtener_resultado_aceptacion(self):
            return {
                "aceptada": self._aceptada,
                "motivos": [] if self._aceptada else ["x"],
            }

    def _plan(self):
        from core.problem_solver.models import ExecutionPlan, StepPlan

        return ExecutionPlan(problema_original="x", pasos=[
            StepPlan(orden=1, nombre="P", tipo_agente="Python", configuracion={})
        ])

    def test_sin_aceptacion_se_guarda_fallida(self, monkeypatch):
        # Sin hilo de aprendizaje: solo interesa el estado que se persiste.
        monkeypatch.setattr(
            "core.execution_recorder.threading.Thread",
            lambda *a, **k: type("_T", (), {"start": lambda self: None})(),
        )
        from core.execution_recorder import registrar_ejecucion_en_aprendizaje

        db = self._DbFalso()
        registrar_ejecucion_en_aprendizaje(
            scheduler=self._SchedulerFalso(aceptada=True),
            db=db,
            plan=self._plan(),
            problema="x",
            duracion_total=1.0,
        )
        assert db.estado_guardado == "completada"

        db2 = self._DbFalso()
        registrar_ejecucion_en_aprendizaje(
            scheduler=self._SchedulerFalso(aceptada=False),
            db=db2,
            plan=self._plan(),
            problema="x",
            duracion_total=1.0,
        )
        assert db2.estado_guardado == "fallida"


class TestPersistenciaProblema:
    """H5: el recorder guarda problema, plan, resultado y aceptación."""

    def test_recorder_guarda_problema_y_aceptacion(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "core.execution_recorder.threading.Thread",
            lambda *a, **k: type("_T", (), {"start": lambda self: None})(),
        )
        from core.execution_recorder import registrar_ejecucion_en_aprendizaje
        from core.problem_solver.models import ExecutionPlan, StepPlan
        from storage.database import Database

        db = Database(str(tmp_path / "hist.db"))
        agente = Agente(
            nombre="EscribirDocumento",
            tipo=TipoAgente.FILE,
            estado=EstadoAgente.ERROR,
            resultado={"error": "0 imágenes"},
        )

        class _Scheduler:
            _agentes_plan_original = []

            def __init__(self):
                self.agentes = {agente.id: agente}

            def obtener_resultado_aceptacion(self):
                return {
                    "aceptada": False,
                    "motivos": ["'EscribirDocumento': 0 imágenes raster (mínimo 1)"],
                }

        plan = ExecutionPlan(
            problema_original="crea un docx con una imagen",
            titulo="Documento",
            pasos=[
                StepPlan(orden=1, nombre="EscribirDocumento", tipo_agente="File")
            ],
        )

        ejecucion_id = registrar_ejecucion_en_aprendizaje(
            scheduler=_Scheduler(),
            db=db,
            plan=plan,
            problema="crea un docx con una imagen",
            duracion_total=1.5,
        )

        fila = db.obtener_ejecucion(ejecucion_id)
        assert fila["problema"] == "crea un docx con una imagen"
        assert fila["estado"] == "fallida"
        assert fila["aceptada"] == 0
        assert "imágenes" in fila["motivo_fallo"]
        assert "EscribirDocumento" in fila["plan_json"]


def test_serializar_plan_es_seguro():
    from core.execution_recorder import _serializar_plan
    from core.problem_solver.models import ExecutionPlan, StepPlan

    assert _serializar_plan(None) == ""
    plan = ExecutionPlan(titulo="t", pasos=[StepPlan(nombre="A", tipo_agente="Python")])
    texto = _serializar_plan(plan)
    assert "A" in texto and "Python" in texto
