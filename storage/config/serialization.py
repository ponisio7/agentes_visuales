"""Serialización de agentes y migración de esquemas."""

import dataclasses
from typing import Dict

from core.agent import Agente, TipoAgente


def agente_a_dict(agente: Agente) -> Dict:
    """
    Serializa un Agente completo, excluyendo campos de runtime.

    Args:
        agente: Instancia de Agente

    Returns:
        Dict: Datos serializados del agente
    """
    agente_dict = dataclasses.asdict(agente)

    # Convertir TipoAgente a string
    if isinstance(agente_dict.get('tipo'), TipoAgente):
        agente_dict['tipo'] = agente_dict['tipo'].value
    elif hasattr(agente.tipo, 'value'):
        agente_dict['tipo'] = agente.tipo.value

    # Excluir campos de runtime
    for campo in Agente.CAMPOS_RUNTIME:
        agente_dict.pop(campo, None)

    return agente_dict


def migrar_v1_a_v2(data: Dict) -> Dict:
    """
    Migra una configuración de versión 1.x a 2.0.

    Args:
        data: Datos de configuración v1

    Returns:
        Dict: Datos migrados a v2
    """
    data['version'] = '2.0'

    for agente in data.get('agentes', []):
        if 'continuar_en_error' not in agente:
            agente['continuar_en_error'] = False
        if 'timeout_loop' not in agente:
            agente['timeout_loop'] = 300

    return data