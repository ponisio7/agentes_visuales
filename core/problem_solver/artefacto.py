# core/problem_solver/artefacto.py
"""Detección del archivo que pide un enunciado en lenguaje natural.

Lo usan los planes de **respaldo** (``ProblemSolver._crear_plan_fallback`` y el
paso por defecto de ``PlanBuilder``): cuando el LLM no está disponible no se
puede saber el CONTENIDO que pedía el usuario, pero sí se puede materializar el
ARCHIVO que nombró. Antes ni eso: el respaldo devolvía ``status: 'ok'`` sin
crear nada (1.2/1.10 del ROADMAP).

Vive en su propio módulo para que ``solver`` y ``builder`` puedan usarlo sin
importarse entre sí.
"""
from __future__ import annotations

import re

# Extensiones que el respaldo puede escribir como TEXTO plano.
#
# Los formatos binarios (.docx, .pdf, .xlsx…) se excluyen a propósito:
# escribirlos "a mano" produciría un archivo corrupto con la extensión
# correcta, que es peor que no crearlo. Esos casos los cubre el contrato de
# aceptación del plan real, no el respaldo.
EXTENSIONES_ARTEFACTO = (
    "txt", "md", "csv", "tsv", "json", "html", "htm", "xml", "yaml", "yml",
    "py", "svg", "log", "rst",
)

_RE_ARTEFACTO = re.compile(
    r"(?<![\w/.-])([\w.-]+\.(?:" + "|".join(EXTENSIONES_ARTEFACTO) + r"))\b",
    re.IGNORECASE,
)


def detectar_artefacto(problema: str) -> str | None:
    """Devuelve el nombre del archivo pedido en el enunciado, si lo hay.

    Solo reconoce extensiones de texto plano (ver ``EXTENSIONES_ARTEFACTO``).
    Devuelve ``None`` si el enunciado no nombra ningún archivo escribible.
    """
    if not problema:
        return None
    coincidencia = _RE_ARTEFACTO.search(str(problema))
    return coincidencia.group(1) if coincidencia else None
