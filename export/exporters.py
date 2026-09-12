# export/exporters.py
"""
DEPRECADO: usar `export` directamente.

Este módulo existe solo para compatibilidad con código antiguo que hacía:
    from export.exporters import ResultExporter, ExportResult, ExportConfig

Toda la funcionalidad vive ahora en:
    - export.models      → ExportConfig, ExportResult
    - export.base        → ResultExporter
    - export.csv_exporter, json_exporter, etc.
"""

from export import (
    ResultExporter,
    ExportConfig,
    ExportResult,
    exportar_resultados,
)

__all__ = [
    "ResultExporter",
    "ExportConfig",
    "ExportResult",
    "exportar_resultados",
]