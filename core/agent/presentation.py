# core/agent/presentation.py
"""Métricas, representaciones para UI y helpers de presentación."""
import time
from typing import TYPE_CHECKING

from .enums import EstadoAgente, TipoAgente

if TYPE_CHECKING:
    from .model import Agente


# ── Métricas ──
def obtener_tiempo_ejecucion(agente: "Agente") -> float:
    if agente.tiempo_inicio and agente.tiempo_fin:
        return agente.tiempo_fin - agente.tiempo_inicio
    elif agente.tiempo_inicio:
        return time.time() - agente.tiempo_inicio
    return 0.0


def obtener_duracion_estimada(agente: "Agente") -> float:
    if agente.tiempo_inicio:
        return obtener_tiempo_ejecucion(agente)
    return agente.duracion


def obtener_progreso_real(agente: "Agente") -> int:
    if agente.estado in (
        EstadoAgente.COMPLETADO,
        EstadoAgente.ERROR,
        EstadoAgente.CANCELADO,
    ):
        return 100
    elif agente.estado == EstadoAgente.EJECUTANDO:
        return max(agente.progreso, 10)
    elif agente.estado == EstadoAgente.LISTO:
        return 50
    elif agente.estado == EstadoAgente.ESPERANDO:
        return 25
    return agente.progreso


# ── Representaciones UI ──
def resumen_corto(agente: "Agente") -> str:
    if agente.tipo == TipoAgente.LOOP:
        sufijo = ", continúa en error" if agente.continuar_en_error else ""
        return (
            f"{agente.nombre} (Loop: {agente.fuente_items} → "
            f"{agente.max_iteraciones} max{sufijo})"
        )
    elif agente.tipo == TipoAgente.LLM:
        return f"{agente.nombre} (LLM: {agente.modelo_llm})"
    elif agente.tipo == TipoAgente.PYTHON:
        return f"{agente.nombre} (Python: {len(agente.codigo_python)} chars)"
    elif agente.tipo == TipoAgente.SHELL:
        return f"{agente.nombre} (Shell: {agente.comando_shell[:30]}...)"
    elif agente.tipo == TipoAgente.HTTP:
        return f"{agente.nombre} (HTTP: {agente.metodo_http} {agente.url_http[:30]}...)"
    elif agente.tipo == TipoAgente.FILE:
        return f"{agente.nombre} (File: {agente.operacion_file})"
    return f"{agente.nombre} ({agente.tipo.value})"


def obtener_icono(agente: "Agente") -> str:
    return TipoAgente.icono(agente.tipo)


def obtener_color_estado(agente: "Agente") -> str:
    return EstadoAgente.color(agente.estado)


def obtener_emoji_estado(agente: "Agente") -> str:
    return EstadoAgente.emoji(agente.estado)


def obtener_info_tooltip(agente: "Agente") -> str:
    lines = [
        f"🤖 {agente.nombre}",
        f"📌 Tipo: {agente.tipo.value}",
        f"📊 Estado: {obtener_emoji_estado(agente)} {agente.estado.value}",
        f"📝 {agente.descripcion or 'Sin descripción'}",
    ]
    if agente.tiene_dependencias():
        deps = agente.dependencias_nombres or agente.dependencias_ids
        lines.append(f"🔗 Dependencias: {', '.join(deps)}")
    if agente.estado == EstadoAgente.EJECUTANDO:
        lines.append(f"⏱ Tiempo: {obtener_tiempo_ejecucion(agente):.1f}s")
    if agente.error:
        lines.append(f"❌ Error: {agente.error[:100]}")
    if agente.resultado:
        lines.append(f"📊 Resultado: {str(agente.resultado)[:100]}...")
    return "\n".join(lines)


__all__ = [
    "obtener_tiempo_ejecucion",
    "obtener_duracion_estimada",
    "obtener_progreso_real",
    "resumen_corto",
    "obtener_icono",
    "obtener_color_estado",
    "obtener_emoji_estado",
    "obtener_info_tooltip",
]