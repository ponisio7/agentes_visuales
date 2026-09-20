#!/usr/bin/env python3
"""
Punto de entrada de Agentes Visuales.

Uso interactivo:
    agentes_visuales                         # Arranca la GUI
    agentes_visuales --check-env             # Diagnóstico de configuración de IA
    agentes_visuales --version

Uso desde otras apps (headless, sin GUI):
    agentes_visuales run --prompt "resume esto"
    agentes_visuales run --file tarea.json
    cat tarea.json | agentes_visuales run --stdin
    agentes_visuales list-agents
    agentes_visuales serve --port 8765       # servidor HTTP (POST /run)

Contrato del modo ``run``:
    - stdout: SOLO el resultado (texto, o JSON con ``--json``)
    - stderr: logs, avisos y errores
    - exit code: ver las constantes EXIT_* de abajo
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Rutas y valores por defecto del modo headless.
DB_PATH_POR_DEFECTO = "agent_history.db"
MAX_CONCURRENT_DEFAULT = 4
MAX_PASOS_DEFAULT = 6
TIMEOUT_CHECK_ENV_DEFAULT = 5.0


# ---------------------------------------------------------------------------
# Versión
# ---------------------------------------------------------------------------
def _leer_version() -> str:
    """Lee la versión desde ``pyproject.toml`` (única fuente de verdad).

    Antes había tres versiones distintas conviviendo: ``__version__`` valía
    "1.1.0", el tag era v3.0.1 y el CHANGELOG hablaba de v3.0. Ahora
    ``[project].version`` en pyproject.toml manda, y si no se puede leer
    (p. ej. despliegue sin el fichero) se degrada a un valor explícito.
    """
    try:
        import tomllib

        with open(Path(__file__).resolve().parent / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except Exception:
        return "0.0.0+sin-pyproject"


__version__ = _leer_version()

# Exit codes documentados (útiles para otras apps)
EXIT_OK = 0
EXIT_GUI_ERROR = 1
EXIT_BAD_ARGS = 2
EXIT_ENV_ERROR = 3
EXIT_RUN_ERROR = 4
EXIT_USER_ABORT = 130


class _TareaInvalida(Exception):
    """La tarea de entrada (``--prompt``/``--file``/``--stdin``) no es válida."""


# ---------------------------------------------------------------------------
# Utilidades argparse
# ---------------------------------------------------------------------------
def _timeout_positivo(valor: str) -> float:
    """Convierte ``--timeout`` a float exigiendo un valor > 0."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"se esperaba un número, no {valor!r}") from None
    if numero <= 0:
        raise argparse.ArgumentTypeError("el timeout debe ser mayor que 0")
    return numero


def _entero_positivo(valor: str) -> int:
    """Convierte ``--max-pasos`` a int exigiendo un valor > 0."""
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"se esperaba un entero, no {valor!r}") from None
    if numero <= 0:
        raise argparse.ArgumentTypeError("debe ser mayor que 0")
    return numero


def _entero_o_defecto(valor: Any, defecto: int) -> int:
    """Coacciona un valor de la tarea JSON a entero > 0, con fallback."""
    try:
        numero = int(valor)
    except (TypeError, ValueError):
        return defecto
    return numero if numero > 0 else defecto


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------
def _construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentes_visuales",
        description="Visualizador y orquestador de agentes con IA.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  agentes_visuales                          # Arranca la GUI\n"
            "  agentes_visuales --check-env              # Diagnóstico rápido\n"
            "  agentes_visuales run --prompt 'hola'      # Ejecución headless\n"
            "  agentes_visuales run --file tarea.json    # Desde fichero\n"
            "  cat tarea.json | agentes_visuales run --stdin\n"
            "  agentes_visuales list-agents\n"
            "  agentes_visuales --version\n"
        ),
    )

    # Flags globales (retrocompatibles)
    parser.add_argument(
        "--check-env",
        action="store_true",
        help="Diagnostica la configuración de la API key y la conectividad.",
    )
    # default=None (y no 5.0) para que el subcomando `run` no herede el
    # timeout corto del ping HTTP. `--check-env` aplica 5.0 si no se indica.
    parser.add_argument(
        "--timeout",
        type=_timeout_positivo,
        default=None,
        metavar="SEGUNDOS",
        help=(
            "Timeout por defecto en segundos. "
            f"check-env: {TIMEOUT_CHECK_ENV_DEFAULT} si no se indica. "
            "run: sin límite si no se indica."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"agentes-visuales {__version__}"
    )

    # Subcomandos
    sub = parser.add_subparsers(dest="comando", metavar="COMANDO")

    # --- run -----------------------------------------------------------------
    p_run = sub.add_parser(
        "run",
        help="Ejecuta una tarea en modo headless (sin GUI).",
        description=(
            "Genera un plan con el LLM (ProblemSolver) y lo ejecuta con el "
            "Scheduler, sin abrir la GUI."
        ),
    )
    p_run.add_argument("--prompt", "-p", type=str, default=None,
                       help="Prompt o instrucción a ejecutar.")
    p_run.add_argument("--file", "-f", type=Path, default=None,
                       help="Fichero JSON con la tarea.")
    p_run.add_argument("--stdin", action="store_true",
                       help="Lee la tarea (JSON) desde stdin.")
    p_run.add_argument("--agent", "-a", type=str, default=None,
                       help="Ejecuta solo los agentes del plan con este nombre "
                            "(por defecto: todos).")
    p_run.add_argument("--max-pasos", type=_entero_positivo, default=argparse.SUPPRESS,
                       metavar="N",
                       help=f"Máximo de pasos del plan (por defecto: {MAX_PASOS_DEFAULT}).")
    p_run.add_argument("--output", "-o", type=Path, default=None,
                       help="Guarda el resultado completo (JSON) en este fichero.")
    p_run.add_argument("--json", action="store_true",
                       help="Salida en JSON (recomendado para integración).")
    p_run.add_argument("--quiet", "-q", action="store_true",
                       help="Silencia logs en stderr (solo errores).")
    p_run.add_argument("--no-aprender", action="store_true",
                       help="No guarda la ejecución en aprendizaje ni activa el "
                            "Plan B (no toca agent_history.db).")
    # default=SUPPRESS: si no se pasa, NO pisa el --timeout global.
    # Con un default normal, el subparser sobrescribía el valor global.
    p_run.add_argument("--timeout", type=_timeout_positivo, default=argparse.SUPPRESS,
                       help="Timeout de la ejecución en segundos "
                            "(por defecto: sin límite).")

    # --- list-agents ---------------------------------------------------------
    p_list = sub.add_parser("list-agents", help="Lista los tipos de agente disponibles.")
    p_list.add_argument("--json", action="store_true", help="Salida JSON.")

    # --- serve ---------------------------------------------------------------
    p_serve = sub.add_parser("serve", help="Servidor HTTP para otras apps (POST /run).")
    p_serve.add_argument("--host", default="127.0.0.1",
                         help="Interfaz de escucha (por defecto: 127.0.0.1).")
    p_serve.add_argument("--port", type=int, default=8765,
                         help="Puerto de escucha (por defecto: 8765).")

    return parser


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def _configurar_logging(quiet: bool = False):
    """Configura el logging raíz (consola + fichero rotativo).

    Se usa ``force=True`` para que ``basicConfig`` no sea un no-op cuando ya
    existen handlers (p. ej. si la app se reconfigura): de lo contrario el
    nivel INFO y el handler de consola no se aplicaban.
    """
    try:
        os.makedirs("logs", exist_ok=True)
    except OSError as e:
        print(f"⚠️  No se pudo crear el directorio de logs: {e}", file=sys.stderr)

    nivel_consola = logging.ERROR if quiet else logging.INFO
    logging.basicConfig(
        level=nivel_consola,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )
    try:
        fh = RotatingFileHandler(
            "logs/agentes_visuales.log",
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setLevel(logging.INFO)
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        ))
        logging.getLogger().addHandler(fh)
    except OSError as e:
        print(f"⚠️  No se pudo abrir el log de fichero: {e}", file=sys.stderr)

    for ruidoso in ("urllib3", "openai", "matplotlib", "PIL",
                    "httpx", "httpx2", "httpcore", "charset_normalizer"):
        logging.getLogger(ruidoso).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# --check-env
# ---------------------------------------------------------------------------
def _ejecutar_check_env(timeout: float | None) -> int:
    try:
        from core.env_checker import ejecutar_check_env
    except ImportError as e:
        print(f"❌ No se pudo cargar el módulo de diagnóstico: {e}", file=sys.stderr)
        return EXIT_ENV_ERROR
    try:
        return ejecutar_check_env(
            timeout=timeout if timeout is not None else TIMEOUT_CHECK_ENV_DEFAULT
        )
    except KeyboardInterrupt:
        print("\n⏹  Diagnóstico interrumpido por el usuario.")
        return EXIT_USER_ABORT
    except Exception as e:
        print(f"❌ Error inesperado en --check-env: {e}", file=sys.stderr)
        return EXIT_ENV_ERROR


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------
def _arrancar_gui() -> int:
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("❌ PyQt6 no está instalado.\n   Instálalo con: pip install PyQt6", file=sys.stderr)
        return EXIT_GUI_ERROR

    try:
        from ui.simple_main_window import SimpleMainWindow
    except ImportError as e:
        print(f"❌ No se pudo cargar la GUI: {e}", file=sys.stderr)
        return EXIT_GUI_ERROR

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("Agentes Visuales")
    app.setApplicationVersion(__version__)

    try:
        window = SimpleMainWindow()
    except Exception as e:
        print(f"❌ Error inicializando la ventana principal: {e}", file=sys.stderr)
        return EXIT_GUI_ERROR
    window.show()
    return app.exec()


# ---------------------------------------------------------------------------
# Modo headless: utilidades comunes
# ---------------------------------------------------------------------------
def _listar_agentes() -> list[dict[str, Any]]:
    """Catálogo real de tipos de agente, desde ``core.agent``.

    Antes se intentaba importar ``core.agents_registry`` (que no existe) y se
    caía a un glob de ``core/agents/`` (que tampoco existe), devolviendo una
    lista vacía con ``ok: true``: un fallo silencioso.
    """
    from core.agent import TipoAgente, obtener_config_tipo

    catalogo: list[dict[str, Any]] = []
    for tipo in TipoAgente:
        config = obtener_config_tipo(tipo) or {}
        catalogo.append({
            "nombre": tipo.value,
            "categoria": config.get("categoria") or TipoAgente.categoria(tipo),
            "descripcion": config.get("descripcion") or "",
            "campos_requeridos": list(config.get("campos_requeridos") or []),
        })
    catalogo.sort(key=lambda a: (a["categoria"], a["nombre"]))
    return catalogo


def _parsear_tarea(crudo: str, origen: str) -> dict[str, Any]:
    try:
        tarea = json.loads(crudo)
    except json.JSONDecodeError as e:
        raise _TareaInvalida(f"JSON inválido en {origen}: {e}") from e
    if not isinstance(tarea, dict):
        raise _TareaInvalida(f"el JSON de {origen} debe ser un objeto")
    return tarea


def _cargar_tarea(args) -> dict[str, Any]:
    """Construye el dict de tarea desde ``--stdin``, ``--file`` o ``--prompt``.

    Orden de prioridad: stdin > fichero > prompt.
    """
    if args.stdin:
        try:
            crudo = sys.stdin.read()
        except OSError as e:
            raise _TareaInvalida(f"no se pudo leer stdin: {e}") from e
        if not crudo.strip():
            raise _TareaInvalida("stdin vacío: se esperaba un objeto JSON")
        return _parsear_tarea(crudo, "stdin")

    if args.file:
        try:
            crudo = args.file.read_text(encoding="utf-8")
        except OSError as e:
            raise _TareaInvalida(f"no se pudo leer {args.file}: {e}") from e
        return _parsear_tarea(crudo, str(args.file))

    if args.prompt:
        return {"prompt": args.prompt}

    raise _TareaInvalida(
        "debes indicar --prompt, --file o --stdin.\n"
        "   Ejemplo: agentes_visuales run --prompt 'hola'"
    )


def _json_limpio(valor: Any) -> Any:
    """Devuelve una copia serializable a JSON (los objetos raros van a str)."""
    try:
        json.dumps(valor)
        return valor
    except (TypeError, ValueError):
        return json.loads(json.dumps(valor, ensure_ascii=False, default=str))


def _extraer_texto(resultado: Any) -> str:
    """Extrae el texto imprimible del resultado de un agente.

    Sin listas de claves conocidas: si el resultado es un dict con un único
    valor de texto se usa ese; en cualquier otro caso se vuelca el JSON.
    """
    if isinstance(resultado, str):
        return resultado
    if isinstance(resultado, dict) and len(resultado) == 1:
        unico = next(iter(resultado.values()))
        if isinstance(unico, str) and unico.strip():
            return unico
    return json.dumps(resultado, ensure_ascii=False, indent=2, default=str)


_QT_APP = None


def _asegurar_qt():
    """Devuelve una ``QCoreApplication`` (creándola si hace falta).

    El Scheduler es un ``QObject`` y entrega ``ejecucion_terminada`` por
    ``QueuedConnection``: sin un bucle de eventos Qt la señal nunca llega.
    Solo se necesita QtCore, así que no hace falta display.

    La referencia se guarda en ``_QT_APP``: si el único dueño es una variable
    local, el recolector de basura destruye la aplicación y ``QEventLoop``
    falla con "Cannot be used without QCoreApplication".
    """
    global _QT_APP
    from PyQt6.QtCore import QCoreApplication

    app = QCoreApplication.instance()
    if app is None:
        app = QCoreApplication([sys.argv[0] if sys.argv else "agentes_visuales"])
    _QT_APP = app
    return app


# ---------------------------------------------------------------------------
# Modo headless: pipeline real (ProblemSolver -> Scheduler)
# ---------------------------------------------------------------------------
def _ejecutar_pipeline(
    problema: str,
    *,
    max_pasos: int = MAX_PASOS_DEFAULT,
    timeout: float | None = None,
    aprender: bool = True,
    agente: str | None = None,
    log: logging.Logger | None = None,
) -> dict[str, Any]:
    """Genera el plan y lo ejecuta sin GUI.

    Devuelve un dict serializable con el plan, el estado de cada agente, el
    texto del último resultado correcto y metadatos de la ejecución.
    """
    from PyQt6.QtCore import QEventLoop, QTimer

    from core.agent import EstadoAgente
    from core.llm_client import obtener_llm_client_compartido
    from core.problem_solver import ProblemSolver
    from core.scheduler import Scheduler

    log = log or logger
    _asegurar_qt()

    if timeout is not None:
        try:
            timeout = float(timeout)
        except (TypeError, ValueError):
            timeout = None

    llm = obtener_llm_client_compartido()
    if llm is None or not getattr(llm, "disponible", False):
        raise RuntimeError(
            "LLM no disponible: configura DEEPSEEK_API_KEY (usa --check-env para diagnosticar)"
        )

    solver = ProblemSolver(llm)
    log.info("Generando plan para: %s", problema[:100])
    plan = solver.resolver_problema(problema, max_pasos=max_pasos)

    agentes = list(plan.agentes_generados)
    if agente:
        agentes = [a for a in agentes if getattr(a, "nombre", None) == agente]
        if not agentes:
            raise RuntimeError(f"el plan no tiene ningún agente llamado '{agente}'")
    if not agentes:
        raise RuntimeError("el plan no generó agentes")

    scheduler = Scheduler(max_concurrent=MAX_CONCURRENT_DEFAULT)
    scheduler.agregar_agentes(agentes)
    scheduler.resolver_dependencias()

    if aprender:
        # Plan B (recuperación de fallos críticos), igual que en la GUI.
        try:
            from core.plan_recovery import PlanRecovery

            scheduler.set_contexto_plan_b(
                recovery=PlanRecovery(
                    llm_client=llm,
                    problem_solver=solver,
                    db_path=DB_PATH_POR_DEFECTO,
                ),
                problema_original=problema,
                plan_original=plan,
            )
        except Exception as e:
            log.debug("Plan B no disponible: %s", e)

    inicio = time.time()
    bucle = QEventLoop()
    estado = {"terminada": False}

    def _al_terminar():
        estado["terminada"] = True
        bucle.quit()

    scheduler.ejecucion_terminada.connect(_al_terminar)

    # Latido: obliga al intérprete a ejecutar código cada 200 ms para que
    # Ctrl-C (SIGINT) no quede bloqueado dentro del bucle de eventos de Qt.
    latido = QTimer()
    latido.timeout.connect(lambda: None)
    latido.start(200)

    temporizador = None
    if timeout is not None:
        temporizador = QTimer()
        temporizador.setSingleShot(True)
        temporizador.timeout.connect(bucle.quit)
        temporizador.start(max(1, int(timeout * 1000)))

    log.info("Ejecutando %d agentes (max_concurrent=%d)", len(agentes), MAX_CONCURRENT_DEFAULT)
    scheduler.iniciar()

    try:
        if not estado["terminada"]:
            if scheduler.esta_ejecutando():
                bucle.exec()
            else:
                # iniciar() abortó en la validación previa (ciclos o LOOP inválido)
                raise RuntimeError("la ejecución no pudo arrancar (revisa los logs)")
    except KeyboardInterrupt:
        log.warning("Interrumpido por el usuario: deteniendo la ejecución")
        scheduler.detener()
        raise
    finally:
        latido.stop()
        if temporizador is not None:
            temporizador.stop()

    agotado = not estado["terminada"]
    if agotado:
        log.warning("Timeout de %ss agotado: deteniendo la ejecución", timeout)
        scheduler.detener()
    duracion = time.time() - inicio

    salida_agentes: list[dict[str, Any]] = []
    for i, a in enumerate(agentes):
        estado_agente = getattr(a, "estado", None)
        tipo = getattr(a, "tipo", None)
        salida_agentes.append({
            "nombre": getattr(a, "nombre", f"agente_{i + 1}"),
            "tipo": getattr(tipo, "value", str(tipo)),
            "estado": getattr(estado_agente, "value", str(estado_agente)),
            "ok": estado_agente == EstadoAgente.COMPLETADO,
            "error": getattr(a, "error", "") or "",
            "duracion": round(float(getattr(a, "duracion", 0.0) or 0.0), 2),
            "resultado": _json_limpio(getattr(a, "resultado", None)),
        })

    completados = [a for a in salida_agentes if a["ok"]]
    correcto = bool(completados) and len(completados) == len(salida_agentes)

    ejecucion_id = None
    if aprender and estado["terminada"]:
        try:
            from core.execution_recorder import registrar_ejecucion_en_aprendizaje
            from storage.database import Database

            ejecucion_id = registrar_ejecucion_en_aprendizaje(
                scheduler=scheduler,
                db=Database(),
                plan=plan,
                problema=problema,
                duracion_total=duracion,
            )
        except Exception as e:
            log.warning("No se pudo registrar la ejecución en aprendizaje: %s", e)

    return {
        "ok": correcto,
        "problema": problema,
        "titulo": plan.titulo,
        "plan_id": plan.id,
        "pasos": [
            {"orden": p.orden, "nombre": p.nombre, "tipo_agente": p.tipo_agente}
            for p in plan.pasos
        ],
        "agentes": _json_limpio(salida_agentes),
        "resultado": _extraer_texto(completados[-1]["resultado"]) if completados else "",
        "duracion": round(duracion, 2),
        "timeout_agotado": agotado,
        "ejecucion_id": ejecucion_id,
        "advertencias": list(getattr(plan, "advertencias", []) or []),
    }


# ---------------------------------------------------------------------------
# Modo headless: run
# ---------------------------------------------------------------------------
def _ejecutar_run(args) -> int:
    log = logging.getLogger("run")

    try:
        tarea = _cargar_tarea(args)
    except _TareaInvalida as e:
        print(f"❌ {e}", file=sys.stderr)
        return EXIT_BAD_ARGS

    problema = str(tarea.get("prompt") or tarea.get("problema") or "").strip()
    if not problema:
        print("❌ la tarea no incluye 'prompt'", file=sys.stderr)
        return EXIT_BAD_ARGS

    timeout = args.timeout
    if timeout is None and tarea.get("timeout") is not None:
        try:
            timeout = float(tarea["timeout"])
        except (TypeError, ValueError):
            print("❌ 'timeout' de la tarea no es un número", file=sys.stderr)
            return EXIT_BAD_ARGS

    # La CLI gana sobre la tarea: --max-pasos usa SUPPRESS, así que solo está
    # presente si el usuario lo pasó.
    max_pasos = getattr(args, "max_pasos", None)
    if max_pasos is None:
        max_pasos = _entero_o_defecto(tarea.get("max_pasos"), MAX_PASOS_DEFAULT)

    try:
        salida = _ejecutar_pipeline(
            problema,
            max_pasos=max_pasos,
            timeout=timeout,
            aprender=bool(tarea.get("aprender", True)) and not args.no_aprender,
            agente=args.agent or tarea.get("agent") or None,
            log=log,
        )
    except KeyboardInterrupt:
        print("\n⏹  Ejecución interrumpida por el usuario.", file=sys.stderr)
        return EXIT_USER_ABORT
    except Exception as e:
        log.exception("Error ejecutando la tarea")
        if args.json:
            print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        else:
            print(f"❌ {e}", file=sys.stderr)
        return EXIT_RUN_ERROR

    if args.output:
        try:
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(salida, f, ensure_ascii=False, indent=2)
        except OSError as e:
            print(f"⚠️  No se pudo escribir {args.output}: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(salida, ensure_ascii=False))
    else:
        for advertencia in salida.get("advertencias", []):
            print(f"⚠  {advertencia}", file=sys.stderr)
        if salida.get("resultado"):
            print(salida["resultado"])
        for a in salida.get("agentes", []):
            if not a.get("ok"):
                detalle = a.get("error") or a.get("estado")
                print(f"❌ [{a.get('tipo')}] {a.get('nombre')}: {detalle}", file=sys.stderr)

    return EXIT_OK if salida.get("ok") else EXIT_RUN_ERROR


# ---------------------------------------------------------------------------
# Modo headless: list-agents
# ---------------------------------------------------------------------------
def _ejecutar_list_agents(args) -> int:
    try:
        agentes = _listar_agentes()
    except ImportError as e:
        print(f"❌ No se pudo cargar el catálogo de agentes: {e}", file=sys.stderr)
        return EXIT_RUN_ERROR

    if not agentes:
        print("❌ El catálogo de agentes está vacío", file=sys.stderr)
        return EXIT_RUN_ERROR

    if args.json:
        print(json.dumps({"ok": True, "agentes": agentes}, ensure_ascii=False))
        return EXIT_OK

    categoria_actual = None
    for a in agentes:
        if a["categoria"] != categoria_actual:
            categoria_actual = a["categoria"]
            print(f"\n{categoria_actual}:")
        print(f"  {a['nombre']:<10} {a['descripcion']}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Modo headless: serve
# ---------------------------------------------------------------------------
def _ejecutar_serve(args) -> int:
    """Servidor HTTP mínimo: ``POST /run`` ejecuta el pipeline headless.

    El servidor HTTP vive en un hilo secundario y encola los trabajos; el
    hilo principal (el único con bucle de eventos Qt, que es lo que el
    Scheduler necesita) los consume con un ``QTimer`` y deja el resultado en
    el propio trabajo.
    """
    import queue
    import signal
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from PyQt6.QtCore import QTimer

    app = _asegurar_qt()
    trabajos: queue.Queue = queue.Queue()

    def _procesar_trabajos():
        while True:
            try:
                trabajo = trabajos.get_nowait()
            except queue.Empty:
                break
            try:
                trabajo["resultado"] = _ejecutar_pipeline(
                    trabajo["problema"],
                    max_pasos=trabajo["max_pasos"],
                    timeout=trabajo["timeout"],
                    aprender=trabajo["aprender"],
                    agente=trabajo["agente"],
                    log=logging.getLogger("serve"),
                )
            except Exception as e:
                logger.exception("Error ejecutando un trabajo de /run")
                trabajo["error"] = str(e)
            finally:
                trabajo["evento"].set()
        QTimer.singleShot(25, _procesar_trabajos)

    class Handler(BaseHTTPRequestHandler):
        server_version = f"agentes-visuales/{__version__}"

        def _responder(self, codigo: int, payload: dict):
            cuerpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)

        def do_GET(self):  # noqa: N802 (nombre impuesto por http.server)
            if self.path.rstrip("/") in ("", "/health"):
                self._responder(200, {"ok": True, "version": __version__})
            else:
                self._responder(404, {"ok": False, "error": "ruta no encontrada; usa POST /run"})

        def do_POST(self):  # noqa: N802 (nombre impuesto por http.server)
            if self.path.rstrip("/") != "/run":
                self._responder(404, {"ok": False, "error": "ruta no encontrada; usa POST /run"})
                return

            try:
                largo = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                largo = 0
            crudo = self.rfile.read(largo) if largo > 0 else b""
            try:
                tarea = json.loads(crudo or b"{}")
            except json.JSONDecodeError as e:
                self._responder(400, {"ok": False, "error": f"JSON inválido: {e}"})
                return
            if not isinstance(tarea, dict):
                self._responder(400, {"ok": False, "error": "el cuerpo debe ser un objeto JSON"})
                return

            problema = str(tarea.get("prompt") or tarea.get("problema") or "").strip()
            if not problema:
                self._responder(400, {"ok": False, "error": "falta 'prompt'"})
                return

            trabajo = {
                "problema": problema,
                "max_pasos": _entero_o_defecto(tarea.get("max_pasos"), MAX_PASOS_DEFAULT),
                "timeout": tarea.get("timeout"),
                "aprender": bool(tarea.get("aprender", True)),
                "agente": tarea.get("agent"),
                "evento": threading.Event(),
                "resultado": None,
                "error": None,
            }
            trabajos.put(trabajo)

            if not trabajo["evento"].wait(timeout=args.timeout):
                self._responder(504, {"ok": False, "error": "timeout esperando la ejecución"})
                return
            if trabajo["error"]:
                self._responder(500, {"ok": False, "error": trabajo["error"]})
                return

            salida = trabajo["resultado"] or {}
            self._responder(200 if salida.get("ok") else 500, salida)

        def log_message(self, *a):  # silencio: los logs van por logging
            pass

    try:
        servidor = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as e:
        print(f"❌ No se pudo abrir {args.host}:{args.port}: {e}", file=sys.stderr)
        return EXIT_RUN_ERROR

    hilo = threading.Thread(target=servidor.serve_forever, name="http-agentes", daemon=True)
    hilo.start()
    print(
        f"🌐 Sirviendo en http://{args.host}:{args.port}/run (Ctrl-C para parar)",
        file=sys.stderr,
    )
    QTimer.singleShot(0, _procesar_trabajos)

    # Ctrl-C: el bucle de Qt bloquea al intérprete, así que se instala un
    # manejador de SIGINT que cierra la aplicación, más un latido que deja
    # al intérprete ejecutarlo.
    parada = {"manual": False}

    def _parar(*_):
        parada["manual"] = True
        app.quit()

    sigint_anterior = signal.signal(signal.SIGINT, _parar)
    latido = QTimer()
    latido.timeout.connect(lambda: None)
    latido.start(200)

    try:
        codigo = app.exec()
        if parada["manual"]:
            print("\n⏹  Servidor detenido.", file=sys.stderr)
            return EXIT_USER_ABORT
        return codigo
    finally:
        latido.stop()
        signal.signal(signal.SIGINT, sigint_anterior)
        servidor.shutdown()
        servidor.server_close()
        hilo.join(timeout=5)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    parser = _construir_parser()
    args = parser.parse_args()

    # Logging: silencioso si --quiet en run
    quiet = bool(getattr(args, "quiet", False))
    _configurar_logging(quiet=quiet)

    # Retrocompatibilidad: --check-env sin subcomando
    if args.check_env:
        return _ejecutar_check_env(timeout=args.timeout)

    # Subcomandos
    if args.comando == "run":
        return _ejecutar_run(args)
    if args.comando == "list-agents":
        return _ejecutar_list_agents(args)
    if args.comando == "serve":
        return _ejecutar_serve(args)

    # Sin subcomando: GUI (comportamiento original)
    return _arrancar_gui()


if __name__ == "__main__":
    sys.exit(main())
