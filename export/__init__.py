# export/__init__.py
"""
Módulo de exportación de resultados.

API pública estable:
    from export import ResultExporter, ExportConfig, ExportResult, exportar_resultados
"""

from .models import ExportConfig, ExportResult
from .base import ResultExporter


def exportar_resultados(agentes, ruta=None, formato="json", **kwargs) -> ExportResult:
    """
    Función rápida para exportar resultados.

    Args:
        agentes: Lista de agentes a exportar
        ruta: Ruta de destino (opcional)
        formato: Formato de exportación
        **kwargs: Configuración adicional

    Returns:
        ExportResult: Resultado de la exportación
    """
    exporter = ResultExporter()
    return exporter.exportar(agentes, ruta, formato, **kwargs)


__all__ = [
    "ResultExporter",
    "ExportConfig",
    "ExportResult",
    "exportar_resultados",
]
