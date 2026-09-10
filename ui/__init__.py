# ui/__init__.py
"""
Paquete UI de Agentes Visuales.
Contiene todos los widgets y diálogos de la interfaz de usuario.
"""

from .main_window import MainWindow
from .agent_widget import AgentWidget
from .graph_view import GraphView
from .metrics_dashboard import MetricsDashboard
from .agent_config_dialog import AgentConfigDialog
from .theme_manager import ThemeManager
from .notification_manager import NotificationManager
from .text_import_dialog import TextImportDialog
from .ai_assistant_dialog import AIAssistantDialog
from .problem_solver_dialog import ProblemSolverDialog

__all__ = [
    'MainWindow',
    'AgentWidget',
    'GraphView',
    'MetricsDashboard',
    'AgentConfigDialog',
    'ThemeManager',
    'NotificationManager',
    'TextImportDialog',
    'AIAssistantDialog',
    'ProblemSolverDialog'
]