# core/log_bus.py
"""Bus de logs unificado (H4).

Una única fuente de logs para terminal, GUI, web y fichero:

* ``logging`` ya cubre terminal (``basicConfig``) y fichero
  (``RotatingFileHandler``). Este módulo añade un ``Handler`` que copia cada
  registro a un buffer acotado en memoria.
* La GUI y la web consumen ese buffer con un **cursor**: piden solo lo nuevo
  desde la última posición que vieron. Así no hay señales cruzadas entre
  hilos ni se repite historial.

Guardrails:

* El buffer está acotado (``deque(maxlen=...)``): nunca crece sin límite.
* ``emit`` no bloquea al emisor más que un ``append`` bajo lock.
* Protección anti-recursión: si el propio bus loguea algo, se ignora; y un
  guard de reentrada por hilo evita un bucle si un handler del logging
  escribe a su vez.
* No se instala más de una vez (``instalar_handler`` es idempotente).
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

MAX_ENTRADAS_DEFAULT = 2000
NOMBRE_LOGGER_INTERNO = "core.log_bus"


@dataclass(frozen=True)
class EntradaLog:
    """Un registro de log listo para mostrar."""
    seq: int
    ts: float
    nivel: str
    logger: str
    mensaje: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "nivel": self.nivel,
            "logger": self.logger,
            "mensaje": self.mensaje,
        }


class BusLogs:
    """Buffer acotado de registros con lectura por cursor."""

    def __init__(self, max_entradas: int = MAX_ENTRADAS_DEFAULT):
        self._max = max(1, int(max_entradas))
        self._entradas: deque[EntradaLog] = deque(maxlen=self._max)
        self._lock = threading.Lock()
        self._seq = 0

    def agregar(self, registro: logging.LogRecord) -> None:
        """Añade un registro (thread-safe, no bloqueante más allá del lock)."""
        try:
            mensaje = registro.getMessage()
        except Exception:
            mensaje = str(getattr(registro, "msg", ""))
        with self._lock:
            self._seq += 1
            self._entradas.append(EntradaLog(
                seq=self._seq,
                ts=registro.created or time.time(),
                nivel=registro.levelname,
                logger=registro.name,
                mensaje=mensaje,
            ))

    def desde(self, cursor: int = 0, limite: int = 200) -> tuple[list[EntradaLog], int]:
        """Entradas con ``seq > cursor`` (máx. ``limite``) y el cursor nuevo.

        Si el cursor se quedó por detrás de lo que conserva el buffer, se
        devuelve lo disponible (sin error) y el cursor avanza al último.
        """
        with self._lock:
            if not self._entradas:
                return [], self._seq
            nuevo = self._seq
            limite = max(1, int(limite)) if limite else 200
            seleccion = [e for e in self._entradas if e.seq > cursor][:limite]
            if seleccion:
                nuevo = seleccion[-1].seq
            return seleccion, nuevo

    def ultimas(self, n: int = 100) -> list[EntradaLog]:
        with self._lock:
            return list(self._entradas)[-max(1, int(n)):]

    @property
    def cursor_actual(self) -> int:
        with self._lock:
            return self._seq

    def limpiar(self) -> None:
        with self._lock:
            self._entradas.clear()


class HandlerBus(logging.Handler):
    """``logging.Handler`` que copia los registros al :class:`BusLogs`."""

    def __init__(self, bus: BusLogs, level: int = logging.INFO):
        super().__init__(level=level)
        self._bus = bus
        self._local = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        # Anti-recursión: el propio bus nunca se copia a sí mismo, y un guard
        # por hilo evita un bucle si algo del bus logueara durante el emit.
        if record.name.startswith(NOMBRE_LOGGER_INTERNO):
            return
        if getattr(self._local, "en_emit", False):
            return
        self._local.en_emit = True
        try:
            self._bus.agregar(record)
        except Exception:
            # Un fallo del bus jamás debe romper a quien loguea.
            pass
        finally:
            self._local.en_emit = False


# ============================================================
# SINGLETON
# ============================================================

_bus: BusLogs | None = None
_handler: HandlerBus | None = None
_lock = threading.Lock()


def obtener_bus_logs() -> BusLogs:
    """Devuelve el bus de logs compartido (lo crea si no existe)."""
    global _bus
    with _lock:
        if _bus is None:
            _bus = BusLogs()
        return _bus


def instalar_handler(nivel: int = logging.INFO) -> HandlerBus:
    """Instala el handler del bus en el logger raíz (idempotente)."""
    global _bus, _handler
    with _lock:
        bus = _bus
        if bus is None:
            bus = BusLogs()
            _bus = bus
        if _handler is not None:
            return _handler
        handler = HandlerBus(bus, level=nivel)
        logging.getLogger().addHandler(handler)
        _handler = handler
        # No registramos con logger.info aquí para no contaminar el arranque.
        return handler


def reset_bus_logs() -> None:
    """Desinstala el handler y vacía el bus (tests)."""
    global _bus, _handler
    with _lock:
        if _handler is not None:
            try:
                logging.getLogger().removeHandler(_handler)
            except Exception:
                pass
            _handler = None
        _bus = None


__all__ = [
    "BusLogs",
    "EntradaLog",
    "HandlerBus",
    "instalar_handler",
    "obtener_bus_logs",
    "reset_bus_logs",
]
