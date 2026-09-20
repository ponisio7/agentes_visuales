# web/jobs.py
"""Gestor de trabajos no bloqueante (H9).

``POST /run`` es bloqueante: el cliente HTTP espera a que termine la
ejecución. Para integrar la app como servicio hace falta poder lanzar un
trabajo, recibir un ``job_id`` y consultar su estado y sus logs en vivo.

El trabajo real lo sigue ejecutando el hilo principal de Qt (el ``Scheduler``
es un ``QObject``); este gestor solo mantiene el registro de trabajos y su
estado, compartiendo el mismo diccionario que consume ``ColaTrabajos``.
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any

# Estados de un trabajo.
QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"

TERMINALES = frozenset({COMPLETED, FAILED, CANCELLED})


class GestorTrabajos:
    """Registro thread-safe de trabajos sobre una ``ColaTrabajos``."""

    def __init__(self, cola: Any, max_trabajos: int = 200):
        self._cola = cola
        self._max = max(1, int(max_trabajos))
        self._jobs: dict[str, dict[str, Any]] = {}
        self._orden: list[str] = []
        self._lock = threading.Lock()

    # ------------------------------------------------------------
    # Creación
    # ------------------------------------------------------------
    def crear(
        self,
        *,
        problema: str,
        max_pasos: int,
        timeout: float | None,
        aprender: bool,
        agente: str | None,
    ) -> dict[str, Any]:
        """Encola un trabajo y devuelve su resumen (con ``job_id``)."""
        from core.log_bus import obtener_bus_logs

        job_id = uuid.uuid4().hex[:12]
        trabajo = self._cola.encolar(
            problema=problema,
            max_pasos=max_pasos,
            timeout=timeout,
            aprender=aprender,
            agente=agente,
            job_id=job_id,
            estado=QUEUED,
        )
        trabajo["creado"] = time.time()
        # Los logs del trabajo empiezan en el cursor actual del bus.
        try:
            trabajo["cursor_inicio"] = obtener_bus_logs().cursor_actual
        except Exception:
            trabajo["cursor_inicio"] = 0

        with self._lock:
            self._jobs[job_id] = trabajo
            self._orden.append(job_id)
            # Poda de trabajos antiguos ya terminados.
            while len(self._orden) > self._max:
                antiguo = self._orden[0]
                if self._jobs.get(antiguo, {}).get("estado") in TERMINALES:
                    self._orden.pop(0)
                    self._jobs.pop(antiguo, None)
                else:
                    break
        return self.resumen(job_id) or {}

    # ------------------------------------------------------------
    # Consulta
    # ------------------------------------------------------------
    def _obtener_crudo(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs.get(job_id)

    def obtener(self, job_id: str) -> dict[str, Any] | None:
        """Resumen serializable del trabajo (o None si no existe)."""
        trabajo = self._obtener_crudo(job_id)
        if trabajo is None:
            return None
        return self.resumen(job_id)

    def resumen(self, job_id: str) -> dict[str, Any] | None:
        trabajo = self._obtener_crudo(job_id)
        if trabajo is None:
            return None
        salida = trabajo.get("resultado") or {}
        aceptacion = (salida or {}).get("aceptacion") or {}
        return {
            "job_id": job_id,
            "estado": trabajo.get("estado", QUEUED),
            "problema": trabajo.get("problema", ""),
            "creado": trabajo.get("creado"),
            "iniciado": trabajo.get("iniciado"),
            "terminado": trabajo.get("terminado"),
            "error": trabajo.get("error"),
            # ``ok`` es de la petición; el resultado de la ejecución va aquí.
            "resultado_ok": (salida or {}).get("ok"),
            "estado_ejecucion": (salida or {}).get("estado"),
            "ejecucion_id": (salida or {}).get("ejecucion_id"),
            "aceptada": aceptacion.get("aceptada"),
            "motivos": aceptacion.get("motivos") or [],
            "resultado": (salida or {}).get("resultado", ""),
            "cancelado": bool(trabajo.get("cancelado")),
        }

    def cursor_inicio(self, job_id: str) -> int:
        trabajo = self._obtener_crudo(job_id)
        if trabajo is None:
            return 0
        return int(trabajo.get("cursor_inicio") or 0)

    def logs(self, job_id: str, cursor: int = 0, limite: int = 200):
        """Entradas del bus desde ``cursor`` (o desde el inicio del trabajo).

        Devuelve ``(entradas, cursor_nuevo)`` o ``(None, None)`` si el
        trabajo no existe.
        """
        trabajo = self._obtener_crudo(job_id)
        if trabajo is None:
            return None, None
        from core.log_bus import obtener_bus_logs

        if cursor <= 0:
            cursor = int(trabajo.get("cursor_inicio") or 0)
        return obtener_bus_logs().desde(cursor, limite)

    def es_terminal(self, job_id: str) -> bool:
        trabajo = self._obtener_crudo(job_id)
        return bool(trabajo and trabajo.get("estado") in TERMINALES)

    def listar(self, limite: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._orden)[-max(1, int(limite)):]
        return [r for r in (self.resumen(i) for i in reversed(ids)) if r]

    # ------------------------------------------------------------
    # Cancelación (best-effort)
    # ------------------------------------------------------------
    def cancelar(self, job_id: str) -> tuple[bool, str]:
        """Marca el trabajo como cancelado.

        - Encolado: no llegará a ejecutarse.
        - En ejecución: no se puede interrumpir el pipeline desde el hilo
          HTTP (el Scheduler vive en el hilo de Qt); se marca la petición y
          el trabajo termina cuando la ejecución acabe.

        Devuelve ``(encontrado, mensaje)``.
        """
        trabajo = self._obtener_crudo(job_id)
        if trabajo is None:
            return False, "job no encontrado"
        if trabajo.get("estado") in TERMINALES:
            return True, f"el job ya está {trabajo.get('estado')}"
        trabajo["cancelado"] = True
        if trabajo.get("estado") == QUEUED:
            trabajo["estado"] = CANCELLED
            return True, "job cancelado antes de ejecutarse"
        return True, "cancelación solicitada; el job terminará su ejecución actual"
