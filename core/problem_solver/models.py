# core/problem_solver/models.py
"""
Modelos de datos de ProblemSolver: StepPlan, ExecutionPlan.
Extraído literalmente de core/problem_solver.py (monolito) — Paso 2.
"""
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.agent import Agente

from .constants import PlanStatus


@dataclass
class StepPlan:
    """Un paso individual del plan descompuesto."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    orden: int = 0
    nombre: str = ""
    descripcion: str = ""
    tipo_agente: str = "Python"
    dependencia_ids: list[str] = field(default_factory=list)
    configuracion: dict[str, Any] = field(default_factory=dict)
    justificacion: str = ""
    es_critico: bool = False

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'orden': self.orden,
            'nombre': self.nombre,
            'descripcion': self.descripcion,
            'tipo_agente': self.tipo_agente,
            'dependencia_ids': self.dependencia_ids,
            'configuracion': self.configuracion,
            'justificacion': self.justificacion,
            'es_critico': self.es_critico,
        }


@dataclass
class ExecutionPlan:
    """Plan completo de ejecución generado por el solver."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    problema_original: str = ""
    titulo: str = ""
    analisis: str = ""
    pasos: list[StepPlan] = field(default_factory=list)
    status: PlanStatus = PlanStatus.DRAFT
    estimacion_tiempo: float = 0.0
    agentes_generados: list[Agente] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)
    metadatos: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'problema_original': self.problema_original,
            'titulo': self.titulo,
            'analisis': self.analisis,
            'pasos': [p.to_dict() for p in self.pasos],
            'status': self.status.value,
            'estimacion_tiempo': self.estimacion_tiempo,
            'advertencias': self.advertencias,
            'metadatos': self.metadatos,
        }

    def obtener_agentes_por_nombre(self) -> dict[str, Agente]:
        return {a.nombre: a for a in self.agentes_generados}

    def obtener_pasos_por_nombre(self) -> dict[str, StepPlan]:
        return {p.nombre: p for p in self.pasos}
