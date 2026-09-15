#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Punto de entrada de Agentes Visuales.

Uso:
    agentes_visuales                     # Arranca la GUI
    agentes_visuales --check-env         # Diagnóstico de configuración de IA
    agentes_visuales --check-env --timeout 15
    agentes_visuales --version
"""
import sys
import argparse
import logging
from logging.handlers import RotatingFileHandler
import os

__version__ = "1.1.0"


def _construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentes_visuales",
        description="Visualizador y orquestador de agentes con IA.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  agentes_visuales                       # Arranca la GUI\n"
            "  agentes_visuales --check-env           # Diagnóstico rápido de IA\n"
            "  agentes_visuales --version\n"
        ),
    )
    parser.add_argument(
        "--check-env",
        action="store_true",
        help="Diagnostica la configuración de la API key y la conectividad.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        metavar="SEGUNDOS",
        help="Timeout del ping HTTP en --check-env (por defecto: 5.0).",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"agentes-visuales {__version__}",
    )
    return parser


def _ejecutar_check_env(timeout: float) -> int:
    try:
        from core.env_checker import ejecutar_check_env
    except ImportError as e:
        print(f"❌ No se pudo cargar el módulo de diagnóstico: {e}", file=sys.stderr)
        return 3
    try:
        return ejecutar_check_env(timeout=timeout)
    except KeyboardInterrupt:
        print("\n⏹  Diagnóstico interrumpido por el usuario.")
        return 130
    except Exception as e:
        print(f"❌ Error inesperado en --check-env: {e}", file=sys.stderr)
        return 3


def _arrancar_gui() -> int:
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print("❌ PyQt6 no está instalado.\n   Instálalo con: pip install PyQt6", file=sys.stderr)
        return 1

    try:
        from ui.simple_main_window import SimpleMainWindow
    except ImportError as e:
        print(f"❌ No se pudo cargar la GUI: {e}", file=sys.stderr)
        return 1

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("Agentes Visuales")
    app.setApplicationVersion(__version__)

    window = SimpleMainWindow()
    window.show()
    return app.exec()


def _configurar_logging():
    os.makedirs("logs", exist_ok=True)
    file_handler = RotatingFileHandler(
        "logs/agentes_visuales.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    logging.getLogger().addHandler(file_handler)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("matplotlib").setLevel(logging.WARNING)
    logging.getLogger("PIL").setLevel(logging.WARNING)
    # ✅ Silenciar el ruido de httpx y httpcore
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("charset_normalizer").setLevel(logging.WARNING)

def main() -> int:
    _configurar_logging()
    parser = _construir_parser()
    args = parser.parse_args()

    if args.check_env:
        return _ejecutar_check_env(timeout=args.timeout)

    return _arrancar_gui()


if __name__ == "__main__":
    sys.exit(main())