# tests/test_cancellation.py
"""Pruebas del sistema de cancelación (``core/cancellation.py``).

Cubre tokens, callbacks (incluidos los que fallan) y el gestor central.
Son relevantes para la robustez multihilo: un token que no se cancela, o
callbacks ejecutados con el lock tomado, pueden colgar la aplicación.
"""

import threading

from core.cancellation import (
    CancellationManager,
    CancellationState,
    CancellationToken,
    obtener_gestor_cancelacion,
)


class TestCancellationToken:
    def test_estado_inicial(self):
        token = CancellationToken()
        assert token.esta_activo() is True
        assert token.esta_cancelado() is False
        assert token.estado == CancellationState.ACTIVE

    def test_ids_unicos(self):
        # Regresión: antes se usaba el milisegundo y podían colisionar.
        ids = {CancellationToken().id for _ in range(3000)}
        assert len(ids) == 3000

    def test_cancelar_devuelve_true_y_luego_false(self):
        token = CancellationToken()
        assert token.cancelar("test") is True
        assert token.esta_cancelado() is True
        assert token.cancelar("otra vez") is False

    def test_completar(self):
        token = CancellationToken()
        token.completar()
        assert token.estado == CancellationState.COMPLETED
        assert token.esta_activo() is False
        # Un token completado ya no se puede cancelar.
        assert token.cancelar() is False

    def test_metadata_cancelacion(self):
        token = CancellationToken()
        token.cancelar("razón x")
        assert token.obtener_metadata("razon_cancelacion") == "razón x"
        assert token.obtener_metadata("timestamp_cancelacion") is not None

    def test_callback_se_ejecuta(self):
        token = CancellationToken()
        vistos = []
        token.agregar_callback(lambda t: vistos.append(t.id))
        assert token.cancelar("test") is True
        assert vistos == [token.id]

    def test_callback_no_duplicado(self):
        token = CancellationToken()

        def cb(_):
            pass

        token.agregar_callback(cb)
        token.agregar_callback(cb)
        assert len(token._callbacks) == 1

    def test_callback_sobre_token_ya_cancelado_se_ejecuta(self):
        # Regresión: agregar_callback() solo registraba el callback, así que
        # si el token se cancelaba antes de registrarlo el callback no se
        # disparaba nunca (ventana de carrera real en shell_executor, cuyo
        # communicate() depende del callback para matar el proceso).
        token = CancellationToken()
        token.cancelar("antes de registrar")
        vistos = []

        token.agregar_callback(lambda t: vistos.append(t.id))

        assert vistos == [token.id]
        # No queda registrado: no habrá otra cancelación.
        assert token._callbacks == []

    def test_callback_sobre_token_completado_no_hace_nada(self):
        token = CancellationToken()
        token.completar()
        vistos = []

        token.agregar_callback(lambda t: vistos.append(t.id))

        assert vistos == []
        assert token._callbacks == []

    def test_callback_que_falla_en_token_cancelado_no_propaga(self):
        token = CancellationToken()
        token.cancelar("test")

        def malo(_):
            raise RuntimeError("boom")

        # No debe propagar la excepción al llamador.
        token.agregar_callback(malo)

    def test_eliminar_callback(self):
        token = CancellationToken()

        def cb(_):
            pass

        token.agregar_callback(cb)
        token.eliminar_callback(cb)
        assert token._callbacks == []

    def test_callback_que_falla_no_impide_los_demas(self):
        token = CancellationToken()
        vistos = []

        def malo(_):
            raise RuntimeError("boom")

        token.agregar_callback(malo)
        token.agregar_callback(lambda t: vistos.append(t.id))
        assert token.cancelar("test") is True
        assert vistos == [token.id]

    def test_metadata_generica(self):
        token = CancellationToken()
        assert token.obtener_metadata("x", "defecto") == "defecto"
        token.establecer_metadata("x", 1)
        assert token.obtener_metadata("x") == 1

    def test_repr(self):
        token = CancellationToken()
        assert "CancellationToken" in repr(token)


class TestCancellationManager:
    def test_crear_y_obtener(self):
        manager = CancellationManager()
        token = manager.crear_token({"agente_id": "A1"})
        assert manager.obtener_token(token.id) is token
        assert manager.obtener_token("no-existe") is None

    def test_crear_token_con_metadata(self):
        manager = CancellationManager()
        token = manager.crear_token({"agente_id": "A1", "extra": 2})
        assert token.obtener_metadata("agente_id") == "A1"
        assert token.obtener_metadata("extra") == 2

    def test_cancelar_token(self):
        manager = CancellationManager()
        token = manager.crear_token()
        assert manager.cancelar_token(token.id) is True
        assert token.esta_cancelado() is True
        assert manager.cancelar_token(token.id) is False
        assert manager.cancelar_token("no-existe") is False

    def test_cancelar_todos_solo_activos(self):
        manager = CancellationManager()
        t1 = manager.crear_token()
        t2 = manager.crear_token()
        t3 = manager.crear_token()
        manager.cancelar_token(t1.id)
        assert manager.cancelar_todos("masiva") == 2
        assert t2.esta_cancelado() and t3.esta_cancelado()
        # Una segunda pasada no cancela nada.
        assert manager.cancelar_todos() == 0

    def test_cancelar_por_agente(self):
        manager = CancellationManager()
        token = manager.crear_token({"agente_id": "A1"})
        manager.crear_token({"agente_id": "A2"})
        assert manager.cancelar_por_agente("A1") is True
        assert token.esta_cancelado() is True
        assert manager.cancelar_por_agente("A1") is False
        assert manager.cancelar_por_agente("inexistente") is False

    def test_eliminar_token(self):
        manager = CancellationManager()
        token = manager.crear_token()
        assert manager.eliminar_token(token.id) is True
        assert manager.eliminar_token(token.id) is False

    def test_limpiar_completados(self):
        manager = CancellationManager()
        activo = manager.crear_token()
        completado = manager.crear_token()
        completado.completar()
        cancelado = manager.crear_token()
        cancelado.cancelar()
        assert manager.limpiar_completados() == 2
        assert manager.obtener_token(activo.id) is activo

    def test_estadisticas(self):
        manager = CancellationManager()
        manager.crear_token()
        manager.crear_token().completar()
        manager.crear_token().cancelar()
        stats = manager.obtener_estadisticas()
        assert stats == {
            "total": 3,
            "activos": 1,
            "cancelados": 1,
            "completados": 1,
        }

    def test_callback_reentra_en_gestor_sin_deadlock(self):
        """Un callback que cancela otro token no debe bloquear el gestor.

        Regresión: antes ``cancelar_token`` ejecutaba los callbacks con el
        lock del gestor tomado, lo que podía provocar un deadlock.
        """
        manager = CancellationManager()
        t1 = manager.crear_token()
        t2 = manager.crear_token()
        t1.agregar_callback(lambda _: manager.cancelar_token(t2.id, "desde callback"))

        resultado = {}

        def ejecutar():
            resultado["ok"] = manager.cancelar_token(t1.id)

        hilo = threading.Thread(target=ejecutar, daemon=True)
        hilo.start()
        hilo.join(timeout=5)

        assert not hilo.is_alive(), "deadlock al ejecutar callback que reentra"
        assert resultado["ok"] is True
        assert t2.esta_cancelado() is True


class TestSingleton:
    def test_obtener_gestor_es_singleton(self):
        assert obtener_gestor_cancelacion() is obtener_gestor_cancelacion()


class TestCancelacionEfectivaEnEjecutores:
    """Verifica que la cancelación llega a matar el proceso de verdad.

    ``ShellExecutor`` no sondea el token: depende por completo del callback
    registrado con ``agregar_callback`` para interrumpir su
    ``communicate()``. Si el token ya estaba cancelado al registrar (ventana
    de carrera), antes el callback no se disparaba y el comando seguía
    corriendo hasta agotar el timeout.
    """

    def test_token_cancelado_tras_popen_mata_el_comando(self, monkeypatch):
        import subprocess
        import time

        from core.agent import Agente, TipoAgente
        from core.executors import shell_executor as se_mod
        from core.executors.shell_executor import ShellExecutor

        token = CancellationToken()
        popen_real = subprocess.Popen

        def popen_que_cancela(*args, **kwargs):
            proceso = popen_real(*args, **kwargs)
            # Simula la carrera exacta: la cancelación llega después de
            # crear el proceso pero antes de agregar_callback().
            token.cancelar("carrera")
            return proceso

        monkeypatch.setattr(se_mod.subprocess, "Popen", popen_que_cancela)

        agente = Agente(
            nombre="ShellCancelado",
            tipo=TipoAgente.SHELL,
            comando_shell="sleep 30",
            timeout_shell=30,
        )

        inicio = time.time()
        exito, mensaje, _ = ShellExecutor.ejecutar(
            agente, {}, cancellation_token=token
        )
        transcurrido = time.time() - inicio

        assert exito is False
        assert "cancelad" in mensaje.lower(), mensaje
        # Con el bug, communicate() esperaba los 30s del timeout.
        assert transcurrido < 5, (
            f"tardó {transcurrido:.1f}s en cancelar: el callback no mató el proceso"
        )
