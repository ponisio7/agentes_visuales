# tests/test_aceptacion_plan.py
"""Contrato de aceptación en el plan: builder, validador estático y main.

Cubre la parte de H6 que ocurre ANTES de ejecutar:

* el builder declara invariantes automáticos de los pasos que producen
  artefactos (existe/no vacío, imagen raster, documento con imagen);
* el validador rechaza de forma estática los contratos imposibles
  (archivo que ningún paso produce, imagen exigida sin fuente de imagen);
* ``main._calcular_estado_final`` convierte «terminó» en «completada/fallida».
"""
import logging

from core.problem_solver.builder import PlanBuilder
from core.problem_solver.code_corrector import PythonCodeCorrector
from core.problem_solver.models import ContratoAceptacion, ExecutionPlan, StepPlan
from core.problem_solver.prompt_builder import PromptBuilder
from core.problem_solver.validator import PlanValidator
from main import _calcular_estado_final


def _builder_validator():
    log = logging.getLogger("test.aceptacion_plan")
    log.addHandler(logging.NullHandler())
    validador = PlanValidator(log)
    return PlanBuilder(log, PythonCodeCorrector(), validador), validador


def _plan_file(destino: str, config_extra: dict | None = None) -> ExecutionPlan:
    config = {"operacion": "escribir", "archivo_destino": destino}
    config.update(config_extra or {})
    plan = ExecutionPlan(problema_original="escribe un documento")
    plan.pasos = [
        StepPlan(
            orden=1,
            nombre="Productor",
            tipo_agente="Python",
            dependencia_ids=[],
            configuracion={"codigo": "resultado = {'texto': 'hola'}"},
        ),
        StepPlan(
            orden=2,
            nombre="Escritor",
            tipo_agente="File",
            dependencia_ids=["Productor"],
            configuracion=config,
        ),
    ]
    return plan


# ============================================================
# BUILDER: INVARIANTES AUTOMÁTICOS
# ============================================================

def test_builder_deriva_archivo_de_paso_file():
    builder, _ = _builder_validator()
    plan = _plan_file("informe.txt")
    agentes = builder.generar_agentes(plan)

    escritor = next(a for a in agentes if a.nombre == "Escritor")
    assert escritor.contrato_aceptacion == {
        "archivos": ["informe.txt"],
        "imagenes": [],
        "formato_imagen": None,
        "min_bytes": 0,
        "min_caracteres": 0,
        "json_parseable": False,
        "claves_requeridas": [],
        "requiere_imagen": False,
        "min_imagenes": 0,
        "min_items": 0,
        "max_errores": None,
    }


def test_builder_deriva_imagen_raster_de_paso_png():
    builder, _ = _builder_validator()
    plan = _plan_file("grafico.png")
    agentes = builder.generar_agentes(plan)

    escritor = next(a for a in agentes if a.nombre == "Escritor")
    assert escritor.contrato_aceptacion["imagenes"] == ["grafico.png"]


def test_builder_deriva_imagen_incrustada_si_el_enunciado_la_pide():
    builder, _ = _builder_validator()
    plan = _plan_file("cuento.docx")
    plan.problema_original = "escribe un cuento con una ilustracion"
    agentes = builder.generar_agentes(plan)

    escritor = next(a for a in agentes if a.nombre == "Escritor")
    assert escritor.contrato_aceptacion["requiere_imagen"] is True
    assert escritor.contrato_aceptacion["min_imagenes"] == 1


def test_builder_no_exige_imagen_si_el_enunciado_no_la_pide():
    builder, _ = _builder_validator()
    plan = _plan_file("cuento.docx")
    plan.problema_original = "escribe un cuento de terror"
    agentes = builder.generar_agentes(plan)

    escritor = next(a for a in agentes if a.nombre == "Escritor")
    assert escritor.contrato_aceptacion["requiere_imagen"] is False


def test_builder_respeta_contrato_declarado_por_el_llm():
    builder, _ = _builder_validator()
    plan = builder.construir_plan("x", {
        "titulo": "t",
        "pasos": [{
            "orden": 1,
            "nombre": "Escritor",
            "tipo": "File",
            "configuracion": {"operacion": "escribir", "archivo_destino": "d.json"},
            "es_critico": True,
            "aceptacion": {"archivos": ["d.json"], "json_parseable": True},
        }],
    })

    assert plan.pasos[0].es_critico is True
    assert plan.pasos[0].aceptacion.archivos == ["d.json"]
    assert plan.pasos[0].aceptacion.json_parseable is True


def test_contrato_from_dict_tolera_alias_y_basura():
    contrato = ContratoAceptacion.from_dict({"ficheros": "x.txt", "min_imagen": "2"})
    assert contrato.archivos == ["x.txt"]
    assert contrato.min_imagenes == 2
    assert contrato.requiere_imagen is True

    assert ContratoAceptacion.from_dict(None) is None
    assert ContratoAceptacion.from_dict({"desconocido": 1}) is None


# ============================================================
# VALIDADOR ESTÁTICO
# ============================================================

def test_validador_rechaza_imagen_exigida_sin_fuente():
    builder, validador = _builder_validator()
    plan = _plan_file("cuento.docx")
    plan.pasos[1].aceptacion = ContratoAceptacion(
        archivos=["cuento.docx"], requiere_imagen=True, min_imagenes=1
    )
    # El productor no menciona ninguna imagen.
    plan.pasos[0].configuracion = {"codigo": "resultado = {'texto': 'hola'}"}
    plan.agentes_generados = builder.generar_agentes(plan)

    ok, errores = validador.validar_plan(plan)

    assert ok is False
    assert any("imagen" in e.lower() for e in errores)


def test_validador_acepta_contrato_de_imagen_con_fuente():
    builder, validador = _builder_validator()
    plan = _plan_file("cuento.docx")
    plan.pasos[0].configuracion = {
        "codigo": "resultado = {'texto': 'hola', 'imagenes': ['x.png']}"
    }
    plan.pasos[1].aceptacion = ContratoAceptacion(
        archivos=["cuento.docx"], requiere_imagen=True, min_imagenes=1
    )
    plan.agentes_generados = builder.generar_agentes(plan)

    ok, errores = validador.validar_plan(plan)

    assert ok is True, errores


def test_validador_rechaza_archivo_que_nadie_produce():
    builder, validador = _builder_validator()
    plan = _plan_file("cuento.docx")
    plan.pasos[1].aceptacion = ContratoAceptacion(archivos=["otro_fichero.docx"])
    plan.agentes_generados = builder.generar_agentes(plan)

    ok, errores = validador.validar_plan(plan)

    assert ok is False
    assert any("otro_fichero.docx" in e for e in errores)


# ============================================================
# PROMPT
# ============================================================

def test_prompt_declara_contrato_de_aceptacion():
    prompt = PromptBuilder.build_system_prompt()

    assert "CONTRATO DE ACEPTACIÓN" in prompt
    assert "aceptacion" in prompt
    assert "es_critico" in prompt
    assert "requiere_imagen" in prompt

    user_prompt = PromptBuilder.build_user_prompt("haz un cuento con imagen")
    assert "aceptacion" in user_prompt
    assert "es_critico" in user_prompt


# ============================================================
# MAIN: ok/estado HONESTOS
# ============================================================

def test_main_ok_solo_si_todos_completados_y_aceptado():
    agentes_ok = [{"nombre": "a", "ok": True}, {"nombre": "b", "ok": True}]
    aceptado = {"aceptada": True}
    rechazado = {"aceptada": False}

    assert _calcular_estado_final(agentes_ok, aceptado) == (True, "completada")
    assert _calcular_estado_final(agentes_ok, rechazado) == (False, "fallida")

    agentes_con_error = [{"nombre": "a", "ok": True}, {"nombre": "b", "ok": False}]
    assert _calcular_estado_final(agentes_con_error, aceptado) == (False, "fallida")
    assert _calcular_estado_final([], aceptado) == (False, "fallida")
