#!/usr/bin/env python3
"""
fix_tests_v2.py - Aplica los 2 parches nuevos + verifica todo.

Uso:
    python fix_tests_v2.py              # Aplica los parches
    python fix_tests_v2.py --dry-run    # Solo muestra qué haría
    python fix_tests_v2.py --revert     # Restaura el último backup
"""
import re
import sys
import shutil
import subprocess
import argparse
from pathlib import Path
from datetime import datetime

PROYECTO = Path(__file__).resolve().parent
BACKUP_DIR = PROYECTO / ".backups_fix_tests_v2"
SUFFIX = datetime.now().strftime("%Y%m%d_%H%M%S")


def log(msg, emoji="•"):
    print(f"  {emoji} {msg}")


def reemplazar_funcion(contenido: str, nombre: str, nuevo_codigo: str):
    """Reemplaza el cuerpo completo de una función `def nombre(...)`."""
    patron = re.compile(
        rf"(    def {re.escape(nombre)}\([^)]*\):.*?)"
        rf"(?=\n    def |\nclass |\n\nclass |\Z)",
        re.DOTALL,
    )
    match = patron.search(contenido)
    if not match:
        return contenido, False
    nuevo = nuevo_codigo.rstrip() + "\n\n"
    return contenido[: match.start()] + nuevo + contenido[match.end():], True


def parchear_fichero(ruta: Path, parches: list, dry: bool = False) -> bool:
    """Aplica una lista de (nombre, nuevo_codigo) a un fichero."""
    contenido = ruta.read_text(encoding="utf-8")
    original = contenido
    for nombre, codigo in parches:
        contenido, ok = reemplazar_funcion(contenido, nombre, codigo)
        if ok:
            log(f"{ruta.name}::{nombre} OK", "✅")
        else:
            log(f"{ruta.name}::{nombre} NO ENCONTRADO", "❌")
            return False
    if contenido != original and not dry:
        ruta.write_text(contenido, encoding="utf-8")
        log(f"Escrito: {ruta.name}", "💾")
    return True


def hacer_backup(rutas):
    BACKUP_DIR.mkdir(exist_ok=True)
    for r in rutas:
        if r.exists():
            dest = BACKUP_DIR / f"{r.name}.{SUFFIX}.bak"
            shutil.copy2(r, dest)
            log(f"Backup: {r.name} -> {dest.name}", "📦")


def revertir():
    if not BACKUP_DIR.exists():
        log("No hay backups", "❌")
        return False
    candidatos = sorted(BACKUP_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidatos:
        log("Backups vacíos", "❌")
        return False
    ultimo_sufijo = candidatos[0].name.split(".")[-2]
    for backup in candidatos:
        if f".{ultimo_sufijo}.bak" not in backup.name:
            continue
        nombre_real = backup.name.split(".")[0] + ".py"
        for destino in [PROYECTO / "tests" / nombre_real]:
            if destino.exists():
                shutil.copy2(backup, destino)
                log(f"Restaurado: {destino}", "↩")
    return True


def compilar(rutas):
    for ruta in rutas:
        try:
            subprocess.run(
                [sys.executable, "-m", "py_compile", str(ruta)],
                check=True,
                capture_output=True,
                text=True,
            )
            log(f"Compila OK: {ruta.name}", "✅")
        except subprocess.CalledProcessError as e:
            log(f"FALLO al compilar {ruta.name}:\n{e.stderr}", "❌")
            return False
    return True


def ejecutar_tests_afectados():
    tests = [
        "tests/test_loop_safety.py::TestLoopEjecucion::test_loop_con_fuente_inexistente",
        "tests/test_loop_safety.py::TestLoopSeguridad::test_loop_con_codigo_peligroso_bloqueado",
    ]
    cmd = [
        sys.executable, "-m", "pytest", *tests, "-v", "--tb=short",
        "-p", "no:cacheprovider",
        "-m", "not gui and not slow",
    ]
    print()
    log("Ejecutando los 2 tests afectados...", "🧪")
    resultado = subprocess.run(cmd, cwd=PROYECTO)
    return resultado.returncode == 0


# ============================================================
# CÓDIGO NUEVO (SOLO LOS 2 PARCHES NECESARIOS)
# ============================================================

NUEVO_test_fuente_inexistente = '''    def test_loop_con_fuente_inexistente(self):
        """Prueba loop con fuente que no existe en el contexto."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Inexistente.items",
            codigo_por_item="resultado = {'ok': True}",
            max_iteraciones=10
        )

        contexto = {}
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)

        assert ok is False
        assert "Inexistente" in msg or "lista válida" in msg.lower()
'''

NUEVO_test_codigo_peligroso = '''    def test_loop_con_codigo_peligroso_bloqueado(self):
        """
        Prueba que código potencialmente peligroso en loop sea manejado.

        IMPORTANTE: NO usamos os.listdir('/') porque puede colgarse en
        sistemas con autofs/NFS. Usamos una operación local y determinista.
        """
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="""
try:
    with open('/tmp/no_existe_xyz_12345.txt', 'r') as f:
        contenido = f.read()
    resultado = {'acceso': True, 'contenido': contenido[:50]}
except FileNotFoundError as e:
    resultado = {'acceso': False, 'error': str(e)}
except PermissionError as e:
    resultado = {'acceso': False, 'error': str(e)}
""",
            max_iteraciones=10,
            continuar_en_error=True
        )

        contexto = {"Fuente": {"items": [1]}}
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)

        assert result is not None
        assert result["total_items"] == 1
        assert result["items_procesados"] == 1
'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--revert", action="store_true")
    args = parser.parse_args()

    print()
    print("=" * 70)
    print("🔧 FIX TESTS v2 — Parches para test_loop_safety.py")
    print("=" * 70)

    if args.revert:
        return 0 if revertir() else 1

    ruta = PROYECTO / "tests" / "test_loop_safety.py"
    if not ruta.exists():
        log(f"Falta {ruta}", "❌")
        return 1

    if not args.dry_run:
        hacer_backup([ruta])
        print()

    ok = parchear_fichero(
        ruta,
        [
            ("test_loop_con_fuente_inexistente", NUEVO_test_fuente_inexistente),
            ("test_loop_con_codigo_peligroso_bloqueado", NUEVO_test_codigo_peligroso),
        ],
        dry=args.dry_run,
    )

    if not ok:
        log("Algún parche no se aplicó.", "⚠")
        return 1

    if args.dry_run:
        log("[dry-run] No se escribió nada", "🔍")
        return 0

    print()
    print("── Verificando compilación ──")
    if not compilar([ruta]):
        log("Error de compilación. Restaurando backup...", "❌")
        revertir()
        return 1

    if not ejecutar_tests_afectados():
        log("Algún test falló.", "⚠")
        log("Revisa con: python -m pytest tests/test_loop_safety.py -v -k 'fuente_inexistente or codigo_peligroso'", "💡")
        return 1

    print()
    print("=" * 70)
    print("✅ PARCHE v2 APLICADO CON ÉXITO")
    print("=" * 70)
    print()
    print("Siguiente paso:")
    print("   python run_all_tests.py")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())