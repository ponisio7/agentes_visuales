# core/logging_config.py
"""
Configuración de logging thread-safe para toda la aplicación.

PROBLEMA QUE RESUELVE
=====================
En Python 3.13, el logging estándar NO es thread-safe a nivel de `Handler.emit()`
y `Handler.flush()`. Cuando varios hilos (p. ej. workers de `ThreadPoolExecutor`
en `Scheduler`) llaman a `logger.debug()` simultáneamente mientras pytest (u otro
consumidor) hace `flush()` sobre el mismo stream, puede producirse corrupción de
memoria en el buffer interno del handler y, finalmente, un SIGSEGV (`rc=-11`).

SÍNTOMA TÍPICO
==============
    Thread 0x... (most recent call first):
      File "/usr/lib/python3.13/logging/__init__.py", line 1137 in flush
      File "/usr/lib/python3.13/logging/__init__.py", line 1155 in emit
      ...
      File "core/agent/model.py", line 878 in transicionar_a
      File "core/scheduler.py", line 491 in _ejecutar_agente
      File "concurrent/futures/thread.py", line 59 in run
    Fatal Python error: Segmentation fault

SOLUCIÓN
========
Usar `QueueHandler` + `QueueListener` (patrón canónico de la documentación
oficial de Python para "logging from multiple threads"). Los workers solo
encolan mensajes (operación thread-safe por diseño); un único hilo dedicado
los drena y los escribe. Esto serializa TODAS las escrituras y elimina la
concurrencia en `emit()` / `flush()`.

CARACTERÍSTICAS
===============
- ✅ Thread-safe a nivel de root logger (todos los `logging.getLogger(...)`).
- ✅ Idempotente: llamar varias veces no duplica handlers ni listeners.
- ✅ No captura `sys.stderr` al construirse (evita conflicto con pytest).
- ✅ Modo `use_null_handler=True` para tests: cero escritura, cero riesgo.
- ✅ Cleanup automático con `atexit` y API explícita `detener_logging()`.
- ✅ Respeta el nivel global y por handler (`respect_handler_level=True`).
- ✅ Funciona aunque se llame antes o después de que pytest capture stderr.
- ✅ No revienta si se llama desde dentro de un handler de logging.
"""

from __future__ import annotations

import atexit
import logging
import logging.handlers
import queue
import sys
import threading
from typing import Optional

# ============================================================
# ESTADO GLOBAL DEL MÓDULO
# ============================================================

_listener: Optional[logging.handlers.QueueListener] = None
_lock = threading.RLock()

# Flag para no registrar `atexit` más de una vez
_atexit_registered = False


# ============================================================
# HANDLERS AUXILIARES
# ============================================================

class _DynamicStderrHandler(logging.Handler):
    """
    Handler que resuelve `sys.stderr` en CADA emit, no al construirse.

    ¿Por qué? Pytest reemplaza `sys.stderr` por su propio capturador al
    inicio de cada test. Si construimos un `StreamHandler(sys.stderr)` al
    importar `conftest.py`, guardaríamos una referencia al stderr original
    de la terminal. Si lo construimos después, guardaríamos el capturador
    de pytest, que puede no ser thread-safe y reintroducir el SIGSEGV.

    Este handler evita ambos problemas: cada vez que emite, mira el
    `sys.stderr` actual. Además, el `QueueListener` corre en un solo hilo,
    así que el acceso concurrente a `sys.stderr` no existe.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            stream = sys.stderr  # resolver dinámicamente en cada emit
            if stream is None:
                return
            msg = self.format(record)
            stream.write(msg + "\n")
            # NO hacemos flush() aquí: el QueueListener ya corre en un
            # hilo dedicado, y forzar flush en cada mensaje multiplica
            # el riesgo de bloqueos sin ganar nada (Python hace flush
            # al salir del proceso o al llenarse el buffer).
        except Exception:
            self.handleError(record)


class _NullHandlerSafe(logging.Handler):
    """
    Handler nulo thread-safe (no usa el `logging.NullHandler` estándar
    porque este último tampoco es 100% libre de sorpresas en 3.13).

    Simplemente descarta los registros. Útil en tests para evitar
    cualquier escritura real mientras el `QueueListener` está activo.
    """

    def emit(self, record: logging.LogRecord) -> None:
        pass


# ============================================================
# API PÚBLICA
# ============================================================

def configurar_logging_thread_safe(
    level: int = logging.INFO,
    use_null_handler: bool = False,
    *,
    fmt: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt: Optional[str] = None,
) -> None:
    """
    Instala un `QueueHandler` en el root logger y arranca un `QueueListener`
    en un hilo dedicado. A partir de este momento, TODOS los mensajes de
    logging de la aplicación se escriben de forma serializada.

    Es idempotente: si ya está configurado, no hace nada.

    Args:
        level: Nivel mínimo del root logger. Los `logger.debug(...)` por
            debajo de este nivel ni siquiera se encolan (ahorro de CPU).
            En tests se recomienda `logging.WARNING`.
        use_null_handler: Si es `True`, el listener descarta los mensajes
            en lugar de escribirlos a stderr. Recomendado en tests para
            eliminar por completo la interacción con `sys.stderr` (y con
            el capturador de pytest).
        fmt: Formato de los mensajes.
        datefmt: Formato de fecha opcional.

    Notas:
        - Si se llama varias veces con distintos `level`, solo la primera
          llamada tiene efecto. Para reconfigurar, llamar antes a
          `detener_logging()`.
        - Seguro de llamar desde dentro de un handler de logging: el
          `RLock` permite reentrada.
    """
    global _listener, _atexit_registered

    with _lock:
        if _listener is not None:
            return  # ya configurado

        root = logging.getLogger()

        # ── 1. Retirar handlers previos (evita duplicar salida) ──
        for h in list(root.handlers):
            root.removeHandler(h)
            # Cerrar handlers que tengan close() (StreamHandler, FileHandler)
            try:
                h.close()
            except Exception:
                pass

        # ── 2. Cola ilimitada: los workers nunca se bloquean al encolar ──
        log_queue: queue.Queue = queue.Queue(-1)

        # ── 3. Handler que usan los workers: solo encola ──
        queue_handler = logging.handlers.QueueHandler(log_queue)

        # ── 4. Handler real que consume el listener ──
        if use_null_handler:
            sink_handler: logging.Handler = _NullHandlerSafe()
        else:
            sink_handler = _DynamicStderrHandler()
            sink_handler.setFormatter(logging.Formatter(fmt, datefmt=datefmt))

        # ── 5. Listener en un único hilo dedicado ──
        _listener = logging.handlers.QueueListener(
            log_queue,
            sink_handler,
            respect_handler_level=True,
        )
        _listener.start()

        # ── 6. Instalar en el root y fijar nivel ──
        root.addHandler(queue_handler)
        root.setLevel(level)

        # ── 7. Registrar cleanup UNA sola vez ──
        if not _atexit_registered:
            atexit.register(_cleanup_logging)
            _atexit_registered = True


def detener_logging(timeout: float = 2.0) -> None:
    """
    Detiene el listener de logging de forma ordenada.

    - Vacía la cola pendiente antes de parar (sin bloquear más de `timeout`).
    - Es idempotente: si no hay listener activo, no hace nada.
    - Seguro de llamar desde `atexit` o manualmente.

    Args:
        timeout: Tiempo máximo a esperar a que el listener drene la cola.
    """
    global _listener

    with _lock:
        if _listener is None:
            return
        listener = _listener
        _listener = None

    # Fuera del lock: `listener.stop()` espera al hilo, y no queremos
    # mantener el RLock mientras se une el hilo (podría bloquear a otro
    # hilo que llame a configurar/detener en paralelo).
    try:
        listener.stop()
    except Exception:
        pass

    # Limpieza best-effort: quitar el QueueHandler del root para que
    # llamadas posteriores a logger.* no encolen en una cola huérfana.
    try:
        root = logging.getLogger()
        for h in list(root.handlers):
            if isinstance(h, logging.handlers.QueueHandler):
                root.removeHandler(h)
                try:
                    h.close()
                except Exception:
                    pass
    except Exception:
        pass


def esta_configurado() -> bool:
    """Retorna True si el logging thread-safe está activo."""
    with _lock:
        return _listener is not None


# ============================================================
# CLEANUP AUTOMÁTICO
# ============================================================

def _cleanup_logging() -> None:
    """Wrapper de atexit para que un fallo aquí no tumbe el proceso."""
    try:
        detener_logging()
    except Exception:
        pass


# ============================================================
# AUTO-CONFIGURACIÓN OPCIONAL AL IMPORTAR
# ============================================================
# Si el entorno define AGENTES_LOG_LEVEL, configuramos automáticamente
# al importar. Esto es útil para scripts sueltos y para que `conftest.py`
# no tenga que acordarse de llamar a la función.
#
# Ejemplo:
#   AGENTES_LOG_LEVEL=WARNING python -m pytest ...
#
import os as _os

_auto = _os.environ.get("AGENTES_LOG_LEVEL")
if _auto:
    try:
        _nivel = getattr(logging, _auto.upper())
    except AttributeError:
        _nivel = logging.INFO
    # En tests, si el usuario no lo pide explícitamente, usamos null handler
    _null = _os.environ.get("AGENTES_LOG_NULL", "").lower() in ("1", "true", "yes")
    configurar_logging_thread_safe(level=_nivel, use_null_handler=_null)