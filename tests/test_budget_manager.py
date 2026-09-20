# tests/test_budget_manager.py
"""Presupuesto de ejecución (V3.8-2): tiempo + llamadas + tokens + coste.

Sin límites configurados nada cambia (opt-in): el manager nunca está agotado.
Con límites, ``agotado()`` es la señal que el ``RecoveryManager`` convierte en
parada dura ``BUDGET_EXCEEDED``. El reloj es inyectable para no dormir en los
tests.
"""
import json

import pytest

from core.budget_manager import (
    MOTIVO_COSTE,
    MOTIVO_LLAMADAS,
    MOTIVO_TIEMPO,
    MOTIVO_TOKENS,
    BudgetManager,
    configuracion_presupuesto,
)
from core.llm_client import _notificar_observadores


class _ResultadoFalso:
    """Doble de ``LLMResultado`` con la misma interfaz usada por el budget."""

    def __init__(self, prompt: int, completion: int):
        self._prompt = prompt
        self._completion = completion

    def tokens(self):
        return {
            "prompt": self._prompt,
            "completion": self._completion,
            "total": self._prompt + self._completion,
        }


# ============================================================
# Opt-in: sin límites no hay agotamiento
# ============================================================

def test_sin_limites_nunca_esta_agotado():
    budget = BudgetManager()
    budget.iniciar(ahora=0.0)
    budget.registrar_llamada(tokens_prompt=10**6, tokens_completion=10**6, ahora=10**6)

    assert budget.hay_limites() is False
    assert budget.agotado(ahora=10**6) is False
    assert budget.motivo_agotado(ahora=10**6) is None
    assert budget.restante(ahora=10**6) == {
        "segundos": None,
        "llamadas": None,
        "tokens": None,
        "coste": None,
    }


# ============================================================
# Cada dimensión
# ============================================================

def test_limite_de_llamadas():
    budget = BudgetManager(max_llamadas=2)
    assert budget.agotado() is False
    budget.registrar_llamada()
    assert budget.agotado() is False
    budget.registrar_llamada()
    assert budget.agotado() is True
    assert budget.motivo_agotado() == MOTIVO_LLAMADAS
    assert budget.consumo.llamadas == 2


def test_limite_de_tokens():
    budget = BudgetManager(max_tokens=100)
    budget.registrar_llamada(tokens_prompt=60, tokens_completion=30)
    assert budget.agotado() is False
    budget.registrar_llamada(tokens_prompt=5, tokens_completion=5)
    assert budget.motivo_agotado() == MOTIVO_TOKENS
    assert budget.consumo.tokens_total == 100


def test_limite_de_tiempo():
    budget = BudgetManager(max_segundos=60)
    budget.iniciar(ahora=1000.0)
    assert budget.agotado(ahora=1030.0) is False
    assert budget.motivo_agotado(ahora=1060.0) == MOTIVO_TIEMPO


def test_limite_de_coste_con_precios():
    budget = BudgetManager(
        max_coste=1.0,
        precio_1k_prompt=1.0,
        precio_1k_completion=2.0,
    )
    # 500 prompt = 0.5 ; 100 completion = 0.2 → 0.7
    budget.registrar_llamada(tokens_prompt=500, tokens_completion=100)
    assert budget.consumo.coste == pytest.approx(0.7)
    assert budget.agotado() is False
    budget.registrar_llamada(tokens_prompt=500)
    assert budget.motivo_agotado() == MOTIVO_COSTE


def test_sin_precios_el_coste_es_cero_y_no_se_inventa_dinero():
    budget = BudgetManager(max_coste=5.0)
    budget.registrar_llamada(tokens_prompt=10**6, tokens_completion=10**6)
    assert budget.consumo.coste == 0.0
    budget.iniciar()
    assert budget.agotado() is False


# ============================================================
# Restante y resumen
# ============================================================

def test_restante_por_dimension():
    budget = BudgetManager(max_segundos=100, max_llamadas=5, max_tokens=1000, max_coste=10)
    budget.iniciar(ahora=0.0)
    budget.registrar_llamada(tokens_prompt=100, tokens_completion=100, ahora=20.0)

    restante = budget.restante(ahora=20.0)
    assert restante["segundos"] == pytest.approx(80.0)
    assert restante["llamadas"] == 4
    assert restante["tokens"] == 800
    assert restante["coste"] == pytest.approx(10.0)


def test_resumen_serializable():
    budget = BudgetManager(max_llamadas=3, moneda="EUR")
    budget.iniciar(ahora=0.0)
    budget.registrar_llamada(tokens_prompt=10, tokens_completion=5, ahora=1.0)

    resumen = budget.resumen(ahora=1.0)
    assert resumen["iniciado"] is True
    assert resumen["agotado"] is False
    assert resumen["moneda"] == "EUR"
    assert json.loads(json.dumps(resumen))["consumo"]["tokens_total"] == 15


def test_iniciar_reinicia_el_consumo():
    budget = BudgetManager(max_llamadas=1)
    budget.iniciar()
    budget.registrar_llamada()
    assert budget.agotado() is True

    budget.iniciar()
    assert budget.agotado() is False
    assert budget.consumo.llamadas == 0


# ============================================================
# Contabilidad real: observador del punto único de salida del LLM
# ============================================================

def test_observar_contabiliza_tokens():
    budget = BudgetManager(max_tokens=200)
    budget.observar(_ResultadoFalso(100, 50))
    assert budget.consumo.llamadas == 1
    assert budget.consumo.tokens_prompt == 100
    assert budget.consumo.tokens_completion == 50


def test_conectar_y_desconectar_el_observador_global():
    budget = BudgetManager(max_llamadas=10)
    budget.conectar()
    try:
        _notificar_observadores(_ResultadoFalso(10, 10))
        assert budget.consumo.llamadas == 1
    finally:
        budget.desconectar()

    _notificar_observadores(_ResultadoFalso(10, 10))
    assert budget.consumo.llamadas == 1, "tras desconectar no debe contar más"


def test_observar_tolera_un_resultado_sin_tokens():
    budget = BudgetManager()
    budget.observar(object())  # sin .tokens()
    assert budget.consumo.llamadas == 1
    assert budget.consumo.tokens_total == 0


def test_context_manager_conecta_y_desconecta():
    with BudgetManager(max_llamadas=1) as budget:
        _notificar_observadores(_ResultadoFalso(0, 0))
        assert budget.consumo.llamadas == 1
    _notificar_observadores(_ResultadoFalso(0, 0))
    assert budget.consumo.llamadas == 1


# ============================================================
# Configuración por entorno
# ============================================================

def test_configuracion_por_defecto_sin_variables(monkeypatch):
    for var in (
        "AGENTES_BUDGET_MAX_SEGUNDOS",
        "AGENTES_BUDGET_MAX_LLAMADAS",
        "AGENTES_BUDGET_MAX_TOKENS",
        "AGENTES_BUDGET_MAX_COSTE",
        "AGENTES_PRECIO_1K_PROMPT",
        "AGENTES_PRECIO_1K_COMPLETION",
        "AGENTES_MONEDA",
    ):
        monkeypatch.delenv(var, raising=False)

    config = configuracion_presupuesto()
    assert config["max_segundos"] is None
    assert config["max_llamadas"] is None
    assert config["max_tokens"] is None
    assert config["max_coste"] is None
    assert config["moneda"] == "EUR"
    assert BudgetManager.desde_entorno().hay_limites() is False


def test_configuracion_desde_entorno(monkeypatch):
    monkeypatch.setenv("AGENTES_BUDGET_MAX_LLAMADAS", "7")
    monkeypatch.setenv("AGENTES_BUDGET_MAX_TOKENS", "5000")
    monkeypatch.setenv("AGENTES_BUDGET_MAX_SEGUNDOS", "120.5")
    monkeypatch.setenv("AGENTES_PRECIO_1K_PROMPT", "0.5")
    monkeypatch.setenv("AGENTES_MONEDA", "USD")

    budget = BudgetManager.desde_entorno()
    assert budget.max_llamadas == 7
    assert budget.max_tokens == 5000
    assert budget.max_segundos == pytest.approx(120.5)
    assert budget.precio_1k_prompt == pytest.approx(0.5)
    assert budget.moneda == "USD"
    assert budget.hay_limites() is True


def test_valores_de_entorno_invalidos_se_ignoran(monkeypatch):
    monkeypatch.setenv("AGENTES_BUDGET_MAX_LLAMADAS", "no-es-un-numero")
    monkeypatch.setenv("AGENTES_BUDGET_MAX_TOKENS", "-3")
    budget = BudgetManager.desde_entorno()
    assert budget.max_llamadas is None
    assert budget.max_tokens is None
