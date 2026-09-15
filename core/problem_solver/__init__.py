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
from .constants import (
    PlanStatus,
    PlanComplexity,
    CAMPOS_VALIDOS_POR_TIPO,
)
from .models import StepPlan, ExecutionPlan
from .prompt_builder import PromptBuilder
from .code_corrector import PythonCodeCorrector
from .file_normalizer import FileNameNormalizer
from .solver import ProblemSolver
from .cli import ejecutar_plan_prueba

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