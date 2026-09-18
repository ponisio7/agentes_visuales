# core/__init__.py
"""
Núcleo de Agentes Visuales.
Contiene los modelos, ejecutores y lógica principal de los agentes.
"""

from .agent import Agente, EstadoAgente, TipoAgente
from .ai_assistant import AIAssistant
from .bridge import SchedulerBridge
from .cancellation import (
    CancellationManager,
    CancellationState,
    CancellationToken,
    obtener_gestor_cancelacion,
)
from .event_bus import Event, EventBus, EventType, obtener_bus
from .executors import AgentExecutor
from .llm_client import LLMClient
from .sandbox import PythonSandbox
from .scheduler import Scheduler
from .text_parser import AgentTextParser, ParseResult
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
