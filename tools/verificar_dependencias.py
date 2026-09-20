#!/usr/bin/env python3
# tools/verificar_dependencias.py
"""Comprueba que las dependencias REALES del proyecto están instaladas.

No sustituye a ``pip install -r requirements.txt``: sirve para diagnosticar
una instalación limpia antes de ejecutar la suite o la app.

Uso:
    python tools/verificar_dependencias.py
    python tools/verificar_dependencias.py --json

Códigos de salida:
    0 → todas las dependencias obligatorias están presentes
    1 → falta alguna dependencia obligatoria
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys

# (módulo importable, paquete de requirements, obligatorio, uso)
DEPENDENCIAS = [
    ("PyQt6", "PyQt6", True, "GUI y Scheduler (QObject)"),
    ("openai", "openai", True, "cliente LLM"),
    ("requests", "requests", True, "HTTP y diagnóstico"),
    ("numpy", "numpy", True, "datos y embeddings"),
    ("pandas", "pandas", True, "análisis de datos"),
    ("matplotlib", "matplotlib", True, "gráficos"),
    ("docx", "python-docx", True, "documentos .docx"),
    ("openpyxl", "openpyxl", True, "hojas .xlsx"),
    ("reportlab", "reportlab", True, "PDF"),
    ("markdown", "markdown", True, "Markdown"),
    ("pypdf", "pypdf", True, "verificación de PDF (H6)"),
    ("plyer", "plyer", True, "notificaciones"),
    ("joblib", "joblib", True, "modelos de aprendizaje"),
    ("sklearn", "scikit-learn", True, "aprendizaje"),
    ("sentence_transformers", "sentence-transformers", True,
     "matcher A/B (H2) y retrieval de casos (H8)"),
    ("torch", "torch", True, "backend de sentence-transformers"),
    ("fpdf", "fpdf2", True, "PDF alternativo"),
    ("weasyprint", "weasyprint", True, "PDF desde HTML"),
    ("flask", "flask", True, "entorno web"),
    ("playwright", "playwright", True, "agentes Browser"),
    ("ddgs", "ddgs", False, "agente Search (o duckduckgo_search)"),
    ("duckduckgo_search", "duckduckgo-search", False,
     "agente Search (nombre antiguo)"),
]


def _disponible(modulo: str) -> bool:
    try:
        return importlib.util.find_spec(modulo) is not None
    except (ImportError, ValueError):
        return False


def comprobar() -> list[dict]:
    filas = []
    for modulo, paquete, obligatorio, uso in DEPENDENCIAS:
        filas.append({
            "modulo": modulo,
            "paquete": paquete,
            "obligatorio": obligatorio,
            "uso": uso,
            "instalado": _disponible(modulo),
        })
    return filas


def _hay_search(filas: list[dict]) -> bool:
    return any(
        f["instalado"] and f["modulo"] in ("ddgs", "duckduckgo_search")
        for f in filas
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="salida JSON")
    args = parser.parse_args()

    filas = comprobar()
    faltan_obligatorias = [
        f for f in filas if f["obligatorio"] and not f["instalado"]
    ]
    search_ok = _hay_search(filas)

    if args.json:
        print(json.dumps({
            "ok": not faltan_obligatorias and search_ok,
            "dependencias": filas,
            "search_disponible": search_ok,
        }, ensure_ascii=False, indent=2))
        return 0 if (not faltan_obligatorias and search_ok) else 1

    print("Dependencias de agentes_visuales")
    print("=" * 64)
    for fila in filas:
        estado = "✅" if fila["instalado"] else ("❌" if fila["obligatorio"] else "⚠️ ")
        print(f" {estado} {fila['modulo']:<22} {fila['paquete']:<24} {fila['uso']}")
    print("=" * 64)

    if not search_ok:
        print("❌ Falta el buscador: instala 'ddgs' o 'duckduckgo-search'.")
    if faltan_obligatorias:
        print("❌ Faltan dependencias obligatorias:")
        for f in faltan_obligatorias:
            print(f"   - {f['paquete']}")
        print("   Instálalas con: pip install -r requirements.txt")
        return 1

    print("✅ Todas las dependencias obligatorias están instaladas.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
