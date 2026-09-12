# core/agent/serialization.py
"""Clonación, reset, serialización y hash del agente."""
import uuid
import json
import hashlib
import time
from dataclasses import asdict, fields
from typing import TYPE_CHECKING, Dict

from .enums import EstadoAgente, TipoAgente

if TYPE_CHECKING:
    from .model import Agente


def clonar(agente: "Agente", nuevo_id: bool = True) -> "Agente":
    """Crea una copia profunda del agente (sin campos runtime)."""
    from .model import Agente  # import local para evitar ciclo

    data = asdict(agente)
    for campo in agente.CAMPOS_RUNTIME:
        data.pop(campo, None)
    if nuevo_id:
        data['id'] = str(uuid.uuid4())[:8]
    return Agente(**data)


def copiar_estado(destino: "Agente", origen: "Agente"):
    """Copia el estado de ejecución de otro agente."""
    destino.estado = origen.estado
    destino.progreso = origen.progreso
    destino.mensaje = origen.mensaje
    destino.tiempo_inicio = origen.tiempo_inicio
    destino.tiempo_fin = origen.tiempo_fin
    destino.resultado = origen.resultado
    destino.salida = origen.salida
    destino.error = origen.error
    destino.reintentos = origen.reintentos


def resetear_estado(agente: "Agente"):
    """Vuelve el agente a su estado inicial."""
    agente.estado = EstadoAgente.PENDIENTE
    agente.progreso = 0
    agente.mensaje = ""
    agente.tiempo_inicio = None
    agente.tiempo_fin = None
    agente.resultado = None
    agente.salida = ""
    agente.error = ""
    agente.reintentos = 0


def to_dict(agente: "Agente") -> Dict:
    """Representación para historial de ejecuciones."""
    return {
        "id": agente.id,
        "nombre": agente.nombre,
        "tipo": agente.tipo.value,
        "descripcion": agente.descripcion,
        "dependencias": agente.dependencias_ids,
        "duracion": agente.duracion,
        "estado": agente.estado.value,
        "progreso": agente.progreso,
        "mensaje": agente.mensaje,
        "resultado": agente.resultado,
        "salida": agente.salida[:500] if agente.salida else "",
        "error": agente.error[:500] if agente.error else "",
        "tiempo_ejecucion": (
            (agente.tiempo_fin - agente.tiempo_inicio)
            if (agente.tiempo_inicio and agente.tiempo_fin)
            else 0
        ),
    }


def to_json(agente: "Agente") -> str:
    """Serializa a JSON."""
    return json.dumps(to_dict(agente), default=str, ensure_ascii=False)


def from_dict(cls, data: Dict) -> "Agente":
    """
    Reconstruye un Agente desde un diccionario.
    Se pasa la clase desde model.py para evitar imports circulares.
    """
    kwargs = data.copy()
    valid_fields = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in kwargs.items() if k in valid_fields}

    if 'tipo' in kwargs:
        if isinstance(kwargs['tipo'], str):
            kwargs['tipo'] = TipoAgente.from_string(kwargs['tipo'])
        elif not isinstance(kwargs['tipo'], TipoAgente):
            kwargs['tipo'] = TipoAgente.PYTHON

    for campo in ['dependencias_ids', 'dependencias_nombres', 'funciones_import']:
        if campo in kwargs and not isinstance(kwargs[campo], list):
            kwargs[campo] = []

    return cls(**kwargs)


def obtener_hash_configuracion(agente: "Agente") -> str:
    """Hash de la configuración (sin campos runtime)."""
    data = asdict(agente)
    for campo in agente.CAMPOS_RUNTIME:
        data.pop(campo, None)
    json_str = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(json_str.encode('utf-8')).hexdigest()[:16]


def obtener_firma_ejecucion(agente: "Agente") -> str:
    """Hash de la ejecución incluyendo resultado."""
    data = {
        'id': agente.id,
        'nombre': agente.nombre,
        'tipo': agente.tipo.value,
        'estado': agente.estado.value,
        'resultado': agente.resultado,
        'error': agente.error,
        'tiempo': agente.obtener_tiempo_ejecucion(),
    }
    json_str = json.dumps(data, sort_keys=True, default=str)
    return hashlib.sha256(json_str.encode('utf-8')).hexdigest()[:16]


__all__ = [
    "clonar",
    "copiar_estado",
    "resetear_estado",
    "to_dict",
    "to_json",
    "from_dict",
    "obtener_hash_configuracion",
    "obtener_firma_ejecucion",
]