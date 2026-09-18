# core/problem_solver/code_corrector.py
"""
PythonCodeCorrector: corrección automática de código Python generado por el LLM.
Extraído literalmente de core/problem_solver.py (monolito) — Paso 4.
"""
import logging
import re

logger = logging.getLogger(__name__)


class PythonCodeCorrector:
    """
    Corrige automáticamente código Python generado por LLM.
    Detecta y repara variables incorrectas como 'respuesta', 'data', etc.
    """

    PATTERNS = [
        {
            "pattern": r'\brespuesta\b(?![.]|\s*=)',
            "template": "contexto.get('{dep}', {{}}).get('body', '{{}}')",
            "description": "Variable 'respuesta' → contexto.get('Dep', {{}}).get('body', '{{}}')"
        },
        {
            "pattern": r'\bdata\s*=\s*["\']?[a-z_]+["\']?\s*$',  # solo líneas tipo `data = foo`
            "template": "data = contexto.get('{dep}', {{}})",
            "description": "data = variable_simple → data = contexto.get('Dep', {{}})"
        },
        {
            "pattern": r'\bresult\b(?![.]|\s*=)',
            "template": "contexto.get('{dep}', {{}})",
            "description": "Variable 'result' → contexto.get('Dep', {{}})"
        },
        {
            "pattern": r'\bresponse\b(?![.]|\s*=)',
            "template": "contexto.get('{dep}', {{}}).get('body', '{{}}')",
            "description": "Variable 'response' → contexto.get('Dep', {{}}).get('body', '{{}}')"
        },
        {
            "pattern": r'\bjson_data\b(?![.]|\s*=)',
            "template": "contexto.get('{dep}', {{}}).get('json', {{}})",
            "description": "Variable 'json_data' → contexto.get('Dep', {{}}).get('json', {{}})"
        },
        {
            "pattern": r'\brespuesta_json\b(?![.]|\s*=)',
            "template": "contexto.get('{dep}', {{}}).get('json', {{}})",
            "description": "Variable 'respuesta_json' → contexto.get('Dep', {{}}).get('json', {{}})"
        },
    ]

    @classmethod
    def corregir(cls, codigo: str, dependencias: list[str]) -> str:
        """Corrige el código Python aplicando todos los patrones."""
        if not codigo or not codigo.strip():
            return codigo

        if cls._usa_contexto_correcto(codigo):
            logger.debug("✅ Código ya usa contexto correctamente")
            return codigo

        codigo_corregido = codigo
        dep = dependencias[0] if dependencias else ""

        for pattern_info in cls.PATTERNS:
            if re.search(pattern_info["pattern"], codigo_corregido):
                if dep:
                    replacement = pattern_info["template"].format(dep=dep)
                else:
                    replacement = "contexto"

                codigo_corregido = re.sub(
                    pattern_info["pattern"],
                    replacement,
                    codigo_corregido
                )
                logger.info(f"🔧 {pattern_info['description']}")

        return codigo_corregido

    @classmethod
    def _usa_contexto_correcto(cls, codigo: str) -> bool:
        """Verifica si el código ya usa contexto correctamente."""
        if 'contexto.get' in codigo:
            return True
        if 'contexto' in codigo and 'respuesta' not in codigo and 'data' not in codigo:
            return True
        return False
