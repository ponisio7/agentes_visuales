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

import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# ESCALERA DE ESTRATEGIAS (H7)
# ============================================================
# Cada intento de Plan B usa una estrategia DISTINTA. No se obliga a que
# todas se usen: el LLM decide, pero el prompt le fuerza a diversificar y el
# scheduler no reintenta una estrategia que ya produjo un plan idéntico.
ESTRATEGIAS = (
    "correccion_puntual",
    "cambiar_configuracion",
    "cambiar_tipo_agente",
    "reestructurar_plan",
    "fallback_alternativo",
)

_DESCRIPCION_ESTRATEGIA = {
    "correccion_puntual": (
        "Corrige solo el punto que falló, manteniendo la estructura del plan."
    ),
    "cambiar_configuracion": (
        "Cambia la configuración o la fuente de datos del paso que falló "
        "(otra librería, otra URL, otros parámetros)."
    ),
    "cambiar_tipo_agente": (
        "Sustituye el tipo de agente del paso que falló por otro distinto "
        "(p. ej. Python → LLM, HTTP → Browser)."
    ),
    "reestructurar_plan": (
        "Reestructura el plan: divide o fusiona pasos, cambia el orden y las "
        "dependencias para evitar el fallo."
    ),
    "fallback_alternativo": (
        "Usa un enfoque alternativo completo (datos sintéticos, otra vía de "
        "obtención del resultado) para conseguir el mismo objetivo."
    ),
}


def estrategia_para_intento(intento: int) -> str:
    """Estrategia correspondiente al intento N (1-based), con tope."""
    if intento < 1:
        intento = 1
    indice = min(intento - 1, len(ESTRATEGIAS) - 1)
    return ESTRATEGIAS[indice]


def descripcion_estrategia(estrategia: str) -> str:
    return _DESCRIPCION_ESTRATEGIA.get(estrategia, _DESCRIPCION_ESTRATEGIA[ESTRATEGIAS[0]])


def _env_int(nombre: str, defecto: int) -> int:
    try:
        valor = int(os.environ.get(nombre, ""))
    except (TypeError, ValueError):
        return defecto
    return valor if valor > 0 else defecto


def _env_float(nombre: str, defecto: float) -> float:
    try:
        valor = float(os.environ.get(nombre, ""))
    except (TypeError, ValueError):
        return defecto
    return valor if valor > 0 else defecto


def configuracion_plan_b() -> dict:
    """Parámetros del Plan B (H7), configurables por entorno.

    - ``AGENTES_PLAN_B_MAX_INTENTOS`` (por defecto 3)
    - ``AGENTES_PLAN_B_MAX_SEGUNDOS`` (por defecto 300)
    """
    return {
        "max_intentos": _env_int("AGENTES_PLAN_B_MAX_INTENTOS", 3),
        "presupuesto_seg": _env_float("AGENTES_PLAN_B_MAX_SEGUNDOS", 300.0),
    }


# ============================================================
# FIRMAS (ANTI-REPETICIÓN)
# ============================================================

def _hash(texto: str) -> str:
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


# Campos de un agente que definen su "esencia" para la firma. Los volátiles
# (estado, resultado, duración) quedan fuera.
_CAMPOS_FIRMA_AGENTE = (
    "tipo", "nombre", "dependencias_nombres", "operacion_file",
    "archivo_origen", "archivo_destino", "url_http", "metodo_http",
    "query_search", "fuente_items", "codigo_python", "codigo_por_item",
    "prompt_llm", "comando_shell",
)


def firma_agente(agente: Any) -> str:
    """Firma de un agente: tipo + nombre + dependencias + configuración."""
    partes = []
    for campo in _CAMPOS_FIRMA_AGENTE:
        valor = getattr(agente, campo, None)
        if valor in (None, "", [], {}):
            continue
        if campo in ("codigo_python", "codigo_por_item", "prompt_llm", "comando_shell"):
            valor = str(valor)[:2000]
        elif not isinstance(valor, (str, int, float, bool)):
            valor = json.dumps(valor, sort_keys=True, default=str)[:500]
        partes.append(f"{campo}={valor}")
    return _hash("|".join(partes))[:16]


def firma_plan(plan: Any) -> str:
    """Firma de un plan completo: agentes/pasos, tipos, dependencias y config.

    Dos planes con la misma firma son «esencialmente el mismo plan»: si uno
    falló, el otro no debe ejecutarse.
    """
    firmas = []
    agentes = getattr(plan, "agentes_generados", None) or []
    if agentes:
        for agente in agentes:
            firmas.append(firma_agente(agente))
    else:
        for paso in getattr(plan, "pasos", None) or []:
            tipo = getattr(paso, "tipo_agente", "")
            nombre = getattr(paso, "nombre", "")
            deps = ",".join(getattr(paso, "dependencia_ids", []) or [])
            try:
                config = json.dumps(
                    getattr(paso, "configuracion", {}) or {}, sort_keys=True, default=str
                )[:1000]
            except (TypeError, ValueError):
                config = str(getattr(paso, "configuracion", ""))[:1000]
            firmas.append(_hash(f"{tipo}|{nombre}|{deps}|{config}")[:16])
    return _hash("::".join(firmas))[:16]


# ============================================================
# REGISTRO DE REPARACIONES (reparaciones_plan)
# ============================================================

def registrar_reparacion(
    db_path: str,
    *,
    problema: str,
    intento: int,
    agente: str = "",
    error: str = "",
    estrategia: str = "",
    plan_firma: str = "",
    resultado: str = "",
    exito: bool = False,
    ejecucion_id: int | None = None,
) -> bool:
    """Guarda un intento de reparación en ``reparaciones_plan``.

    Es best-effort: nunca debe romper la recuperación. Si la tabla no existe
    (BD muy antigua) simplemente no registra.
    """
    if not db_path:
        return False
    try:
        with closing(sqlite3.connect(db_path, timeout=10)) as conn:
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute(
                """INSERT INTO reparaciones_plan (
                       problema, tipo, fecha, ejecucion_id, intento, agente,
                       error, estrategia, plan_firma, resultado, exito
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    (problema or "")[:2000],
                    estrategia or "desconocida",
                    datetime.now().isoformat(),
                    ejecucion_id,
                    int(intento or 0),
                    (agente or "")[:200],
                    (error or "")[:2000],
                    estrategia or "",
                    plan_firma or "",
                    (resultado or "")[:2000],
                    1 if exito else 0,
                ),
            )
            conn.commit()
        return True
    except Exception as e:
        logger.debug(f"No se pudo registrar la reparación: {e}")
        return False


# ============================================================
# CONTEXTO ESTRUCTURADO DEL ERROR (H7)
# ============================================================

def formatear_error_estructurado(
    agente_fallido: Any,
    error: str,
    estrategia: str,
    errores_previos: list[dict] | None = None,
) -> str:
    """Bloque legible con el error y lo YA INTENTADO, para el prompt del LLM.

    El mensaje no es un texto suelto: describe el paso, su tipo, el error real,
    lo que ya se probó y qué NO repetir. Así el siguiente plan diversifica de
    verdad en lugar de reescribir lo mismo.
    """
    tipo = getattr(getattr(agente_fallido, "tipo", None), "value", "?")
    nombre = getattr(agente_fallido, "nombre", "?")
    lineas = [
        "INFORMACIÓN ESTRUCTURADA DEL FALLO:",
        f"- PASO: {nombre}",
        f"- TIPO: {tipo}",
        f"- ERROR: {(error or '(sin error registrado)')[:500]}",
        f"- ESTRATEGIA OBLIGATORIA DE ESTE INTENTO: {estrategia}",
        f"  ({descripcion_estrategia(estrategia)})",
        "- NO REPETIR: la misma estrategia sin modificación.",
    ]
    if errores_previos:
        lineas.append("- YA INTENTADO (no lo repitas):")
        for prev in errores_previos[-5:]:
            lineas.append(
                f"    · intento {prev.get('intento', '?')} "
                f"[{prev.get('estrategia', '?')}] "
                f"{prev.get('agente', '?')}: "
                f"{str(prev.get('error', ''))[:180]}"
            )
    return "\n".join(lineas)


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

INFORMACIÓN ESTRUCTURADA DEL FALLO (H7):
{error_estructurado}

CONFIGURACIÓN DEL PASO QUE FALLÓ (no la repitas tal cual):
{codigo_fallido}

REGLA CRÍTICA E INVIOLABLE:
- NO repitas los mismos pasos del plan fallido.
- NO uses el mismo enfoque que llevó al error.
- Si el paso falló por "{error}", busca una forma DISTINTA de conseguir
  el mismo objetivo. Cambia de estrategia, de fuentes de datos, de
  librerías, de estructura.
- Si un paso requería una API externa que falló, intenta otra fuente
  o datos sintéticos/alternativos.
- Distingue errores de SINTAXIS (el código no compila) de errores de
  CONTRATO en tiempo de ejecución (una variable vacía, un formato de
  archivo no aceptado, una clave que el productor no devuelve, un artefacto
  que no cumple su contrato de aceptación). Un error de contrato NO se
  arregla reescribiendo lo mismo: cambia cómo se obtiene o se transforma el
  dato.
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
      "tipo": "Python|Shell|HTTP|LLM|File|Loop|Browser|Search",
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

- **Browser**: {{"url": "https://...", "acciones": [{{"tipo": "esperar",
                "selector": "CSS", "timeout": 10000}}, {{"tipo": "extraer",
                "selector": "CSS", "formato": "html|text|attr",
                "nombre": "clave", "atributo": "href", "multiple": false}}],
                "timeout": 30, "timeout_accion": 10000, "headless": true,
                "bloquear_recursos": false, "user_agent": ""}}
  Acciones válidas: esperar, extraer, click, rellenar, scroll, screenshot,
  ejecutar_js, navegar. PROHIBIDO usar "direccion", "link", "steps".
  Para páginas HTML (no APIs JSON); HTTP solo sirve para APIs JSON.

- **Search**: {{"query": "...", "max_resultados": 5, "region": "wt-wt",
              "timeout": 30}}
  PROHIBIDO usar "busqueda", "term", "keywords".

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
        *,
        estrategia: str | None = None,
        errores_previos: list[dict] | None = None,
        firmas_fallidas: set[str] | None = None,
        intento: int = 1,
    ) -> Any | None:
        """Pide al LLM un plan alternativo y lo devuelve como ExecutionPlan.

        Args:
            estrategia: estrategia de la escalera (H7) que debe seguir el plan.
                Si es ``None`` se deriva del número de intento.
            errores_previos: traza de intentos anteriores para el bloque
                estructurado y la lista de "no repetir".
            firmas_fallidas: firmas de planes que ya fallaron. Si el plan
                generado tiene la misma firma, se reintenta UNA vez con una
                instrucción explícita y, si vuelve a repetirse, se descarta
                para pedir otra estrategia.
            intento: número de intento (1-based).

        Devuelve None si algo falla o si el plan repite una firma fallida.
        """
        estrategia = estrategia or estrategia_para_intento(intento)

        def _aceptar(plan: Any) -> Any | None:
            """Descarta un plan cuya firma ya falló antes (anti-repetición)."""
            if plan is None:
                return None
            firma = firma_plan(plan)
            if firmas_fallidas and firma in firmas_fallidas:
                logger.warning(
                    f"PlanRecovery: el plan generado repite la firma {firma} "
                    f"de un plan que YA falló → se descarta y se pedirá otra "
                    f"estrategia"
                )
                return None
            return plan

        try:
            plan_resumen = self._resumir_plan_fallido(plan_fallido)
            lecciones = self._obtener_lecciones()

            tipo = getattr(getattr(agente_fallido, "tipo", None), "value", "?")
            nombre = getattr(agente_fallido, "nombre", "?")
            codigo_fallido = self._resumen_agente_fallido(agente_fallido)
            error_estructurado = formatear_error_estructurado(
                agente_fallido, error, estrategia, errores_previos
            )
            prompt = PROMPT_PLAN_B.format(
                problema=problema_original,
                plan_resumen=plan_resumen,
                agente_fallido=nombre,
                tipo_agente=tipo,
                error=(error or "")[:500],
                error_estructurado=error_estructurado,
                codigo_fallido=codigo_fallido,
                lecciones=lecciones or "(sin lecciones relevantes)",
            )
            if firmas_fallidas:
                prompt += (
                    f"\n\n⚠️ ANTI-REPETICIÓN: {len(firmas_fallidas)} plan(es) "
                    f"anterior(es) con este objetivo ya fallaron. No generes un "
                    f"plan equivalente: cambia de estrategia "
                    f"({estrategia}).\n"
                )

            logger.info(
                f"🔧 PlanRecovery: pidiendo plan alternativo al LLM "
                f"(estrategia={estrategia}, intento={intento})..."
            )
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

            # 9. Validar el plan alternativo (sintaxis + contratos + estructura)
            ok, errores = self._validar_plan_b(plan_b)
            if not ok:
                logger.warning(
                    f"PlanRecovery: primera versión del plan tiene errores "
                    f"en {len(errores)} agente(s):"
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
                    estrategia=estrategia,
                )

                if plan_b_corregido is not None:
                    logger.info(
                        f"✅ PlanRecovery: segunda versión validada con "
                        f"{len(plan_b_corregido.agentes_generados)} agentes"
                    )
                    return _aceptar(plan_b_corregido)

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
            return _aceptar(plan_b)

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
        estrategia: str | None = None,
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
=== CORRECCIÓN DE ERRORES DEL PLAN ALTERNATIVO ===

El plan anterior falló en EJECUCIÓN con este error real:

{(error or "(sin error registrado)")[:500]}

Y la validación previa detectó además estos problemas:

{lista_errores}

⚠️ INSTRUCCIONES PARA CORREGIR (MUY IMPORTANTE):

0. **No repitas el enfoque que falló**. Si el error es de contrato en
   tiempo de ejecución (contenido vacío, formato de imagen no aceptado,
   clave que la dependencia no devuelve), cambia CÓMO se obtiene o se
   transforma el dato; reescribir lo mismo solo vuelve a fallar.

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

Genera el plan COMPLETO de nuevo, CORRIGIENDO la sintaxis Y el contrato
que provocó el error de ejecución. No repitas el paso tal cual.

Empieza directamente con {{. NO escribas explicaciones antes del JSON.
"""

        # Reconstruir el prompt completo del plan B + la corrección
        prompt_completo = PROMPT_PLAN_B.format(
            problema=problema_original,
            plan_resumen=plan_resumen,
            agente_fallido=nombre,
            tipo_agente=tipo,
            error=(error or "")[:500],
            error_estructurado=formatear_error_estructurado(
                agente_fallido, error, estrategia or ESTRATEGIAS[0]
            ),
            codigo_fallido=self._resumen_agente_fallido(agente_fallido),
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

            # Validar DE NUEVO (sintaxis + contratos + estructura)
            ok, errores = self._validar_plan_b(plan_b)
            if not ok:
                logger.warning(
                    f"PlanRecovery reintento: la segunda versión también "
                    f"tiene errores en {len(errores)} agente(s):"
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
        Detecta SyntaxError, NameError, json.loads placeholder, {{X}},
        nombres libres sin definir y claves fuera del contrato del productor.
        """
        from core.problem_solver.validator import PlanValidator

        errores = []
        agentes = getattr(plan, "agentes_generados", []) or []
        nombres_agentes = {a.nombre for a in agentes}
        tipos_agentes = {
            a.nombre: getattr(getattr(a, "tipo", None), "value", "?")
            for a in agentes
        }

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
                        tipos_agentes=tipos_agentes,
                    )
                )

        return (len(errores) == 0, errores)

    def _validar_plan_b(self, plan: Any) -> tuple[bool, list[str]]:
        """Valida el plan alternativo con el MISMO validador que el inicial.

        Antes solo se comprobaba la sintaxis del código Python, así que un
        Plan B podía repetir un contrato roto (variable sin resolver, clave
        inexistente, ``File`` sin fuente de contenido) y volver a fallar en
        ejecución. Ahora se aplica ``PlanValidator.validar_plan`` completo.
        """
        ok, errores = self._validar_sintaxis_agentes(plan)

        validador = getattr(self.problem_solver, "validator", None)
        if validador is not None:
            try:
                ok_plan, errores_plan = validador.validar_plan(plan)
                if not ok_plan:
                    ok = False
                    for error in errores_plan:
                        if error not in errores:
                            errores.append(error)
            except Exception as e:
                logger.debug(f"PlanRecovery: validación completa no disponible: {e}")

        return (ok, errores)

    @staticmethod
    def _resumen_agente_fallido(agente: Any) -> str:
        """Configuración legible del agente que falló, para el prompt.

        Sin esto el LLM no ve QUÉ código falló y tiende a repetirlo.
        """
        if agente is None:
            return "(no disponible)"
        campos = (
            "codigo_python", "codigo_por_item", "prompt_llm", "comando_shell",
            "url_http", "query_search", "operacion_file", "archivo_origen",
            "archivo_destino", "fuente_items",
        )
        partes = []
        for campo in campos:
            valor = getattr(agente, campo, None)
            if valor in (None, "", [], {}):
                continue
            texto = valor if isinstance(valor, str) else str(valor)
            partes.append(f"- {campo}: {texto[:800]}")
        return "\n".join(partes) if partes else "(sin configuración legible)"

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
