# tests/test_event_bus.py
"""Pruebas del bus de eventos (``core/event_bus.py``).

Cubre suscripción/desuscripción, tolerancia a callbacks que fallan (incluidos
``functools.partial`` sin ``__name__``) y el snapshot defensivo que evita
que un worker mute el payload que ya leyó la UI.
"""

import functools

import pytest

from core.event_bus import Event, EventType, obtener_bus


@pytest.fixture
def bus():
    """Bus global limpio para cada test."""
    b = obtener_bus()
    b.reiniciar()
    b.limpiar_historial()
    b._suscriptores.clear()
    b._suscriptores_todos.clear()
    b._max_historial = 1000
    yield b
    b.reiniciar()
    b.limpiar_historial()
    b._suscriptores.clear()
    b._suscriptores_todos.clear()
    b._max_historial = 1000


class TestSuscripcion:
    def test_suscribir_y_recibir(self, bus):
        recibidos = []
        bus.suscribir(EventType.LOG_MENSAJE, recibidos.append)
        evento = Event(EventType.LOG_MENSAJE, datos={"mensaje": "hola"})
        assert bus.publicar(evento) is True
        assert recibidos == [evento]

    def test_suscripcion_duplicada_devuelve_false(self, bus):
        cb = lambda e: None  # noqa: E731
        assert bus.suscribir(EventType.LOG_MENSAJE, cb) is True
        assert bus.suscribir(EventType.LOG_MENSAJE, cb) is False

    def test_no_recibe_otros_tipos(self, bus):
        recibidos = []
        bus.suscribir(EventType.LOG_MENSAJE, recibidos.append)
        bus.publicar(Event(EventType.LOG_ERROR, datos={}))
        assert recibidos == []

    def test_desuscribir(self, bus):
        recibidos = []
        bus.suscribir(EventType.LOG_MENSAJE, recibidos.append)
        assert bus.desuscribir(EventType.LOG_MENSAJE, recibidos.append) is True
        bus.publicar(Event(EventType.LOG_MENSAJE, datos={}))
        assert recibidos == []
        assert bus.desuscribir(EventType.LOG_MENSAJE, recibidos.append) is False

    def test_suscribir_todos(self, bus):
        recibidos = []
        bus.suscribir_todos(recibidos.append)
        bus.publicar(Event(EventType.LOG_MENSAJE, datos={}))
        bus.publicar(Event(EventType.AGENTE_ERROR, datos={}))
        assert len(recibidos) == 2

    def test_desuscribir_todos(self, bus):
        recibidos = []
        bus.suscribir_todos(recibidos.append)
        assert bus.desuscribir_todos(recibidos.append) is True
        bus.publicar(Event(EventType.LOG_MENSAJE, datos={}))
        assert recibidos == []


class TestErroresEnCallbacks:
    def test_un_callback_que_falla_no_impide_los_demas(self, bus):
        recibidos = []

        def malo(_):
            raise RuntimeError("boom")

        bus.suscribir(EventType.LOG_MENSAJE, malo)
        bus.suscribir(EventType.LOG_MENSAJE, recibidos.append)
        assert bus.publicar(Event(EventType.LOG_MENSAJE, datos={})) is True
        assert len(recibidos) == 1

    def test_callback_partial_que_falla_no_aborta(self, bus):
        """Regresión: un partial no tiene ``__name__`` y rompía el logging."""
        recibidos = []

        def malo(_):
            raise RuntimeError("boom")

        bus.suscribir(EventType.LOG_MENSAJE, functools.partial(malo))
        bus.suscribir(EventType.LOG_MENSAJE, recibidos.append)
        assert bus.publicar(Event(EventType.LOG_MENSAJE, datos={})) is True
        assert len(recibidos) == 1

    def test_callback_de_todos_que_falla_no_impide_los_demas(self, bus):
        recibidos = []

        def malo(_):
            raise RuntimeError("boom")

        bus.suscribir_todos(malo)
        bus.suscribir_todos(recibidos.append)
        assert bus.publicar(Event(EventType.LOG_MENSAJE, datos={})) is True
        assert len(recibidos) == 1


class TestHistorialYEstado:
    def test_historial_guarda_eventos(self, bus):
        bus.publicar(Event(EventType.LOG_MENSAJE, datos={"m": 1}))
        historial = bus.obtener_historial()
        assert len(historial) == 1
        assert historial[0].tipo == EventType.LOG_MENSAJE

    def test_historial_limitado(self, bus):
        bus._max_historial = 5
        for i in range(20):
            bus.publicar(Event(EventType.LOG_MENSAJE, datos={"i": i}))
        assert len(bus.obtener_historial(limit=1000)) == 5

    def test_limpiar_historial(self, bus):
        bus.publicar(Event(EventType.LOG_MENSAJE, datos={}))
        bus.limpiar_historial()
        assert bus.obtener_historial() == []

    def test_detener_y_reiniciar(self, bus):
        bus.detener()
        assert bus.publicar(Event(EventType.LOG_MENSAJE, datos={})) is False
        bus.reiniciar()
        assert bus.publicar(Event(EventType.LOG_MENSAJE, datos={})) is True

    def test_context_manager_detiene(self, bus):
        with bus:
            assert bus.publicar(Event(EventType.LOG_MENSAJE, datos={})) is True
        assert bus.publicar(Event(EventType.LOG_MENSAJE, datos={})) is False

    def test_estadisticas(self, bus):
        recibidos = []
        bus.suscribir(EventType.LOG_MENSAJE, recibidos.append)
        bus.publicar(Event(EventType.LOG_MENSAJE, datos={}))
        stats = bus.obtener_estadisticas()
        assert stats["total_eventos"] == 1
        assert stats["tipos"]["LOG_MENSAJE"] == 1
        assert stats["suscriptores"]["LOG_MENSAJE"] == 1
        assert stats["activo"] is True

    def test_evento_origen_por_defecto(self):
        evento = Event(EventType.LOG_MENSAJE)
        assert evento.origen == "desconocido"


class TestSnapshotDefensivo:
    def test_agente_completado_copia_el_payload(self, bus):
        resultado = {"clave": {"anidado": [1, 2]}}
        bus.publicar_agente_completado("A1", "Agente1", resultado=resultado)
        evento = bus.obtener_historial(1)[0]

        # Mutar el original no debe alterar el evento publicado.
        resultado["clave"]["anidado"].append(3)
        assert evento.datos["resultado"]["clave"]["anidado"] == [1, 2]

    def test_ejecucion_terminada_copia_stats(self, bus):
        stats = {"total": 1, "detalle": [1, 2]}
        bus.publicar_ejecucion_terminada(stats)
        evento = bus.obtener_historial(1)[0]
        stats["detalle"].append(3)
        assert evento.datos["stats"]["detalle"] == [1, 2]

    def test_helpers_publican_datos(self, bus):
        recibidos = []
        bus.suscribir_todos(recibidos.append)

        bus.publicar_agente_actualizado("A1")
        bus.publicar_agente_error("A1", "n", "fallo")
        bus.publicar_log("mensaje")
        bus.publicar_ejecucion_iniciada(3)
        bus.publicar_ejecucion_pausada()
        bus.publicar_ejecucion_reanudada()
        bus.publicar_estado_cambiado(True)

        assert len(recibidos) == 7
        assert bus.obtener_historial(1)[0].datos == {"ejecutando": True}


class TestSingleton:
    def test_misma_instancia(self):
        assert obtener_bus() is obtener_bus()
