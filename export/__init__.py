"""Paquete de exportación de resultados.

Expone la API pública de exportación para que pueda importarse como
``from export import ResultExporter, exportar_resultados``.
"""
from .exporters import (
    ExportConfig,
    ExportResult,
    ResultExporter,
    exportar_resultados,
)
from .utils import aplanar_diccionario, aplanar_lista

__all__ = [
    "ExportConfig",
    "ExportResult",
    "ResultExporter",
    "exportar_resultados",
    "aplanar_diccionario",
    "aplanar_lista",
]
