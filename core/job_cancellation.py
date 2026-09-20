"""
core/job_cancellation.py
Cancelación REAL de trabajos en ejecución (V3.8-3).

Antes, ``POST /api/jobs/<id>/cancel`` solo marcaba la petición: el pipeline
ya en marcha seguía hasta el final y el job terminaba como ``failed``. El
``CancellationToken`` existía, pero nadie lo enlazaba al job.

El problema de fondo es de hilos: el ``Scheduler`` es un ``QObject`` que vive
en el hilo principal de Qt, y la petición de cancelación llega en un hilo de
werkzeug. Llamar a ``Scheduler.detener()`` desde ese hilo sería tocar el
scheduler desde fuera de su hilo.

Solución: este registro thread-safe. El hilo HTTP **solo deja una solicitud**;
el bucle de eventos de Qt la consume (~cada 200 ms) y llama a
``Scheduler.detener()`` en su propio hilo, que cancela tokens y workers de
verdad. Así la cancelación es real y respeta el modelo de hilos.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

RAZON_POR_DEFECTO = "Cancelado por el usuario"


class JobCancellationRegistry:
    """Registro de solicitudes de cancelación por ``job_id``.

    - ``registrar(job_id, cancelador)``: el pipeline anuncia que está
      ejecutando ese job y cómo detenerlo.
    - ``solicitar(job_id)``: el hilo HTTP pide la cancelación (no bloquea).
    - ``consumir(job_id)``: el hilo de Qt recoge la solicitud y la ejecuta.

    Es seguro entre hilos: todas las operaciones van bajo un ``RLock``.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._canceladores: dict[str, Callable[[str], Any]] = {}
        self._solicitudes: dict[str, str] = {}
        self._cancelados: set[str] = set()

    # ------------------------------------------------------------
    # Ciclo de vida del job
    # ------------------------------------------------------------
    def registrar(self, job_id: str, cancelador: Callable[[str], Any]) -> None:
        """Anuncia que ``job_id`` está en ejecución y cómo detenerlo."""
        if not job_id:
            return
        with self._lock:
            self._canceladores[job_id] = cancelador
        logger.debug(f"Cancelación: job {job_id} registrado")

    def desregistrar(self, job_id: str) -> None:
        """Deja de rastrear un job (idempotente)."""
        if not job_id:
            return
        with self._lock:
            self._canceladores.pop(job_id, None)
            self._solicitudes.pop(job_id, None)

    def limpiar(self, job_id: str) -> None:
        """Borra todo el estado del job (fin de ejecución)."""
        if not job_id:
            return
        with self._lock:
            self._canceladores.pop(job_id, None)
            self._solicitudes.pop(job_id, None)
            self._cancelados.discard(job_id)

    # ------------------------------------------------------------
    # Solicitud (hilo HTTP)
    # ------------------------------------------------------------
    def solicitar(self, job_id: str, razon: str = RAZON_POR_DEFECTO) -> bool:
        """Pide la cancelación de un job. Devuelve si hay algo que cancelar.

        No invoca el cancelador: solo deja la solicitud para que la recoja el
        hilo dueño del ``Scheduler``.
        """
        if not job_id:
            return False
        with self._lock:
            self._solicitudes[job_id] = razon or RAZON_POR_DEFECTO
            registrado = job_id in self._canceladores
        logger.info(f"Cancelación solicitada para el job {job_id}: {razon}")
        return registrado

    def hay_solicitud(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._solicitudes

    def consumir(self, job_id: str) -> str | None:
        """Recoge y borra la solicitud pendiente (o ``None``)."""
        if not job_id:
            return None
        with self._lock:
            razon = self._solicitudes.pop(job_id, None)
            if razon is not None:
                self._cancelados.add(job_id)
        return razon

    def fue_cancelado(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._cancelados

    # ------------------------------------------------------------
    # Cancelación inmediata (para quien YA está en el hilo correcto)
    # ------------------------------------------------------------
    def cancelar_ahora(self, job_id: str, razon: str = RAZON_POR_DEFECTO) -> bool:
        """Invoca el cancelador registrado en el hilo que llama.

        Solo debe usarse desde el hilo dueño del ``Scheduler`` (o en tests).
        """
        with self._lock:
            cancelador = self._canceladores.get(job_id)
            self._cancelados.add(job_id)
            self._solicitudes.pop(job_id, None)
        if cancelador is None:
            return False
        try:
            cancelador(razon)
            return True
        except Exception as e:
            logger.warning(f"Cancelación del job {job_id} falló: {e}")
            return False

    def esta_registrado(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._canceladores

    def estadisticas(self) -> dict[str, Any]:
        with self._lock:
            return {
                "registrados": len(self._canceladores),
                "solicitudes": len(self._solicitudes),
                "cancelados": len(self._cancelados),
            }


_REGISTRO: JobCancellationRegistry | None = None
_REGISTRO_LOCK = threading.Lock()


def obtener_registro_cancelacion() -> JobCancellationRegistry:
    """Singleton del registro (thread-safe)."""
    global _REGISTRO
    with _REGISTRO_LOCK:
        if _REGISTRO is None:
            _REGISTRO = JobCancellationRegistry()
        return _REGISTRO


def reset_registro_cancelacion() -> None:
    """Reinicia el singleton (tests / arranque limpio)."""
    global _REGISTRO
    with _REGISTRO_LOCK:
        _REGISTRO = None


__all__ = [
    "JobCancellationRegistry",
    "RAZON_POR_DEFECTO",
    "obtener_registro_cancelacion",
    "reset_registro_cancelacion",
]
