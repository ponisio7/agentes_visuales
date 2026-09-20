"""
core/recovery_manager.py
Decide si un fallo merece un Plan B o si hay que parar en seco (V3.8).

Antes, **cualquier** fallo terminal gastaba intentos de Plan B: una llamada
al LLM, dinero y tiempo. Un fallo que no puede arreglarse reescribiendo el
plan —falta la API key, falta una dependencia del entorno, el problema es
inválido, la política de seguridad bloquea la operación, el contrato de
aceptación es imposible, el presupuesto se agotó o se superó el timeout
global— se detecta aquí y **aborta sin gastar intentos**.

Flujo:

    fallo → clasificar → ¿es parada dura?
                              ├── SÍ → RecoveryDecision(permitir_reintento=False)
                              └── NO → RecoveryDecision(permitir_reintento=True,
                                                         estrategia, coste_estimado)

El Scheduler consulta a :class:`RecoveryManager` **antes** de reclamar el
turno de Plan B. Así el recovery deja de ser «falló → prueba otra cosa» y
pasa a ser «analiza el fallo → decide si merece la pena continuar → elige
estrategia».

Este módulo es deliberadamente puro (sin red, sin BD): solo clasifica texto
y estado. Se puede probar sin LLM ni Qt.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# CÓDIGOS DE PARADA DURA
# ============================================================
# Error que NO se arregla reescribiendo el plan: ni el LLM ni otro agente
# pueden resolverlo, así que insistir solo quema dinero.
API_KEY_MISSING = "API_KEY_MISSING"
DEPENDENCY_MISSING = "DEPENDENCY_MISSING"
INVALID_PROBLEM = "INVALID_PROBLEM"
SECURITY_BLOCK = "SECURITY_BLOCK"
INVALID_CONTRACT = "INVALID_CONTRACT"
BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
TIMEOUT_GLOBAL = "TIMEOUT_GLOBAL"

# Orden = severidad (para prioridad y para logs). ``PARADAS_DURAS`` es la
# fuente de verdad de «este fallo no gasta intentos».
PARADAS_DURAS: tuple[str, ...] = (
    API_KEY_MISSING,
    DEPENDENCY_MISSING,
    INVALID_PROBLEM,
    SECURITY_BLOCK,
    INVALID_CONTRACT,
    BUDGET_EXCEEDED,
    TIMEOUT_GLOBAL,
)
_PARADAS_DURAS_SET = frozenset(PARADAS_DURAS)

# Explicación para el usuario (log / API / GUI).
DESCRIPCION_CODIGO: dict[str, str] = {
    API_KEY_MISSING: "Falta la API key (o es inválida): el LLM no puede usarse.",
    DEPENDENCY_MISSING: "Falta una dependencia del entorno y el plan no puede ejecutarse.",
    INVALID_PROBLEM: "El problema o la tarea no es válida/interpretable.",
    SECURITY_BLOCK: "La política de seguridad bloqueó la operación.",
    INVALID_CONTRACT: "El contrato de aceptación es imposible de cumplir.",
    BUDGET_EXCEEDED: "Se agotó el presupuesto de la ejecución.",
    TIMEOUT_GLOBAL: "Se superó el tiempo máximo global de la ejecución.",
}


# ============================================================
# PATRONES DE CLASIFICACIÓN
# ============================================================
# Se aplican en orden sobre el mensaje en minúsculas. Los patrones son
# deliberadamente ESPECÍFICOS: un falso positivo aquí impediría un Plan B
# legítimo, que es peor que un falso negativo. Por eso NO hay patrones
# genéricos como «error» o «fallo».
_PATRONES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        API_KEY_MISSING,
        (
            r"deepseek_api_key",
            r"api[_\s-]?key",
            r"invalid api key",
            r"incorrect api key",
            r"no se encontr[oó]\s+(la\s+)?api",
            r"cliente llm no disponible",
            # ``401`` solo cuenta como auth si va pegado a HTTP/unauthorized:
            # «401 bytes» no es un fallo de credenciales.
            r"http[^\n]{0,12}\b401\b",
            r"\b401\b[^\n]{0,20}(unauthorized|no autorizado|invalid)",
            r"\bunauthorized\b",
            r"autenticaci[oó]n",
            r"falta la clave",
        ),
    ),
    (
        DEPENDENCY_MISSING,
        (
            # Errores de importación del propio executor (el módulo del
            # agente no existe en el entorno).
            r"error de importaci[oó]n",
            # Dependencias del runtime que no se arreglan reescribiendo el plan.
            r"no module named ['\"]?(playwright|torch|sentence_transformers|openai|flask|ddgs|openpyxl|docx|pypdf)",
            r"executable doesn'?t exist",
            r"please run the following command to download new browsers",
            r"browsertype\.launch",
            r"falta la dependencia",
            r"dependencia (ausente|no instalada)",
            r"no est[aá] instalad[oa]",
        ),
    ),
    (
        INVALID_PROBLEM,
        (
            r"el problema no puede estar vac[ií]o",
            r"problema inv[aá]lido",
            r"invalid problem",
            r"no se pudo interpretar el problema",
            r"tarea vac[ií]a",
        ),
    ),
    (
        SECURITY_BLOCK,
        (
            r"bloquead[oa] por (la )?seguridad",
            r"bloqueo de seguridad",
            r"ruta no permitida",
            r"url no permitida",
            r"no permitid[oa] por (la )?(pol[ií]tica|allowlist)",
            r"fuera del (directorio|allowlist)",
            r"kill switch",
            r"operaci[oó]n destructiva",
            r"acceso denegado por (la )?pol[ií]tica",
        ),
    ),
    (
        INVALID_CONTRACT,
        (
            r"contrato de aceptaci[oó]n imposible",
            r"contrato imposible",
            r"contrato (de aceptaci[oó]n )?inv[aá]lido",
            r"contrato de aceptaci[oó]n mal formado",
        ),
    ),
    (
        BUDGET_EXCEEDED,
        (
            r"presupuesto agotado",
            r"presupuesto excedido",
            r"budget exceeded",
            r"l[ií]mite de tokens",
            r"sin presupuesto",
        ),
    ),
    (
        TIMEOUT_GLOBAL,
        (
            r"timeout global",
            r"tiempo m[aá]ximo global",
            r"timeout de la ejecuci[oó]n",
            r"global timeout",
        ),
    ),
)

_PATRONES_COMPILADOS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (codigo, re.compile("|".join(patrones), re.IGNORECASE))
    for codigo, patrones in _PATRONES
)

# Un fallo de aceptación en RUNTIME es exactamente lo que el Plan B debe
# intentar arreglar: nunca se clasifica como contrato inválido.
_ACEPTACION_RUNTIME = re.compile(r"aceptaci[oó]n fallida", re.IGNORECASE)


# ============================================================
# COSTE ESTIMADO (unidades relativas de llamada al LLM)
# ============================================================
# 1.0 = una llamada de planificación completa (la que genera un Plan B).
# No es dinero: es un peso comparable entre estrategias. El BudgetManager
# (V3.8-2) lo usa para decidir si queda presupuesto.
COSTE_POR_ESTRATEGIA: dict[str, float] = {
    "correccion_puntual": 1.0,
    "cambiar_configuracion": 1.0,
    "cambiar_tipo_agente": 1.2,
    "reestructurar_plan": 1.6,
    "fallback_alternativo": 2.0,
}
COSTE_POR_DEFECTO = 1.2

# Prioridad de la decisión: más alto = más urgente de atender.
PRIORIDAD_PARADA_DURA = 100
PRIORIDAD_RECUPERABLE_BASE = 50


def coste_estimado_estrategia(estrategia: str) -> float:
    """Coste relativo estimado de una estrategia de recuperación."""
    return float(COSTE_POR_ESTRATEGIA.get(estrategia, COSTE_POR_DEFECTO))


def descripcion_codigo(codigo: str | None) -> str:
    """Texto legible de un código de parada (vacío si no hay código)."""
    if not codigo:
        return ""
    return DESCRIPCION_CODIGO.get(codigo, f"Parada dura: {codigo}")


# ============================================================
# DECISIÓN
# ============================================================

@dataclass
class RecoveryDecision:
    """Resultado explícito y auditable de analizar un fallo.

    - ``permitir_reintento``: si False, NO se debe lanzar Plan B.
    - ``motivo``: explicación legible (humana).
    - ``estrategia``: estrategia recomendada (vacía si no hay reintento).
    - ``coste_estimado``: coste relativo de la estrategia (0.0 en parada dura).
    - ``prioridad``: 100 = parada dura; menor = recuperable menos urgente.
    - ``codigo``: código de parada si aplica, si no ``None``.
    - ``parada_dura``: atajo de ``codigo in PARADAS_DURAS``.
    - ``detalles``: contexto extra serializable (agente, intento, origen…).
    """

    permitir_reintento: bool
    motivo: str
    estrategia: str = ""
    coste_estimado: float = 0.0
    prioridad: int = 0
    codigo: str | None = None
    parada_dura: bool = False
    detalles: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Representación serializable (API / GUI / BD)."""
        return {
            "permitir_reintento": bool(self.permitir_reintento),
            "motivo": self.motivo,
            "estrategia": self.estrategia,
            "coste_estimado": float(self.coste_estimado),
            "prioridad": int(self.prioridad),
            "codigo": self.codigo,
            "parada_dura": bool(self.parada_dura),
            "detalles": dict(self.detalles),
        }

    def __str__(self) -> str:  # pragma: no cover - solo para logs
        if self.parada_dura:
            return f"PARADA DURA [{self.codigo}]: {self.motivo}"
        return (
            f"Reintento permitido ({self.estrategia}, "
            f"coste≈{self.coste_estimado:.1f}, prioridad={self.prioridad})"
        )


# ============================================================
# CLASIFICACIÓN
# ============================================================

def clasificar_fallo(
    error: str,
    *,
    origen: str = "ejecucion",
) -> str | None:
    """Devuelve el código de parada dura del mensaje, o ``None``.

    ``origen`` permite señalar fallos que no se distinguen por el texto:

    - ``"importacion"`` → :data:`DEPENDENCY_MISSING` (el executor no cargó).
    - ``"planificacion"`` → :data:`INVALID_PROBLEM` si no hay error legible.
    - ``"presupuesto"`` → :data:`BUDGET_EXCEEDED`.
    - ``"timeout_global"`` → :data:`TIMEOUT_GLOBAL`.
    - ``"seguridad"`` → :data:`SECURITY_BLOCK`.
    - ``"contrato"`` → :data:`INVALID_CONTRACT`.
    """
    origen_normalizado = (origen or "ejecucion").strip().lower()
    mapa_origen = {
        "importacion": DEPENDENCY_MISSING,
        "planificacion": INVALID_PROBLEM,
        "presupuesto": BUDGET_EXCEEDED,
        "timeout_global": TIMEOUT_GLOBAL,
        "seguridad": SECURITY_BLOCK,
        "contrato": INVALID_CONTRACT,
    }
    if origen_normalizado in mapa_origen:
        return mapa_origen[origen_normalizado]

    texto = (error or "").strip()
    if not texto:
        return None

    # El fallo de aceptación en runtime es recuperable por definición; se
    # descarta antes de mirar el resto de patrones.
    if _ACEPTACION_RUNTIME.search(texto):
        return None

    for codigo, patron in _PATRONES_COMPILADOS:
        if patron.search(texto):
            return codigo
    return None


# ============================================================
# GESTOR
# ============================================================

class RecoveryManager:
    """Analiza fallos y decide si merece la pena intentar un Plan B.

    Es stateful a propósito: recuerda las paradas duras de la ejecución para
    que la API/GUI puedan explicar por qué no se insistió.
    """

    def __init__(self, *, budget: Any = None):
        # ``budget`` es opcional: el BudgetManager (V3.8-2) se inyecta aquí
        # para que una parada por presupuesto no dependa del texto del error.
        self._budget = budget
        self._paradas: list[RecoveryDecision] = []

    # ------------------------------------------------------------
    # Consulta
    # ------------------------------------------------------------
    def decidir(
        self,
        *,
        error: str = "",
        agente: Any = None,
        intento: int = 1,
        estrategia: str = "",
        origen: str = "ejecucion",
        codigo_forzado: str | None = None,
        presupuesto_agotado: bool = False,
    ) -> RecoveryDecision:
        """Decide si el fallo permite un Plan B.

        Nunca lanza: ante cualquier duda, permite el reintento (perder un
        Plan B legítimo es peor que gastar una llamada).
        """
        intento = max(1, int(intento or 1))
        nombre_agente = getattr(agente, "nombre", "") or ""

        codigo = None
        if codigo_forzado in _PARADAS_DURAS_SET:
            codigo = codigo_forzado
        elif presupuesto_agotado or self._presupuesto_agotado():
            codigo = BUDGET_EXCEEDED
        else:
            codigo = clasificar_fallo(error, origen=origen)

        if codigo in _PARADAS_DURAS_SET:
            decision = RecoveryDecision(
                permitir_reintento=False,
                motivo=descripcion_codigo(codigo),
                estrategia="",
                coste_estimado=0.0,
                prioridad=PRIORIDAD_PARADA_DURA,
                codigo=codigo,
                parada_dura=True,
                detalles={
                    "agente": nombre_agente,
                    "intento": intento,
                    "origen": origen,
                    "error": (error or "")[:500],
                },
            )
            self._paradas.append(decision)
            logger.warning(
                f"🛑 Parada dura [{codigo}] en '{nombre_agente or '?'}': "
                f"{decision.motivo} (no se gasta Plan B)"
            )
            return decision

        estrategia = estrategia or self._estrategia_sugerida(intento)
        coste = coste_estimado_estrategia(estrategia)
        decision = RecoveryDecision(
            permitir_reintento=True,
            motivo="Fallo recuperable: el Plan B puede intentar otra estrategia.",
            estrategia=estrategia,
            coste_estimado=coste,
            prioridad=max(10, PRIORIDAD_RECUPERABLE_BASE - (intento - 1) * 10),
            codigo=None,
            parada_dura=False,
            detalles={
                "agente": nombre_agente,
                "intento": intento,
                "origen": origen,
                "error": (error or "")[:500],
            },
        )
        return decision

    # ------------------------------------------------------------
    # Presupuesto
    # ------------------------------------------------------------
    def set_budget(self, budget: Any) -> None:
        """Inyecta el BudgetManager (V3.8-2)."""
        self._budget = budget

    def _presupuesto_agotado(self) -> bool:
        if self._budget is None:
            return False
        try:
            return bool(self._budget.agotado())
        except Exception as e:  # nunca romper la recuperación por el budget
            logger.debug(f"RecoveryManager: budget no consultable: {e}")
            return False

    # ------------------------------------------------------------
    # Estrategia
    # ------------------------------------------------------------
    @staticmethod
    def _estrategia_sugerida(intento: int) -> str:
        """Escalera de estrategias (import perezoso para no acoplar módulos)."""
        try:
            from .plan_recovery import estrategia_para_intento

            return estrategia_para_intento(intento)
        except Exception:  # pragma: no cover - defensivo
            return "correccion_puntual"

    # ------------------------------------------------------------
    # Historial / estado
    # ------------------------------------------------------------
    def hubo_parada_dura(self) -> bool:
        return bool(self._paradas)

    def paradas(self) -> list[dict[str, Any]]:
        """Paradas duras registradas (serializables)."""
        return [p.to_dict() for p in self._paradas]

    def ultima_parada(self) -> RecoveryDecision | None:
        return self._paradas[-1] if self._paradas else None

    def reset(self) -> None:
        """Limpia el estado entre ejecuciones."""
        self._paradas.clear()


__all__ = [
    "API_KEY_MISSING",
    "DEPENDENCY_MISSING",
    "INVALID_PROBLEM",
    "SECURITY_BLOCK",
    "INVALID_CONTRACT",
    "BUDGET_EXCEEDED",
    "TIMEOUT_GLOBAL",
    "PARADAS_DURAS",
    "DESCRIPCION_CODIGO",
    "COSTE_POR_ESTRATEGIA",
    "RecoveryDecision",
    "RecoveryManager",
    "clasificar_fallo",
    "coste_estimado_estrategia",
    "descripcion_codigo",
]
