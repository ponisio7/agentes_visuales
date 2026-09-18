# core/problem_solver/parser.py
"""
PlanParser: parsing robusto de la respuesta JSON del LLM.
Extraído de los métodos privados de ProblemSolver (monolito) — Paso 5,
Opción B (clase con estado, instanciada con `logger` en el `__init__`
de ProblemSolver).

Métodos migrados 1:1 desde el monolito (misma lógica, solo cambia el
"self" de ProblemSolver por el de PlanParser, y se hacen públicos los
que `solver.py` necesita llamar desde fuera):

    _parsear_respuesta               -> parsear                     (público)
    _limpiar_respuesta_agresivamente -> limpiar_respuesta_agresivamente (público, lo usa _consultar_llm_con_reintentos)
    _reparar_json                    -> reparar_json                (público, también lo usa _consultar_llm_con_reintentos)
    _buscar_json                     -> _buscar_json                (privado, solo uso interno)
    _extraer_json_por_partes         -> _extraer_json_por_partes    (privado, solo uso interno)
"""
import json
import re

from core.utils import extraer_json_de_llm


class PlanParser:
    """Parsea y repara la respuesta JSON cruda del LLM."""

    def __init__(self, logger):
        self.logger = logger

    def parsear(self, respuesta: str) -> dict:
        """Parsea la respuesta JSON del LLM."""
        if not respuesta or not respuesta.strip():
            raise ValueError("La respuesta está vacía")

        self.logger.debug(f"📥 Parseando respuesta de {len(respuesta)} caracteres")

        try:
            data = extraer_json_de_llm(respuesta)
            if data is not None:
                self.logger.debug("✅ JSON parseado con extraer_json_de_llm")
                return data
        except Exception as e:
            self.logger.debug(f"extraer_json_de_llm falló: {e}")

        json_match = self._buscar_json(respuesta)
        if json_match:
            try:
                return json.loads(json_match)
            except json.JSONDecodeError:
                pass

        json_reparado = self.reparar_json(respuesta)
        if json_reparado:
            try:
                return json.loads(json_reparado)
            except json.JSONDecodeError:
                pass

        json_extraido = self._extraer_json_por_partes(respuesta)
        if json_extraido:
            try:
                return json.loads(json_extraido)
            except json.JSONDecodeError:
                pass

        self.logger.error("❌ No se pudo parsear JSON")
        self.logger.debug(f"Respuesta: {respuesta[:500]}...")
        raise ValueError("No se encontró un objeto JSON válido en la respuesta")

    def limpiar_respuesta_agresivamente(self, respuesta: str) -> str:
        """Limpia la respuesta agresivamente para extraer JSON puro."""
        if not respuesta:
            return ""

        reparado = respuesta
        reparado = re.sub(r'^```json\s*', '', reparado, flags=re.MULTILINE)
        reparado = re.sub(r'^```\s*', '', reparado, flags=re.MULTILINE)
        reparado = re.sub(r'\s*```$', '', reparado, flags=re.MULTILINE)

        inicio = reparado.find('{')
        fin = reparado.rfind('}')
        if inicio != -1 and fin != -1 and fin > inicio:
            reparado = reparado[inicio:fin + 1]

        reparado = ''.join(char for char in reparado if ord(char) >= 32 or char in '\n\r\t')

        reparado = reparado.strip()
        return reparado

    def reparar_json(self, texto: str) -> str | None:
        """Intenta reparar un JSON mal formado."""
        if not texto or not texto.strip():
            return None

        reparado = texto

        reparado = re.sub(r'^```json\s*', '', reparado, flags=re.MULTILINE)
        reparado = re.sub(r'^```\s*', '', reparado, flags=re.MULTILINE)
        reparado = re.sub(r'\s*```$', '', reparado, flags=re.MULTILINE)

        inicio = reparado.find('{')
        fin = reparado.rfind('}')
        if inicio != -1 and fin != -1 and fin > inicio:
            reparado = reparado[inicio:fin + 1]

        reparado = re.sub(r'//.*?$', '', reparado, flags=re.MULTILINE)
        reparado = re.sub(r'/\*.*?\*/', '', reparado, flags=re.DOTALL)

        reparado = re.sub(r"([{,])\s*'([^']*)'\s*:", r'\1"\2":', reparado)
        reparado = re.sub(r":\s*'([^']*)'", r': "\1"', reparado)

        reparado = re.sub(r'([{,])\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:', r'\1"\2":', reparado)

        reparado = re.sub(r',\s*}', '}', reparado)
        reparado = re.sub(r',\s*]', ']', reparado)

        reparado = re.sub(r'\\"', '"', reparado)
        reparado = re.sub(r'"{2,}', '"', reparado)

        try:
            json.loads(reparado)
            return reparado
        except json.JSONDecodeError:
            return None

    def _buscar_json(self, texto: str) -> str | None:
        """Busca un objeto JSON en el texto."""
        inicio = texto.find('{')
        fin = texto.rfind('}')
        if inicio != -1 and fin != -1 and fin > inicio:
            return texto[inicio:fin + 1]
        return None

    def _extraer_json_por_partes(self, texto: str) -> str | None:
        """Extrae JSON buscando estructuras comunes."""
        patrones = [
            r'\{[^{}]*"pasos"[^{}]*\[[^\]]*\][^{}]*\}',
            r'\{[^{}]*"titulo"[^{}]*\}',
            r'\{\s*"[^"]+"\s*:\s*\[[^\]]*\]\s*\}',
            r'\{\s*"[^"]+"\s*:\s*"[^"]*"\s*\}',
        ]

        for patron in patrones:
            try:
                matches = re.findall(patron, texto, re.DOTALL)
                for match in matches:
                    if len(match) > 20:
                        reparado = re.sub(r"'", '"', match)
                        reparado = re.sub(r',\s*}', '}', reparado)
                        reparado = re.sub(r',\s*]', ']', reparado)
                        try:
                            json.loads(reparado)
                            return reparado
                        except Exception:
                            continue
            except Exception:
                continue

        return None
