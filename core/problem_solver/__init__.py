# core/problem_solver/__init__.py
"""
Paquete ProblemSolver. Reexporta la API pública para no romper imports.

El código se fue fragmentando desde el antiguo core/problem_solver.py
(monolito de 2247 líneas) en submódulos especializados:
    - constants: enums y mapas de campos válidos
    - models: StepPlan, ExecutionPlan
    - prompt_builder: construcción del system/user prompt
    - code_corrector: corrección de código Python del LLM
    - file_normalizer: normalización de nombres de archivo
    - parser: parsing robusto de la respuesta JSON del LLM
    - validator: validación de planes
    - builder: construcción de Agentes desde StepPlan
    - solver: ProblemSolver (orquestador)
    - cli: helpers de línea de comandos
"""
from .cli import ejecutar_plan_prueba
from .code_corrector import PythonCodeCorrector
from .constants import (
    CAMPOS_VALIDOS_POR_TIPO,
    PlanComplexity,
    PlanStatus,
)
from .file_normalizer import FileNameNormalizer
from .models import ExecutionPlan, StepPlan
from .prompt_builder import PromptBuilder
from .solver import ProblemSolver

__all__ = [
    "ProblemSolver",
    "ExecutionPlan",
    "StepPlan",
    "PlanStatus",
    "PlanComplexity",
    "PromptBuilder",
    "PythonCodeCorrector",
    "FileNameNormalizer",
    "CAMPOS_VALIDOS_POR_TIPO",
    "ejecutar_plan_prueba",
]
