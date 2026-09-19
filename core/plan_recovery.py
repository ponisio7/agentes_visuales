"""
core/plan_recovery.py
Genera planes alternativos (Plan B) cuando un plan falla críticamente.

Flujo:
  1. El Scheduler detecta un fallo que bloquearía dependientes.
  2. Llama a PlanRecovery.generar_plan_b() con el contexto.
  3. PlanRecovery construye un prompt, llama al LLM y parsea el plan.
  4. El Scheduler carga el nuevo plan y reinicia la ejecución.

Máximo de Plan B por ejecución: 2 (configurable en el Scheduler).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


PROMPT_PLAN_B = """Eres un planificador experto. El plan anterior FALLÓ \
durante su ejecución. Debes generar un plan ALTERNATIVO que cumpla la \
misma tarea original.

TAREA ORIGINAL:
{problema}

PLAN QUE FALLÓ:
{plan_resumen}

PASO QUE FALLÓ:
- Nombre: {agente_fallido}
- Tipo: {tipo_agente}
- Error: {error}

REGLA CRÍTICA E INVIOLABLE:
- NO repitas los mismos pasos del plan fallido.
- NO uses el mismo enfoque que llevó al error.
- Si el paso falló por "{error}", busca una forma DISTINTA de conseguir
  el mismo objetivo. Cambia de estrategia, de fuentes de datos, de
  librerías, de estructura.
- Si un paso requería una API externa que falló, intenta otra fuente
  o datos sintéticos/alternativos.
- Sé conciso: máximo 6 pasos.

CALIDAD DEL CÓDIGO (OBLIGATORIO):
- TODO el código Python debe ser SINTÁCTICAMENTE VÁLIDO.
- Las asignaciones SIEMPRE llevan `=`: `x = valor`, NO `x valor`.
- Los bloques (`if`, `for`, `def`, `try`) terminan con `:`.
- La indentación es SIEMPRE 4 espacios, nunca tabuladores.
- No mezcles código con texto explicativo dentro de los campos
  `codigo_python`, `comando_shell` o `codigo_por_item`.
- **ESCapes EN PYTHON**: Si necesitas escribir regex o strings con
  backslashes, usa raw strings: `r'[+\\-*/]'` en lugar de `'[+\\-*/]'`.
- Verifica cada línea mentalmente antes de enviar la respuesta.

LECCIONES APRENDIDAS DEL HISTORIAL:
{lecciones}

=== FORMATO DE RESPUESTA OBLIGATORIO ===

Tu respuesta debe contener EXCLUSIVAMENTE el JSON del plan.

PROHIBIDO escribir razonamiento, explicaciones o texto en inglés.
PROHIBIDO usar bloques ``` antes o después del JSON.
Empieza tu respuesta directamente con {{ y termínala con }}.

El JSON debe tener EXACTAMENTE esta estructura:

{{
  "titulo": "Título del plan alternativo",
  "analisis": "Análisis breve de por qué este plan es distinto (1-2 frases)",
  "estimacion_tiempo_segundos": 30,
  "pasos": [
    {{
      "orden": 1,
      "nombre": "NombreDelAgente",
      "descripcion": "Qué hace este paso",
      "tipo": "Python|Shell|HTTP|LLM|File|Loop",
      "dependencias": ["NombreAgente1"],
      "configuracion": {{
        // AQUÍ VAN LOS CAMPOS ESPECÍFICOS DEL TIPO
      }},
      "justificacion": "Por qué este paso es necesario"
    }}
  ]
}}

=== NOMBRES EXACTOS DE LOS CAMPOS EN 'configuracion' POR TIPO ===

NUNCA uses sinónimos. Usa EXACTAMENTE estos nombres de campo:

- **Python**: {{"codigo": "...", "timeout": 30}}
  PROHIBIDO usar "script", "code", "código".

- **Shell**: {{"comando": "...", "timeout": 30, "working_dir": ""}}
  PROHIBIDO usar "command", "cmd", "shell_command".

- **HTTP**: {{"url": "...", "metodo": "GET|POST|PUT|DELETE",
              "headers": {{}}, "body": "", "timeout": 30}}
  PROHIBIDO usar "accion", "endpoint", "uri".

- **LLM**: {{"prompt": "...", "modelo": "deepseek-v4-pro",
            "temperatura": 0.7, "max_tokens": 4000,
            "reasoning_effort": "low", "thinking_enabled": false}}
  PROHIBIDO usar "instruction", "instruction_prompt".

- **File**: {{"operacion": "leer|escribir|copiar|mover|eliminar",
             "archivo_origen": "...", "archivo_destino": "...",
             "modo_salida_file": "auto|contenido|json|texto"}}
  PROHIBIDO usar "accion", "ruta", "path", "contenido", "file".

- **Loop**: {{"fuente_items": "Dependencia.clave",
             "codigo_por_item": "...",
             "max_iteraciones": 100, "timeout_loop": 300,
             "timeout_python": 30, "continuar_en_error": false}}
  PROHIBIDO usar "source", "items_source".

=== EJEMPLO DE PASO CORRECTO (File) ===

{{
  "orden": 1,
  "nombre": "CrearCalculadoraHTML",
  "descripcion": "Escribe el archivo cal.html con la calculadora",
  "tipo": "File",
  "dependencias": [],
  "configuracion": {{
    "operacion": "escribir",
    "archivo_destino": "cal.html",
    "modo_salida_file": "auto"
  }},
  "justificacion": "Genera el archivo que luego se abrirá en el navegador"
}}

IMPORTANTE: El contenido HTML/código NO va dentro de la configuracion del
agente File. El agente File lee el contenido de su dependencia (un agente
Python o LLM anterior). Si quieres escribir un archivo con contenido fijo,
usa un agente Python que devuelva `resultado = {{'contenido': '...'}}` y
haz que el agente File dependa de él.

Empieza ahora. Tu primera letra debe ser {{
"""


class PlanRecovery:
    """
    Encapsula la generación de planes alternativos.
    No toca el Scheduler directamente; solo produce nuevos planes.
    """

    def __init__(self, llm_client, problem_solver, db_path: str):
        self.llm_client = llm_client
        self.problem_solver = problem_solver
        self.db_path = db_path

    def generar_plan_b(
        self,
        problema_original: str,
        plan_fallido: Any,
        agente_fallido: Any,
        error: str,
    ) -> Any | None:
        """
        Pide al LLM un plan alternativo y lo devuelve como ExecutionPlan.
        Devuelve None si algo falla.
        """
        try:
            plan_resumen = self._resumir_plan_fallido(plan_fallido)
            lecciones = self._obtener_lecciones()

            tipo = getattr(getattr(agente_fallido, "tipo", None), "value", "?")
            nombre = getattr(agente_fallido, "nombre", "?")
            prompt = PROMPT_PLAN_B.format(
                problema=problema_original,
                plan_resumen=plan_resumen,
                agente_fallido=nombre,
                tipo_agente=tipo,
                error=(error or "")[:500],
                lecciones=lecciones or "(sin lecciones relevantes)",
            )

            logger.info("🔧 PlanRecovery: pidiendo plan alternativo al LLM...")
            respuesta = self.llm_client.chat(
                prompt=prompt,
                system_prompt=(
                    "Eres un planificador experto. Respondes SIEMPRE "
                    "con JSON puro, sin razonamiento en texto."
                ),
                temperature=0.3,
                max_tokens=4000,
                reasoning_effort="low",
                thinking_enabled=False,
            )
            if not respuesta or not respuesta.strip():
                logger.warning("PlanRecovery: LLM devolvió respuesta vacía")
                return None

            # Debug de la respuesta cruda (temporal, se puede quitar después)
            logger.debug(f"📄 Respuesta cruda (primeros 1000 chars):\n{respuesta[:1000]}")

            # 5. Parsear la respuesta del LLM a dict (igual que hace ProblemSolver)
            plan_dict = self._parsear_respuesta_json(respuesta)
            if plan_dict is None:
                logger.warning("PlanRecovery: LLM no devolvió JSON válido")
                return None

            logger.info(
                f"📄 Plan parseado: claves={list(plan_dict.keys())[:8] if isinstance(plan_dict, dict) else 'N/A'}"
            )

            # 6. Construir el ExecutionPlan
            plan_b = self.problem_solver.builder.construir_plan(
                problema_original, plan_dict
            )
            if plan_b is None:
                logger.warning("PlanRecovery: construir_plan devolvió None")
                return None

            # 7. Generar los agentes a partir de los pasos (igual que hace
            # ProblemSolver.resolver_problema)
            if not getattr(plan_b, "agentes_generados", None):
                try:
                    plan_b.agentes_generados = (
                        self.problem_solver.builder.generar_agentes(plan_b)
                    )
                    logger.info(
                        f"PlanRecovery: {len(plan_b.agentes_generados)} "
                        f"agentes generados"
                    )
                except Exception as e:
                    logger.exception(
                        f"PlanRecovery: generar_agentes falló: {e}"
                    )
                    return None

            # 8. Verificar que hay agentes
            if not getattr(plan_b, "agentes_generados", None):
                logger.warning(
                    "PlanRecovery: plan alternativo sin agentes tras generación"
                )
                return None

            # 9. Validar sintaxis de los agentes Python/Loop
            ok, errores = self._validar_sintaxis_agentes(plan_b)
            if not ok:
                logger.warning(
                    f"PlanRecovery: primera versión del plan tiene errores "
                    f"de sintaxis en {len(errores)} agente(s):"
                )
                for err in errores[:5]:
                    logger.warning(f"   · {err}")

                # ✅ NUEVO: reintentar UNA VEZ con instrucción correctiva
                logger.info(
                    "PlanRecovery: reintentando con instrucción correctiva..."
                )
                plan_b_corregido = self._reintentar_plan_b_con_correccion(
                    problema_original=problema_original,
                    plan_resumen=plan_resumen,
                    agente_fallido=agente_fallido,
                    tipo=tipo,
                    nombre=nombre,
                    error=error,
                    errores_sintaxis=errores,
                    lecciones=lecciones,
                )

                if plan_b_corregido is not None:
                    logger.info(
                        f"✅ PlanRecovery: segunda versión validada con "
                        f"{len(plan_b_corregido.agentes_generados)} agentes"
                    )
                    return plan_b_corregido

                logger.warning(
                    "PlanRecovery: la segunda versión también falló. "
                    "Descartando plan B."
                )
                return None

            # 10. Plan válido a la primera
            logger.info(
                f"✅ PlanRecovery: plan alternativo válido con "
                f"{len(plan_b.agentes_generados)} agentes"
            )
            return plan_b

        except Exception as e:
            logger.exception(f"PlanRecovery: error generando plan B: {e}")
            return None

    def _reintentar_plan_b_con_correccion(
        self,
        problema_original: str,
        plan_resumen: str,
        agente_fallido: Any,
        tipo: str,
        nombre: str,
        error: str,
        errores_sintaxis: list,
        lecciones: str,
    ) -> Any | None:
        """
        Reintenta generar el plan B con una instrucción correctiva que
        describe exactamente los errores de sintaxis detectados.

        Estrategia:
        - Temperatura baja (0.1) para que el LLM sea más determinista
          en la generación de código.
        - Prompt con los errores exactos y reglas explícitas de
          corrección.
        - Sin thinking para ir directo a la generación.

        Returns:
            ExecutionPlan corregido, o None si también falla.
        """
        # Formatear los errores como lista legible
        lista_errores = "\n".join(
            f"  - {e}" for e in errores_sintaxis[:5]
        )

        prompt_correccion = f"""
=== CORRECCIÓN DE ERRORES DE SINTAXIS ===

El plan anterior tenía los siguientes errores de sintaxis en código Python:

{lista_errores}

⚠️ INSTRUCCIONES PARA CORREGIR (MUY IMPORTANTE):

1. **Definición de función vs llamada**:
   - Un bloque `def nombre_funcion(...):` debe tener SOLO nombres de
     parámetros entre paréntesis, NO llamadas a funciones ni expresiones.
   - ❌ INCORRECTO: `def validar(contexto.get('API', {{}}).get('body')):`
   - ✅ CORRECTO:
     ```python
     def validar(datos):
         # aquí va el cuerpo
         return ...

     # Y LUEGO se llama:
     resultado = validar(contexto.get('API', {{}}).get('body', ''))
     ```
   - NUNCA pongas `contexto.get(...)` dentro de los paréntesis de un `def`.

2. **Cuerpo de funciones obligatorio**:
   - Cada `def` DEBE tener al menos una línea de cuerpo indentada.

3. **Bloques de control**:
   - `if`, `for`, `while`, `try`, `except` terminan SIEMPRE con `:`.
   - El cuerpo va indentado con 4 espacios.

4. **Asignaciones**:
   - `x = valor` (con `=`), NUNCA `x valor`.

5. **Indentación uniforme**:
   - 4 espacios por nivel. NO mezcles tabuladores con espacios.

Genera el plan COMPLETO de nuevo, CORRIGIENDO todos estos errores.
Mantén la misma estructura y lógica, solo corrige la sintaxis.

Empieza directamente con {{. NO escribas explicaciones antes del JSON.
"""

        # Reconstruir el prompt completo del plan B + la corrección
        prompt_completo = PROMPT_PLAN_B.format(
            problema=problema_original,
            plan_resumen=plan_resumen,
            agente_fallido=nombre,
            tipo_agente=tipo,
            error=(error or "")[:500],
            lecciones=lecciones or "(sin lecciones relevantes)",
        ) + prompt_correccion

        try:
            respuesta = self.llm_client.chat(
                prompt=prompt_completo,
                system_prompt=(
                    "Eres un planificador experto. Respondes SIEMPRE "
                    "con JSON puro, sin razonamiento en texto. "
                    "Generas código Python SINTÁCTICAMENTE CORRECTO."
                ),
                temperature=0.1,  # baja para más determinismo
                max_tokens=4000,
                reasoning_effort="low",
                thinking_enabled=False,
            )

            if not respuesta or not respuesta.strip():
                logger.warning(
                    "PlanRecovery reintento: respuesta vacía del LLM"
                )
                return None

            logger.info(
                f"📄 PlanRecovery reintento: respuesta recibida "
                f"({len(respuesta)} caracteres)"
            )

            # Parsear
            plan_dict = self._parsear_respuesta_json(respuesta)
            if plan_dict is None:
                logger.warning(
                    "PlanRecovery reintento: LLM no devolvió JSON válido"
                )
                return None

            # Construir plan
            plan_b = self.problem_solver.builder.construir_plan(
                problema_original, plan_dict
            )
            if plan_b is None:
                logger.warning(
                    "PlanRecovery reintento: construir_plan devolvió None"
                )
                return None

            # Generar agentes
            if not getattr(plan_b, "agentes_generados", None):
                try:
                    plan_b.agentes_generados = (
                        self.problem_solver.builder.generar_agentes(plan_b)
                    )
                except Exception as e:
                    logger.exception(
                        f"PlanRecovery reintento: generar_agentes falló: {e}"
                    )
                    return None

            if not getattr(plan_b, "agentes_generados", None):
                logger.warning(
                    "PlanRecovery reintento: plan sin agentes tras generación"
                )
                return None

            # Validar sintaxis DE NUEVO
            ok, errores = self._validar_sintaxis_agentes(plan_b)
            if not ok:
                logger.warning(
                    f"PlanRecovery reintento: la segunda versión también "
                    f"tiene errores de sintaxis en {len(errores)} agente(s):"
                )
                for err in errores[:5]:
                    logger.warning(f"   · {err}")
                return None

            logger.info(
                f"✅ PlanRecovery reintento: segunda versión corregida "
                f"y validada ({len(plan_b.agentes_generados)} agentes)"
            )
            return plan_b

        except Exception as e:
            logger.exception(
                f"PlanRecovery reintento: error inesperado: {e}"
            )
            return None

    @staticmethod
    def _parsear_respuesta_json(respuesta: str) -> dict | None:
        """
        Extrae el JSON de la respuesta del LLM. Maneja:
        - JSON puro
        - JSON envuelto en ```json ... ```
        - Múltiples bloques JSON (usa el primero con 'pasos' o 'plan')
        - JSON anidado
        """
        import json
        import re

        if not respuesta:
            return None

        texto = respuesta.strip()
        texto = re.sub(r"^```(?:json)?\s*", "", texto)
        texto = re.sub(r"\s*```\s*$", "", texto)

        # ── Intento 1: parsear directo ──
        try:
            data = json.loads(texto)
            resultado = PlanRecovery._normalizar_plan_dict(data)
            if resultado:
                return resultado
        except json.JSONDecodeError:
            pass

        # ── Intento 2: bloques {...} balanceados ──
        bloques = PlanRecovery._extraer_bloques_json(texto)
        for bloque in bloques:
            try:
                data = json.loads(bloque)
                if isinstance(data, dict) and ("pasos" in data or "plan" in data):
                    resultado = PlanRecovery._normalizar_plan_dict(data)
                    if resultado:
                        return resultado
            except json.JSONDecodeError:
                continue

        # ── Intento 3: arrays [...] con pasos ──
        for bloque in PlanRecovery._extraer_bloques_array(texto):
            try:
                data = json.loads(bloque)
                if isinstance(data, list) and data:
                    return {"pasos": data}
            except json.JSONDecodeError:
                continue

        # ── Intento 4: cualquier JSON válido (fallback) ──
        for bloque in bloques:
            try:
                data = json.loads(bloque)
                resultado = PlanRecovery._normalizar_plan_dict(data)
                if resultado:
                    return resultado
            except json.JSONDecodeError:
                continue

        logger.warning(
            f"PlanRecovery: no se pudo parsear JSON. "
            f"Primeros 300 chars: {respuesta[:300]}"
        )
        return None

    @staticmethod
    def _extraer_bloques_json(texto: str) -> list:
        """Extrae todos los bloques `{...}` balanceados del texto."""
        bloques = []
        depth = 0
        inicio = -1
        en_string = False
        escape = False

        for i, ch in enumerate(texto):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                en_string = not en_string
                continue
            if en_string:
                continue

            if ch == "{":
                if depth == 0:
                    inicio = i
                depth += 1
            elif ch == "}":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and inicio >= 0:
                        bloques.append(texto[inicio:i + 1])
                        inicio = -1

        return bloques

    @staticmethod
    def _extraer_bloques_array(texto: str) -> list:
        """Extrae bloques `[...]` balanceados del texto."""
        bloques = []
        depth = 0
        inicio = -1
        en_string = False
        escape = False

        for i, ch in enumerate(texto):
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                en_string = not en_string
                continue
            if en_string:
                continue

            if ch == "[":
                if depth == 0:
                    inicio = i
                depth += 1
            elif ch == "]":
                if depth > 0:
                    depth -= 1
                    if depth == 0 and inicio >= 0:
                        bloques.append(texto[inicio:i + 1])
                        inicio = -1

        return bloques

    @staticmethod
    def _normalizar_plan_dict(data) -> dict | None:
        """Normaliza un dict/lista a `{"pasos": [...]}`."""
        if isinstance(data, dict):
            if "pasos" in data and isinstance(data["pasos"], list):
                return data
            if "plan" in data:
                if isinstance(data["plan"], dict) and "pasos" in data["plan"]:
                    return data["plan"]
                if isinstance(data["plan"], list):
                    return {"pasos": data["plan"]}
            # Dict sin pasos: devolverlo tal cual (que construir_plan decida)
            return data
        if isinstance(data, list):
            return {"pasos": data}
        return None

    @staticmethod
    def _validar_sintaxis_agentes(plan: Any) -> tuple[bool, list[str]]:
        """
        Valida código de agentes usando el mismo AST que el plan inicial.
        Detecta SyntaxError, NameError, json.loads placeholder y {{X}}.
        """
        from core.problem_solver.validator import PlanValidator

        errores = []
        agentes = getattr(plan, "agentes_generados", []) or []
        nombres_agentes = {a.nombre for a in agentes}

        for agente in agentes:
            tipo = getattr(getattr(agente, "tipo", None), "value", "?")
            pares = []
            if tipo == "Python":
                pares.append(("codigo_python", getattr(agente, "codigo_python", "") or ""))
            elif tipo == "Loop":
                pares.append(("codigo_por_item", getattr(agente, "codigo_por_item", "") or ""))

            for nombre_campo, codigo in pares:
                errores.extend(
                    PlanValidator._validar_codigo_python_ast(
                        codigo=codigo,
                        nombre=f"{agente.nombre}.{nombre_campo}",
                        nombres_agentes=nombres_agentes,
                    )
                )

        return (len(errores) == 0, errores)

    def _resumir_plan_fallido(self, plan: Any) -> str:
        """Convierte el plan en un resumen legible para el prompt."""
        if plan is None:
            return "(plan no disponible)"
        pasos = getattr(plan, "pasos", []) or []
        lineas = []
        for i, paso in enumerate(pasos, 1):
            tipo = getattr(paso, "tipo_agente", "?")
            nombre = getattr(paso, "nombre", f"paso_{i}")
            lineas.append(f"  {i}. [{tipo}] {nombre}")
        return "\n".join(lineas) if lineas else "(plan vacío)"

    def _obtener_lecciones(self) -> str:
        """Recupera las lecciones actuales del historial."""
        try:
            from learning.lessons import ExtractorLecciones
            ext = ExtractorLecciones(self.db_path)
            lecciones = ext.extraer(max_lecciones=8)
            return ext.formatear_para_prompt(lecciones)
        except Exception as e:
            logger.debug(f"No se pudieron obtener lecciones: {e}")
            return ""
