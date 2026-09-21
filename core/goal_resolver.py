# core/goal_resolver.py
"""
GoalResolver — modo «Resolver tarea» (V4.0).

Diferencia con `run`:

  - ``run`` es de **un solo tiro**: plan → ejecutar → veredicto. Si la
    verificación no acepta el artefacto, la ejecución termina y se reporta el
    fallo (Plan B repara pasos sueltos, pero nadie vuelve a plantear el plan).
  - ``resolver`` es un **bucle dirigido por el objetivo y por la verificación**:

        planificar → comprobar que el plan es VERIFICABLE → ejecutar → verificar
             ↑                                                            │
             └──────────── re-planificar con la evidencia del fallo ──────┘

    hasta que el artefacto cumple su contrato de aceptación, o se agotan los
    intentos / el presupuesto. Entonces se para y se dice exactamente por qué.

Esto es lo que convierte «orquestador que ejecuta» en «sistema que persigue un
resultado». No inventa capacidades nuevas: **reutiliza** todo lo cerrado en
v3.7–v3.8:

  - verificación determinista + gate de aceptación (H6 / V3.8-6),
  - recovery y Plan B dentro de cada intento (H7),
  - presupuesto de tiempo/llamadas/tokens/coste, compartido por los intentos
    (V3.8-2),
  - lecciones y retrieval de casos exitosos (H8), que `ProblemSolver` ya
    inyecta en el prompt de planificación.

Decisiones de diseño:

  - El módulo es **puro**: no importa Qt ni el Scheduler. El planificador y el
    ejecutor se inyectan como callables, de modo que el bucle se puede probar
    sin LLM ni GUI y reutilizar desde CLI, web o API.
  - El sistema **decide la verificación**: antes de gastar una ejecución
    comprueba que el plan declara un contrato de aceptación en el paso que
    produce el resultado. Un plan que no se puede verificar no se ejecuta: se
    devuelve al planificador con el motivo. Es la regla de H6 aplicada al
    proceso de planificación, no solo al final.
  - Parar es un resultado legítimo: `ResultadoResolucion` siempre dice si se
    resolvió, cuántos intentos se gastaron, por qué se paró y cuánto costó.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# Motivos de parada
# ============================================================
PARADA_VERIFICADO = "verificado"
PARADA_INTENTOS = "intentos_agotados"
PARADA_PRESUPUESTO = "presupuesto_agotado"

# Máximo de intentos por defecto. Dos: un plan y una re-planificación con la
# evidencia del primer fallo. Más intentos sin cambiar de estrategia solo
# gastan presupuesto (el bucle pasa el motivo concreto, no repite a ciegas).
MAX_INTENTOS_DEFAULT = 2

# Instrucción con la que se devuelve al planificador un plan no verificable.
INSTRUCCION_VERIFICABILIDAD = (
    "El plan anterior NO es verificable y se ha descartado sin ejecutarlo.\n\n"
    "PROBLEMAS DETECTADOS:\n{problemas}\n\n"
    "Corrige el plan: el paso que produce el resultado final (o un paso "
    "marcado 'es_critico': true) DEBE declarar 'aceptacion' con invariantes "
    "comprobables en disco (archivos, formato, JSON parseable, imágenes...). "
    "Un paso sin 'aceptacion' no se puede verificar y el sistema no dará por "
    "bueno su resultado."
)


# ============================================================
# Datos
# ============================================================
@dataclass
class ResultadoIntento:
    """Lo que devuelve un ejecutor tras intentar un plan."""
    exito: bool
    motivo: str = ""
    aceptacion: dict = field(default_factory=dict)
    salida: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "exito": self.exito,
            "motivo": self.motivo,
            "aceptacion": self.aceptacion,
        }


@dataclass
class IntentoResolucion:
    """Traza de un intento del bucle (se ejecute o no)."""
    numero: int
    plan_id: str = ""
    plan_titulo: str = ""
    verificable: bool = False
    problemas_verificabilidad: list[str] = field(default_factory=list)
    ejecutado: bool = False
    exito: bool = False
    motivo: str = ""
    aceptacion: dict = field(default_factory=dict)
    error: str = ""
    # V4.0-3: riesgo de fallo estimado ANTES de ejecutar (observabilidad).
    # No altera la decisión: el clasificador entrenado ordena bien (lift ≈ 2.6
    # en el decil de mayor riesgo sobre el histórico) pero con umbral 0.5 su
    # precisión/cobertura de fallo son malas, así que se registra para seguirlo
    # y no para gatear ni re-planificar.
    riesgo_fallo: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "numero": self.numero,
            "plan_id": self.plan_id,
            "plan_titulo": self.plan_titulo,
            "verificable": self.verificable,
            "problemas_verificabilidad": list(self.problemas_verificabilidad),
            "ejecutado": self.ejecutado,
            "exito": self.exito,
            "motivo": self.motivo,
            "aceptacion": self.aceptacion,
            "error": self.error,
            "riesgo_fallo": self.riesgo_fallo,
        }


@dataclass
class ResultadoResolucion:
    """Veredicto final del modo «Resolver tarea»."""
    resuelto: bool
    objetivo: str
    motivo: str = ""
    parada: str = PARADA_INTENTOS
    intentos: list[IntentoResolucion] = field(default_factory=list)
    plan_final: Any = None
    salida_final: dict = field(default_factory=dict)
    presupuesto: dict = field(default_factory=dict)

    @property
    def n_intentos(self) -> int:
        return len(self.intentos)

    @property
    def ejecuciones(self) -> int:
        return sum(1 for i in self.intentos if i.ejecutado)

    def to_dict(self) -> dict:
        return {
            "resuelto": self.resuelto,
            "objetivo": self.objetivo,
            "motivo": self.motivo,
            "parada": self.parada,
            "intentos": [i.to_dict() for i in self.intentos],
            "n_intentos": self.n_intentos,
            "ejecuciones": self.ejecuciones,
            "presupuesto": self.presupuesto,
            "ok": self.resuelto,
            "estado": "completada" if self.resuelto else "fallida",
        }


# ============================================================
# Verificabilidad del plan
# ============================================================
def _tiene_contrato(paso: Any) -> bool:
    contrato = getattr(paso, "aceptacion", None)
    if contrato is None:
        return False
    try:
        return not contrato.es_vacio()
    except Exception:
        return True


def _pasos_terminales(plan: Any) -> list[Any]:
    """Pasos cuyo resultado no consume ningún otro paso."""
    pasos = list(getattr(plan, "pasos", None) or [])
    consumidos: set[str] = set()
    for paso in pasos:
        for dep in getattr(paso, "dependencia_ids", None) or []:
            consumidos.add(str(dep))
    return [p for p in pasos if getattr(p, "nombre", "") not in consumidos]


def problemas_de_verificabilidad(plan: Any) -> list[str]:
    """¿Puede el sistema comprobar que este plan produjo lo que prometió?

    Es la puerta que V4.0 añade al proceso de planificación. Devuelve la lista
    de problemas (vacía = plan verificable).

    Un plan es verificable si al menos uno de sus pasos «que cierran la tarea»
    —marcado ``es_critico`` o terminal (nada depende de él)— declara un
    contrato de aceptación no vacío. Si ninguno lo declara, el resultado no se
    puede comprobar y ejecutar sería gastar presupuesto a ciegas.
    """
    pasos = list(getattr(plan, "pasos", None) or [])
    if not pasos:
        return ["el plan no tiene pasos"]

    criticos = [p for p in pasos if getattr(p, "es_critico", False)]
    terminales = _pasos_terminales(plan)

    candidatos: list[Any] = []
    for paso in list(criticos) + list(terminales):
        if paso not in candidatos:
            candidatos.append(paso)

    if any(_tiene_contrato(p) for p in candidatos):
        return []

    nombres = ", ".join(f"'{getattr(p, 'nombre', '?')}'" for p in candidatos)
    return [
        "ningún paso final o crítico declara un contrato de aceptación "
        f"comprobable en disco ({nombres}); el resultado no se podría verificar"
    ]


# ============================================================
# Evidencia para la re-planificación
# ============================================================
def construir_evidencia(intento: IntentoResolucion) -> str:
    """Instrucción correctiva para el siguiente intento.

    No repite el plan a ciegas: pasa al planificador el motivo **concreto**
    por el que el intento anterior no se aceptó.
    """
    if intento.error:
        return (
            "El intento anterior no llegó a producir un plan:\n"
            f"{intento.error}\n\n"
            "Vuelve a generar el plan completo para el objetivo."
        )

    if not intento.ejecutado:
        return INSTRUCCION_VERIFICABILIDAD.format(
            problemas="\n".join(f"- {p}" for p in intento.problemas_verificabilidad)
        )

    lineas = [
        "El plan anterior SÍ se ejecutó, pero NO superó la verificación de "
        "aceptación. Este es el motivo concreto:",
        f"{intento.motivo or 'no cumple el contrato'}",
        "",
        "Genera un plan nuevo (puedes cambiar de estrategia y de pasos, no "
        "solo retocar el anterior) que produzca un artefacto que SÍ cumpla "
        "esos invariantes. Mantén el objetivo del usuario sin cambiarlo.",
    ]
    return "\n".join(lineas)


def resultado_desde_salida(salida: dict) -> ResultadoIntento:
    """Adapta la salida de un ejecutor tipo ``main._ejecutar_plan``.

    Un intento solo cuenta como éxito si los agentes terminaron bien **y** la
    aceptación dio el visto bueno: «terminado» no es «aceptado» (H6).
    """
    salida = dict(salida or {})
    aceptacion = dict(salida.get("aceptacion") or {})

    errores: list[str] = []
    for agente in salida.get("agentes") or []:
        if isinstance(agente, dict) and not agente.get("ok"):
            detalle = agente.get("error") or agente.get("estado") or "fallo"
            errores.append(f"'{agente.get('nombre', '?')}': {detalle}")

    exito = bool(salida.get("ok")) and bool(aceptacion.get("aceptada", salida.get("ok")))
    motivos = [str(m) for m in (aceptacion.get("motivos") or [])]
    if motivos:
        motivo = "; ".join(motivos)
    elif errores:
        motivo = "; ".join(errores)
    elif not exito:
        motivo = str(salida.get("estado") or "la ejecución no fue aceptada")
    else:
        motivo = ""

    return ResultadoIntento(
        exito=exito, motivo=motivo, aceptacion=aceptacion, salida=salida
    )


# ============================================================
# Resolver
# ============================================================
class GoalResolver:
    """Bucle objetivo → plan → ejecución → verificación → re-planificación.

    Args:
        planificador: ``(objetivo, evidencia) -> ExecutionPlan``. ``evidencia``
            es ``""`` en el primer intento y, después, la instrucción
            correctiva construida a partir del fallo anterior.
        ejecutor: ``(plan, objetivo) -> ResultadoIntento``.
        presupuesto: ``BudgetManager`` opcional, **compartido** por todos los
            intentos para que el coste total esté acotado.
        max_intentos: tope de intentos (planificaciones), no de reintentos de
            agente. Debe ser ≥ 1.
        exigir_verificacion: si es ``True`` (por defecto), un plan sin contrato
            de aceptación en el paso final/crítico no se ejecuta.
        predictor: callable opcional ``(plan) -> dict`` con el riesgo estimado
            de fallo ANTES de ejecutar (V4.0-3). Es **observabilidad**: se
            registra en la traza del intento y NO altera ninguna decisión.
    """

    def __init__(
        self,
        planificador: Callable[[str, str], Any],
        ejecutor: Callable[[Any, str], ResultadoIntento],
        *,
        presupuesto: Any = None,
        max_intentos: int = MAX_INTENTOS_DEFAULT,
        exigir_verificacion: bool = True,
        predictor: Callable[[Any], dict] | None = None,
        logger_: logging.Logger | None = None,
    ):
        if not callable(planificador):
            raise TypeError("planificador debe ser invocable")
        if not callable(ejecutor):
            raise TypeError("ejecutor debe ser invocable")
        try:
            max_intentos = int(max_intentos)
        except (TypeError, ValueError):
            max_intentos = MAX_INTENTOS_DEFAULT
        self.max_intentos = max(1, max_intentos)
        self.planificador = planificador
        self.ejecutor = ejecutor
        self.presupuesto = presupuesto
        self.exigir_verificacion = bool(exigir_verificacion)
        self.predictor = predictor
        self.logger = logger_ or logger

    def _riesgo(self, plan: Any) -> dict:
        """Riesgo estimado de fallo del plan (nunca rompe el bucle)."""
        if self.predictor is None:
            return {}
        try:
            datos = self.predictor(plan)
            return dict(datos) if datos else {}
        except Exception as e:
            self.logger.debug("Resolver: predictor de riesgo no disponible: %s", e)
            return {}

    # ------------------------------------------------------------
    def resolver(self, objetivo: str) -> ResultadoResolucion:
        """Persigue el objetivo hasta verificarlo o agotar intentos/presupuesto."""
        if not objetivo or not str(objetivo).strip():
            raise ValueError("El objetivo no puede estar vacío")
        objetivo = str(objetivo).strip()

        intentos: list[IntentoResolucion] = []
        resuelto = False
        parada = PARADA_INTENTOS
        plan_final: Any = None
        salida_final: dict = {}
        motivo = ""

        if self.presupuesto is not None and not self.presupuesto.iniciado:
            self.presupuesto.iniciar()

        for numero in range(1, self.max_intentos + 1):
            # ── 0. Presupuesto: parar es un resultado, no un fallo oculto ──
            agotado = None
            if self.presupuesto is not None:
                agotado = self.presupuesto.motivo_agotado()
            if agotado:
                parada = PARADA_PRESUPUESTO
                motivo = (
                    f"presupuesto agotado ({agotado}) antes del intento {numero}"
                )
                self.logger.warning("🛑 Resolver: %s", motivo)
                break

            # ── 1. Planificar (con la evidencia del intento anterior) ──
            evidencia = "" if numero == 1 else construir_evidencia(intentos[-1])
            try:
                plan = self.planificador(objetivo, evidencia)
            except Exception as e:
                intentos.append(
                    IntentoResolucion(
                        numero=numero, error=f"la planificación falló: {e}"
                    )
                )
                motivo = f"la planificación falló: {e}"
                self.logger.warning("⚠️ Resolver: intento %d sin plan: %s", numero, e)
                continue

            plan_final = plan
            problemas = problemas_de_verificabilidad(plan)
            base = IntentoResolucion(
                numero=numero,
                plan_id=str(getattr(plan, "id", "")),
                plan_titulo=str(getattr(plan, "titulo", "")),
                problemas_verificabilidad=list(problemas),
            )

            # ── 2. Gate de verificabilidad: no se ejecuta lo que no se puede
            #       comprobar; se devuelve al planificador con el motivo. ──
            if problemas and self.exigir_verificacion:
                base.verificable = False
                base.motivo = "; ".join(problemas)
                intentos.append(base)
                motivo = (
                    "el plan no declara verificación para el resultado: "
                    + "; ".join(problemas)
                )
                self.logger.warning("⚠️ Resolver: intento %d no verificable: %s",
                                    numero, base.motivo)
                continue

            # ── 3. Ejecutar y verificar ──
            base.verificable = True
            base.riesgo_fallo = self._riesgo(plan)
            if base.riesgo_fallo.get("disponible"):
                self.logger.info(
                    "📉 Resolver: riesgo de fallo estimado %.2f (confianza %s)",
                    float(base.riesgo_fallo.get("probabilidad_fallo") or 0.0),
                    base.riesgo_fallo.get("confianza"),
                )
            self.logger.info(
                "▶️ Resolver: intento %d/%d — plan '%s' (%d pasos)",
                numero, self.max_intentos, base.plan_titulo or base.plan_id,
                len(getattr(plan, "pasos", None) or []),
            )
            try:
                resultado = self.ejecutor(plan, objetivo)
            except Exception as e:
                base.error = f"la ejecución falló: {e}"
                base.motivo = base.error
                intentos.append(base)
                motivo = base.error
                self.logger.warning("⚠️ Resolver: ejecución del intento %d falló: %s",
                                    numero, e)
                continue

            if resultado is None:
                resultado = ResultadoIntento(exito=False, motivo="el ejecutor no devolvió resultado")
            base.ejecutado = True
            base.exito = bool(resultado.exito)
            base.motivo = resultado.motivo
            base.aceptacion = dict(resultado.aceptacion or {})
            intentos.append(base)
            salida_final = dict(resultado.salida or {})

            if resultado.exito:
                resuelto = True
                parada = PARADA_VERIFICADO
                motivo = "el artefacto cumple el contrato de aceptación"
                self.logger.info("✅ Resolver: objetivo verificado en el intento %d", numero)
                break

            motivo = resultado.motivo or "la verificación no aceptó el resultado"
            self.logger.warning(
                "❌ Resolver: intento %d no aceptado: %s", numero, motivo
            )
        else:
            parada = PARADA_INTENTOS
            if not motivo:
                motivo = (
                    f"no se superó la verificación en {self.max_intentos} intento(s)"
                )

        presupuesto = {}
        if self.presupuesto is not None:
            try:
                presupuesto = self.presupuesto.resumen()
            except Exception as e:
                self.logger.debug("Resolver: no se pudo resumir el presupuesto: %s", e)

        return ResultadoResolucion(
            resuelto=resuelto,
            objetivo=objetivo,
            motivo=motivo,
            parada=parada,
            intentos=intentos,
            plan_final=plan_final,
            salida_final=salida_final,
            presupuesto=presupuesto,
        )


__all__ = [
    "GoalResolver",
    "ResultadoIntento",
    "IntentoResolucion",
    "ResultadoResolucion",
    "resultado_desde_salida",
    "problemas_de_verificabilidad",
    "construir_evidencia",
    "MAX_INTENTOS_DEFAULT",
    "PARADA_VERIFICADO",
    "PARADA_INTENTOS",
    "PARADA_PRESUPUESTO",
]
