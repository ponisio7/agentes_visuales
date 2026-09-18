# run_all_tests.py
#!/usr/bin/env python3
"""
Ejecutor de pruebas con streaming en vivo.

Uso:
    python run_all_tests.py                    # Pruebas rápidas (sin GUI)
    python run_all_tests.py --include-gui      # Todas, incluyendo GUI
    python run_all_tests.py --verbose          # Más detalle (pytest -vv)
    python run_all_tests.py --watchdog 30      # Avisa si un test tarda > 30s
"""
import sys
import os
import subprocess
import importlib.util
import threading
import time
import argparse
from pathlib import Path


# ============================================================
# COLORES ANSI (con fallback si no hay TTY)
# ============================================================
_USE_COLORS = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

def _c(texto: str, codigo: str) -> str:
    if not _USE_COLORS:
        return texto
    return f"\033[{codigo}m{texto}\033[0m"

def verde(t):    return _c(t, "32")
def rojo(t):     return _c(t, "31")
def amarillo(t): return _c(t, "33")
def azul(t):     return _c(t, "34")
def cyan(t):     return _c(t, "36")
def gris(t):     return _c(t, "90")
def negrita(t):  return _c(t, "1")


# ============================================================
# VERIFICACIÓN DE DEPENDENCIAS
# ============================================================
def verificar_dependencias() -> bool:
    dependencias = [
        'pytest', 'PyQt6', 'matplotlib', 'requests',
        'openai', 'plyer', 'numpy', 'pandas',
    ]
    faltantes = [d for d in dependencias if importlib.util.find_spec(d) is None]
    if faltantes:
        print(rojo(f"❌ Faltan dependencias: {', '.join(faltantes)}"))
        print(f"   Instalar con: pip install {' '.join(faltantes)}")
        return False
    return True


# ============================================================
# WATCHDOG: avisa si un test tarda demasiado
# ============================================================
class Watchdog:
    """
    Hilo que imprime el tiempo transcurrido cada N segundos y avisa
    si la última línea no ha cambiado en mucho tiempo.
    """
    def __init__(self, intervalo: int = 15, umbral_alerta: int = 45):
        self.intervalo = intervalo
        self.umbral_alerta = umbral_alerta
        self.ultima_linea = ""
        self.ultima_actividad = time.time()
        self.inicio = time.time()
        self._stop = threading.Event()
        self._thread = None

    def registrar_linea(self, linea: str):
        """Llamado desde el hilo lector cuando llega una nueva línea."""
        self.ultima_linea = linea.strip()
        self.ultima_actividad = time.time()

    def _run(self):
        while not self._stop.wait(self.intervalo):
            elapsed_total = time.time() - self.inicio
            elapsed_silencio = time.time() - self.ultima_actividad
            mm, ss = divmod(int(elapsed_total), 60)
            print(
                gris(f"   ⏱ {mm:02d}:{ss:02d} transcurridos | "
                     f"último: {self.ultima_linea[:80] or '(iniciando)'}")
            )
            if elapsed_silencio > self.umbral_alerta:
                print(amarillo(
                    f"   ⚠️  Sin actividad desde {int(elapsed_silencio)}s — "
                    f"posible bloqueo en el test en curso"
                ))

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)


# ============================================================
# GRUPOS DE TESTS
# ============================================================
#
# Los tests se dividen en 3 grupos que se ejecutan en PROCESOS
# SEPARADOS. Motivo: Python 3.13 tiene un segfault conocido cuando
# un proceso carga PyQt6 + NumPy + Pandas (66 extension modules) y
# luego hace fork() para subprocess (sandbox/scheduler).
#
# Ejecutar cada grupo en su propio pytest evita que las extensiones
# nativas del grupo A contaminen al grupo B.
#
GRUPOS_TESTS = {
    "A_ui_datos": [
        # Cargan PyQt6 + matplotlib + pandas + numpy + sklearn
        "tests/test_config_manager.py",
        "tests/test_exporters.py",
        "tests/test_ab_sintetico.py",
    ],
    "B_scheduler": [
        # Scheduler + subprocess/ThreadPoolExecutor + base de datos
        "tests/test_scheduler.py",
        "tests/test_scheduler_orden.py",
        "tests/test_scheduler_ciclos.py",
        "tests/test_scheduler_resolucion.py",
        "tests/test_ejecucion_individual.py",
    ],
    "C_integration": [
        # Flujo completo (scheduler + sandbox + DB): aislado para no mezclar
        # con otros grupos que cargan extensiones nativas pesadas.
        "tests/test_integration.py",
    ],
    "D_sandbox": [
        # Sandbox (subprocess)
        "tests/test_sandbox.py",
        "tests/test_sandbox_smoke.py",
    ],
    "E_loops": [
        # Agentes Loop (scheduler + sandbox)
        "tests/test_loop_safety.py",
    ],
    "F_ligeros": [
        # Modelos, mocks y validadores sin extensiones nativas pesadas
        "tests/test_agent.py",
        "tests/test_conexion_a_DeepSeek_manualmente.py",
        "tests/test_contrato_cuento.py",
        "tests/test_contrato_salida.py",
        "tests/test_plan_recovery.py",
        "tests/test_plan_validator.py",
        "tests/test_validador.py",
    ],
}

# Señales que indican que el intérprete murió (no que un test falló). En
# CPython 3.13 + PyQt6 + extensiones nativas + fork se observa un SIGSEGV
# esporádico; para esos casos el runner reintenta una vez el grupo.
SENALES_CAIDA_INTERPRETE = {-11, -6, -4, -8}  # SIGSEGV, SIGABRT, SIGILL, SIGFPE


def descubrir_grupos_completos() -> dict[str, list[str]]:
    """Devuelve los grupos asegurando que ningún ``test_*.py`` quede fuera.

    Los tests que no aparezcan en ``GRUPOS_TESTS`` se agrupan en
    ``Z_sin_clasificar`` para que el runner nunca los ignore en silencio.
    """
    asignados = {path for paths in GRUPOS_TESTS.values() for path in paths}
    tests_dir = Path(__file__).resolve().parent / "tests"
    faltantes = sorted(
        f"tests/{archivo.name}"
        for archivo in tests_dir.glob("test_*.py")
        if f"tests/{archivo.name}" not in asignados
    )

    grupos = {nombre: list(paths) for nombre, paths in GRUPOS_TESTS.items()}
    if faltantes:
        grupos.setdefault("Z_sin_clasificar", []).extend(faltantes)
    return grupos


def ejecutar_comando_pytest(
    cmd: list[str],
    env: dict,
    f_out,
    intervalo_watchdog: int,
    umbral_alerta: int,
) -> tuple[int, float, int, bool]:
    """Ejecuta pytest en un subproceso con streaming y watchdog.

    Returns:
        (returncode, segundos, lineas, interrumpido_por_usuario)
    """
    t0 = time.time()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    wd = Watchdog(intervalo=intervalo_watchdog, umbral_alerta=umbral_alerta)
    wd.start()

    lineas = 0
    interrumpido = False
    try:
        for linea in proc.stdout:
            lineas += 1
            f_out.write(linea)
            f_out.flush()
            sys.stdout.write(_colorear_linea(linea))
            sys.stdout.flush()
            wd.registrar_linea(linea)
        proc.wait()
    except KeyboardInterrupt:
        interrumpido = True
        print()
        print(amarillo("⏹  Interrumpido. Terminando pytest..."))
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    finally:
        wd.stop()

    return proc.returncode, time.time() - t0, lineas, interrumpido


# ============================================================
# RUNNER PRINCIPAL CON STREAMING
# ============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Ejecuta las pruebas del proyecto")
    parser.add_argument("--include-gui", action="store_true",
                        help="Incluye pruebas marcadas como 'gui' y 'slow'")
    parser.add_argument("--verbose", "-vv", action="store_true",
                        help="Pasa -vv a pytest (más detalle)")
    parser.add_argument("--watchdog", type=int, default=15,
                        help="Intervalo del watchdog en segundos (default: 15)")
    parser.add_argument("--watchdog-alerta", type=int, default=45,
                        help="Segundos sin actividad antes de avisar (default: 45)")
    parser.add_argument("--single-process", action="store_true",
                        help="Ejecutar toda la suite en un solo pytest "
                             "(útil para depurar, PERO puede segfaultear en py3.13)")
    parser.add_argument("--no-retry", action="store_true",
                        help="No reintentar grupos que mueran por señal "
                             "(SIGSEGV/SIGABRT) del intérprete")
    args = parser.parse_args()

    print("=" * 70)
    print(negrita(cyan("🧪 EJECUTANDO PRUEBAS UNITARIAS - Agentes Visuales")))
    print("=" * 70)

    if not verificar_dependencias():
        return 1

    # Nos movemos al directorio del script
    script_dir = Path(__file__).resolve().parent
    os.chdir(script_dir)

    # Log completo
    log_dir = script_dir / "logs"
    log_dir.mkdir(exist_ok=True)
    log_file = log_dir / "pytest_full_output.log"

    # Entorno común
    env = os.environ.copy()
    env["MPLBACKEND"] = "Agg"
    env["PYTHONUNBUFFERED"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"

    print()
    print(gris(f"📄 Log completo: {log_file}"))
    print(gris(f"🖥️  QT_QPA_PLATFORM=offscreen  MPLBACKEND=Agg"))
    if args.single_process:
        print(amarillo("⚠️  Modo single-process: toda la suite en un pytest"))
    else:
        print(gris("🔀 Modo multi-proceso: grupos aislados "
                   "(evita segfault py3.13 + Qt + fork)"))
    print()

    # Determinar qué grupos ejecutar
    if args.single_process:
        grupos_a_ejecutar = {"ALL": None}   # None = todo tests/
    else:
        grupos_a_ejecutar = descubrir_grupos_completos()

    if not args.include_gui:
        print(azul("ℹ️  Modo rápido: excluyendo pruebas GUI/slow."))
        print(azul("   Usa --include-gui para ejecutarlas."))
    else:
        print(amarillo("⚠️  Incluyendo TODAS las pruebas (GUI y slow)."))

    # ── Preparar comando base ──
    def _cmd_pytest(paths: list[str]) -> list[str]:
        cmd = [sys.executable, "-m", "pytest", "--color=no", "-ra", "--maxfail=1000"]
        cmd += paths
        if args.verbose:
            cmd.append("-vv")
        if not args.include_gui:
            cmd += ["-m", "not gui and not slow"]
        return cmd

    # ── Ejecutar cada grupo ──
    start_time_global = time.time()
    resultados: dict[str, tuple[int, float, int]] = {}   # grupo -> (rc, elapsed, lineas)
    lineas_totales = 0

    with open(log_file, "w", encoding="utf-8") as f_out:
        for nombre_grupo, paths in grupos_a_ejecutar.items():
            print()
            print(cyan("═" * 70))
            if nombre_grupo == "ALL":
                print(cyan(negrita("▶️  GRUPO ÚNICO: tests/")))
            else:
                print(cyan(negrita(f"▶️  GRUPO {nombre_grupo}  ({len(paths)} archivos)")))
                for p in paths:
                    print(cyan(f"     • {p}"))
            print(cyan("═" * 70))

            cmd = _cmd_pytest(["tests/"] if nombre_grupo == "ALL" else paths)

            f_out.write("\n" + "=" * 80 + "\n")
            f_out.write(f"GRUPO: {nombre_grupo}\n")
            f_out.write(f"CMD:   {' '.join(cmd)}\n")
            f_out.write("=" * 80 + "\n\n")
            f_out.flush()

            rc, elapsed_grupo, lineas_grupo, interrumpido = ejecutar_comando_pytest(
                cmd, env, f_out, args.watchdog, args.watchdog_alerta
            )
            lineas_totales += lineas_grupo

            # Reintento acotado ante muerte del intérprete por señal. No se
            # reintentan fallos de tests (rc positivo) ni interrupciones.
            if (rc in SENALES_CAIDA_INTERPRETE and not interrumpido
                    and not args.no_retry):
                print(amarillo(
                    f"\n   ⚠️  Grupo {nombre_grupo} murió por señal {rc}. "
                    f"Reintentando una vez (flake conocido CPython 3.13 + Qt)..."
                ))
                f_out.write(f"\n[REINTENTO tras señal {rc}]\n")
                f_out.flush()
                rc, elapsed_grupo, lineas_grupo, interrumpido = ejecutar_comando_pytest(
                    cmd, env, f_out, args.watchdog, args.watchdog_alerta
                )
                lineas_totales += lineas_grupo

            resultados[nombre_grupo] = (rc, elapsed_grupo, lineas_grupo)

            if rc == 0:
                print(verde(f"\n   ✅ Grupo {nombre_grupo}: OK "
                            f"({elapsed_grupo:.1f}s, {lineas_grupo} líneas)"))
            else:
                print(rojo(f"\n   ❌ Grupo {nombre_grupo}: FALLÓ "
                           f"(returncode={rc}, "
                           f"{elapsed_grupo:.1f}s, {lineas_grupo} líneas)"))

    elapsed = time.time() - start_time_global
    mm, ss = divmod(int(elapsed), 60)

    print()
    print(cyan("─" * 70))
    print(cyan("⏹  FIN DE PYTEST"))
    print(cyan("─" * 70))
    print()
    print(gris(f"⏱  Duración total: {mm:02d}:{ss:02d}"))
    print(gris(f"📄 Salida completa en: {log_file}"))
    print(gris(f"📊 {lineas_totales} líneas capturadas"))
    print()

    # ── Resumen por grupo ──
    print(negrita("📋 Resumen por grupo:"))
    todos_ok = True
    for nombre, (rc, elapsed_g, lineas_g) in resultados.items():
        estado = verde("✅ OK") if rc == 0 else rojo(f"❌ rc={rc}")
        print(f"   {estado}  {nombre:<20} {elapsed_g:>6.1f}s  {lineas_g:>5} líneas")
        if rc != 0:
            todos_ok = False
    print()

    if todos_ok:
        print(verde(negrita("✅ TODAS LAS PRUEBAS PASARON")))
        return 0
    else:
        print(rojo(negrita("❌ ALGUNAS PRUEBAS FALLARON")))
        print(rojo(f"   Revisa el detalle en: {log_file}"))
        # Devolvemos el primer returncode no-cero (para CI)
        for rc, _, _ in resultados.values():
            if rc != 0:
                return rc
        return 1


# ============================================================
# COLORES PARA LA SALIDA DE PYTEST
# ============================================================
def _colorear_linea(linea: str) -> str:
    """
    Aplica colores a las líneas de pytest según su contenido.
    Si NO_COLOR o no hay TTY, devuelve la línea sin tocar.
    """
    if not _USE_COLORS:
        return linea

    s = linea.rstrip("\n")
    if "PASSED" in s:
        s = s.replace("PASSED", verde("PASSED"))
    elif "FAILED" in s:
        s = s.replace("FAILED", rojo("FAILED"))
    elif "ERROR" in s:
        s = s.replace("ERROR", rojo("ERROR"))
    elif "SKIPPED" in s:
        s = s.replace("SKIPPED", amarillo("SKIPPED"))
    elif "XFAIL" in s:
        s = s.replace("XFAIL", amarillo("XFAIL"))
    elif s.startswith("=") and "passed" in s:
        s = verde(negrita(s))
    elif s.startswith("=") and "failed" in s:
        s = rojo(negrita(s))
    elif "FAILURES" in s:
        s = rojo(negrita(s))

    return s + "\n"


if __name__ == "__main__":
    sys.exit(main())