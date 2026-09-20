# tests/test_jobs_api.py
"""API de trabajos no bloqueante (H9).

``POST /run`` sigue siendo bloqueante (compatibilidad); ``/api/jobs`` encola y
devuelve un ``job_id`` al instante. El trabajo real lo consume
``ColaTrabajos.ejecutar_pendientes``, que en producción llama al hilo de Qt:
en los tests se inyecta un ejecutor falso.
"""
import pytest

from core.log_bus import reset_bus_logs
from web.app import ColaTrabajos, create_app
from web.jobs import GestorTrabajos


@pytest.fixture
def entorno():
    reset_bus_logs()
    cola = ColaTrabajos()
    gestor = GestorTrabajos(cola)
    app = create_app(cola, version="test", gestor=gestor)
    cliente = app.test_client()
    yield cola, gestor, cliente
    reset_bus_logs()


def _ejecutor_ok(problema, **kwargs):
    return {
        "ok": True,
        "estado": "completada",
        "ejecucion_id": 42,
        "resultado": f"hecho: {problema}",
        "aceptacion": {"aceptada": True, "verificada": True, "motivos": []},
    }


# ============================================================
# GESTOR (unitario)
# ============================================================

def test_gestor_crear_y_consultar():
    cola = ColaTrabajos()
    gestor = GestorTrabajos(cola)

    resumen = gestor.crear(problema="p", max_pasos=3, timeout=None,
                           aprender=False, agente=None)
    job_id = resumen["job_id"]
    assert resumen["estado"] == "queued"
    assert gestor.obtener(job_id)["estado"] == "queued"
    assert gestor.obtener("no-existe") is None


def test_gestor_cancelar_encolado():
    cola = ColaTrabajos()
    gestor = GestorTrabajos(cola)
    job_id = gestor.crear(problema="p", max_pasos=3, timeout=None,
                          aprender=False, agente=None)["job_id"]

    encontrado, _ = gestor.cancelar(job_id)
    assert encontrado is True
    assert gestor.obtener(job_id)["estado"] == "cancelled"

    # El ejecutor no lo procesa.
    procesados = cola.ejecutar_pendientes(ejecutar=_ejecutor_ok)
    assert procesados == 0
    assert gestor.obtener(job_id)["estado"] == "cancelled"


def test_gestor_poda_trabajos_terminados():
    cola = ColaTrabajos()
    gestor = GestorTrabajos(cola, max_trabajos=3)
    ids = []
    for i in range(6):
        ids.append(gestor.crear(problema=f"p{i}", max_pasos=1, timeout=None,
                                aprender=False, agente=None)["job_id"])
    cola.ejecutar_pendientes(ejecutar=_ejecutor_ok)

    # Un trabajo nuevo dispara la poda de los terminados más antiguos.
    ultimo = gestor.crear(problema="nuevo", max_pasos=1, timeout=None,
                          aprender=False, agente=None)["job_id"]

    assert gestor.obtener(ultimo) is not None
    assert gestor.obtener(ids[0]) is None
    assert len(gestor.listar(limite=100)) <= 3


# ============================================================
# API HTTP
# ============================================================

def test_post_jobs_devuelve_job_id_sin_bloquear(entorno):
    cola, gestor, cliente = entorno

    respuesta = cliente.post("/api/jobs", json={"prompt": "haz algo"})

    assert respuesta.status_code == 202
    datos = respuesta.get_json()
    assert datos["ok"] is True
    assert datos["job_id"]
    assert datos["estado"] == "queued"


def test_post_jobs_valida_el_cuerpo(entorno):
    _, _, cliente = entorno
    assert cliente.post("/api/jobs", json={}).status_code == 400
    assert cliente.post("/api/jobs", data="no json",
                        content_type="text/plain").status_code == 400


def test_flujo_queued_a_completed(entorno):
    cola, gestor, cliente = entorno
    job_id = cliente.post("/api/jobs", json={"prompt": "haz algo"}).get_json()["job_id"]

    assert cliente.get(f"/api/jobs/{job_id}").get_json()["estado"] == "queued"

    cola.ejecutar_pendientes(ejecutar=_ejecutor_ok)

    datos = cliente.get(f"/api/jobs/{job_id}").get_json()
    assert datos["estado"] == "completed"
    assert datos["resultado_ok"] is True
    assert datos["aceptada"] is True
    assert datos["ejecucion_id"] == 42


def test_flujo_fallido(entorno):
    cola, gestor, cliente = entorno

    def _ejecutor_fallido(problema, **kwargs):
        return {"ok": False, "estado": "fallida",
                "aceptacion": {"aceptada": False, "motivos": ["sin imagen"]}}

    job_id = cliente.post("/api/jobs", json={"prompt": "haz algo"}).get_json()["job_id"]
    cola.ejecutar_pendientes(ejecutar=_ejecutor_fallido)

    datos = cliente.get(f"/api/jobs/{job_id}").get_json()
    assert datos["estado"] == "failed"
    assert datos["resultado_ok"] is False
    assert datos["aceptada"] is False
    assert "sin imagen" in datos["motivos"]


def test_job_inexistente_404(entorno):
    _, _, cliente = entorno
    assert cliente.get("/api/jobs/nope").status_code == 404
    assert cliente.post("/api/jobs/nope/cancel").status_code == 404
    assert cliente.get("/api/jobs/nope/logs").status_code == 404


def test_logs_del_job_por_sse(entorno):
    import logging

    from core.log_bus import instalar_handler

    cola, gestor, cliente = entorno
    instalar_handler(nivel=logging.INFO)

    job_id = cliente.post("/api/jobs", json={"prompt": "haz algo"}).get_json()["job_id"]
    logging.getLogger("test.jobs").info("log del trabajo en curso")
    # El stream SSE termina cuando el job es terminal.
    cola.ejecutar_pendientes(ejecutar=_ejecutor_ok)

    respuesta = cliente.get(f"/api/jobs/{job_id}/logs?cursor=0", buffered=True)
    assert respuesta.status_code == 200
    assert respuesta.mimetype == "text/event-stream"

    cuerpo = respuesta.get_data(as_text=True)
    assert "log del trabajo en curso" in cuerpo
    assert "event: fin" in cuerpo


def test_cancel_por_http(entorno):
    _, _, cliente = entorno
    job_id = cliente.post("/api/jobs", json={"prompt": "haz algo"}).get_json()["job_id"]

    respuesta = cliente.post(f"/api/jobs/{job_id}/cancel")
    assert respuesta.status_code == 200
    assert respuesta.get_json()["estado"] == "cancelled"


def test_listar_jobs(entorno):
    _, _, cliente = entorno
    cliente.post("/api/jobs", json={"prompt": "uno"})
    cliente.post("/api/jobs", json={"prompt": "dos"})

    datos = cliente.get("/api/jobs").get_json()
    assert datos["ok"] is True
    assert len(datos["jobs"]) == 2


def test_run_sigue_funcionando(entorno):
    """Compatibilidad: POST /api/run sigue bloqueando y devolviendo la salida."""
    import threading
    import time

    cola, gestor, cliente = entorno
    resultado = {}

    def _cliente():
        resultado["respuesta"] = cliente.post("/api/run", json={"prompt": "haz algo"})

    hilo = threading.Thread(target=_cliente, daemon=True)
    hilo.start()

    # Consumir la cola hasta que el cliente reciba su respuesta.
    inicio = time.time()
    while hilo.is_alive() and (time.time() - inicio) < 5:
        cola.ejecutar_pendientes(ejecutar=_ejecutor_ok)
        time.sleep(0.01)
    hilo.join(timeout=5)

    respuesta = resultado["respuesta"]
    assert respuesta.status_code == 200
    assert respuesta.get_json()["ok"] is True
