# tests/test_scheduler_aceptacion.py
"""Gate de aceptación en el Scheduler y en los artefactos de la ejecución (H6).

Casos obligatorios:

* un ``.docx`` sin imagen → ejecución FALLIDA con motivo;
* un ``.docx`` con imagen → ejecución COMPLETADA;
* JSON no parseable → FALLIDA;
* una ejecución con ``errores=1`` → FALLIDA (y guardada como ``fallida``,
  no como ``completada``: la regresión de la ejecución 469).
"""
import logging

import pytest
from docx import Document
from PIL import Image

from core.agent import Agente, EstadoAgente, TipoAgente
from core.problem_solver.builder import PlanBuilder
from core.problem_solver.code_corrector import PythonCodeCorrector
from core.problem_solver.models import ContratoAceptacion, ExecutionPlan, StepPlan
from core.problem_solver.validator import PlanValidator
from core.scheduler import Scheduler

TEXTO = (
    "En una ciudad pequena habia una panaderia que abria antes del amanecer. "
    "Cada manana el panadero encendia el horno y dejaba que el olor despertara "
    "a los vecinos. "
) * 3


def _builder_validator():
    log = logging.getLogger("test.aceptacion")
    log.addHandler(logging.NullHandler())
    validador = PlanValidator(log)
    return PlanBuilder(log, PythonCodeCorrector(), validador), validador


def _plan_documento(
    imagenes: list[str],
    aceptacion: ContratoAceptacion | None,
    *,
    problema: str = "documento con texto e imagen",
) -> ExecutionPlan:
    plan = ExecutionPlan(problema_original=problema)
    plan.pasos = [
        StepPlan(
            orden=1,
            nombre="GenerarContenido",
            tipo_agente="Python",
            dependencia_ids=[],
            configuracion={"codigo": (
                f"resultado = {{'texto': {TEXTO!r}, 'imagenes': {imagenes!r}}}"
            )},
        ),
        StepPlan(
            orden=2,
            nombre="EscribirDocumento",
            tipo_agente="File",
            dependencia_ids=["GenerarContenido"],
            configuracion={
                "operacion": "escribir",
                "archivo_destino": "documento.docx",
            },
            aceptacion=aceptacion,
            es_critico=True,
        ),
    ]
    return plan


def _preparar_scheduler(plan: ExecutionPlan) -> Scheduler:
    builder, validador = _builder_validator()
    plan.agentes_generados = builder.generar_agentes(plan)
    ok, errores = validador.validar_plan(plan)
    assert ok, errores

    scheduler = Scheduler(max_concurrent=2)
    scheduler.agregar_agentes(plan.agentes_generados)
    scheduler.resolver_dependencias()
    return scheduler


def _ejecutar(scheduler: Scheduler, qapp, esperar, timeout: float = 30.0):
    try:
        scheduler.iniciar()
        terminado = esperar(
            lambda: scheduler._terminado_notificado,
            timeout=timeout,
            qapp=qapp,
        )
        assert terminado, "la ejecución no terminó a tiempo"
    finally:
        try:
            scheduler.detener()
        except Exception:
            pass
        try:
            scheduler._executor.shutdown(wait=True, cancel_futures=True)
        except Exception:
            pass


@pytest.fixture
def cwd_temporal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# ============================================================
# .docx SIN IMAGEN → FALLIDA CON MOTIVO
# ============================================================

def test_docx_sin_imagen_ejecucion_fallida(cwd_temporal, qapp, esperar):
    contrato = ContratoAceptacion(
        archivos=["documento.docx"], requiere_imagen=True, min_imagenes=1
    )
    scheduler = _preparar_scheduler(_plan_documento([], contrato))

    _ejecutar(scheduler, qapp, esperar)

    escritor = scheduler.obtener_agente_por_nombre("EscribirDocumento")
    assert escritor.estado == EstadoAgente.ERROR

    aceptacion = scheduler.obtener_resultado_aceptacion()
    assert aceptacion["aceptada"] is False
    assert aceptacion["verificada"] is True
    assert any("imagen" in motivo.lower() for motivo in aceptacion["motivos"])
    assert "0 imágenes raster" in aceptacion["resumen"]

    # El motivo concreto llega al Plan B (recovery) y al historial.
    assert "Aceptación fallida" in (escritor.error or "")
    assert "documento.docx" in (escritor.error or "")


def test_docx_con_imagen_ejecucion_completada(cwd_temporal, qapp, esperar):
    imagen = cwd_temporal / "ilustracion.png"
    Image.new("RGB", (60, 40), "blue").save(imagen, format="PNG")

    contrato = ContratoAceptacion(
        archivos=["documento.docx"], requiere_imagen=True, min_imagenes=1
    )
    scheduler = _preparar_scheduler(_plan_documento([str(imagen)], contrato))

    _ejecutar(scheduler, qapp, esperar)

    escritor = scheduler.obtener_agente_por_nombre("EscribirDocumento")
    assert escritor.estado == EstadoAgente.COMPLETADO

    aceptacion = scheduler.obtener_resultado_aceptacion()
    assert aceptacion["aceptada"] is True, aceptacion["resumen"]
    assert aceptacion["verificada"] is True

    documento = Document(str(cwd_temporal / "documento.docx"))
    assert len(documento.inline_shapes) >= 1


def test_docx_sin_imagen_con_invariante_automatico(cwd_temporal, qapp, esperar):
    """Sin declarar aceptación, el builder la deriva del enunciado + destino."""
    plan = _plan_documento([], None, problema="escribe un cuento con una imagen")
    scheduler = _preparar_scheduler(plan)

    escritor = scheduler.obtener_agente_por_nombre("EscribirDocumento")
    assert escritor.contrato_aceptacion is not None
    assert escritor.contrato_aceptacion.get("requiere_imagen") is True

    _ejecutar(scheduler, qapp, esperar)
    assert scheduler.obtener_resultado_aceptacion()["aceptada"] is False


# ============================================================
# JSON NO PARSEABLE → FALLIDA
# ============================================================

def test_json_no_parseable_ejecucion_fallida(cwd_temporal, qapp, esperar):
    plan = ExecutionPlan(problema_original="produce datos.json")
    plan.pasos = [
        StepPlan(
            orden=1,
            nombre="ProducirJson",
            tipo_agente="Python",
            dependencia_ids=[],
            configuracion={"codigo": (
                "resultado = {'json': None, 'body': '{esto no es json'}"
            )},
            aceptacion=ContratoAceptacion(json_parseable=True),
            es_critico=True,
        ),
    ]
    scheduler = _preparar_scheduler(plan)
    _ejecutar(scheduler, qapp, esperar)

    agente = scheduler.obtener_agente_por_nombre("ProducirJson")
    assert agente.estado == EstadoAgente.ERROR

    aceptacion = scheduler.obtener_resultado_aceptacion()
    assert aceptacion["aceptada"] is False
    assert any("json" in motivo.lower() for motivo in aceptacion["motivos"])


# ============================================================
# ERRORES=1 → FALLIDA
# ============================================================

def test_ejecucion_con_errores_es_fallida(cwd_temporal, qapp, esperar):
    scheduler = Scheduler(max_concurrent=2)
    ok_agent = Agente(
        nombre="Correcto",
        tipo=TipoAgente.PYTHON,
        codigo_python="resultado = {'status': 'ok'}",
    )
    falla = Agente(
        nombre="Falla",
        tipo=TipoAgente.PYTHON,
        max_reintentos=0,
        codigo_python="raise ValueError('boom')",
    )
    scheduler.agregar_agentes([ok_agent, falla])
    scheduler.resolver_dependencias()

    _ejecutar(scheduler, qapp, esperar)

    stats = scheduler.obtener_estadisticas()
    assert stats["errores"] == 1
    assert stats["completados"] == 1

    aceptacion = scheduler.obtener_resultado_aceptacion()
    assert aceptacion["aceptada"] is False
    assert any("Falla" in motivo for motivo in aceptacion["motivos"])
