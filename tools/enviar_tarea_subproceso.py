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
        # La app devuelve "ok": true solo si TODOS los agentes completaron
        # Y la aceptación de la salida pasó (H6).
        "ok": bool(salida.get("ok")),
        "returncode": proc.returncode,
        "salida": salida,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "aviso": aviso,
    }


def resumen_verificacion(salida: dict) -> dict:
    """Extrae el veredicto de aceptación y los artefactos del JSON de la app.

    La validación depende del VerificationEngine (H6): ``ok`` ya no significa
    «los agentes terminaron», sino «el artefacto cumple el contrato». Aquí se
    resume para el informe por tarea.
    """
    aceptacion = salida.get("aceptacion") or {}
    artefactos: list[str] = []
    criterios_fallidos: list[str] = []
    for paso in aceptacion.get("pasos") or []:
        for criterio in paso.get("criterios_comprobados") or []:
            if isinstance(criterio, str) and criterio.startswith(
                ("archivo:", "imagen:", "directorio:")
            ):
                artefactos.append(criterio.split(":", 1)[1])
        criterios_fallidos.extend(paso.get("criterios_fallidos") or [])

    return {
        "verificada": bool(aceptacion.get("verificada")),
        "aceptada": aceptacion.get("aceptada"),
        "motivos": list(aceptacion.get("motivos") or []),
        "criterios_fallidos": criterios_fallidos,
        "artefactos": sorted(set(artefactos)),
    }


def resumen_tarea(res: dict) -> dict:
    """Informe estructurado de una tarea: estado, errores, artefactos, verificación."""
    salida = res.get("salida") or {}
    agentes = salida.get("agentes") or []
    errores = [
        {
            "nombre": a.get("nombre"),
            "estado": a.get("estado"),
            "error": (a.get("error") or "")[:300],
        }
        for a in agentes
        if not a.get("ok")
    ]
    return {
        "ok": bool(res.get("ok")),
        "returncode": res.get("returncode"),
        "estado": salida.get("estado") or ("fallida" if res.get("returncode") else ""),
        "duracion": salida.get("duracion"),
        "ejecucion_id": salida.get("ejecucion_id"),
        "titulo": salida.get("titulo"),
        "resultado": str(salida.get("resultado") or "")[:300],
        "errores": errores,
        "verificacion": resumen_verificacion(salida),
        "aviso": res.get("aviso", ""),
    }


def _imprimir_informe(informe: dict) -> None:
    ver = informe["verificacion"]
    print(
        f"     estado={informe['estado']!r} | "
        f"duración={informe['duracion']}s | "
        f"ejecución_id={informe['ejecucion_id']} | "
        f"plan={informe['titulo']!r}"
    )
    if ver["verificada"]:
        artefactos = ", ".join(ver["artefactos"]) or "(sin artefactos declarados)"
        estado_ver = "aceptada" if ver["aceptada"] else "rechazada"
        print(f"     verificación: {estado_ver} | artefactos: {artefactos}")
    else:
        print("     verificación: no verificable (sin contrato declarado)")
    if ver["motivos"]:
        for motivo in ver["motivos"][:5]:
            print(f"       ✗ {motivo}")
    if informe["errores"]:
        for err in informe["errores"][:5]:
            print(f"       ✗ [{err['estado']}] {err['nombre']}: {err['error']}")
    if informe["resultado"]:
        print(f"     resultado: {informe['resultado'][:160]}")


def procesar_lista(
    tareas: list[str],
    *,
    cwd: Path,
    aprender: bool,
    max_pasos: int,
    timeout: int,
    ver_logs: bool,
    continue_on_error: bool = False,
) -> int:
    """Envía las tareas EN SERIE: no manda la siguiente hasta que la anterior OK.

    Cada tarea se valida con el VerificationEngine de la app (``ok`` exige
    aceptación). Con ``continue_on_error`` se sigue con la siguiente tarea
    aunque una falle; sin él, la lista se detiene en el primer fallo.
    Devuelve 0 si TODAS pasaron, 1 si alguna falló.
    """
    fallos = 0
    for i, tarea in enumerate(tareas, 1):
        print(f"\n▶ [{i}/{len(tareas)}] {tarea}", flush=True)

        if ver_logs:
            res = ejecutar_con_logs_en_vivo(tarea, cwd=cwd, timeout=timeout)
        else:
            res = ejecutar_tarea(
                tarea, cwd=cwd, max_pasos=max_pasos,
                timeout=timeout, aprender=aprender,
            )

        informe = resumen_tarea(res)

        if not informe["ok"]:
            fallos += 1
            detalle = (
                informe["verificacion"]["motivos"]
                or informe["errores"]
                or (res.get("salida") or {}).get("error")
                or informe["aviso"]
                or (res.get("stderr") or "")[-300:]
            )
            print(f"  ❌ falló (exit={informe['returncode']}): {detalle}")
            _imprimir_informe(informe)
            if continue_on_error and i < len(tareas):
                print("  ⏭ --continue-on-error: se pasa a la siguiente tarea")
                continue
            if not continue_on_error:
                print("  ⏹ se detiene la lista (usa --continue-on-error para seguir)")
            return 1

        print(f"  ✅ OK en {informe['duracion']}s")
        _imprimir_informe(informe)

    if fallos:
        print(f"\n⚠ {fallos} tarea(s) fallaron de {len(tareas)}")
        return 1
    print(f"\n✅ {len(tareas)} tarea(s) completadas y verificadas")
    return 0


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
    parser.add_argument("--continue-on-error", action="store_true",
                        help="No detener la lista si una tarea falla la verificación.")
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
        continue_on_error=args.continue_on_error,
    )


if __name__ == "__main__":
    sys.exit(main())
