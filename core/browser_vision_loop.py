"""
core/browser_vision_loop.py
Bucle cerrado Browser + Visión: percibir → razonar → actuar → verificar (V3.8-5).

H11 fase 1 tenía las piezas suelas (captura → modelo multimodal → decisión
validada por allowlist) pero **sin bucle**: nadie volvía a capturar tras
actuar ni limitaba el proceso. Aquí se cierra el ciclo:

    Browser (DOM/texto + captura)
            ↓
    modelo multimodal (VLM) → decisión validada
            ↓
    acción Browser (allowlist)
            ↓
    nueva captura (verificación)
            └──────────────→ vuelve al VLM

Límites duros (no negociables):

- ``max_steps`` (20 por defecto)
- ``max_segundos`` (120)
- ``max_capturas`` (30)
- ``max_fallos_consecutivos`` (3)

Cada acción deja traza con ``{accion, objetivo, razon, evidencia}`` más el
resultado real, para poder auditar por qué se hizo cada cosa.

Guardrails que se conservan de H11:

* **Kill switch**: sin ``AGENTES_VISION_HABILITADA`` el bucle no arranca.
* **Allowlist**: una acción fuera del vocabulario del ``BrowserExecutor`` se
  rechaza (además de la validación que ya hace ``core.vision``).
* **Sin escritorio**: esto es visión de PÁGINA.

El driver se inyecta, así que el bucle se prueba sin Playwright. Para usarlo
con Playwright está :func:`crear_driver_playwright`, que reutiliza el
``BrowserExecutor`` (misma allowlist y misma ejecución de acciones).
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# LÍMITES
# ============================================================

MAX_STEPS_DEFECTO = 20
MAX_SEGUNDOS_DEFECTO = 120.0
MAX_CAPTURAS_DEFECTO = 30
MAX_FALLOS_CONSECUTIVOS_DEFECTO = 3


def _env_int(nombre: str, defecto: int) -> int:
    try:
        valor = int(float(os.environ.get(nombre, "")))
    except (TypeError, ValueError):
        return defecto
    return valor if valor > 0 else defecto


def _env_float(nombre: str, defecto: float) -> float:
    try:
        valor = float(os.environ.get(nombre, ""))
    except (TypeError, ValueError):
        return defecto
    return valor if valor > 0 else defecto


def configuracion_vision_loop() -> dict[str, Any]:
    """Límites del bucle, configurables por entorno."""
    return {
        "max_steps": _env_int("AGENTES_VISION_MAX_STEPS", MAX_STEPS_DEFECTO),
        "max_segundos": _env_float(
            "AGENTES_VISION_MAX_SEGUNDOS", MAX_SEGUNDOS_DEFECTO
        ),
        "max_capturas": _env_int(
            "AGENTES_VISION_MAX_CAPTURAS", MAX_CAPTURAS_DEFECTO
        ),
        "max_fallos_consecutivos": _env_int(
            "AGENTES_VISION_MAX_FALLOS", MAX_FALLOS_CONSECUTIVOS_DEFECTO
        ),
    }


# ============================================================
# DRIVER (interfaz mínima, sin Playwright)
# ============================================================

@dataclass
class DriverNavegador:
    """Lo que el bucle necesita del navegador.

    - ``capturar()``: screenshot en bytes.
    - ``ejecutar(accion)``: ejecuta la acción validada y devuelve
      ``{"ok": bool, "detalle": str}``.
    - ``contexto()``: texto visible/estado (opcional, ayuda al VLM).
    """

    capturar: Callable[[], bytes | None]
    ejecutar: Callable[[dict[str, Any]], dict[str, Any]]
    contexto: Callable[[], str] | None = None
    descripcion: str = ""

    def estado_texto(self) -> str:
        if self.contexto is None:
            return ""
        try:
            return str(self.contexto() or "")[:2000]
        except Exception as e:  # nunca romper el bucle por el contexto
            logger.debug(f"Visión: contexto no disponible: {e}")
            return ""


# ============================================================
# TRAZA
# ============================================================

@dataclass
class PasoVision:
    """Una acción del bucle, con su justificación y su resultado."""

    paso: int
    captura: int
    accion: str
    objetivo: str = ""
    razon: str = ""
    evidencia: str = ""
    ok: bool = False
    detalle: str = ""
    duracion: float = 0.0
    detalles_accion: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "paso": self.paso,
            "captura": self.captura,
            "accion": self.accion,
            "objetivo": self.objetivo,
            "razon": self.razon,
            "evidencia": self.evidencia,
            "ok": bool(self.ok),
            "detalle": self.detalle,
            "duracion": round(float(self.duracion), 3),
            "detalles_accion": dict(self.detalles_accion),
        }


# ============================================================
# BUCLE
# ============================================================

class BrowserVisionLoop:
    """Ejecuta el ciclo percibir → razonar → actuar → verificar con límites."""

    def __init__(
        self,
        driver: DriverNavegador | None,
        llm: Any = None,
        *,
        instruccion: str = "",
        max_steps: int | None = None,
        max_segundos: float | None = None,
        max_capturas: int | None = None,
        max_fallos_consecutivos: int | None = None,
        decidir: Callable[..., dict | None] | None = None,
        acciones_permitidas: tuple[str, ...] | None = None,
        cancellation_token: Any = None,
        ahora: Callable[[], float] | None = None,
    ):
        cfg = configuracion_vision_loop()
        self.driver = driver
        self.llm = llm
        self.instruccion = instruccion or ""
        self.max_steps = int(max_steps if max_steps is not None else cfg["max_steps"])
        self.max_segundos = float(
            max_segundos if max_segundos is not None else cfg["max_segundos"]
        )
        self.max_capturas = int(
            max_capturas if max_capturas is not None else cfg["max_capturas"]
        )
        self.max_fallos_consecutivos = int(
            max_fallos_consecutivos
            if max_fallos_consecutivos is not None
            else cfg["max_fallos_consecutivos"]
        )
        # ``decidir`` se inyecta en tests; en producción se usa core.vision.
        self._decidir_fn = decidir
        if acciones_permitidas is None:
            from .vision import ACCIONES_PERMITIDAS

            acciones_permitidas = ACCIONES_PERMITIDAS
        self.acciones_permitidas = tuple(acciones_permitidas)
        self.cancellation_token = cancellation_token
        self._ahora = ahora or time.monotonic

    # ------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------
    def _segundos(self, inicio: float) -> float:
        return max(0.0, self._ahora() - inicio)

    def _cancelado(self) -> bool:
        token = self.cancellation_token
        if token is None:
            return False
        try:
            return bool(token.esta_cancelado())
        except Exception:
            return False

    def _decidir(self, captura: bytes, contexto: str) -> dict | None:
        if self._decidir_fn is not None:
            return self._decidir_fn(captura, self.instruccion, contexto)
        from .vision import decidir_accion_desde_captura

        return decidir_accion_desde_captura(
            self.llm, captura, self.instruccion, contexto=contexto
        )

    @staticmethod
    def _resultado(
        ok: bool,
        motivo: str,
        trace: list[PasoVision],
        capturas: int,
        segundos: float,
    ) -> dict[str, Any]:
        return {
            "ok": bool(ok),
            "motivo": motivo,
            "pasos": len(trace),
            "capturas": int(capturas),
            "segundos": round(float(segundos), 3),
            "trace": [p.to_dict() for p in trace],
        }

    # ------------------------------------------------------------
    # Ejecución
    # ------------------------------------------------------------
    def ejecutar(self) -> dict[str, Any]:
        """Corre el ciclo completo y devuelve el resultado con la traza."""
        from .vision import vision_habilitada

        inicio = self._ahora()
        trace: list[PasoVision] = []
        capturas = 0
        fallos_consecutivos = 0

        # ── Guardrails previos (sin gastar ni una llamada al VLM) ──
        if not vision_habilitada():
            logger.info("VisionLoop: kill switch desactivado; no se ejecuta")
            return self._resultado(
                False, "vision_deshabilitada", trace, capturas,
                self._segundos(inicio),
            )
        if self.driver is None:
            return self._resultado(
                False, "sin_driver", trace, capturas, self._segundos(inicio)
            )
        if not self.instruccion.strip():
            return self._resultado(
                False, "sin_instruccion", trace, capturas, self._segundos(inicio)
            )

        motivo = "pasos_agotados"
        ok = False

        for paso in range(1, self.max_steps + 1):
            if self._cancelado():
                motivo = "cancelado"
                break
            if self._segundos(inicio) > self.max_segundos:
                motivo = "tiempo_agotado"
                break
            if capturas >= self.max_capturas:
                motivo = "capturas_agotadas"
                break

            # 1. PERCIBIR
            try:
                captura = self.driver.capturar()
            except Exception as e:
                motivo = f"error_captura: {e}"
                logger.warning(f"VisionLoop: fallo capturando: {e}")
                break
            if not captura:
                motivo = "captura_no_disponible"
                break
            capturas += 1
            contexto = self.driver.estado_texto()

            # 2. RAZONAR
            try:
                decision = self._decidir(captura, contexto)
            except Exception as e:
                motivo = f"error_decision: {e}"
                logger.warning(f"VisionLoop: fallo decidiendo: {e}")
                break
            if decision is None:
                motivo = "sin_decision"
                break

            accion = decision.get("accion")
            if accion is None:
                # El modelo considera que el objetivo ya está cumplido.
                ok = True
                motivo = "objetivo_alcanzado"
                break

            tipo = str(accion.get("tipo", "") or "").strip().lower()
            if tipo not in self.acciones_permitidas:
                motivo = f"accion_no_permitida:{tipo}"
                logger.warning(f"VisionLoop: acción rechazada por allowlist: {tipo}")
                break

            # 3. ACTUAR
            momento = self._ahora()
            try:
                resultado = self.driver.ejecutar(accion) or {}
            except Exception as e:
                resultado = {"ok": False, "detalle": f"error: {e}"}
            duracion = self._ahora() - momento
            exito_accion = bool(resultado.get("ok"))

            trace.append(PasoVision(
                paso=paso,
                captura=capturas,
                accion=tipo,
                objetivo=str(decision.get("objetivo") or "")[:300],
                razon=str(decision.get("razon") or "")[:500],
                evidencia=str(decision.get("evidencia") or "")[:500],
                ok=exito_accion,
                detalle=str(resultado.get("detalle") or "")[:500],
                duracion=duracion,
                detalles_accion=dict(accion),
            ))

            # 4. VERIFICAR (la siguiente iteración vuelve a capturar). Si la
            # acción falla repetidamente, se corta para no dar vueltas.
            if exito_accion:
                fallos_consecutivos = 0
            else:
                fallos_consecutivos += 1
                if fallos_consecutivos >= self.max_fallos_consecutivos:
                    motivo = "fallos_consecutivos"
                    break

        return self._resultado(ok, motivo, trace, capturas, self._segundos(inicio))


# ============================================================
# ADAPTADOR PLAYWRIGHT (opt-in)
# ============================================================

def crear_driver_playwright(
    page: Any,
    *,
    agente: Any = None,
    timeout_accion_ms: int = 5000,
    timeout_s: int = 30,
    datos_extraidos: dict | None = None,
    screenshots: list | None = None,
) -> DriverNavegador:
    """Adapta una página de Playwright a :class:`DriverNavegador`.

    Reutiliza ``BrowserExecutor._ejecutar_accion``: la visión NO abre una vía
    de ejecución nueva ni se salta el allowlist ni el contrato del executor.
    """
    from .executors.browser_executor import BrowserExecutor

    datos = datos_extraidos if datos_extraidos is not None else {}
    shots = screenshots if screenshots is not None else []

    def _capturar() -> bytes | None:
        try:
            return page.screenshot(full_page=False)
        except Exception as e:
            logger.debug(f"VisionLoop: screenshot falló: {e}")
            return None

    def _contexto() -> str:
        try:
            return str(page.inner_text("body") or "")[:2000]
        except Exception:
            return ""

    def _ejecutar(accion: dict) -> dict:
        registro = BrowserExecutor._ejecutar_accion(
            page=page,
            accion=accion,
            variables={},
            timeout_accion_ms=int(timeout_accion_ms),
            timeout_s=int(timeout_s),
            agente=agente,
            datos_extraidos=datos,
            screenshots=shots,
        )
        return {
            "ok": bool(registro.get("ok")),
            "detalle": str(registro.get("detalle") or registro.get("error") or ""),
        }

    return DriverNavegador(
        capturar=_capturar,
        ejecutar=_ejecutar,
        contexto=_contexto,
        descripcion="playwright",
    )


__all__ = [
    "BrowserVisionLoop",
    "DriverNavegador",
    "MAX_CAPTURAS_DEFECTO",
    "MAX_FALLOS_CONSECUTIVOS_DEFECTO",
    "MAX_SEGUNDOS_DEFECTO",
    "MAX_STEPS_DEFECTO",
    "PasoVision",
    "configuracion_vision_loop",
    "crear_driver_playwright",
]
