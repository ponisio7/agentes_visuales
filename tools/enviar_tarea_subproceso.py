#!/usr/bin/env python3
# tools/enviar_tarea_subproceso.py
"""
Ejemplo: llamar a Agentes Visuales desde OTRA aplicación Python lanzándolo
como SUBPROCESO.

No necesita servidor ni importar el proyecto: ejecuta
``python main.py run --json`` y lee el JSON de stdout.

Contrato de la CLI (``python main.py run --help``):
  · stdout -> SOLO el JSON del resultado (con ``--json``).
  · stderr -> los logs (con ``--quiet`` solo errores).
  · exit code -> 0 OK | 2 argumentos | 3 entorno | 4 error de ejecución
                 | 130 abortado por el usuario.

⚠️ TRAMPA DEL cwd (importante):
  El proceso escribe los artefactos Y la base de datos
  (``agent_history.db``, ruta relativa) respecto a SU directorio de trabajo.
  Por eso:
    - ``cwd=<raíz del proyecto>`` -> artefactos en la raíz y BD real.
    - ``cwd=<otra carpeta>``     -> artefactos ahí, pero la BD sería OTRA
      (se crearía una nueva) salvo que uses aprendizaje desactivado.
  ``aprender=False`` (``--no-aprender``) no toca la BD... y también desactiva
  el Plan B (ver ``main._ejecutar_pipeline``: el recovery se inyecta
  ``if aprender``).

  ⚠️ OJO, comprobado en real: incluso con ``--no-aprender``, al generar el plan
  se inicializa el ``LearningEngine`` (para inyectar lecciones) y **crea/abre
  ``agent_history.db`` y ``learning_models/`` en el cwd**. Si no quieres una BD
  nueva, lanza el subproceso con ``cwd=<raíz del proyecto>``.

Uso:
    python tools/enviar_tarea_subproceso.py "di hola" "suma 2+2"
    python tools/enviar_tarea_subproceso.py --aprender "genera un CSV"
    python tools/enviar_tarea_subproceso.py --ver-logs "di hola"
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
from pathlib import Path

# ---------------------------------------------------------------------------
# Rutas de la app (este fichero vive en <proyecto>/tools/)
# ---------------------------------------------------------------------------
APP_DIR = Path(__file__).resolve().parent.parent
MAIN = APP_DIR / "main.py"
PYTHON = APP_DIR / ".venv" / "bin" / "python"
if not PYTHON.exists():          # fuera del venv del proyecto
    PYTHON = Path(sys.executable)

# Margen del guardia del subproceso sobre el timeout interno de la ejecución.
MARGEN_PROCESO = 120


def ejecutar_tarea(
    prompt: str,
    *,
    cwd: Path,
    max_pasos: int = 6,
    timeout: int = 300,
    aprender: bool = False,
    agente: str | None = None,
    salida_json: Path | None = None,
) -> dict:
    """Lanza una tarea en un subproceso y devuelve el resultado parseado.

    Returns:
        dict con ``ok``, ``returncode``, ``salida`` (el JSON de la app cuando
        se pudo parsear), ``stdout`` y ``stderr``.
    """
    cmd = [
        str(PYTHON), str(MAIN), "run",
        "--prompt", prompt,
        "--json",
        "--max-pasos", str(max_pasos),
        "--timeout", str(timeout),
    ]
    if not aprender:
        cmd.append("--no-aprender")
    if agente:
        cmd += ["--agent", agente]
    if salida_json:
        cmd += ["--output", str(salida_json)]

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout + MARGEN_PROCESO,
            check=False,          # el exit code se interpreta a mano
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "returncode": None,
            "error": f"el subproceso superó {timeout + MARGEN_PROCESO}s",
            "salida": {},
            "stdout": "",
            "stderr": "",
        }

    salida: dict = {}
    aviso = ""
    if proc.stdout.strip():
        try:
            salida = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            aviso = f"stdout no era JSON válido: {e}"
    else:
        aviso = "stdout vacío"

    return {
        # La app devuelve "ok": true solo si TODOS los agentes completaron.
        "ok": bool(salida.get("ok")),
        "returncode": proc.returncode,
        "salida": salida,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "aviso": aviso,
    }


def ejecutar_tarea_json(tarea: dict, *, cwd: Path, timeout: int = 300) -> dict:
    """Variante con tarea JSON por stdin (permite pasar max_pasos/timeout/etc.)."""
    cmd = [str(PYTHON), str(MAIN), "run", "--stdin", "--json"]
    proc = subprocess.run(
        cmd,
        cwd=str(cwd),
        input=json.dumps(tarea, ensure_ascii=False),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout + MARGEN_PROCESO,
        check=False,
    )
    try:
        salida = json.loads(proc.stdout) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        salida = {}
    return {"ok": bool(salida.get("ok")), "returncode": proc.returncode,
            "salida": salida, "stdout": proc.stdout, "stderr": proc.stderr}


def ejecutar_con_logs_en_vivo(prompt: str, *, cwd: Path, timeout: int = 300) -> dict:
    """Igual que ``ejecutar_tarea`` pero muestra los logs (stderr) en vivo.

    Se leen los dos flujos en hilos separados para no bloquear ninguno
    (si solo se leyera uno, el otro podría llenar el buffer y colgar el
    proceso).
    """
    cmd = [
        str(PYTHON), str(MAIN), "run", "--prompt", prompt,
        "--json", "--no-aprender", "--timeout", str(timeout),
    ]
    proc = subprocess.Popen(
        cmd, cwd=str(cwd),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8",
    )

    trozos: list[str] = []
    errores: list[str] = []

    def _leer(stream, destino, prefijo=None):
        for linea in stream:
            destino.append(linea)
            if prefijo:
                print(f"{prefijo}{linea.rstrip()}", file=sys.stderr, flush=True)

    h_out = threading.Thread(target=_leer, args=(proc.stdout, trozos), daemon=True)
    h_err = threading.Thread(target=_leer, args=(proc.stderr, errores, "  │ "), daemon=True)
    h_out.start()
    h_err.start()
    proc.wait(timeout=timeout + MARGEN_PROCESO)
    h_out.join(timeout=5)
    h_err.join(timeout=5)

    stdout = "".join(trozos)
    try:
        salida = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        salida = {}
    return {"ok": bool(salida.get("ok")), "returncode": proc.returncode,
            "salida": salida, "stderr": "".join(errores)}


def procesar_lista(tareas: list[str], *, cwd: Path, aprender: bool,
                   max_pasos: int, timeout: int, ver_logs: bool) -> int:
    """Envía las tareas EN SERIE: no manda la siguiente hasta que la anterior OK."""
    for i, tarea in enumerate(tareas, 1):
        print(f"\n▶ [{i}/{len(tareas)}] {tarea}", flush=True)

        if ver_logs:
            res = ejecutar_con_logs_en_vivo(tarea, cwd=cwd, timeout=timeout)
        else:
            res = ejecutar_tarea(
                tarea, cwd=cwd, max_pasos=max_pasos,
                timeout=timeout, aprender=aprender,
            )

        salida = res.get("salida") or {}
        if not res["ok"]:
            detalle = salida.get("error") or [
                a for a in salida.get("agentes", []) if not a.get("ok")
            ] or res.get("aviso") or res.get("stderr", "")[-300:]
            print(f"  ❌ falló (exit={res['returncode']}): {detalle}")
            print("  ⏹ se detiene la lista (no se envía la siguiente)")
            return 1

        print(f"  ✅ OK en {salida.get('duracion')}s | "
              f"plan={salida.get('titulo')!r} | "
              f"ejecucion_id={salida.get('ejecucion_id')}")
        print(f"     resultado: {str(salida.get('resultado', ''))[:160]}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tareas", nargs="*",
                        help="Tareas a ejecutar en serie (por defecto, una de demo).")
    parser.add_argument("--cwd", type=Path, default=APP_DIR,
                        help="Directorio de trabajo del subproceso: aquí caen los "
                             "artefactos (y la BD si aprender=True).")
    parser.add_argument("--aprender", action="store_true",
                        help="Guarda en agent_history.db y activa el Plan B.")
    parser.add_argument("--max-pasos", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=300,
                        help="Timeout de la ejecución (segundos).")
    parser.add_argument("--ver-logs", action="store_true",
                        help="Muestra los logs del subproceso en vivo (stderr).")
    args = parser.parse_args()

    tareas = args.tareas or ["di hola"]
    args.cwd.mkdir(parents=True, exist_ok=True)
    print(f"Intérprete : {PYTHON}")
    print(f"App        : {MAIN}")
    print(f"cwd tareas : {args.cwd}")
    print(f"aprender   : {args.aprender}")
    return procesar_lista(
        tareas, cwd=args.cwd, aprender=args.aprender,
        max_pasos=args.max_pasos, timeout=args.timeout, ver_logs=args.ver_logs,
    )


if __name__ == "__main__":
    sys.exit(main())
