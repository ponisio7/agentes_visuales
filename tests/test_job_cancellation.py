# tests/test_job_cancellation.py
"""Cancelación real de trabajos (V3.8-3).

``/api/jobs/<id>/cancel`` solo marcaba la petición: el pipeline en marcha
seguía hasta el final. Ahora el hilo HTTP deja una *solicitud* en un registro
thread-safe y el hilo de Qt la convierte en ``Scheduler.detener()``. Aquí se
prueba el registro, el cableado con ``GestorTrabajos``/``ColaTrabajos`` y el
endpoint HTTP, sin Qt ni red.
"""
import pytest

from core.job_cancellation import (
    JobCancellationRegistry,
    obtener_registro_cancelacion,
    reset_registro_cancelacion,
)
from core.log_bus import reset_bus_logs
from web.app import ColaTrabajos, create_app
from web.jobs import GestorTrabajos


@pytest.fixture(autouse=True)
def _registro_limpio():
    reset_registro_cancelacion()
    reset_bus_logs()
    yield
    reset_registro_cancelacion()
    reset_bus_logs()


def _ejecutor_ok(problema, **kwargs):
    return {"ok": True, "estado": "completada", "resultado": f"hecho: {problema}"}


# ============================================================
# Registro (unitario)
# ============================================================

def test_solicitar_y_consumir():
    registro = JobCancellationRegistry()
    assert registro.solicitar("job1") is False, "sin cancelador registrado"
    assert registro.hay_solicitud("job1") is True

    razon = registro.consumir("job1")
    assert razon
    assert registro.hay_solicitud("job1") is False
    assert registro.fue_cancelado("job1") is True
    # Consumir dos veces no inventa una segunda cancelación.
    assert registro.consumir("job1") is None


def test_registrar_y_cancelar_ahora():
    registro = JobCancellationRegistry()
    razones = []
    registro.registrar("job1", razones.append)

    assert registro.esta_registrado("job1") is True
    assert registro.cancelar_ahora("job1", "porque sí") is True
    assert razones == ["porque sí"]
    assert registro.fue_cancelado("job1") is True


def test_cancelar_ahora_sin_cancelador_no_rompe():
    registro = JobCancellationRegistry()
    assert registro.cancelar_ahora("fantasma") is False


def test_solicitar_marca_si_hay_algo_que_cancelar():
    registro = JobCancellationRegistry()
    registro.registrar("job1", lambda razon: None)
    assert registro.solicitar("job1") is True


def test_limpiar_borra_todo_el_estado():
    registro = JobCancellationRegistry()
    registro.registrar("job1", lambda razon: None)
    registro.solicitar("job1")

    registro.limpiar("job1")

    assert registro.esta_registrado("job1") is False
    assert registro.hay_solicitud("job1") is False
    assert registro.fue_cancelado("job1") is False
    assert registro.estadisticas()["registrados"] == 0


def test_singleton_es_el_mismo_objeto():
    primero = obtener_registro_cancelacion()
    assert primero is obtener_registro_cancelacion()

    reset_registro_cancelacion()

    assert obtener_registro_cancelacion() is not primero


# ============================================================
# GestorTrabajos: la cancelación en ejecución es real
# ============================================================

def test_cancelar_job_encolado_sigue_igual():
    cola = ColaTrabajos()
    gestor = GestorTrabajos(cola)
    job_id = gestor.crear(problema="p", max_pasos=1, timeout=None,
                          aprender=False, agente=None)["job_id"]

    encontrado, mensaje = gestor.cancelar(job_id)

    assert encontrado is True
    assert "antes de ejecutarse" in mensaje
    assert gestor.obtener(job_id)["estado"] == "cancelled"


def test_cancelar_job_en_ejecucion_deja_solicitud_real():
    cola = ColaTrabajos()
    gestor = GestorTrabajos(cola)
    job_id = gestor.crear(problema="p", max_pasos=1, timeout=None,
                          aprender=False, agente=None)["job_id"]

    # El pipeline registró el job al empezar a ejecutarlo.
    registro = obtener_registro_cancelacion()
    registro.registrar(job_id, lambda razon: None)
    gestor._obtener_crudo(job_id)["estado"] = "running"

    encontrado, mensaje = gestor.cancelar(job_id)

    assert encontrado is True
    assert "detendrá en breve" in mensaje
    assert registro.hay_solicitud(job_id) is True


def test_cancelar_job_en_ejecucion_sin_registrar_avisa():
    cola = ColaTrabajos()
    gestor = GestorTrabajos(cola)
    job_id = gestor.crear(problema="p", max_pasos=1, timeout=None,
                          aprender=False, agente=None)["job_id"]
    gestor._obtener_crudo(job_id)["estado"] = "running"

    _, mensaje = gestor.cancelar(job_id)

    assert "terminará su ejecución actual" in mensaje


# ============================================================
# ColaTrabajos
# ============================================================

def test_ejecutar_pendientes_pasa_el_job_id():
    cola = ColaTrabajos()
    gestor = GestorTrabajos(cola)
    job_id = gestor.crear(problema="p", max_pasos=1, timeout=None,
                          aprender=False, agente=None)["job_id"]
    visto = {}

    def _ejecutor(problema, **kwargs):
        visto.update(kwargs)
        return {"ok": True}

    cola.ejecutar_pendientes(ejecutar=_ejecutor)

    assert visto.get("job_id") == job_id


def test_cancelacion_durante_la_ejecucion_marca_cancelled():
    cola = ColaTrabajos()
    gestor = GestorTrabajos(cola)
    job_id = gestor.crear(problema="p", max_pasos=1, timeout=None,
                          aprender=False, agente=None)["job_id"]

    def _ejecutor_cancelado(problema, **kwargs):
        # La petición llega mientras el pipeline corre.
        gestor.cancelar(kwargs.get("job_id") or job_id)
        return {"ok": True, "estado": "completada"}

    cola.ejecutar_pendientes(ejecutar=_ejecutor_cancelado)

    resumen = gestor.obtener(job_id)
    assert resumen["estado"] == "cancelled"
    assert resumen["cancelado"] is True


# ============================================================
# Endpoint HTTP
# ============================================================

def test_cancel_http_de_job_en_ejecucion():
    reset_registro_cancelacion()
    cola = ColaTrabajos()
    gestor = GestorTrabajos(cola)
    app = create_app(cola, version="test", gestor=gestor)
    cliente = app.test_client()

    job_id = cliente.post("/api/jobs", json={"prompt": "haz algo"}).get_json()["job_id"]
    obtener_registro_cancelacion().registrar(job_id, lambda razon: None)
    gestor._obtener_crudo(job_id)["estado"] = "running"

    respuesta = cliente.post(f"/api/jobs/{job_id}/cancel")

    assert respuesta.status_code == 200
    datos = respuesta.get_json()
    assert datos["ok"] is True
    assert "detendrá en breve" in datos["mensaje"]
    assert obtener_registro_cancelacion().hay_solicitud(job_id) is True
