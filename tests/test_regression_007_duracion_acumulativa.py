"""Regresión 007 — la duración de un agente se ACUMULA entre reintentos.

Bug 1.5 del ROADMAP (punto 3.7):

    El Scheduler hacía ``agente.duracion = duracion`` en la fase de
    finalización, con ``duracion`` calculada por intento. Con reintentos de
    5 s + 7 s + 4 s, la duración final era 4 s (la del último intento) en vez
    de 16 s.

Arreglo: se acumula en ``agente._duracion_acumulada`` y ``agente.duracion``
refleja la suma. Se usa un atributo propio para no arrastrar el ``0.1`` de
relleno que fija ``Agente.__post_init__`` cuando la duración es <= 0.

El test compara contra un control de UN solo intento: así la comprobación no
depende de lo que tarde el sandbox en arrancar.
"""
from __future__ import annotations

import pytest

from core.agent import Agente, EstadoAgente, TipoAgente
from core.scheduler import Scheduler

SUEÑO = 0.3

# Cada intento duerme SUEÑO y falla las dos primeras veces. El contador vive en
# un fichero porque el código corre en el sandbox (subproceso).
CODIGO = f"""
import os, time
time.sleep({SUEÑO})
ruta = "contador_reintentos.txt"
intentos = 0
if os.path.exists(ruta):
    intentos = int(open(ruta).read().strip() or 0)
intentos += 1
open(ruta, "w").write(str(intentos))
if intentos < {{maximo}}:
    raise ValueError(f"Fallo intencional #{{intentos}}")
resultado = {{'exito': True, 'intentos': intentos}}
"""


def _ejecutar(scheduler: Scheduler, qapp, esperar, timeout: float = 60.0) -> None:
    terminado = False

    def _al_terminar():
        nonlocal terminado
        terminado = True

    scheduler.ejecucion_terminada.connect(_al_terminar)
    scheduler.iniciar()
    try:
        assert esperar(lambda: terminado, timeout=timeout, qapp=qapp), (
            "la ejecución no terminó a tiempo"
        )
    finally:
        try:
            scheduler.detener()
        except Exception:
            pass


def _correr(tmp_path, monkeypatch, qapp, esperar, reintentos: int, fallos: int) -> Agente:
    """Ejecuta un agente que falla ``fallos`` veces antes de completar."""
    contador = tmp_path / "contador_reintentos.txt"
    if contador.exists():
        contador.unlink()

    scheduler = Scheduler(max_concurrent=1)
    agente = Agente(
        nombre="ConReintentos",
        tipo=TipoAgente.PYTHON,
        codigo_python=CODIGO.replace("{maximo}", str(fallos)),
        max_reintentos=reintentos,
    )
    scheduler.agregar_agente(agente)
    scheduler.resolver_dependencias()

    # El sandbox resuelve las rutas relativas contra el cwd del proceso.
    monkeypatch.chdir(tmp_path)
    _ejecutar(scheduler, qapp, esperar)

    return agente


@pytest.mark.slow
def test_la_duracion_suma_los_tres_intentos(tmp_path, monkeypatch, qapp, esperar):
    # Calentamiento: el primer arranque del sandbox paga costes en frío (escritura
    # del script, imports) que falsearían la comparación contra el control.
    _correr(tmp_path, monkeypatch, qapp, esperar, reintentos=0, fallos=99)

    control = _correr(tmp_path, monkeypatch, qapp, esperar, reintentos=0, fallos=99)
    con_reintentos = _correr(
        tmp_path, monkeypatch, qapp, esperar, reintentos=2, fallos=3
    )

    assert control.reintentos == 0
    assert con_reintentos.reintentos == 2, "deben agotarse 2 reintentos"
    assert con_reintentos.estado == EstadoAgente.COMPLETADO

    # El coste por intento es aproximadamente constante, así que 3 intentos
    # deben durar ~3x lo que 1. Con el comportamiento antiguo (sobrescribir)
    # el cociente sería ~1.
    uno = control.duracion
    tres = con_reintentos.duracion
    assert uno > 0, "el control debe medir algo"
    assert tres > 1.6 * uno, (
        f"duracion={tres:.2f}s frente a {uno:.2f}s de un solo intento: "
        "parece el último intento, no la suma"
    )


@pytest.mark.slow
def test_duracion_coincide_con_el_acumulador(tmp_path, monkeypatch, qapp, esperar):
    agente = _correr(tmp_path, monkeypatch, qapp, esperar, reintentos=1, fallos=2)

    assert agente.reintentos == 1
    assert agente.duracion == pytest.approx(agente._duracion_acumulada)


@pytest.mark.slow
def test_un_solo_intento_no_cambia_de_semantica(tmp_path, monkeypatch, qapp, esperar):
    """Sin reintentos la duración sigue siendo la del único intento."""
    agente = _correr(tmp_path, monkeypatch, qapp, esperar, reintentos=0, fallos=99)

    assert agente.reintentos == 0
    assert agente.duracion == pytest.approx(agente._duracion_acumulada)
    # El relleno de 0.1 de __post_init__ no debe contaminar la suma.
    assert agente.duracion > 0.1
