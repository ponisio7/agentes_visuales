# tests/test_flujo_e2e_aceptacion.py
"""Flujo end-to-end determinista: fallo de aceptación → Plan B → verificación.

No usa LLM: el Plan B se inyecta con un recovery falso. Ejercita el camino
real completo:

    plan → agentes (sandbox/File reales) → verificación en disco
         → FALLA (docx sin imagen) → Plan B → nuevo plan
         → verificación en disco → PASA → aceptación de la ejecución

Es la prueba de arquitectura de la REGLA DE ORO: «terminado» no es «resuelto».
"""
import pytest
from docx import Document
from PIL import Image

from core.agent import Agente, EstadoAgente, TipoAgente
from core.problem_solver.models import ContratoAceptacion
from core.scheduler import Scheduler

TEXTO = (
    "Habia una vez una panaderia que abria antes del amanecer y dejaba que "
    "el olor del pan despertara a todo el barrio. "
) * 3

CONTRATO_DOCX = ContratoAceptacion(
    archivos=["documento.docx"], requiere_imagen=True, min_imagenes=1
).to_dict()


class _PlanFalso:
    def __init__(self, agentes):
        self.agentes_generados = agentes


class _RecoveryFalso:
    """Recovery que captura el motivo y devuelve el plan B ya construido."""

    db_path = ""

    def __init__(self, agentes_plan_b):
        self._agentes = agentes_plan_b
        self.llamadas = []

    def generar_plan_b(self, **kwargs):
        self.llamadas.append(kwargs)
        return _PlanFalso(self._agentes)


def _agente_docx(nombre: str, *, es_critico: bool = True) -> Agente:
    return Agente(
        nombre=nombre,
        tipo=TipoAgente.FILE,
        dependencias_nombres=["GenerarContenido"],
        operacion_file="escribir",
        archivo_destino="documento.docx",
        es_critico=es_critico,
        contrato_aceptacion=CONTRATO_DOCX,
    )


def _agente_productor(nombre: str, imagenes: list[str]) -> Agente:
    return Agente(
        nombre=nombre,
        tipo=TipoAgente.PYTHON,
        codigo_python=f"resultado = {{'texto': {TEXTO!r}, 'imagenes': {imagenes!r}}}",
    )


@pytest.fixture
def cwd_temporal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _ejecutar(scheduler, qapp, esperar, timeout=60.0):
    try:
        scheduler.iniciar()
        assert esperar(
            lambda: scheduler._terminado_notificado, timeout=timeout, qapp=qapp
        ), "la ejecución no terminó a tiempo"
    finally:
        try:
            scheduler.detener()
        except Exception:
            pass
        scheduler._executor.shutdown(wait=True, cancel_futures=True)


def test_fallo_de_aceptacion_dispara_plan_b_y_acaba_aceptado(
    cwd_temporal, qapp, esperar
):
    # ── Plan A: produce texto SIN imágenes → el .docx no tendrá imagen ──
    a1 = _agente_productor("GenerarContenido", [])
    a2 = _agente_docx("EscribirDocumento")

    # ── Plan B: produce una imagen raster REAL → el .docx la inserta ──
    imagen = cwd_temporal / "ilustracion.png"
    Image.new("RGB", (60, 40), "blue").save(imagen, format="PNG")
    b1 = _agente_productor("GenerarContenido", [str(imagen)])
    b2 = _agente_docx("EscribirDocumento")

    scheduler = Scheduler(max_concurrent=2, max_intentos_plan_b=2)
    recovery = _RecoveryFalso([b1, b2])
    scheduler.set_contexto_plan_b(recovery, "cuento con imagen", None)

    scheduler.agregar_agentes([a1, a2])
    scheduler.resolver_dependencias()

    _ejecutar(scheduler, qapp, esperar)

    # 1. El Plan B se disparó con el motivo concreto de la aceptación.
    assert recovery.llamadas, "el fallo de aceptación no llegó al Plan B"
    motivo = recovery.llamadas[0].get("error", "")
    assert "Aceptación fallida" in motivo
    assert "imagen" in motivo.lower()

    # 2. El nuevo plan produjo el artefacto correcto.
    assert (cwd_temporal / "documento.docx").exists()
    documento = Document(str(cwd_temporal / "documento.docx"))
    assert len(documento.inline_shapes) >= 1, "el .docx final no tiene imagen"

    # 3. La ejecución se acepta: artefacto verificado en disco.
    aceptacion = scheduler.obtener_resultado_aceptacion()
    assert aceptacion["aceptada"] is True, aceptacion["resumen"]
    assert aceptacion["verificada"] is True

    # 4. El verificador dejó evidencia del artefacto.
    artefactos = []
    for paso in aceptacion["pasos"]:
        artefactos.extend(paso.get("criterios_comprobados") or [])
    assert any("documento.docx" in a for a in artefactos)


def test_ejecucion_sin_plan_b_queda_fallida(cwd_temporal, qapp, esperar):
    """Sin recovery, un artefacto que no cumple el contrato deja la ejecución
    fallida (no «completada»): la regresión de la ejecución 469."""
    a1 = _agente_productor("GenerarContenido", [])
    a2 = _agente_docx("EscribirDocumento")

    scheduler = Scheduler(max_concurrent=2)
    scheduler.agregar_agentes([a1, a2])
    scheduler.resolver_dependencias()

    _ejecutar(scheduler, qapp, esperar)

    assert a2.estado == EstadoAgente.ERROR
    aceptacion = scheduler.obtener_resultado_aceptacion()
    assert aceptacion["aceptada"] is False
    assert "imagen" in aceptacion["resumen"].lower()
