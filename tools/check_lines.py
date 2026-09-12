# tools/check_lines.py
"""Verifica que ningún archivo .py supere MAX_LINES.

Uso:
    python tools/check_lines.py            # busca desde el CWD
    python tools/check_lines.py --root .   # raíz explícita
"""
import argparse
import pathlib
import sys

MAX_LINES = 500
EXCLUDE_DIRS = {
    ".venv", "venv", "env", "__pycache__", ".git",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "build", "dist", ".eggs", "node_modules",
}


def iter_py_files(root: pathlib.Path):
    for p in root.rglob("*.py"):
        # Excluir cualquier ruta que contenga una carpeta excluida
        if any(part in EXCLUDE_DIRS for part in p.parts):
            continue
        yield p


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", default=".",
        help="Raíz desde la que buscar (por defecto: cwd)"
    )
    parser.add_argument(
        "--max", type=int, default=MAX_LINES,
        help=f"Límite de líneas (por defecto: {MAX_LINES})"
    )
    args = parser.parse_args()

    root = pathlib.Path(args.root).resolve()
    if not root.exists():
        print(f"❌ La raíz no existe: {root}")
        sys.exit(2)

    print(f"🔍 Analizando: {root}")
    print(f"📏 Límite: {args.max} líneas\n")

    violations = []
    total = 0
    for p in iter_py_files(root):
        total += 1
        try:
            n = len(p.read_text(encoding="utf-8").splitlines())
        except Exception as e:
            print(f"⚠️  No se pudo leer {p}: {e}")
            continue
        if n > args.max:
            violations.append((n, p))

    for n, p in sorted(violations, reverse=True):
        rel = p.relative_to(root)
        print(f"❌ {n:>5}  {rel}")

    print(f"\n📦 Archivos analizados: {total}")
    print(f"🚨 Violaciones: {len(violations)}")

    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()