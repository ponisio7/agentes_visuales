# core/utils/__init__.py
"""
Utilidades puras del proyecto: funciones que no dependen de agentes,
ejecutores ni estado global. Solo transforman datos.

Estas utilidades pueden importarse desde el sandbox, desde tests
unitarios y desde cualquier executor o solver.
"""

from .json_llm import extraer_json_de_llm

__all__ = [
    "extraer_json_de_llm",
]