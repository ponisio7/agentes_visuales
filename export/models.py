# export/models.py
"""
Modelos de datos para el sistema de exportación.
"""

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class ExportConfig:
    """Configuración de exportación."""
    formato: str = "json"
    encoding: str = "utf-8-sig"
    comprimir: bool = False
    incluir_graficos: bool = True
    incluir_timestamp: bool = True
    limit_rows: Optional[int] = None
    flatten: bool = True
    separator: str = "."
    estilo: str = "moderno"
    plantilla: Optional[str] = None
    chunk_size: int = 10000


@dataclass
class ExportResult:
    """Resultado de una exportación."""
    exito: bool
    ruta: str
    formato: str
    filas_exportadas: int
    tamaño_bytes: int
    tiempo_ejecucion: float
    errores: List[str] = field(default_factory=list)
    advertencias: List[str] = field(default_factory=list)