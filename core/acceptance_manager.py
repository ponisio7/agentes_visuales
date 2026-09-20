"""
core/acceptance_manager.py
Veredicto de aceptación de una ejecución (V3.8-6).

La regla de fondo (H6) es: una ejecución solo se acepta si **todos** los
agentes terminaron bien y **todos** los pasos con contrato o críticos
pasaron la verificación determinista. Un paso «no verificable» tampoco se da
por bueno.

Estaba dentro de ``Scheduler._calcular_aceptacion_internal``; se extrae como
función pura (no toca hilos ni señales) para poder probarla sola. El
Scheduler la llama y guarda el resultado.
"""
from __future__ import annotations

from typing import Any

from .agent import EstadoAgente


def calcular_aceptacion(
    agentes: dict[str, Any],
    verificaciones: dict[str, dict] | None = None,
    *,
    parada_dura: dict | None = None,
    presupuesto: Any = None,
) -> dict:
    """Veredicto serializable de aceptación de la ejecución.

    ``verificaciones`` mapea ``agente_id -> ResultadoVerificacion`` (dict).
    ``parada_dura`` y ``presupuesto`` son metadatos de V3.8 que se exponen
    para que API/GUI expliquen por qué se paró y cuánto se gastó.
    """
    verificaciones = verificaciones or {}
    fallos: list[str] = []
    pasos: list[dict] = []
    verificados = 0
    no_verificables = 0

    for agente in agentes.values():
        verificacion = verificaciones.get(agente.id)
        if verificacion is not None:
            verificados += 1
            if not verificacion.get("aceptado", True):
                fallos.append(
                    f"'{agente.nombre}': "
                    f"{'; '.join(verificacion.get('motivos') or []) or 'no cumple el contrato'}"
                )
            for comprobacion in verificacion.get("comprobaciones") or []:
                if comprobacion.get("no_verificable"):
                    no_verificables += 1
                    break
            pasos.append({
                "agente_id": agente.id,
                "nombre": agente.nombre,
                "verificado": True,
                "aceptado": bool(verificacion.get("aceptado")),
                "motivos": list(verificacion.get("motivos") or []),
                "advertencias": list(verificacion.get("advertencias") or []),
                "evidencias": list(verificacion.get("evidencias") or []),
                "criterios_comprobados": list(
                    verificacion.get("criterios_comprobados") or []
                ),
                "criterios_fallidos": list(
                    verificacion.get("criterios_fallidos") or []
                ),
            })
            continue

        if agente.estado in (
            EstadoAgente.ERROR,
            EstadoAgente.TIMEOUT,
            EstadoAgente.CANCELADO,
            EstadoAgente.BLOQUEADO,
            EstadoAgente.SALTADO,
        ):
            fallos.append(
                f"'{agente.nombre}': {agente.mensaje or agente.error or agente.estado.value}"
            )
        # Los pasos críticos o con contrato deben haberse verificado. Si no
        # hay verificación registrada es porque el paso no llegó a
        # ejecutarse: se considera fallo, no «no verificable».
        elif agente.estado == EstadoAgente.COMPLETADO and (
            getattr(agente, "es_critico", False)
            or getattr(agente, "contrato_aceptacion", None)
        ):
            fallos.append(
                f"'{agente.nombre}': terminó sin verificación de aceptación"
            )

    aceptada = not fallos
    resumen = "aceptada" if aceptada else "rechazada: " + "; ".join(fallos)
    resultado: dict[str, Any] = {
        "aceptada": aceptada,
        "verificada": verificados > 0,
        "no_verificables": no_verificables,
        "motivos": fallos,
        "resumen": resumen,
        "pasos": pasos,
    }
    # V3.8: si la ejecución se abortó por una parada dura, se expone el motivo
    # estructurado para que API/GUI expliquen por qué no se insistió.
    if parada_dura:
        resultado["parada_dura"] = dict(parada_dura)
    # V3.8: consumo de la ejecución (tiempo/llamadas/tokens/coste).
    if presupuesto is not None:
        try:
            resultado["presupuesto"] = presupuesto.resumen()
        except Exception:
            pass
    return resultado


__all__ = ["calcular_aceptacion"]
