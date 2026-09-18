# tests/conftest.py
"""Configuración y fixtures compartidas para pytest.

Añade la raíz del proyecto al ``sys.path`` y expone las fixtures comunes a
toda la suite: ``qapp`` (QApplication), ``esperar`` (espera activa sin
bloquear el event loop de Qt) y los schedulers rápidos/básicos usados por
varias pruebas.
"""
import sys
import time
from collections.abc import Callable
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def limpiar_scheduler(scheduler) -> None:
    """Detiene un Scheduler y libera su ThreadPoolExecutor.

    Sin este teardown cada test deja vivos hilos del pool y un QTimer, lo
    que contamina los tests siguientes y contribuye a los segfaults
    esporádicos de CPython 3.13 + Qt + fork.
    """
    try:
        scheduler.detener()
    except Exception:
        pass
    try:
        scheduler._executor.shutdown(wait=True, cancel_futures=True)
    except Exception:
        pass


def esperar_condicion(
    condicion: Callable[[], bool],
    timeout: float = 5.0,
    intervalo: float = 0.05,
    qapp=None,
) -> bool:
    """Espera activa a que ``condicion()`` sea verdadera.

    Procesa los eventos pendientes de Qt (si hay una QApplication) sin
    bloquear con ``QEventLoop.exec()``, para que las señales del scheduler
    se despachen mientras se espera.

    Args:
        condicion: Función que retorna True cuando se cumple la condición.
        timeout: Tiempo máximo de espera en segundos.
        intervalo: Pausa entre comprobaciones.
        qapp: QApplication opcional; si es None se usa la instancia global.

    Returns:
        True si la condición se cumplió antes del timeout, False en caso contrario.
    """
    from PyQt6.QtWidgets import QApplication

    app = qapp if qapp is not None else QApplication.instance()
    inicio = time.time()
    while time.time() - inicio < timeout:
        if condicion():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(intervalo)
    return False


@pytest.fixture(scope="function")
def qapp():
    """Instancia única de QApplication para las pruebas que usan Qt."""
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


@pytest.fixture
def esperar():
    """Devuelve la función de espera activa (acepta ``qapp`` como argumento)."""
    return esperar_condicion


@pytest.fixture
def scheduler_rapido():
    """Scheduler con 3 agentes rápidos en cadena A1 → A2 → A3."""
    from core.agent import Agente
    from core.scheduler import Scheduler

    codigo = "resultado = {'status': 'ok', 'id': 'test'}"
    scheduler = Scheduler(max_concurrent=2)
    a1 = Agente(nombre="A1", duracion=0.05, codigo_python=codigo)
    a2 = Agente(
        nombre="A2",
        duracion=0.05,
        dependencias_nombres=["A1"],
        codigo_python=codigo,
    )
    a3 = Agente(
        nombre="A3",
        duracion=0.05,
        dependencias_nombres=["A2"],
        codigo_python=codigo,
    )
    scheduler.agregar_agentes([a1, a2, a3])
    scheduler.resolver_dependencias()
    yield scheduler
    limpiar_scheduler(scheduler)


@pytest.fixture
def scheduler_basico():
    """Scheduler con 3 agentes en cadena A1 → A2 → A3."""
    from core.agent import Agente
    from core.scheduler import Scheduler

    codigo = "resultado = {'status': 'ok', 'id': 'test'}"
    scheduler = Scheduler(max_concurrent=2)
    a1 = Agente(nombre="A1", duracion=0.1, codigo_python=codigo)
    a2 = Agente(
        nombre="A2",
        duracion=0.1,
        dependencias_nombres=["A1"],
        codigo_python=codigo,
    )
    a3 = Agente(
        nombre="A3",
        duracion=0.1,
        dependencias_nombres=["A2"],
        codigo_python=codigo,
    )
    scheduler.agregar_agentes([a1, a2, a3])
    scheduler.resolver_dependencias()
    yield scheduler
    limpiar_scheduler(scheduler)
