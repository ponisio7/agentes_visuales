# core/utils/__init__.py
"""
Utilidades puras del proyecto: funciones que no dependen de agentes,
ejecutores ni estado global. Solo transforman datos.

Estas utilidades pueden importarse desde el sandbox, desde tests
unitarios y desde cualquier executor o solver.
"""

from .ensamblar_html import (
    ensamblar_calculadora_html,
    extraer_fragmento,
    extraer_respuesta,
)
from .json_llm import extraer_json_de_llm

__all__ = [
    "ensamblar_calculadora_html",
    "extraer_fragmento",
    "extraer_respuesta",
    "extraer_json_de_llm",
]