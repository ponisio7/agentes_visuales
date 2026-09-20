# core/__init__.py
"""
Núcleo de Agentes Visuales.
Contiene los modelos, ejecutores y lógica principal de los agentes.
"""

from .agent import Agente, EstadoAgente, TipoAgente
from .ai_assistant import AIAssistant
from .bridge import SchedulerBridge
from .budget_manager import BudgetManager
from .cancellation import (
    CancellationManager,
    CancellationState,
    CancellationToken,
    obtener_gestor_cancelacion,
)
from .event_bus import Event, EventBus, EventType, obtener_bus
from .executors import AgentExecutor
from .job_cancellation import (
    JobCancellationRegistry,
    obtener_registro_cancelacion,
)
from .llm_client import LLMClient
from .recovery_manager import (
    PARADAS_DURAS,
    RecoveryDecision,
    RecoveryManager,
    clasificar_fallo,
)
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
    'RecoveryManager',
    'RecoveryDecision',
    'PARADAS_DURAS',
    'clasificar_fallo',
    'BudgetManager',
    'JobCancellationRegistry',
    'obtener_registro_cancelacion',
    'extraer_json_de_llm',  # ✅ NUEVO
]
