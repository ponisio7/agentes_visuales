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
# main.py
import logging
from core.logging_config import configurar_logging_thread_safe

configurar_logging_thread_safe(level=logging.INFO)

__version__ = "1.0.0"


# ============================================================
# HOJA DE ESTILO (separada de main para mantenerlo limpio)
# ============================================================
ESTILO_APP = """
    QMainWindow { background-color: #f0f2f5; }
    QGroupBox {
        font-weight: bold;
        border: 1px solid #d1d5db;
        border-radius: 4px;
        margin-top: 10px;
        padding-top: 10px;
    }
    QGroupBox::title {
        subcontrol-origin: margin;
        left: 10px;
        padding: 0 5px 0 5px;
    }
    QPushButton {
        border-radius: 4px;
        padding: 6px 12px;
    }
    QPushButton:disabled {
        background-color: #ccc !important;
        color: #666 !important;
    }
    QLineEdit, QComboBox {
        padding: 4px 8px;
        border: 1px solid #d1d5db;
        border-radius: 4px;
        background-color: white;
    }
    QLineEdit:focus, QComboBox:focus {
        border-color: #007bff;
    }
    QProgressBar {
        border: 1px solid #d1d5db;
        border-radius: 3px;
        text-align: center;
        height: 20px;
    }
    QProgressBar::chunk {
        background-color: #007bff;
        border-radius: 3px;
    }
    QScrollArea {
        background-color: white;
        border: 1px solid #d1d5db;
        border-radius: 4px;
    }
    QTextEdit {
        border: 1px solid #d1d5db;
        border-radius: 4px;
    }
    QLabel { color: #333; }
    QTabWidget::pane {
        border: 1px solid #d1d5db;
        border-radius: 4px;
    }
    QTabBar::tab {
        padding: 6px 12px;
        background-color: #e9ecef;
        border: 1px solid #d1d5db;
        border-bottom: none;
        border-radius: 4px 4px 0 0;
    }
    QTabBar::tab:selected {
        background-color: white;
    }
"""


# ============================================================
# ARGUMENTOS DE LÍNEA DE COMANDOS
# ============================================================
def _construir_parser() -> argparse.ArgumentParser:
    """Construye el parser de argumentos."""
    parser = argparse.ArgumentParser(
        prog="agentes_visuales",
        description="Visualizador y orquestador de agentes con IA.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Ejemplos:\n"
            "  agentes_visuales                       # Arranca la GUI\n"
            "  agentes_visuales --check-env           # Diagnóstico rápido de IA\n"
            "  agentes_visuales --check-env --timeout 15\n"
            "  agentes_visuales --version\n"
        ),
    )

    parser.add_argument(
        "--check-env",
        action="store_true",
        help="Diagnostica la configuración de la API key y la conectividad, "
             "y sale sin abrir la GUI.",
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


# ============================================================
# MODO DIAGNÓSTICO (--check-env)
# ============================================================
def _ejecutar_check_env(timeout: float) -> int:
    """
    Ejecuta el diagnóstico de entorno y devuelve un código de salida:
        0 → Todo OK
        1 → Error de configuración
        2 → Error de red
        3 → Error inesperado
    """
    # Import perezoso: permite usar --check-env sin necesidad de PyQt6
    try:
        from core.env_checker import ejecutar_check_env
    except ImportError as e:
        print(
            f"❌ No se pudo cargar el módulo de diagnóstico: {e}\n"
            f"   Asegúrate de ejecutar desde el directorio del proyecto "
            f"o con el PYTHONPATH correcto.",
            file=sys.stderr,
        )
        return 3

    try:
        return ejecutar_check_env(timeout=timeout)
    except KeyboardInterrupt:
        print("\n⏹  Diagnóstico interrumpido por el usuario.")
        return 130
    except Exception as e:
        print(f"❌ Error inesperado en --check-env: {e}", file=sys.stderr)
        return 3


# ============================================================
# MODO GUI
# ============================================================
def _arrancar_gui() -> int:
    """
    Arranca la aplicación gráfica.

    Returns:
        int: Código de salida de QApplication.exec()
    """
    # Import perezoso: si PyQt6 no está instalado, el mensaje de error es claro
    try:
        from PyQt6.QtWidgets import QApplication
    except ImportError:
        print(
            "❌ PyQt6 no está instalado.\n"
            "   Instálalo con: pip install PyQt6",
            file=sys.stderr,
        )
        return 1

    try:
        from ui.main_window import MainWindow
    except ImportError as e:
        print(
            f"❌ No se pudo cargar la interfaz gráfica: {e}\n"
            f"   Verifica que estás ejecutando desde el directorio del proyecto.",
            file=sys.stderr,
        )
        return 1

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setStyleSheet(ESTILO_APP)
    app.setApplicationName("Agentes Visuales")
    app.setApplicationVersion(__version__)

    window = MainWindow()
    window.show()

    return app.exec()


# ============================================================
# ENTRY POINT
# ============================================================
def main() -> int:
    """Punto de entrada principal."""
    parser = _construir_parser()
    args = parser.parse_args()

    # Modo diagnóstico
    if args.check_env:
        return _ejecutar_check_env(timeout=args.timeout)

    # Modo normal (GUI)
    return _arrancar_gui()


if __name__ == "__main__":
    sys.exit(main())