# core/__init__.py
"""
Núcleo de Agentes Visuales.
Contiene los modelos, ejecutores y lógica principal de los agentes.
"""

from .agent import Agente, EstadoAgente, TipoAgente
from .scheduler import Scheduler
from .bridge import SchedulerBridge
from .executors import AgentExecutor
from .sandbox import PythonSandbox
from .llm_client import LLMClient
from .text_parser import AgentTextParser, ParseResult
from .ai_assistant import AIAssistant
from .event_bus import EventBus, EventType, Event, obtener_bus
from .cancellation import (
    CancellationState, 
    CancellationToken, 
    CancellationManager,
    obtener_gestor_cancelacion
)
from .utils import extraer_json_de_llm  # ✅ NUEVO

__all__ = [
    'Agente',
    'EstadoAgente',
    'TipoAgente',
    'Scheduler',
    'SchedulerBridge',
    'AgentExecutor',
    'PythonSandbox',
    'LLMClient',
    'AgentTextParser',
    'ParseResult',
    'AIAssistant',
    'EventBus',
    'EventType',
    'Event',
    'obtener_bus',
    'CancellationState',
    'CancellationToken',
    'CancellationManager',
    'obtener_gestor_cancelacion',
    'extraer_json_de_llm',  # ✅ NUEVO
]