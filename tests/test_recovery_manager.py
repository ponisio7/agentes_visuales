# tests/test_recovery_manager.py
"""Paradas duras del recovery (V3.8-1).

Antes, cualquier fallo terminal gastaba un intento de Plan B (llamada al
LLM, dinero y tiempo). Aquí se prueba que los fallos que NO se arreglan
reescribiendo el plan se clasifican como parada dura y no permiten reintento,
mientras que los fallos recuperables (incluido el fallo de aceptación en
runtime, que es justo lo que el Plan B debe arreglar) siguen permitiéndolo.

Sin red, sin Qt y sin LLM: el módulo es puro.
"""
import pytest

from core.recovery_manager import (
    API_KEY_MISSING,
    BUDGET_EXCEEDED,
    DEPENDENCY_MISSING,
    INVALID_CONTRACT,
    INVALID_PROBLEM,
    PARADAS_DURAS,
    SECURITY_BLOCK,
    TIMEOUT_GLOBAL,
    RecoveryDecision,
    RecoveryManager,
    clasificar_fallo,
    coste_estimado_estrategia,
    descripcion_codigo,
)

# ============================================================
# Catálogo
# ============================================================

def test_catalogo_de_paradas_duras_completo():
    assert set(PARADAS_DURAS) == {
        API_KEY_MISSING,
        DEPENDENCY_MISSING,
        INVALID_PROBLEM,
        SECURITY_BLOCK,
        INVALID_CONTRACT,
        BUDGET_EXCEEDED,
        TIMEOUT_GLOBAL,
    }
    for codigo in PARADAS_DURAS:
        assert descripcion_codigo(codigo)


# ============================================================
# Clasificación por texto
# ============================================================

@pytest.mark.parametrize(
    "mensaje",
    [
        "No se encontró DEEPSEEK_API_KEY. Opciones: ...",
        "Error 401: Unauthorized",
        "HTTP 401 invalid api key",
        "Cliente LLM no disponible. Verifica la configuración.",
    ],
)
def test_api_key_missing(mensaje):
    assert clasificar_fallo(mensaje) == API_KEY_MISSING


@pytest.mark.parametrize(
    "mensaje",
    [
        "No module named 'playwright'",
        "Executable doesn't exist at /home/u/.cache/ms-playwright/chromium",
        "Please run the following command to download new browsers",
    ],
)
def test_dependency_missing(mensaje):
    assert clasificar_fallo(mensaje) == DEPENDENCY_MISSING


def test_dependency_missing_por_origen_importacion():
    """El scheduler marca los fallos de importación del executor con origen."""
    assert clasificar_fallo("cualquier cosa", origen="importacion") == DEPENDENCY_MISSING


def test_invalid_problem():
    assert clasificar_fallo("El problema no puede estar vacío") == INVALID_PROBLEM
    assert clasificar_fallo("", origen="planificacion") == INVALID_PROBLEM


@pytest.mark.parametrize(
    "mensaje",
    [
        "Operación bloqueada por seguridad",
        "Ruta no permitida: ./../../etc/passwd",
        "URL no permitida por la política",
        "Kill switch activo",
    ],
)
def test_security_block(mensaje):
    assert clasificar_fallo(mensaje) == SECURITY_BLOCK


def test_invalid_contract():
    assert (
        clasificar_fallo("BLOQUEANTE: contrato de aceptación imposible")
        == INVALID_CONTRACT
    )


def test_budget_exceeded_por_texto():
    assert clasificar_fallo("Presupuesto agotado: 0 tokens restantes") == BUDGET_EXCEEDED


def test_timeout_global_por_texto():
    assert clasificar_fallo("Timeout global de la ejecución superado") == TIMEOUT_GLOBAL


# ============================================================
# Falsos positivos: lo que NO debe ser parada dura
# ============================================================

def test_aceptacion_runtime_es_recuperable():
    """El fallo de aceptación en runtime es lo que el Plan B debe arreglar."""
    assert clasificar_fallo("Aceptación fallida: el DOCX no tiene imagen") is None
    # Aunque el motivo mencione algo que en otro contexto sería parada dura.
    assert clasificar_fallo("Aceptación fallida: ruta no permitida") is None


@pytest.mark.parametrize(
    "mensaje",
    [
        "Error: fallo simulado",
        "falló la aceptación",
        "sin imagen",
        "NameError: name 'resultado' is not defined",
        "Timeout después de 30s",
        "Se descargaron 401 bytes",
        "El JSON no tiene la clave 'empresas'",
    ],
)
def test_fallos_recuperables_no_son_parada_dura(mensaje):
    assert clasificar_fallo(mensaje) is None


def test_mensaje_vacio_no_es_parada_dura():
    assert clasificar_fallo("") is None
    assert clasificar_fallo(None) is None  # type: ignore[arg-type]


# ============================================================
# Decisiones
# ============================================================

def test_decision_parada_dura_no_permite_reintento_ni_coste():
    manager = RecoveryManager()
    decision = manager.decidir(error="No se encontró DEEPSEEK_API_KEY")

    assert decision.parada_dura is True
    assert decision.permitir_reintento is False
    assert decision.codigo == API_KEY_MISSING
    assert decision.coste_estimado == 0.0
    assert decision.prioridad == 100
    assert decision.estrategia == ""


def test_decision_recuperable_permite_reintento_con_estrategia_y_coste():
    manager = RecoveryManager()
    decision = manager.decidir(error="Error: fallo simulado", intento=1)

    assert decision.parada_dura is False
    assert decision.permitir_reintento is True
    assert decision.codigo is None
    assert decision.estrategia == "correccion_puntual"
    assert decision.coste_estimado > 0
    assert decision.prioridad == 50


def test_escalera_de_estrategias_por_intento():
    manager = RecoveryManager()
    assert manager.decidir(error="fallo", intento=1).estrategia == "correccion_puntual"
    assert manager.decidir(error="fallo", intento=3).estrategia == "cambiar_tipo_agente"
    # La prioridad baja conforme se agotan intentos, pero nunca a 0.
    assert manager.decidir(error="fallo", intento=5).prioridad == 10


def test_codigo_forzado_tiene_prioridad_sobre_el_texto():
    manager = RecoveryManager()
    decision = manager.decidir(
        error="Error: fallo simulado", codigo_forzado=TIMEOUT_GLOBAL
    )
    assert decision.codigo == TIMEOUT_GLOBAL
    assert decision.parada_dura is True


def test_presupuesto_agotado_es_parada_dura_sin_depender_del_texto():
    manager = RecoveryManager()
    decision = manager.decidir(error="Error: fallo simulado", presupuesto_agotado=True)
    assert decision.codigo == BUDGET_EXCEEDED
    assert decision.parada_dura is True


def test_budget_inyectado_se_consulta():
    class _BudgetAgotado:
        def agotado(self):
            return True

    manager = RecoveryManager(budget=_BudgetAgotado())
    assert manager.decidir(error="cualquier fallo").codigo == BUDGET_EXCEEDED


def test_budget_que_falla_no_rompe_la_recuperacion():
    class _BudgetRoto:
        def agotado(self):
            raise RuntimeError("boom")

    manager = RecoveryManager(budget=_BudgetRoto())
    decision = manager.decidir(error="Error: fallo simulado")
    assert decision.permitir_reintento is True


# ============================================================
# Historial y serialización
# ============================================================

def test_historial_de_paradas_y_reset():
    manager = RecoveryManager()
    assert manager.hubo_parada_dura() is False

    manager.decidir(error="Cliente LLM no disponible")
    manager.decidir(error="Error: fallo simulado")  # recuperable: no se registra

    assert manager.hubo_parada_dura() is True
    paradas = manager.paradas()
    assert len(paradas) == 1
    assert paradas[0]["codigo"] == API_KEY_MISSING
    assert manager.ultima_parada().codigo == API_KEY_MISSING

    manager.reset()
    assert manager.hubo_parada_dura() is False
    assert manager.paradas() == []


def test_decision_es_serializable():
    manager = RecoveryManager()
    decision = manager.decidir(
        error="Error: fallo simulado", agente=type("A", (), {"nombre": "Paso1"})(),
        intento=2,
    )
    datos = decision.to_dict()
    assert datos["permitir_reintento"] is True
    assert datos["detalles"]["agente"] == "Paso1"
    assert datos["detalles"]["intento"] == 2
    # El dict es JSON-friendly (solo tipos básicos).
    import json

    assert json.loads(json.dumps(datos))["estrategia"] == "cambiar_configuracion"


def test_recuperable_incluye_el_error_en_detalles():
    manager = RecoveryManager()
    decision = manager.decidir(error="NameError: name 'x' is not defined")
    assert "NameError" in decision.detalles["error"]


def test_coste_estimado_estrategia_conoce_la_escalera():
    assert coste_estimado_estrategia("correccion_puntual") == 1.0
    assert coste_estimado_estrategia("fallback_alternativo") == 2.0
    # Estrategia desconocida: coste por defecto, nunca 0.
    assert coste_estimado_estrategia("inventada") > 0


def test_recovery_decision_por_defecto_es_reintento():
    decision = RecoveryDecision(permitir_reintento=True, motivo="ok")
    assert decision.parada_dura is False
    assert decision.codigo is None
