# web/app.py
"""
Aplicación web (Flask) de Agentes Visuales.

Expone el mismo pipeline que la CLI/GUI por HTTP:

| Método | Ruta          | Descripción                                |
|--------|---------------|--------------------------------------------|
| GET    | ``/``         | Formulario HTML (prompt, max_pasos, etc.)  |
| POST   | ``/api/run``  | Ejecuta el pipeline y devuelve el JSON     |
| GET    | ``/api/health`` | ``{ok, version}``                        |
| GET    | ``/api/agents`` | Catálogo (mismo formato que la CLI)      |
| GET    | ``/api/logs``   | Logs en vivo del bus unificado (H4)      |

Integración Qt-safe: Flask corre en un hilo secundario y solo **encola**
trabajos en :class:`ColaTrabajos`; el hilo principal de Qt los ejecuta desde
un ``QTimer`` (ver ``main._ejecutar_web``) porque el ``Scheduler`` es un
``QObject`` y necesita el bucle de eventos del hilo principal. Cada trabajo
lleva un ``threading.Event`` para que el request HTTP espere el resultado sin
bloquear a Qt.
"""
from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

PLANTILLA_INDEX = "index.html"


def importar_main():
    """Devuelve el módulo ``main.py`` aunque se ejecute como ``__main__``.

    Al lanzar ``python main.py`` el módulo vive en ``sys.modules['__main__']``
    y no en ``sys.modules['main']``. Hacer ``import main`` en ese caso
    crearía una SEGUNDA copia del módulo (con su propio ``_QT_APP`` y su
    propio cliente LLM) y rompería el singleton de Qt. Se busca primero el
    módulo que realmente expone los helpers de la CLI.
    """
    import sys

    for nombre in ("main", "__main__"):
        modulo = sys.modules.get(nombre)
        if modulo is not None and hasattr(modulo, "_ejecutar_pipeline"):
            return modulo
    import main as modulo  # caso normal: main importado, no ejecutado

    return modulo


class ColaTrabajos:
    """Cola compartida entre el hilo de Flask y el hilo principal de Qt.

    Flask (hilos de werkzeug) llama a :meth:`encolar` y espera con
    :meth:`esperar`; el hilo Qt consume con :meth:`ejecutar_pendientes` desde
    un ``QTimer``. La cola y los ``threading.Event`` son thread-safe, así que
    no hace falta ningún lock adicional.
    """

    def __init__(self) -> None:
        self._cola: queue.Queue[dict[str, Any]] = queue.Queue()

    def encolar(
        self,
        *,
        problema: str,
        max_pasos: int,
        timeout: float | None,
        aprender: bool,
        agente: str | None,
    ) -> dict[str, Any]:
        """Encola un trabajo y devuelve su registro (con el ``Event``)."""
        trabajo: dict[str, Any] = {
            "problema": problema,
            "max_pasos": max_pasos,
            "timeout": timeout,
            "aprender": aprender,
            "agente": agente,
            "evento": threading.Event(),
            "resultado": None,
            "error": None,
        }
        self._cola.put(trabajo)
        return trabajo

    def pendiente(self) -> dict[str, Any] | None:
        """Saca un trabajo de la cola sin bloquear (``None`` si está vacía)."""
        try:
            return self._cola.get_nowait()
        except queue.Empty:
            return None

    @staticmethod
    def esperar(trabajo: dict[str, Any], timeout: float | None = None) -> bool:
        """Espera a que el hilo Qt termine el trabajo.

        ``timeout`` es el ``--timeout`` global de ``main.py web``: limita
        cuánto espera el cliente HTTP. Devuelve False si se agotó.
        """
        return bool(trabajo["evento"].wait(timeout=timeout))

    def ejecutar_pendientes(
        self,
        ejecutar: Callable[..., dict[str, Any]] | None = None,
        log: logging.Logger | None = None,
    ) -> int:
        """Ejecuta todos los trabajos encolados y devuelve cuántos procesó.

        ``ejecutar`` es ``main._ejecutar_pipeline`` (se inyecta para poder
        testear el módulo sin depender de ``main``); si no se pasa, se toma
        del módulo ``main`` real.
        """
        if ejecutar is None:
            ejecutar = importar_main()._ejecutar_pipeline
        log = log or logger

        procesados = 0
        while True:
            trabajo = self.pendiente()
            if trabajo is None:
                break
            procesados += 1
            try:
                trabajo["resultado"] = ejecutar(
                    trabajo["problema"],
                    max_pasos=trabajo["max_pasos"],
                    timeout=trabajo["timeout"],
                    aprender=trabajo["aprender"],
                    agente=trabajo["agente"],
                    log=log,
                )
            except Exception as e:
                log.exception("Error ejecutando un trabajo de /api/run")
                trabajo["error"] = str(e)
            finally:
                trabajo["evento"].set()
        return procesados


def create_app(
    cola: ColaTrabajos,
    *,
    timeout: float | None = None,
    max_pasos_defecto: int = 6,
    version: str = "",
):
    """Crea la ``Flask`` app con el Blueprint ``api``.

    Args:
        cola: cola compartida con el hilo principal de Qt.
        timeout: ``--timeout`` global; limita la espera del cliente HTTP.
        max_pasos_defecto: valor por defecto de ``max_pasos``.
        version: versión del proyecto (``main.__version__``) para ``/api/health``.
    """
    from flask import Blueprint, Flask, jsonify, render_template, request

    api = Blueprint("api", __name__)

    @api.get("/health")
    def health():
        """Estado del servidor: ``{ok, version}``."""
        return jsonify({"ok": True, "version": version})

    @api.get("/agents")
    def agents():
        """Catálogo de agentes: mismo formato que ``list-agents --json``."""
        try:
            # main._listar_agentes() es la única fuente del catálogo.
            catalogo = importar_main()._listar_agentes()
        except ImportError as e:
            return jsonify({
                "ok": False,
                "error": f"No se pudo cargar el catálogo de agentes: {e}",
            }), 500
        return jsonify({"ok": True, "agentes": catalogo})

    @api.get("/logs")
    def logs():
        """Logs en vivo del bus unificado (H4).

        Query: ``?cursor=N&limit=M``. Devuelve las entradas nuevas y el
        cursor para la siguiente consulta. No bloquea al scheduler: solo lee
        un buffer acotado en memoria.
        """
        try:
            from core.log_bus import obtener_bus_logs

            try:
                cursor = int(request.args.get("cursor", 0))
            except (TypeError, ValueError):
                cursor = 0
            try:
                limite = int(request.args.get("limit", 200))
            except (TypeError, ValueError):
                limite = 200

            entradas, nuevo_cursor = obtener_bus_logs().desde(cursor, limite)
            return jsonify({
                "ok": True,
                "entradas": [e.to_dict() for e in entradas],
                "cursor": nuevo_cursor,
            })
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @api.post("/run")
    def run():
        """Ejecuta el pipeline con el mismo JSON de tarea que ``run --stdin``."""
        tarea = request.get_json(silent=True)
        if not isinstance(tarea, dict):
            return jsonify({
                "ok": False,
                "error": "el cuerpo debe ser un objeto JSON",
            }), 400

        problema = str(tarea.get("prompt") or tarea.get("problema") or "").strip()
        if not problema:
            return jsonify({"ok": False, "error": "falta 'prompt'"}), 400

        main_mod = importar_main()
        trabajo = cola.encolar(
            problema=problema,
            max_pasos=main_mod._entero_o_defecto(
                tarea.get("max_pasos"), max_pasos_defecto
            ),
            timeout=tarea.get("timeout"),
            aprender=bool(tarea.get("aprender", True)),
            agente=tarea.get("agent"),
        )

        # El QTimer del hilo principal consume la cola; aquí solo se espera.
        if not ColaTrabajos.esperar(trabajo, timeout):
            return jsonify({
                "ok": False,
                "error": "timeout esperando la ejecución",
            }), 504
        if trabajo["error"]:
            return jsonify({"ok": False, "error": trabajo["error"]}), 500

        salida = trabajo["resultado"] or {}
        return jsonify(salida), (200 if salida.get("ok") else 500)

    app = Flask(__name__)
    app.register_blueprint(api, url_prefix="/api")
    # Los acentos y emojis del resultado deben salir tal cual en el JSON.
    app.json.ensure_ascii = False

    @app.get("/")
    def index():
        """Formulario HTML mínimo para lanzar tareas a mano."""
        return render_template(
            PLANTILLA_INDEX,
            version=version,
            max_pasos=max_pasos_defecto,
            timeout=timeout,
        )

    return app
