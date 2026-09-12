# core/agent/state_machine.py
"""Máquina de estados del agente: transiciones validadas.

Este módulo centraliza las operaciones que consultan o cambian el
estado del agente. Se implementa como funciones puras que reciben
un Agente para evitar el problema de import circular con scheduler.
"""
import logging
from typing import TYPE_CHECKING

from .enums import EstadoAgente

if TYPE_CHECKING:
    from .model import Agente

logger = logging.getLogger(__name__)


def transicionar_a(agente: "Agente", nuevo_estado: EstadoAgente, razon: str = "") -> bool:
    """
    Realiza una transición de estado validada.

    Args:
        agente: Instancia del agente a modificar.
        nuevo_estado: Estado destino.
        razon: Motivo de la transición (queda en agente.mensaje).

    Returns:
        bool: True si la transición fue exitosa.

    Raises:
        ValueError: Si la transición no es válida.
    """
    # Import diferido para no crear ciclo al cargar módulos
    from core.scheduler import TRANSICIONES_VALIDAS

    estado_actual = agente.estado

    if estado_actual == nuevo_estado:
        return True

    if estado_actual not in TRANSICIONES_VALIDAS:
        raise ValueError(
            f"Estado actual '{estado_actual.value}' no tiene transiciones definidas"
        )

    if nuevo_estado not in TRANSICIONES_VALIDAS[estado_actual]:
        validos = [e.value for e in TRANSICIONES_VALIDAS[estado_actual]]
        raise ValueError(
            f"Transición inválida: {estado_actual.value} → {nuevo_estado.value}\n"
            f"Transiciones válidas desde {estado_actual.value}: {validos}"
        )

    agente.estado = nuevo_estado
    if razon:
        agente.mensaje = razon

    logger.debug(
        f"Agente {agente.nombre} ({agente.id}): "
        f"{estado_actual.value} → {nuevo_estado.value} ({razon})"
    )
    return True


def es_terminal(agente: "Agente") -> bool:
    """Indica si el agente está en un estado terminal."""
    return EstadoAgente.es_terminal(agente.estado)


def puede_ejecutarse(agente: "Agente") -> bool:
    """Indica si el agente puede ser ejecutado."""
    return EstadoAgente.puede_ejecutarse(agente.estado)


def esta_activo(agente: "Agente") -> bool:
    """Indica si el agente está en ejecución activa."""
    return EstadoAgente.es_activo(agente.estado)


__all__ = ["transicionar_a", "es_terminal", "puede_ejecutarse", "esta_activo"]