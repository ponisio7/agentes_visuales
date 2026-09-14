# ui/__init__.py
"""
Paquete UI de Agentes Visuales.

Solo exporta SimpleMainWindow. El resto de UIs antiguas viven en
ui/_legacy/ y NO se importan automáticamente para evitar cargar
matplotlib, plyer y demás dependencias pesadas.
"""

from .simple_main_window import SimpleMainWindow

__all__ = ["SimpleMainWindow"]