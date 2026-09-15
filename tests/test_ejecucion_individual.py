import time

from core.agent import Agente, EstadoAgente
from core.scheduler import Scheduler


def esperar_estado(agente, estado, timeout=3):
    limite = time.time() + timeout
    while time.time() < limite:
        if agente.estado == estado:
            return True
        time.sleep(0.01)
    return False


def test_ejecucion_individual_no_requiere_iniciar_scheduler():
    scheduler = Scheduler(max_concurrent=1)
    agente = Agente(nombre="Prueba", duracion=0.05)

    try:
        scheduler.agregar_agente(agente)

        assert scheduler.ejecutando is False
        assert scheduler.ejecutar_agente_individual(agente.id) is True
        assert esperar_estado(agente, EstadoAgente.COMPLETADO)
        assert scheduler.ejecutando is False
        assert agente.resultado is not None
    finally:
        scheduler._executor.shutdown(wait=True)


def test_ejecucion_individual_no_consume_running_del_dag():
    scheduler = Scheduler(max_concurrent=1)
    principal = Agente(nombre="Principal", duracion=0.15)
    prueba = Agente(nombre="Prueba", duracion=0.05)

    try:
        scheduler.agregar_agente(principal)
        scheduler.agregar_agente(prueba)

        scheduler.iniciar()
        time.sleep(0.02)

        # Aunque max_concurrent=1, la prueba individual no se contabiliza
        # en scheduler.running.
        assert scheduler.ejecutar_agente_individual(prueba.id) is True
        assert esperar_estado(prueba, EstadoAgente.COMPLETADO)
        assert esperar_estado(principal, EstadoAgente.COMPLETADO)
    finally:
        scheduler.detener()
        scheduler._executor.shutdown(wait=True)
