# core/problem_solver/constants.py
"""
Constantes y enumeraciones de ProblemSolver.
Extraído literalmente de core/problem_solver.py (monolito) — Paso 2.
"""
from enum import Enum


class PlanStatus(Enum):
    """Estado del plan generado."""
    DRAFT = "draft"
    VALIDATED = "validated"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


class PlanComplexity(Enum):
    """Nivel de complejidad del plan."""
    SIMPLE = "🟢 Simple"
    MODERATE = "🟡 Moderada"
    COMPLEX = "🟠 Compleja"
    VERY_COMPLEX = "🔴 Muy compleja"


# ============================================================
# MAPA CENTRALIZADO DE CAMPOS VÁLIDOS POR TIPO
# ============================================================
# Fuente única de verdad. Se usa para:
#   1. Validar que el LLM no invente campos en 'configuracion'.
#   2. Limpiar campos desconocidos antes de construir el Agente.
#
# ⚠️ IMPORTANTE: para agentes File NO se incluye 'contenido'.
# El contenido se obtiene automáticamente del resultado de la
# dependencia declarada; nunca se configura manualmente.
# ============================================================
CAMPOS_VALIDOS_POR_TIPO: dict[str, set[str]] = {
    "Python": {"codigo", "timeout", "memory_limit_mb"},
    "Shell":  {"comando", "timeout", "working_dir"},
    "HTTP":   {"url", "metodo", "headers", "body", "timeout"},
    "LLM":    {
        "prompt", "modelo", "temperatura", "max_tokens",
        "reasoning_effort", "thinking_enabled",
    },
    "File":   {"operacion", "archivo_origen", "archivo_destino", "modo_salida_file"},
    "Loop":   {
        "fuente_items", "codigo_por_item", "max_iteraciones",
        "timeout_loop", "timeout_python", "continuar_en_error",
    },
    "Browser": {
        "url", "acciones", "timeout", "timeout_accion",
        "headless", "bloquear_recursos", "user_agent",
    },
    "Search": {"query", "max_resultados", "region", "timeout"},
}
