"""
core/budget_manager.py
Presupuesto de una ejecución: tiempo + llamadas + tokens + coste (V3.8-2).

Antes solo existía un presupuesto de **tiempo** para el Plan B
(``AGENTES_PLAN_B_MAX_SEGUNDOS``). Un modo «insistir hasta lograrlo» sin
techo de llamadas/tokens es una factura abierta. Este módulo contabiliza:

- **tiempo** transcurrido desde el inicio de la ejecución;
- **llamadas** al LLM;
- **tokens** (prompt + completion);
- **coste** estimado con precios configurables.

El ``RecoveryManager`` lo consulta (``agotado()``) antes de gastar un Plan B:
si el presupuesto se agotó, la recuperación es una parada dura
(``BUDGET_EXCEEDED``) y no se llama al LLM.

La contabilidad de tokens no acopla este módulo al cliente LLM: se suscribe
al punto único de salida (``core.llm_client.agregar_observador_llamada``).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURACIÓN POR ENTORNO
# ============================================================

def _env_int(nombre: str, defecto: int | None = None) -> int | None:
    valor = os.environ.get(nombre, "")
    if valor is None or str(valor).strip() == "":
        return defecto
    try:
        numero = int(float(valor))
    except (TypeError, ValueError):
        return defecto
    return numero if numero > 0 else defecto


def _env_float(nombre: str, defecto: float | None = None) -> float | None:
    valor = os.environ.get(nombre, "")
    if valor is None or str(valor).strip() == "":
        return defecto
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return defecto
    return numero if numero > 0 else defecto


def configuracion_presupuesto() -> dict[str, Any]:
    """Presupuesto leído del entorno (todo opcional; sin límite si falta).

    - ``AGENTES_BUDGET_MAX_SEGUNDOS``
    - ``AGENTES_BUDGET_MAX_LLAMADAS``
    - ``AGENTES_BUDGET_MAX_TOKENS``
    - ``AGENTES_BUDGET_MAX_COSTE``
    - ``AGENTES_PRECIO_1K_PROMPT`` / ``AGENTES_PRECIO_1K_COMPLETION``
    - ``AGENTES_MONEDA`` (por defecto ``EUR``)
    """
    return {
        "max_segundos": _env_float("AGENTES_BUDGET_MAX_SEGUNDOS"),
        "max_llamadas": _env_int("AGENTES_BUDGET_MAX_LLAMADAS"),
        "max_tokens": _env_int("AGENTES_BUDGET_MAX_TOKENS"),
        "max_coste": _env_float("AGENTES_BUDGET_MAX_COSTE"),
        "precio_1k_prompt": _env_float("AGENTES_PRECIO_1K_PROMPT", 0.0) or 0.0,
        "precio_1k_completion": _env_float("AGENTES_PRECIO_1K_COMPLETION", 0.0) or 0.0,
        "moneda": (os.environ.get("AGENTES_MONEDA") or "EUR").strip() or "EUR",
    }


# ============================================================
# CONSUMO
# ============================================================

@dataclass
class Consumo:
    """Lo gastado hasta ahora por una ejecución."""

    segundos: float = 0.0
    llamadas: int = 0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    coste: float = 0.0

    @property
    def tokens_total(self) -> int:
        return int(self.tokens_prompt) + int(self.tokens_completion)

    def to_dict(self) -> dict[str, Any]:
        return {
            "segundos": round(float(self.segundos), 3),
            "llamadas": int(self.llamadas),
            "tokens_prompt": int(self.tokens_prompt),
            "tokens_completion": int(self.tokens_completion),
            "tokens_total": self.tokens_total,
            "coste": round(float(self.coste), 6),
        }


# Motivos de agotamiento (coinciden con las dimensiones).
MOTIVO_TIEMPO = "tiempo"
MOTIVO_LLAMADAS = "llamadas"
MOTIVO_TOKENS = "tokens"
MOTIVO_COSTE = "coste"


class BudgetManager:
    """Contabiliza y limita el gasto de una ejecución.

    Todos los límites son opcionales: ``None`` significa «sin límite». Un
    ``BudgetManager`` sin límites nunca está agotado (comportamiento anterior
    a V3.8), así que activarlo es opt-in.
    """

    def __init__(
        self,
        *,
        max_segundos: float | None = None,
        max_llamadas: int | None = None,
        max_tokens: int | None = None,
        max_coste: float | None = None,
        precio_1k_prompt: float = 0.0,
        precio_1k_completion: float = 0.0,
        moneda: str = "EUR",
        ahora: Any = None,
    ):
        self.max_segundos = max_segundos
        self.max_llamadas = max_llamadas
        self.max_tokens = max_tokens
        self.max_coste = max_coste
        self.precio_1k_prompt = float(precio_1k_prompt or 0.0)
        self.precio_1k_completion = float(precio_1k_completion or 0.0)
        self.moneda = moneda or "EUR"

        self.consumo = Consumo()
        self._inicio: float | None = None
        # RLock: varios métodos públicos (``registrar_llamada``, ``resumen``)
        # consultan helpers que también toman el lock.
        self._lock = threading.RLock()
        # Reloj inyectable (tests deterministas).
        self._ahora = ahora or time.monotonic

    # ------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------
    def iniciar(self, ahora: float | None = None) -> None:
        """Reinicia el consumo y marca el inicio de la ejecución."""
        momento = self._ahora() if ahora is None else float(ahora)
        with self._lock:
            self.consumo = Consumo()
            self._inicio = momento

    def reset(self) -> None:
        """Alias de :meth:`iniciar` (compatibilidad de nombre)."""
        self.iniciar()

    @property
    def iniciado(self) -> bool:
        with self._lock:
            return self._inicio is not None

    def _segundos(self, ahora: float | None) -> float:
        with self._lock:
            if self._inicio is None:
                return 0.0
            momento = self._ahora() if ahora is None else float(ahora)
            return max(0.0, momento - self._inicio)

    # ------------------------------------------------------------
    # Registro de gasto
    # ------------------------------------------------------------
    def registrar_llamada(
        self,
        *,
        tokens_prompt: int = 0,
        tokens_completion: int = 0,
        coste: float | None = None,
        ahora: float | None = None,
    ) -> Consumo:
        """Suma una llamada al LLM y sus tokens/coste.

        Si ``coste`` es ``None`` se calcula con los precios configurados (si
        no hay precios, el coste queda en 0: no se inventa dinero).
        """
        tokens_prompt = max(0, int(tokens_prompt or 0))
        tokens_completion = max(0, int(tokens_completion or 0))
        if coste is None:
            coste = (
                tokens_prompt / 1000.0 * self.precio_1k_prompt
                + tokens_completion / 1000.0 * self.precio_1k_completion
            )
        with self._lock:
            self.consumo.llamadas += 1
            self.consumo.tokens_prompt += tokens_prompt
            self.consumo.tokens_completion += tokens_completion
            self.consumo.coste += max(0.0, float(coste or 0.0))
            self.consumo.segundos = self._segundos(ahora)
            return self.consumo

    def observar(self, resultado: Any) -> None:
        """Observador de ``core.llm_client``: contabiliza una respuesta real.

        Es tolerante a dobles: si no hay ``tokens()``, cuenta la llamada.
        """
        tokens_prompt = tokens_completion = 0
        try:
            if hasattr(resultado, "tokens"):
                datos = resultado.tokens() or {}
                tokens_prompt = int(datos.get("prompt", 0) or 0)
                tokens_completion = int(datos.get("completion", 0) or 0)
        except Exception as e:  # nunca romper una llamada por contabilidad
            logger.debug(f"BudgetManager: no se pudieron leer tokenes: {e}")
        self.registrar_llamada(
            tokens_prompt=tokens_prompt,
            tokens_completion=tokens_completion,
        )

    def conectar(self) -> None:
        """Suscribe la contabilidad al punto único de salida del LLM."""
        try:
            from .llm_client import agregar_observador_llamada

            agregar_observador_llamada(self.observar)
        except Exception as e:  # pragma: no cover - defensivo
            logger.debug(f"BudgetManager: no se pudo suscribir al LLM: {e}")

    def desconectar(self) -> None:
        """Desuscribe la contabilidad (idempotente)."""
        try:
            from .llm_client import quitar_observador_llamada

            quitar_observador_llamada(self.observar)
        except Exception as e:  # pragma: no cover - defensivo
            logger.debug(f"BudgetManager: no se pudo desuscribir del LLM: {e}")

    def __enter__(self) -> BudgetManager:
        self.conectar()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.desconectar()

    # ------------------------------------------------------------
    # Consulta
    # ------------------------------------------------------------
    def motivo_agotado(self, ahora: float | None = None) -> str | None:
        """Dimensión que se agotó, o ``None`` si queda presupuesto."""
        segundos = self._segundos(ahora)
        with self._lock:
            self.consumo.segundos = segundos
            consumo = self.consumo

            if self.max_segundos is not None and segundos >= self.max_segundos:
                return MOTIVO_TIEMPO
            if self.max_llamadas is not None and consumo.llamadas >= self.max_llamadas:
                return MOTIVO_LLAMADAS
            if self.max_tokens is not None and consumo.tokens_total >= self.max_tokens:
                return MOTIVO_TOKENS
            if self.max_coste is not None and consumo.coste >= self.max_coste:
                return MOTIVO_COSTE
        return None

    def agotado(self, ahora: float | None = None) -> bool:
        """True si no queda presupuesto en ninguna dimensión."""
        return self.motivo_agotado(ahora) is not None

    def hay_limites(self) -> bool:
        return any(
            limite is not None
            for limite in (
                self.max_segundos,
                self.max_llamadas,
                self.max_tokens,
                self.max_coste,
            )
        )

    def restante(self, ahora: float | None = None) -> dict[str, Any]:
        """Lo que queda en cada dimensión (``None`` = sin límite)."""
        segundos = self._segundos(ahora)
        with self._lock:
            self.consumo.segundos = segundos
            consumo = self.consumo
            return {
                "segundos": (
                    None if self.max_segundos is None
                    else max(0.0, self.max_segundos - segundos)
                ),
                "llamadas": (
                    None if self.max_llamadas is None
                    else max(0, self.max_llamadas - consumo.llamadas)
                ),
                "tokens": (
                    None if self.max_tokens is None
                    else max(0, self.max_tokens - consumo.tokens_total)
                ),
                "coste": (
                    None if self.max_coste is None
                    else max(0.0, self.max_coste - consumo.coste)
                ),
            }

    def resumen(self, ahora: float | None = None) -> dict[str, Any]:
        """Resumen serializable: consumo, límites, restante y agotamiento."""
        motivo = self.motivo_agotado(ahora)
        with self._lock:
            return {
                "iniciado": self._inicio is not None,
                "consumo": self.consumo.to_dict(),
                "limites": {
                    "segundos": self.max_segundos,
                    "llamadas": self.max_llamadas,
                    "tokens": self.max_tokens,
                    "coste": self.max_coste,
                },
                "restante": self.restante(ahora),
                "agotado": motivo is not None,
                "motivo_agotado": motivo,
                "moneda": self.moneda,
            }

    def to_dict(self) -> dict[str, Any]:
        return self.resumen()

    @classmethod
    def desde_entorno(cls) -> BudgetManager:
        """Construye el manager con la configuración del entorno."""
        return cls(**configuracion_presupuesto())


__all__ = [
    "BudgetManager",
    "Consumo",
    "MOTIVO_COSTE",
    "MOTIVO_LLAMADAS",
    "MOTIVO_TIEMPO",
    "MOTIVO_TOKENS",
    "configuracion_presupuesto",
]
