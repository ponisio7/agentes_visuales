# storage/database/models.py
"""Modelos de datos para el historial de ejecuciones."""
from typing import List, Dict, Optional
from dataclasses import dataclass, asdict, field
import json


@dataclass
class Ejecucion:
    """Modelo de una ejecución."""
    id: Optional[int] = None
    fecha: Optional[str] = None
    duracion_total: float = 0.0
    agentes_total: int = 0
    completados: int = 0
    errores: int = 0
    cancelados: int = 0
    estado: str = "completada"
    ejecutor: str = ""
    tags: str = ""
    notas: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class AgenteEjecucion:
    """Modelo de un agente en una ejecución."""
    id: Optional[int] = None
    ejecucion_id: int = 0
    agente_id: str = ""
    nombre: str = ""
    tipo: str = ""
    estado: str = ""
    duracion: float = 0.0
    dependencias: List[str] = field(default_factory=list)
    resultado: Optional[Dict] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        data = asdict(self)
        data['dependencias'] = json.dumps(data['dependencias'])
        if data['resultado']:
            data['resultado'] = json.dumps(data['resultado'])
        return data


__all__ = ["Ejecucion", "AgenteEjecucion"]