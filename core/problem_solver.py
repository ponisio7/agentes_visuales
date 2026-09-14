# core/problem_solver.py
"""
Problem Solver: Descompone problemas complejos en una orquestación de agentes.
Usa IA para analizar el problema, planificar pasos y generar el DAG de agentes.

VERSIÓN FINAL (v3 - 2026-09):
- ✅ Prompt unificado y robusto con reglas explícitas de campos
- ✅ Validación de campos desconocidos en 'configuracion' por tipo
- ✅ Corrector automático de código Python (PythonCodeCorrector)
- ✅ Normalizador de nombres de archivos (FileNameNormalizer)
- ✅ Builder de prompts modular (PromptBuilder)
- ✅ Parsing robusto de JSON con múltiples estrategias
- ✅ Fallback inteligente con código correcto
- ✅ Zero hardcodeo: todo configurable vía constantes
- ✅ Defensa en profundidad: prevención + corrección + fallback seguro
- ✅ Helpers por tipo de agente (código mantenible)
- ✅ Propaga advertencias al ExecutionPlan (visibles en la UI)
- ✅ Regla anti "formato binario a mano" (PDF/DOCX/XLSX generados
  concatenando bytes en Python) para evitar documentos corruptos
"""
import json
import re
import logging
import uuid
import sys
import time
import os
from typing import Dict, List, Optional, Tuple, Any, Set
from dataclasses import dataclass, field
from enum import Enum

from core.agent import Agente, TipoAgente
from core.llm_client import LLMClient, LLMError
from core.utils import extraer_json_de_llm

from core.plan_repairs import (
    detectar_requisitos_no_cumplidos,
    aplicar_parche,
    construir_instruccion_regeneracion,
)

# Configurar logger
logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTES Y ENUMERACIONES
# ============================================================

class PlanStatus(Enum):
    """Estado del plan generado."""
    DRAFT = "draft"
    VALIDATED = "validated"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"


class PlanComplexity(Enum):
    """Nivel de complejidad del plan."""
    SIMPLE = "🟢 Simple"
    MODERATE = "🟡 Moderada"
    COMPLEX = "🟠 Compleja"
    VERY_COMPLEX = "🔴 Muy compleja"


# ============================================================
# MAPA CENTRALIZADO DE CAMPOS VÁLIDOS POR TIPO
# ============================================================
# Fuente única de verdad. Se usa para:
#   1. Validar que el LLM no invente campos en 'configuracion'.
#   2. Limpiar campos desconocidos antes de construir el Agente.
#
# ⚠️ IMPORTANTE: para agentes File NO se incluye 'contenido'.
# El contenido se obtiene automáticamente del resultado de la
# dependencia declarada; nunca se configura manualmente.
# ============================================================
CAMPOS_VALIDOS_POR_TIPO: Dict[str, Set[str]] = {
    "Python": {"codigo", "timeout"},
    "Shell":  {"comando", "timeout", "working_dir"},
    "HTTP":   {"url", "metodo", "headers", "body", "timeout"},
    "LLM":    {
        "prompt", "modelo", "temperatura", "max_tokens",
        "reasoning_effort", "thinking_enabled",
    },
    "File":   {"operacion", "archivo_origen", "archivo_destino", "modo_salida_file"},
    "Loop":   {
        "fuente_items", "codigo_por_item", "max_iteraciones",
        "timeout_loop", "timeout_python", "continuar_en_error",
    },
}

# ============================================================
# CONFIGURACIÓN DEL PROMPT (modular)
# ============================================================

class PromptBuilder:
    """
    Construye prompts para el LLM de forma modular y configurable.
    """
    DOCX_IMAGE_RULES = [
        "**REGLA OBLIGATORIA**: Si el usuario pide un documento con imágenes "
        "(menciona 'imágenes', 'ilustraciones', 'con imágenes', 'con dibujos', "
        "'con N imágenes', etc.), DEBES incluir en el plan TODOS estos pasos. "
        "NO basta con generar solo el texto del documento.",

        "**ESTRUCTURA OBLIGATORIA** para 'cuento con N imágenes':",
        "  1. `GenerarCuento` (LLM): produce el cuento Y N descripciones de imágenes.",
        "     `resultado = {'cuento': '...', 'descripciones_imagenes': ['desc1', 'desc2', ...]}`",
        "  2. `GenerarURLs` (Python, depende de GenerarCuento): genera N URLs de imágenes.",
        "     El código debe ser EXACTAMENTE:",
        "     `resultado = {'urls': [f'https://picsum.photos/800/600?random={i}' for i in range(N)]}`",
        "     (donde N es el número de imágenes que pidió el usuario).",
        "  3. `PrepararDocumento` (Python, depende de GenerarCuento y GenerarURLs):",
        "     combina el cuento con las URLs y descripciones.",
        "     `resultado = {'titulo': '...', 'cuento': '...', 'imagenes': [{'url': u, 'descripcion': d} for u, d in zip(urls, descripciones)]}`",
        "  4. `EscribirDOCX` (File, depende de PrepararDocumento): escribe el .docx.",
        "     `configuracion = {'operacion': 'escribir', 'archivo_destino': '<nombre>.docx'}`",

        "**PROHIBIDO USAR placehold.co**: genera imágenes grises de relleno. "
        "Para imágenes reales usa `https://picsum.photos/800/600?random=N` (público, sin auth).",

        "**NO HAGAS DESCARGA HTTP DENTRO DE UN AGENTE Python/Loop**: el sandbox no "
        "tiene red fiable y los timeouts son cortos. El agente `File` de DOCX es "
        "quien descarga las imágenes si le pasas URLs en 'imagenes'.",

        "**NO INVENTES APIs CON AUTH**: no uses `api.unsplash.com` ni otras APIs "
        "que requieren API key. Usa solo APIs públicas sin autenticación.",
    ]

    # Reglas para generación de código Python
    PYTHON_RULES = [
        "**ENTRADA**: Los datos de dependencias están SIEMPRE en el diccionario `contexto`.",
        "  - Ejemplo: `contexto.get('NombreDependencia', {})`",
        "  - Si la dependencia es HTTP, su resultado está en `contexto['NombreDependencia']['json']` (ya parseado)",
        "  - Si necesitas el body crudo, usa `contexto['NombreDependencia']['body']`",
        "**SALIDA**: El código DEBE asignar el resultado final a la variable `resultado`.",
        "  - ¡NO uses `print()`! Usa `resultado = {...}`",
        "**NUNCA** uses variables como 'respuesta', 'data', 'result', 'response' o 'json_data' que no hayan sido definidas.",
        "**SIEMPRE** usa `contexto.get('NombreAgente', {})` para acceder a dependencias.",
        "**CONSISTENCIA DE DATOS ENTRE AGENTES**: Cuando dos dependencias devuelven listas "
        "paralelas que deben cruzarse (ej: una lista de productos y otra de descripciones), "
        "SIEMPRE cruza por `id` usando `str()` en AMBOS lados para evitar mismatch int/str. "
        "Si el cruce por id falla, usa el ÍNDICE como fallback. NUNCA inventes claves nuevas: "
        "usa las claves que YA existen en el contexto.",
        "**GENERACIÓN DE HTML**: Si el problema requiere generar una página web, el agente "
        "final DEBE devolver `resultado = {'html': '<!DOCTYPE html>...'}`. "
        "El HTML va dentro de un dict con la clave EXACTA `html`, nunca como string suelto.",
        "**GENERACIÓN DE MARKDOWN**: Análogo, `resultado = {'markdown': '# Título\\n...'}`.",
        "**EJEMPLO CORRECTO**:",
        "  ```python",
        "  datos_http = contexto.get('ConsultarClima', {})",
        "  datos = datos_http.get('json', {})",
        "  resultado = {",
        "      'temperatura': datos.get('current_weather', {}).get('temperature', 'N/A')",
        "  }",
        "  ```",
        # Contrato de salida de cada tipo (para que el LLM sepa qué claves existen)
        "**CONTRATO DE SALIDA HTTP**: dict con 'status_code', 'headers', 'body', 'json', 'url'",
        "**CONTRATO DE SALIDA LLM**: dict con 'respuesta', 'respuesta_limpia', 'json', 'modelo', 'tokens_uso'",
        "**CONTRATO DE SALIDA SHELL**: dict con 'codigo', 'stdout', 'stderr'",
        "**CONTRATO DE SALIDA FILE (leer)**: dict con 'contenido', 'archivo', 'tamaño', 'json' (si aplica)",
        "**CONTRATO DE SALIDA LOOP**: dict con 'items', 'total_items', 'exitos', 'errores'",
    ]

    # Tipos de agentes disponibles
    AGENT_TYPES = {
        "Python": "Procesamiento de datos, transformaciones, lógica compleja, cálculos.",
        "Shell": "Comandos de terminal, operaciones de sistema, scripts.",
        "HTTP": "Obtener datos de APIs REST, web scraping, integraciones.",
        "LLM": "Análisis de texto, clasificación, resumen, generación de contenido, traducción.",
        "File": "Lectura/escritura de archivos, persistencia de resultados.",
        "Loop": "Procesamiento en lote sobre una lista de items.",
    }

    # ✅ NUEVO: Contratos de salida de cada tipo de agente.
    # Fuente única de verdad sobre qué claves devuelve cada executor.
    CONTRATOS_SALIDA = {
        "Python": {
            "descripcion": "El código Python debe asignar 'resultado' (dict).",
            "claves": "(las que tú definas en 'resultado')",
        },
        "Shell": {
            "descripcion": "El executor devuelve un dict con el resultado del comando.",
            "claves": {
                "codigo": "int - código de salida (0 = éxito)",
                "stdout": "str - salida estándar",
                "stderr": "str - salida de error",
            },
        },
        "HTTP": {
            "descripcion": "El executor devuelve la respuesta HTTP procesada.",
            "claves": {
                "status_code": "int - código HTTP",
                "headers": "dict - cabeceras de la respuesta",
                "body": "str - cuerpo crudo",
                "json": "dict|list|None - cuerpo parseado si es JSON",
                "url": "str - URL final (tras redirecciones)",
                "elapsed": "float - tiempo en segundos",
            },
        },
        "LLM": {
            "descripcion": "El executor devuelve la respuesta del modelo.",
            "claves": {
                "modelo": "str - modelo usado",
                "respuesta": "str - respuesta cruda",
                "respuesta_limpia": "str - sin fences markdown",
                "json": "dict|None - respuesta parseada si es JSON",
                "tokens_uso": "dict - {prompt, completion, total}",
                "tiempo_respuesta": "float - segundos",
            },
        },
        "File": {
            "descripcion": "El executor devuelve info del archivo según operación.",
            "claves": {
                "archivo": "str - ruta",
                "tamaño": "int - bytes",
                "contenido": "str - contenido (si operacion='leer')",
                "json": "dict|None - parseado si el archivo es JSON",
                "caracteres_escritos": "int - (si operacion='escribir')",
            },
        },
        "Loop": {
            "descripcion": "El executor devuelve un resumen del bucle.",
            "claves": {
                "items": "list - lista con el resultado de cada item",
                "total_items": "int - número total de items",
                "exitos": "int - items procesados con éxito",
                "errores": "int - items con error",
                "duracion_total": "float - segundos",
            },
        },
    }

    SHELL_RULES = [
        "**PRIVILEGIOS**: El usuario ejecuta SIN privilegios de root.",
        "Si el comando necesita root (apt, systemctl, mount...), NO lo escribas "
        "sin elevación. El executor aplicará pkexec automáticamente, pero es "
        "más claro si tú ya lo indicas en la descripción del paso.",
        "NUNCA uses 'sudo' explícito en el comando: deja que el executor decida "
        "el método de elevación (pkexec en GUI, sudo -n en desatendido).",
        "Si el comando puede fallar por permisos y es crítico, añade un paso "
        "previo de diagnóstico (Python) que verifique el entorno.",
        # ✅ NUEVO: Contrato de resultado del agente Shell
        "**CONTRATO DE SALIDA SHELL**: Un agente Shell siempre devuelve un dict con:",
        "  - 'codigo' (int): código de salida del comando (0 = éxito)",
        "  - 'stdout' (str): salida estándar",
        "  - 'stderr' (str): salida de error",
        "  Para acceder al código de salida desde Python: "
        "contexto['NombreShell'].get('codigo', -1) == 0",
        "**NUNCA uses 'returncode' ni 'codigo_salida'**: no existen en la API.",
    ]

    # Configuraciones por tipo
    # ⚠️ Solo las claves aquí listadas son aceptadas en 'configuracion'.
    TYPE_CONFIGS = {
        "Python": {
            "codigo": "código Python que asigna la variable 'resultado'",
            "timeout": 30,
        },
        "Shell": {
            "comando": "comando shell",
            "timeout": 30,
            "working_dir": "",
        },
        "HTTP": {
            "url": "URL",
            "metodo": "GET|POST|PUT|DELETE",
            "headers": {},
            "body": "",
            "timeout": 30,
        },
        "LLM": {
            "prompt": "instrucción para el modelo",
            "modelo": "deepseek-v4-flash",
            "temperatura": 0.7,
            "max_tokens": 4000,
            "reasoning_effort": "low",
            "thinking_enabled": False,
        },
        "File": {
            "operacion": "leer|escribir|copiar|mover|eliminar",
            "archivo_origen": "",
            "archivo_destino": "",
            "modo_salida_file": "auto|contenido|json|texto",
        },
        "Loop": {
            "fuente_items": "Dependencia.clave",
            "codigo_por_item": "código para cada item",
            "max_iteraciones": 100,
            "timeout_loop": 300,
            "timeout_python": 30,
            "continuar_en_error": False,
        },
    }

    # Reglas de diseño
    DESIGN_RULES = [
        "**Atomicidad**: Cada paso debe tener UNA sola responsabilidad.",
        "**DAG Válido**: Las dependencias deben formar un grafo acíclico dirigido.",
        "**Nombres Descriptivos**: Usa nombres como 'ObtenerDatos', 'ProcesarResultados'.",
        "**Código Funcional**: El código Python debe ser sintácticamente correcto.",
        "**APIs Reales**: Usa URLs reales de APIs públicas (ej: Open-Meteo, GitHub API, JSONPlaceholder).",
        "**Prompts Claros**: Para LLM, escribe prompts específicos con formato de salida.",
        "**Justificación**: Explica brevemente POR QUÉ cada paso es necesario.",
    ]

    # Reglas específicas sobre campos de 'configuracion'
    FIELD_RULES = [
        "Usa EXCLUSIVAMENTE los campos listados arriba en 'CONFIGURACIONES POR TIPO'.",
        "**NUNCA** inventes campos nuevos ni añadas campos que no estén en la lista.",
        "**NUNCA** añadas un campo 'contenido' en agentes File: el contenido "
        "se toma automáticamente del resultado de la dependencia declarada.",
        "**NUNCA** añadas sintaxis de plantilla como '{{Dependencia.clave}}' en "
        "los valores de 'configuracion': el sistema resuelve dependencias "
        "automáticamente en tiempo de ejecución.",
    ]

    # ✅ NUEVO: Reglas anti "formato binario a mano" (PDF, DOCX, XLSX, etc.)
    # Evita que el LLM genere estos formatos concatenando bytes/strings en
    # Python: un PDF tiene offsets de xref, longitudes de stream y checksums
    # que el LLM no calcula bien, y el resultado es un archivo corrupto o
    # un "doble PDF" cuando ese texto se pasa después a un agente File.
    BINARY_FORMAT_RULES = [
        "**NUNCA** generes un PDF, .docx o .xlsx 'a mano' concatenando bytes "
        "o strings en un agente Python (ej: `contenido = '%PDF-1.4\\n...'`). "
        "Es prácticamente imposible acertar con los offsets de xref, las "
        "longitudes de stream y los checksums exactos que exige el formato.",
        "**Patrón CORRECTO** para generar un documento (PDF, Word, etc.): "
        "1) un agente Python o LLM genera el CONTENIDO como texto plano o "
        "Markdown (`resultado = {'contenido': 'Hola mundo'}`); 2) un agente "
        "File con `operacion: \"escribir\"` y `archivo_destino` con la "
        "extensión correcta recibe ese texto: el sistema hace la conversión "
        "real al formato binario según la extensión.",
        "**NUNCA** pases a un agente File un contenido que YA es un PDF/DOCX "
        "'a mano' (empieza por `%PDF-` o similar): el agente File lo tratará "
        "como texto plano y lo envolverá en un documento nuevo, produciendo "
        "un archivo corrupto o inútil.",
        "**LIBRERÍAS DE PDF DISPONIBLES**: si necesitas generar un PDF "
        "directamente en Python (caso excepcional), usa `reportlab` (ya "
        "instalada). NO uses `fpdf` ni `weasyprint` ni `pdfkit`: no están "
        "disponibles en este entorno.",
        "**PARA .xlsx**: el contenido debe ser una **lista de dicts** "
        "(una entrada por fila, con las claves como cabeceras) o una "
        "**lista de listas** (la primera fila como cabeceras). NUNCA "
        "un string CSV con comas y saltos de línea: el sistema no lo "
        "parsea, lo escribirá todo en una única celda.",
        "**EJEMPLO CORRECTO para nómina**:",
        "```python",
        "resultado = {'contenido': [",
        "    {'Nombre': 'Ana García', 'Salario': 2450, 'Enero': 20, 'Febrero': 19},",
        "    {'Nombre': 'Carlos Ruiz', 'Salario': 3120, 'Enero': 18, 'Febrero': 21},",
        "]}",
        "```",
    ]

    @classmethod
    def build_system_prompt(cls) -> str:
        """
        Construye el system prompt completo para el LLM.

        Ensambla de forma modular:
          - Descripción de tipos de agentes
          - Configuraciones válidas por tipo
          - Reglas de diseño generales
          - Reglas sobre campos de 'configuracion'
          - Reglas específicas para Python
          - Reglas específicas para Shell (privilegios)
          - Contratos de salida de cada tipo (claves exactas)
          - Contratos de HTML/Markdown y cruce de datos
          - Instrucción de formato JSON estricta
        """
        # ── 1. Descripción de tipos de agentes ──
        types_desc = "\n".join(
            f"### {tipo}\n- **Uso**: {desc}"
            for tipo, desc in cls.AGENT_TYPES.items()
        )

        # ── 2. Configuraciones válidas por tipo ──
        configs_desc = "\n".join(
            f"- **{tipo}**: {json.dumps(config, ensure_ascii=False)}"
            for tipo, config in cls.TYPE_CONFIGS.items()
        )

        # ── 3. Reglas formateadas ──
        python_rules = "\n".join(f"  - {rule}" for rule in cls.PYTHON_RULES)
        shell_rules  = "\n".join(f"  - {rule}" for rule in cls.SHELL_RULES)
        design_rules = "\n".join(
            f"{i+1}. {rule}" for i, rule in enumerate(cls.DESIGN_RULES)
        )
        field_rules = "\n".join(
            f"{i+1}. {rule}" for i, rule in enumerate(cls.FIELD_RULES)
        )
        binary_format_rules = "\n".join(
            f"  - {rule}" for rule in cls.BINARY_FORMAT_RULES
        )
        docx_image_rules = "\n".join(f"  - {rule}" for rule in cls.DOCX_IMAGE_RULES)
        # ── 4. Contratos de salida por tipo (claves exactas) ──
        contratos = cls._formatear_contratos()

        # ── 5. Contratos específicos HTML/Markdown y cruce de datos ──
        contrato_html = """
## ⚠️ CONTRATO DE SALIDA PARA HTML / MARKDOWN ⚠️

Si el problema requiere generar una página HTML o un documento Markdown:
- El agente generador DEBE devolver: `resultado = {"html": "<!DOCTYPE html>..."}`
  (o `{"markdown": "# Título..."}` según corresponda)
- NO devuelvas el HTML/Markdown como string suelto: SIEMPRE dentro de un dict
  con clave EXACTA `html` o `markdown`.
- NO añadas claves adicionales al dict de resultado de HTML/Markdown.
- El agente File que escribe el archivo recibirá ese dict y extraerá el string
  automáticamente; tú solo preocúpate por entregarlo con la clave correcta.

## ⚠️ CONTRATO DE CRUCE DE DATOS ENTRE AGENTES ⚠️

Cuando dos agentes producen listas paralelas que deben unirse:
- Cruza por `id` comparando con `str()` en AMBOS lados.
- Si el cruce por `id` no encuentra coincidencia, usa el ÍNDICE como fallback.
- NUNCA dejes un `None` silencioso: si no hay match, usa el dato original como fallback.
"""

        # ── 6. Ensamblar el prompt final ──
        return f"""Eres un arquitecto experto en sistemas de agentes automatizados.

Tu trabajo es analizar un problema complejo y descomponerlo en una orquestación de agentes ejecutables.

## TIPOS DE AGENTES DISPONIBLES

{types_desc}

## CONFIGURACIONES POR TIPO

{configs_desc}

## REGLAS DE DISEÑO

{design_rules}

## ⚠️ REGLAS SOBRE CAMPOS DE 'configuracion' ⚠️

{field_rules}

## ⚠️ REGLA CRÍTICA SOBRE FORMATOS BINARIOS (PDF, DOCX, XLSX) ⚠️

{binary_format_rules}

## ⚠️ REGLAS CRÍTICAS PARA DOCUMENTOS CON IMÁGENES ⚠️

{docx_image_rules}

## ⚠️ REGLAS CRÍTICAS PARA CÓDIGO PYTHON ⚠️

{python_rules}

## ⚠️ REGLAS CRÍTICAS PARA COMANDOS SHELL ⚠️

{shell_rules}

## ⚠️ CONTRATOS DE SALIDA DE CADA TIPO DE AGENTE ⚠️

Estas son las claves EXACTAS que devuelve cada tipo de agente al ejecutarse.
Úsalas en el código Python que generes para acceder a los datos de las
dependencias. **NUNCA inventes nombres de claves** que no aparezcan aquí.

Ejemplos de errores comunes a EVITAR:
- ❌ `contexto['Shell1'].get('returncode')` → ✅ usa `contexto['Shell1'].get('codigo')`
- ❌ `contexto['Shell1'].get('codigo_salida')` → ✅ usa `contexto['Shell1'].get('codigo')`
- ❌ `contexto['HTTP1'].get('data')` → ✅ usa `contexto['HTTP1'].get('json')`
- ❌ `contexto['LLM1'].get('texto')` → ✅ usa `contexto['LLM1'].get('respuesta')`

{contratos}

{contrato_html}

## ⚠️ INSTRUCCIÓN CRÍTICA DE FORMATO ⚠️

Debes responder **ÚNICAMENTE** con un objeto JSON válido.
- NO añadas texto antes del JSON
- NO añadas texto después del JSON
- NO uses markdown (```json)
- NO uses comentarios dentro del JSON
- Asegúrate de que TODAS las comillas sean dobles (")
- Asegúrate de que NO haya comas finales

## FORMATO DE RESPUESTA OBLIGATORIO

{{
  "titulo": "Título descriptivo del plan",
  "analisis": "Análisis del problema (2-3 frases)",
  "estimacion_tiempo_segundos": 30,
  "pasos": [
    {{
      "orden": 1,
      "nombre": "NombreDelAgente",
      "descripcion": "Qué hace este paso",
      "tipo": "Python|Shell|HTTP|LLM|File|Loop",
      "dependencias": ["NombreAgente1"],
      "configuracion": {{
        // SOLO campos listados arriba para este tipo
      }},
      "justificacion": "Por qué este paso es necesario"
    }}
  ]
}}

RESPONDE SOLO CON EL JSON, NADA MÁS."""

    @classmethod
    def build_user_prompt(
        cls,
        problema: str,
        contexto_extra: Optional[Dict] = None,
        max_pasos: int = 10,
        nivel_detalle: str = "normal"
    ) -> str:
        """Construye el prompt del usuario."""
        prompt = f"""PROBLEMA A RESOLVER:
{problema}

RESTRICCIONES:
- Máximo {max_pasos} pasos
- Cada paso debe ser atómico y ejecutable
- Las dependencias deben ser claras y formar un DAG válido

NIVEL DE DETALLE: {nivel_detalle}

"""
        if contexto_extra:
            prompt += f"\nCONTEXTO ADICIONAL:\n{json.dumps(contexto_extra, indent=2, ensure_ascii=False)}\n"

        prompt += """
            ANTES DE RESPONDER, verifica tu plan contra estos puntos:
            1. ¿He incluido un paso para CADA requisito explícito del problema?
            2. Si el usuario pidió "N imágenes", ¿hay un paso que genere exactamente N URLs?
            3. ¿El paso final de File (si aplica) recibe un dict con las claves correctas?
            4. ¿Las dependencias forman un DAG válido (sin ciclos, sin nombres inexistentes)?
            5. ¿El JSON es válido y no tiene comas finales ni texto adicional?

            Genera el plan de ejecución en formato JSON. Responde ÚNICAMENTE con el JSON, sin texto adicional.
            """
        return prompt

    @classmethod
    def _formatear_contratos(cls) -> str:
        """Formatea los contratos de salida para el prompt."""
        lineas = []
        for tipo, info in cls.CONTRATOS_SALIDA.items():
            lineas.append(f"### {tipo}")
            lineas.append(f"  {info['descripcion']}")
            claves = info.get('claves')
            if isinstance(claves, dict):
                for clave, desc in claves.items():
                    lineas.append(f"    • `{clave}`: {desc}")
            else:
                lineas.append(f"    {claves}")
            lineas.append("")
        return "\n".join(lineas)


# ============================================================
# CORRECTOR DE CÓDIGO PYTHON
# ============================================================

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
    def corregir(cls, codigo: str, dependencias: List[str]) -> str:
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


# ============================================================
# NORMALIZADOR DE NOMBRES DE ARCHIVOS
# ============================================================

class FileNameNormalizer:
    """Normaliza nombres de archivos en agentes File."""

    GENERIC_NAMES = frozenset([
        "salida.txt", "output.txt", "resultado.txt", "out.txt",
        "file.txt", "archivo.txt", "data.txt", "datos.txt"
    ])

    CONTEXT_KEYWORDS = {
        "tiempo": "tiempo.txt",
        "clima": "clima.txt",
        "weather": "weather.txt",
        "reporte": "reporte.txt",
        "report": "report.txt",
        "datos": "datos.txt",
        "data": "data.txt",
        "resultado": "resultado.txt",
        "result": "result.txt",
        "log": "log.txt",
        "github": "github.txt",
        "api": "api_response.txt",
        "json": "data.json",
        "config": "config.txt",
        "resumen": "resumen.txt",
        "summary": "summary.txt",
    }

    @classmethod
    def normalizar(cls, plan_dict: Dict) -> Dict:
        """Normaliza nombres de archivos en el plan."""
        pasos = plan_dict.get('pasos', [])
        titulo = plan_dict.get('titulo', '')

        for paso in pasos:
            if paso.get('tipo') != 'File':
                continue

            config = paso.get('configuracion', {})
            if config.get('operacion') != 'escribir':
                continue

            destino = config.get('archivo_destino', '')
            if destino in cls.GENERIC_NAMES:
                nuevo_nombre = cls._inferir_nombre(titulo, paso)
                if nuevo_nombre:
                    config['archivo_destino'] = nuevo_nombre
                    logger.info(f"📁 Nombre de archivo normalizado: {destino} → {nuevo_nombre}")

        return plan_dict

    @classmethod
    def _inferir_nombre(cls, titulo: str, paso: Dict) -> Optional[str]:
        """Infiere un nombre de archivo a partir del título y el paso."""
        titulo_lower = titulo.lower()

        for keyword, filename in cls.CONTEXT_KEYWORDS.items():
            if keyword in titulo_lower:
                return filename

        descripcion = paso.get('descripcion', '').lower()
        for keyword, filename in cls.CONTEXT_KEYWORDS.items():
            if keyword in descripcion:
                return filename

        nombre_paso = paso.get('nombre', 'archivo')
        return f"{nombre_paso.lower()}.txt"


# ============================================================
# MODELOS DE DATOS
# ============================================================

@dataclass
class StepPlan:
    """Un paso individual del plan descompuesto."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    orden: int = 0
    nombre: str = ""
    descripcion: str = ""
    tipo_agente: str = "Python"
    dependencia_ids: List[str] = field(default_factory=list)
    configuracion: Dict[str, Any] = field(default_factory=dict)
    justificacion: str = ""
    es_critico: bool = False

    def to_dict(self) -> Dict:
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
    pasos: List[StepPlan] = field(default_factory=list)
    status: PlanStatus = PlanStatus.DRAFT
    estimacion_tiempo: float = 0.0
    agentes_generados: List[Agente] = field(default_factory=list)
    advertencias: List[str] = field(default_factory=list)
    metadatos: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict:
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

    def obtener_agentes_por_nombre(self) -> Dict[str, Agente]:
        return {a.nombre: a for a in self.agentes_generados}

    def obtener_pasos_por_nombre(self) -> Dict[str, StepPlan]:
        return {p.nombre: p for p in self.pasos}


# ============================================================
# CLASE PRINCIPAL: PROBLEM SOLVER
# ============================================================

class ProblemSolver:
    """
    Descompone problemas complejos en planes de ejecución de agentes.
    """

    # Modelos a probar en orden (configurable)
    DEFAULT_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro"]
    DEFAULT_TEMPERATURE = 0.1
    DEFAULT_MAX_TOKENS = 3000
    DEFAULT_REASONING_EFFORT = "low"
    DEFAULT_THINKING_ENABLED = False

    # ✅ Mapa de campos válidos por tipo (fuente única de verdad)
    CAMPOS_VALIDOS_POR_TIPO = CAMPOS_VALIDOS_POR_TIPO

    def __init__(self, llm_client: Optional[LLMClient] = None):
        """
        Inicializa el solver.

        Args:
            llm_client: Cliente LLM (si no se proporciona, se crea uno)
        """
        if llm_client is None:
            try:
                llm_client = LLMClient()
            except Exception as e:
                raise ValueError(f"Error inicializando cliente LLM: {e}")

        self.llm_client = llm_client
        if not self.llm_client.disponible:
            raise ValueError(
                "ProblemSolver requiere un LLMClient disponible. "
                "Configura la variable de entorno DEEPSEEK_API_KEY."
            )

        self._plan_cache: Dict[str, ExecutionPlan] = {}
        self._ultimo_problema: str = ""
        self._plan_actual: Optional[ExecutionPlan] = None
        self.logger = logger

        # Componentes modulares
        self.prompt_builder = PromptBuilder()
        self.code_corrector = PythonCodeCorrector()
        self.file_normalizer = FileNameNormalizer()

        self.logger.info("ProblemSolver inicializado correctamente")

    # ============================================================
    # API PÚBLICA
    # ============================================================

    def resolver_problema(
    self,
    problema: str,
    contexto_extra: Optional[Dict] = None,
    max_pasos: int = 10,
    nivel_detalle: str = "normal",
    _es_regeneracion: bool = False,
    _instruccion_extra: str = "",
) -> ExecutionPlan:
        """
        Analiza un problema y genera un plan de ejecución completo.

        Si el plan no cumple un requisito explícito del problema (ej:
        "con imágenes"), regenera el plan UNA VEZ con una instrucción
        forzada. Si tras la regeneración sigue sin cumplirlo, aplica un
        parche determinista aislado en `core.plan_repairs` y lo registra
        en el LearningEngine.
        """
        if not problema or not problema.strip():
            raise ValueError("El problema no puede estar vacío")

        self._ultimo_problema = problema
        self.logger.info(f"🧠 Resolviendo problema: {problema[:100]}...")

        # 1. Construir prompt del usuario
        user_prompt = self.prompt_builder.build_user_prompt(
            problema, contexto_extra, max_pasos, nivel_detalle
        )

        # ✅ Lecciones aprendidas
        try:
            from learning import obtener_learning_engine
            engine = obtener_learning_engine()
            if engine is not None:
                bloque_lecciones = engine.obtener_lecciones_para_prompt()
                if bloque_lecciones:
                    user_prompt = user_prompt + "\n\n" + bloque_lecciones
                    self.logger.info("🧠 Lecciones aprendidas inyectadas en el prompt")
        except Exception as e:
            self.logger.debug(f"Learning no disponible: {e}")

        # ✅ Instrucción extra (en regeneraciones)
        if _instruccion_extra:
            user_prompt = user_prompt + "\n\n" + _instruccion_extra
            self.logger.info("🔁 Instrucción de regeneración añadida al prompt")

        # 2. Consultar al LLM
        plan_dict = self._consultar_llm_con_reintentos(user_prompt)

        # 3. Normalizar nombres de archivos
        plan_dict = self.file_normalizer.normalizar(plan_dict)

        # 4. Construir ExecutionPlan
        plan = self._construir_plan(problema, plan_dict)

        # ✅ 5. Guardar referencia (para que _validar_campos_configuracion
        #       pueda propagar advertencias al plan).
        self._plan_actual = plan

        try:
            # 6. Detectar requisitos no cumplidos
            requisitos_faltantes = detectar_requisitos_no_cumplidos(problema, plan)

            # 7. Si hay requisitos faltantes y NO estamos ya en regeneración,
            #    regenerar UNA VEZ con instrucción forzada.
            if requisitos_faltantes and not _es_regeneracion:
                self.logger.warning(
                    f"⚠️ Plan incompleto: faltan requisitos {requisitos_faltantes}. "
                    f"Regenerando con instrucción forzada..."
                )
                self._plan_actual = None  # limpiar antes de recursión

                instruccion = construir_instruccion_regeneracion(requisitos_faltantes)
                return self.resolver_problema(
                    problema=problema,
                    contexto_extra=contexto_extra,
                    max_pasos=max_pasos,
                    nivel_detalle=nivel_detalle,
                    _es_regeneracion=True,
                    _instruccion_extra=instruccion,
                )

            # 8. Si seguimos con requisitos faltantes, aplicar parche (aislado)
            if requisitos_faltantes:
                for req in requisitos_faltantes:
                    self.logger.warning(
                        f"⚠️ Regeneración no resolvió '{req}'. Aplicando parche..."
                    )
                    parche_aplicado = aplicar_parche(problema, plan, req)
                    if parche_aplicado:
                        self.logger.warning(f"🔧 Parche aplicado: {parche_aplicado}")
                        # Registrar en LearningEngine para reforzar la lección
                        self._registrar_reparacion(problema, parche_aplicado)

            # 9. Generar objetos Agente
            plan.agentes_generados = self._generar_agentes(plan)

            # 10. Validar
            es_valido, errores = self._validar_plan(plan)
            if not es_valido:
                plan.advertencias.extend(
                    e for e in errores if e not in plan.advertencias
                )
                self.logger.warning(f"⚠️ Plan con advertencias: {errores}")

            # 11. Guardar en caché
            self._plan_cache[plan.id] = plan

            self.logger.info(
                f"✅ Plan generado: {len(plan.pasos)} pasos, "
                f"{len(plan.agentes_generados)} agentes"
            )
            return plan

        finally:
            self._plan_actual = None

    def _registrar_reparacion(self, problema: str, tipo: str) -> None:
        """Registra una reparación de plan en el LearningEngine."""
        try:
            from learning import obtener_learning_engine
            engine = obtener_learning_engine()
            if engine is not None:
                engine.registrar_reparacion_plan(problema=problema, tipo=tipo)
        except Exception as e:
            self.logger.debug(f"No se pudo registrar reparación: {e}")

    def refinar_plan(self, plan: ExecutionPlan, instruccion: str) -> ExecutionPlan:
        """Refina un plan existente según una instrucción del usuario."""
        if not instruccion or not instruccion.strip():
            raise ValueError("La instrucción de refinamiento no puede estar vacía")
        if not plan or not plan.pasos:
            raise ValueError("El plan a refinar no es válido")

        self.logger.info(f"🔧 Refinando plan: {instruccion[:100]}...")

        system_prompt = (
            "Eres un arquitecto de agentes. Te doy un plan existente y una "
            "instrucción de mejora. Devuelve el plan MODIFICADO en el mismo "
            "formato JSON. Mantén la estructura pero aplica los cambios pedidos."
        )

        user_prompt = (
            f"PLAN ACTUAL:\n{json.dumps(plan.to_dict(), indent=2, ensure_ascii=False)}\n\n"
            f"INSTRUCCIÓN:\n{instruccion}\n\n"
            "Devuelve el plan actualizado en JSON."
        )

        try:
            respuesta = self.llm_client.chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                model="deepseek-v4-flash",
                temperature=0.2,
                max_tokens=4000
            )
            # ⬇️ TEMPORAL: ver qué devuelve el LLM
            logger.info(f"📄 Respuesta cruda (primeros 500 chars):\n{respuesta[:500]}")

            plan_dict = self._parsear_respuesta(respuesta)
            plan_dict = self.file_normalizer.normalizar(plan_dict)
            nuevo_plan = self._construir_plan(plan.problema_original, plan_dict)

            self._plan_actual = nuevo_plan
            try:
                nuevo_plan.agentes_generados = self._generar_agentes(nuevo_plan)

                es_valido, errores = self._validar_plan(nuevo_plan)
                if not es_valido:
                    nuevo_plan.advertencias.extend(
                        e for e in errores if e not in nuevo_plan.advertencias
                    )
            finally:
                self._plan_actual = None

            self._plan_cache[nuevo_plan.id] = nuevo_plan
            return nuevo_plan

        except Exception as e:
            self.logger.error(f"Error refinando plan: {e}")
            raise ValueError(f"No se pudo refinar el plan: {e}")

    def obtener_plan(self, plan_id: str) -> Optional[ExecutionPlan]:
        """Obtiene un plan de la caché por su ID."""
        return self._plan_cache.get(plan_id)

    def listar_planes(self) -> List[Dict]:
        """Lista todos los planes en caché con metadatos."""
        return [
            {
                'id': p.id,
                'titulo': p.titulo,
                'status': p.status.value,
                'num_pasos': len(p.pasos),
                'num_agentes': len(p.agentes_generados),
                'complejidad': self.estimar_complejidad(p),
                'fecha_creacion': p.metadatos.get('fecha_creacion', ''),
            }
            for p in self._plan_cache.values()
        ]

    # ============================================================
    # CONSULTA AL LLM CON REINTENTOS
    # ============================================================

    def _consultar_llm_con_reintentos(self, user_prompt: str) -> Dict:
        """Consulta al LLM con reintentos y modelos alternativos."""
        system_prompt = self.prompt_builder.build_system_prompt()

        for modelo in self.DEFAULT_MODELS:
            try:
                self.logger.info(f"🔄 Intentando con modelo: {modelo}")

                respuesta = self.llm_client.chat(
                    prompt=user_prompt,
                    system_prompt=system_prompt,
                    model=modelo,
                    temperature=self.DEFAULT_TEMPERATURE,
                    max_tokens=self.DEFAULT_MAX_TOKENS,
                    reasoning_effort=self.DEFAULT_REASONING_EFFORT,
                    thinking_enabled=self.DEFAULT_THINKING_ENABLED
                )

                if not respuesta or not respuesta.strip():
                    self.logger.warning(f"⚠️ Respuesta vacía con modelo {modelo}")
                    continue

                self.logger.info(f"📥 Respuesta recibida ({len(respuesta)} caracteres)")
                self._guardar_respuesta_debug(respuesta, modelo)

                respuesta_limpia = self._limpiar_respuesta_agresivamente(respuesta)

                try:
                    plan_dict = json.loads(respuesta_limpia)
                    if plan_dict and plan_dict.get('pasos'):
                        self.logger.info(f"✅ Plan obtenido con modelo {modelo}")
                        return plan_dict
                except json.JSONDecodeError as e:
                    self.logger.warning(f"⚠️ JSON inválido con modelo {modelo}: {e}")

                    reparado = self._reparar_json(respuesta_limpia)
                    if reparado:
                        try:
                            plan_dict = json.loads(reparado)
                            if plan_dict and plan_dict.get('pasos'):
                                self.logger.info(f"✅ Plan REPARADO con modelo {modelo}")
                                return plan_dict
                        except json.JSONDecodeError as e2:
                            self.logger.warning(f"⚠️ JSON reparado aún inválido: {e2}")

                    self.logger.debug(f"Respuesta que falló: {respuesta[:500]}...")
                    continue

            except Exception as e:
                self.logger.warning(f"❌ Falló con modelo {modelo}: {e}")
                continue

        self.logger.error("❌ Todos los modelos fallaron. Usando fallback.")
        return self._crear_plan_fallback(self._ultimo_problema)

    def _limpiar_respuesta_agresivamente(self, respuesta: str) -> str:
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

    def _guardar_respuesta_debug(self, respuesta: str, modelo: str):
        """Guarda la respuesta del LLM para depuración."""
        try:
            os.makedirs("logs", exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            filename = f"logs/llm_response_{timestamp}_{modelo}.txt"
            with open(filename, "w", encoding="utf-8") as f:
                f.write("=" * 70 + "\n")
                f.write(f"MODELO: {modelo}\n")
                f.write(f"TIMESTAMP: {timestamp}\n")
                f.write("=" * 70 + "\n")
                f.write(respuesta)
                f.write("\n" + "=" * 70 + "\n")
                f.write(f"LONGITUD: {len(respuesta)} caracteres\n")
            self.logger.debug(f"📝 Respuesta guardada en: {filename}")
        except Exception as e:
            self.logger.warning(f"No se pudo guardar respuesta para debug: {e}")

    # ============================================================
    # PLAN DE FALLBACK
    # ============================================================

    def _crear_plan_fallback(self, problema: str) -> Dict:
        """Crea un plan de fallback con código Python CORRECTO."""
        self.logger.warning(f"Creando plan de fallback para: {problema[:50]}...")

        nombre = f"Resolver_{re.sub(r'[^a-zA-Z0-9_]', '_', problema[:20])}"

        return {
            "titulo": f"Resolver: {problema[:50]}...",
            "analisis": "Se resuelve el problema utilizando Python con acceso correcto al contexto.",
            "estimacion_tiempo_segundos": 30,
            "pasos": [
                {
                    "orden": 1,
                    "nombre": nombre,
                    "descripcion": problema[:100],
                    "tipo": "Python",
                    "dependencias": [],
                    "configuracion": {
                        "codigo": (
                            "import json\n"
                            "import time\n\n"
                            "# Datos disponibles en 'contexto'\n"
                            "data = contexto if contexto else {}\n\n"
                            "# Procesar el problema\n"
                            "resultado = {\n"
                            "    'status': 'ok',\n"
                            "    'problema': 'Resuelto con fallback',\n"
                            "    'timestamp': time.time(),\n"
                            "    'datos_recibidos': data\n"
                            "}\n"
                        ),
                        "timeout": 30
                    },
                    "justificacion": "Paso único para resolver el problema usando Python con contexto correcto"
                }
            ]
        }

    # ============================================================
    # PARSEO DE RESPUESTA
    # ============================================================

    def _parsear_respuesta(self, respuesta: str) -> Dict:
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

        json_reparado = self._reparar_json(respuesta)
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

    def _buscar_json(self, texto: str) -> Optional[str]:
        """Busca un objeto JSON en el texto."""
        inicio = texto.find('{')
        fin = texto.rfind('}')
        if inicio != -1 and fin != -1 and fin > inicio:
            return texto[inicio:fin + 1]
        return None

    def _extraer_json_por_partes(self, texto: str) -> Optional[str]:
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

    def _reparar_json(self, texto: str) -> Optional[str]:
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

    # ============================================================
    # CONSTRUCCIÓN DEL PLAN
    # ============================================================

    def _construir_plan(self, problema: str, plan_dict: Dict) -> ExecutionPlan:
        """Construye un ExecutionPlan desde el diccionario del LLM."""
        if 'pasos' not in plan_dict or not plan_dict['pasos']:
            self.logger.warning("Plan sin pasos, creando paso por defecto")
            plan_dict['pasos'] = [{
                "orden": 1,
                "nombre": "ResolverProblema",
                "descripcion": problema[:100],
                "tipo": "Python",
                "dependencias": [],
                "configuracion": {
                    "codigo": (
                        "import json\n\n"
                        "data = contexto if contexto else {}\n\n"
                        "resultado = {\n"
                        "    'status': 'ok',\n"
                        "    'problema': 'Resuelto',\n"
                        "    'datos': data\n"
                        "}\n"
                    ),
                    "timeout": 30
                },
                "justificacion": "Paso principal para resolver el problema"
            }]

        plan = ExecutionPlan(
            problema_original=problema,
            titulo=plan_dict.get('titulo', 'Plan sin título'),
            analisis=plan_dict.get('analisis', ''),
            estimacion_tiempo=float(plan_dict.get('estimacion_tiempo_segundos', 0)),
            metadatos={'fecha_creacion': str(uuid.uuid4())}
        )

        pasos_raw = plan_dict.get('pasos', [])
        for i, paso_raw in enumerate(pasos_raw):
            if 'nombre' not in paso_raw:
                paso_raw['nombre'] = f'Paso_{i+1}'
            if 'tipo' not in paso_raw:
                paso_raw['tipo'] = 'Python'

            paso = StepPlan(
                orden=paso_raw.get('orden', i + 1),
                nombre=paso_raw.get('nombre', f'Paso_{i+1}'),
                descripcion=paso_raw.get('descripcion', ''),
                tipo_agente=paso_raw.get('tipo', 'Python'),
                dependencia_ids=paso_raw.get('dependencias', []),
                configuracion=paso_raw.get('configuracion', {}),
                justificacion=paso_raw.get('justificacion', ''),
                es_critico=paso_raw.get('es_critico', False),
            )

            try:
                TipoAgente(paso.tipo_agente)
            except ValueError:
                self.logger.warning(f"Tipo inválido '{paso.tipo_agente}', usando Python")
                paso.tipo_agente = 'Python'

            self._validar_configuracion_paso(paso)
            plan.pasos.append(paso)

        return plan

    

    def _validar_configuracion_paso(self, paso: StepPlan):
        """
        Valida y completa la configuración mínima según el tipo.

        ✅ ADEMÁS: elimina campos desconocidos de 'configuracion' para evitar
        que lleguen al constructor de Agente o queden ocultos en el plan.
        """
        tipo = paso.tipo_agente
        config = paso.configuracion

        # ── Eliminar campos desconocidos de 'configuracion' ──
        permitidos = self.CAMPOS_VALIDOS_POR_TIPO.get(tipo, set())
        if permitidos:
            desconocidos = [k for k in list(config.keys()) if k not in permitidos]
            for k in desconocidos:
                self.logger.warning(
                    f"🧹 Limpiando campo desconocido '{k}' en '{paso.nombre}' "
                    f"(tipo {tipo})"
                )
                config.pop(k, None)

        # ── Completar valores por defecto ──
        if tipo == 'HTTP':
            if 'url' not in config or not config['url']:
                config['url'] = 'https://api.github.com/repos/python/cpython'
            if 'metodo' not in config:
                config['metodo'] = 'GET'
            if 'timeout' not in config:
                config['timeout'] = 15

        elif tipo == 'LLM':
            if 'prompt' not in config or not config['prompt']:
                config['prompt'] = f"Analiza el siguiente contexto:\n{{contexto}}"
            if 'modelo' not in config:
                config['modelo'] = 'deepseek-v4-flash'
            if 'temperatura' not in config:
                config['temperatura'] = 0.7

            # ✅ Valor por defecto más alto: 4000 tokens
            if 'max_tokens' not in config:
                config['max_tokens'] = 4000

            # ✅ Blindaje: subir a un mínimo seguro si viene bajo
            MIN_TOKENS_SEGUROS = 4000
            if int(config['max_tokens']) < MIN_TOKENS_SEGUROS:
                logger.warning(
                    f"⚠️ Paso LLM '{paso.nombre}': max_tokens={config['max_tokens']} "
                    f"insuficiente para thinking mode. "
                    f"Subiendo a {MIN_TOKENS_SEGUROS}."
                )
                config['max_tokens'] = MIN_TOKENS_SEGUROS

            # ✅ reasoning_effort y thinking_enabled con defaults sensatos
            if 'reasoning_effort' not in config:
                config['reasoning_effort'] = 'low'
            if 'thinking_enabled' not in config:
                config['thinking_enabled'] = False

        elif tipo == 'Python':
            if 'codigo' not in config or not config['codigo']:
                config['codigo'] = (
                    "import json\n\n"
                    "data = contexto if contexto else {}\n\n"
                    "resultado = {\n"
                    "    'status': 'ok',\n"
                    "    'datos': data\n"
                    "}\n"
                )
            if 'timeout' not in config:
                config['timeout'] = 30

        elif tipo == 'Shell':
            if 'comando' not in config or not config['comando']:
                config['comando'] = 'echo "Hola desde el agente Shell"'
            if 'timeout' not in config:
                config['timeout'] = 30

        elif tipo == 'File':
            if 'operacion' not in config:
                config['operacion'] = 'leer'
            op = config['operacion']
            if op in ('leer', 'eliminar') and 'archivo_origen' not in config:
                config['archivo_origen'] = 'entrada.txt'
            if op in ('escribir', 'copiar', 'mover') and 'archivo_destino' not in config:
                config['archivo_destino'] = 'salida.txt'
            if 'modo_salida_file' not in config:
                config['modo_salida_file'] = 'auto'

        elif tipo == 'Loop':
            if 'fuente_items' not in config or not config['fuente_items']:
                config['fuente_items'] = 'Dependencia.items'
            if 'codigo_por_item' not in config or not config['codigo_por_item']:
                config['codigo_por_item'] = (
                    "# Procesar cada item\n"
                    "resultado = {\n"
                    "    'indice': indice,\n"
                    "    'item': item,\n"
                    "    'procesado': True\n"
                    "}\n"
                )
            if 'max_iteraciones' not in config:
                config['max_iteraciones'] = 100
            if 'timeout_loop' not in config:
                config['timeout_loop'] = 300
            if 'timeout_python' not in config:
                config['timeout_python'] = 30
            if 'continuar_en_error' not in config:
                config['continuar_en_error'] = False

    # ============================================================
    # GENERACIÓN DE AGENTES
    # ============================================================

    def _generar_agentes(self, plan: ExecutionPlan) -> List[Agente]:
        """Genera objetos Agente a partir del plan, corrigiendo código Python."""
        agentes: List[Agente] = []
        nombre_a_id: Dict[str, str] = {}

        for paso in plan.pasos:
            try:
                agente = self._paso_a_agente(paso)
                agentes.append(agente)
                nombre_a_id[paso.nombre] = agente.id
            except Exception as e:
                self.logger.error(f"Error creando agente '{paso.nombre}': {e}")
                agente = Agente(
                    nombre=paso.nombre,
                    tipo=TipoAgente.PYTHON,
                    descripcion=f"FALLBACK: {paso.descripcion}",
                    codigo_python=(
                        "import json\n"
                        "resultado = {\n"
                        "    'error': 'Fallo en la generación',\n"
                        "    'detalle': str(e)\n"
                        "}\n"
                    )
                )
                agentes.append(agente)
                nombre_a_id[paso.nombre] = agente.id
                plan.advertencias.append(f"Error generando '{paso.nombre}': {e}")

        for i, paso in enumerate(plan.pasos):
            if i >= len(agentes):
                continue
            agente = agentes[i]
            ids_resueltos = []
            nombres_resueltos = []

            for dep_nombre in paso.dependencia_ids:
                if dep_nombre in nombre_a_id:
                    ids_resueltos.append(nombre_a_id[dep_nombre])
                    nombres_resueltos.append(dep_nombre)
                else:
                    plan.advertencias.append(
                        f"'{paso.nombre}' depende de '{dep_nombre}' que no existe"
                    )

            agente.dependencias_ids = ids_resueltos
            agente.dependencias_nombres = nombres_resueltos

        return agentes

    # ============================================================
    # CONVERSIÓN StepPlan → Agente (REFACTORIZADO)
    # ============================================================

    def _paso_a_agente(self, paso: StepPlan) -> Agente:
        """
        Convierte un StepPlan en un objeto Agente listo para ejecutar.

        RESPONSABILIDADES:
        1. Validar el tipo (con fallback seguro a Python).
        2. Detectar y reportar campos desconocidos en 'configuracion'.
        3. Corregir automáticamente el código Python generado por el LLM.
        4. Construir el Agente con la configuración específica del tipo.

        Args:
            paso: StepPlan con la configuración generada por el LLM.

        Returns:
            Agente: Instancia configurada y lista para ejecutar.

        Raises:
            ValueError: Si el tipo de agente no es válido.
        """
        # ── 1. Resolver el tipo (con fallback seguro) ──
        try:
            tipo = TipoAgente(paso.tipo_agente)
        except ValueError:
            self.logger.warning(
                f"Tipo desconocido '{paso.tipo_agente}' en '{paso.nombre}', "
                f"usando Python como fallback"
            )
            tipo = TipoAgente.PYTHON

        # ── 2. Detectar campos desconocidos en 'configuracion' ──
        self._validar_campos_configuracion(paso, tipo)

        # ── 3. Construir kwargs base (comunes a todos los tipos) ──
        config = paso.configuracion or {}
        kwargs: Dict[str, Any] = {
            "nombre": paso.nombre,
            "tipo": tipo,
            "descripcion": paso.descripcion,
            "duracion": 5.0,
            "max_reintentos": 2,
        }

        # ── 4. Añadir configuración específica según tipo ──
        dispatch = {
            TipoAgente.PYTHON: self._kwargs_python,
            TipoAgente.HTTP:   self._kwargs_http,
            TipoAgente.LLM:    self._kwargs_llm,
            TipoAgente.SHELL:  self._kwargs_shell,
            TipoAgente.FILE:   self._kwargs_file,
            TipoAgente.LOOP:   self._kwargs_loop,
        }

        helper = dispatch.get(tipo)
        if helper is not None:
            helper(paso, config, kwargs)
        else:
            self.logger.error(
                f"Tipo '{tipo}' no tiene helper de configuración. "
                f"Se usará configuración por defecto."
            )

        # ── 5. Crear el agente ──
        try:
            return Agente(**kwargs)
        except TypeError as e:
            self.logger.error(
                f"Error construyendo Agente '{paso.nombre}': {e}. "
                f"kwargs recibidos: {sorted(kwargs.keys())}"
            )
            raise

    # ============================================================
    # VALIDACIÓN DE CAMPOS DESCONOCIDOS
    # ============================================================

    def _validar_campos_configuracion(
        self,
        paso: StepPlan,
        tipo: TipoAgente
    ) -> None:
        """
        Verifica que todas las claves de 'configuracion' sean válidas
        para el tipo de agente. Los campos desconocidos se registran
        como advertencias (no bloquean la creación del agente).

        Protege contra LLMs que inventan campos como:
            "contenido": "{{FormatearContenido.contenido}}"
        que el sistema NO soporta para agentes File.
        """
        config = paso.configuracion or {}
        if not config:
            return

        permitidos = self.CAMPOS_VALIDOS_POR_TIPO.get(tipo.value, set())
        desconocidos = [k for k in config.keys() if k not in permitidos]

        if not desconocidos:
            return

        campos_str = ", ".join(f"'{k}'" for k in desconocidos)
        advertencia = (
            f"'{paso.nombre}' (tipo {tipo.value}): campos desconocidos "
            f"en 'configuracion' serán ignorados: {campos_str}"
        )

        self.logger.warning(f"⚠️ {advertencia}")
        self.logger.debug(
            f"   Campos permitidos para {tipo.value}: {sorted(permitidos)}"
        )
        self.logger.debug(f"   Campos recibidos: {sorted(config.keys())}")

        if self._plan_actual is not None:
            self._plan_actual.advertencias.append(advertencia)

    # ============================================================
    # HELPERS DE CONFIGURACIÓN POR TIPO
    # ============================================================

        # ============================================================
    # HELPERS DE CONFIGURACIÓN POR TIPO
    # ============================================================

    def _kwargs_python(
        self,
        paso: StepPlan,
        config: Dict[str, Any],
        kwargs: Dict[str, Any]
    ) -> None:
        """Configura un agente Python con corrección automática de código."""
        codigo_original = config.get('codigo', 'resultado = {"status": "ok"}')
        codigo_corregido = self.code_corrector.corregir(
            codigo_original,
            paso.dependencia_ids
        )
        kwargs['codigo_python'] = codigo_corregido
        kwargs['timeout_python'] = int(config.get('timeout', 30))

    def _kwargs_http(
        self,
        paso: StepPlan,
        config: Dict[str, Any],
        kwargs: Dict[str, Any]
    ) -> None:
        """Configura un agente HTTP."""
        kwargs['url_http'] = config.get('url', '')
        kwargs['metodo_http'] = str(config.get('metodo', 'GET')).upper()
        kwargs['headers_http'] = config.get('headers', {}) or {}
        kwargs['body_http'] = config.get('body', '') or ''
        kwargs['timeout_http'] = int(config.get('timeout', 30))

    def _kwargs_llm(self, paso: StepPlan, config: Dict, kwargs: Dict):
        """
        Configura kwargs para un agente LLM.

        Aplica tres protecciones clave:
        1. Sube max_tokens a un mínimo seguro (4000) si viene bajo.
        2. Endurece el prompt con instrucciones anti-alucinación de IDs.
        3. Propaga reasoning_effort y thinking_enabled al Agente.
        """
        MIN_TOKENS_SEGUROS = 4000

        prompt_original = config.get('prompt', '')
        kwargs['modelo_llm'] = config.get('modelo', 'deepseek-v4-flash')
        kwargs['temperatura_llm'] = float(config.get('temperatura', 0.7))

        # ✅ Blindaje: thinking mode consume tokens del presupuesto
        max_tokens = int(config.get('max_tokens', MIN_TOKENS_SEGUROS) or MIN_TOKENS_SEGUROS)
        if max_tokens < MIN_TOKENS_SEGUROS:
            self.logger.warning(
                f"⚠️ Paso LLM '{paso.nombre}': max_tokens={max_tokens} "
                f"insuficiente para thinking. "
                f"Subiendo a {MIN_TOKENS_SEGUROS}."
            )
            max_tokens = MIN_TOKENS_SEGUROS
        kwargs['max_tokens_llm'] = max_tokens

        # ✅ Propagar reasoning y thinking al Agente
        kwargs['reasoning_effort_llm'] = config.get('reasoning_effort', 'low')
        kwargs['thinking_enabled_llm'] = bool(config.get('thinking_enabled', False))

        # ✅ Endurecer prompt contra alucinación de IDs
        prompt_endurecido = self._endurecer_prompt_llm(prompt_original)
        kwargs['prompt_llm'] = prompt_endurecido

    def _endurecer_prompt_llm(self, prompt_original: str) -> str:
        """
        Añade instrucciones anti-alucinación al prompt de un agente LLM.

        Esto evita que el LLM invente IDs distintos a los de entrada
        (problema típico cuando se le pide procesar una lista de items).
        """
        preambulo = (
            "INSTRUCCIONES CRÍTICAS:\n"
            "1. Recibirás una lista de items en el contexto (probablemente bajo "
            "una clave como 'items', 'top10' o similar).\n"
            "2. Debes procesar EXACTAMENTE los items que recibas, NI MÁS NI MENOS.\n"
            "3. NO inventes IDs, nombres ni campos. Usa los que YA vienen en la entrada.\n"
            "4. Si la entrada tiene 10 items con ids [11,12,...,20], tu salida DEBE "
            "tener 10 items con esos MISMOS ids, no otros.\n"
            "5. Devuelve la respuesta como JSON válido y COMPLETO (sin truncar).\n"
            "\n"
            "TAREA:\n"
        )
        return preambulo + prompt_original

    def _kwargs_shell(
        self,
        paso: StepPlan,
        config: Dict[str, Any],
        kwargs: Dict[str, Any]
    ) -> None:
        """Configura un agente Shell."""
        kwargs['comando_shell'] = config.get('comando', '')
        kwargs['timeout_shell'] = int(config.get('timeout', 30))
        kwargs['working_dir'] = config.get('working_dir', '') or ''

    def _kwargs_file(
        self,
        paso: StepPlan,
        config: Dict[str, Any],
        kwargs: Dict[str, Any]
    ) -> None:
        """
        Configura un agente File.

        NOTA: NO se pasa 'contenido'. El contenido se obtiene
        automáticamente del resultado de la dependencia declarada
        (ver AgentExecutor._ejecutar_file).
        """
        kwargs['operacion_file'] = config.get('operacion', 'leer')
        kwargs['archivo_origen'] = config.get('archivo_origen', '') or ''
        kwargs['archivo_destino'] = config.get('archivo_destino', '') or ''
        kwargs['modo_salida_file'] = config.get('modo_salida_file', 'auto')

    def _kwargs_loop(
        self,
        paso: StepPlan,
        config: Dict[str, Any],
        kwargs: Dict[str, Any]
    ) -> None:
        """Configura un agente Loop."""
        kwargs['fuente_items'] = config.get('fuente_items', '')
        kwargs['codigo_por_item'] = config.get('codigo_por_item', '')
        kwargs['max_iteraciones'] = int(config.get('max_iteraciones', 100))
        kwargs['timeout_loop'] = int(config.get('timeout_loop', 300))
        kwargs['timeout_python'] = int(config.get('timeout_python', 30))
        kwargs['continuar_en_error'] = bool(config.get('continuar_en_error', False))

    # ============================================================
    # VALIDACIÓN
    # ============================================================

    def _validar_plan(self, plan: ExecutionPlan) -> Tuple[bool, List[str]]:
        """Valida el plan generado."""
        errores = []

        if not plan.pasos:
            errores.append("El plan no contiene pasos")
            return False, errores

        nombres = [p.nombre for p in plan.pasos]
        duplicados = set([n for n in nombres if nombres.count(n) > 1])
        if duplicados:
            errores.append(f"Nombres duplicados: {', '.join(duplicados)}")

        nombres_set = set(nombres)
        for paso in plan.pasos:
            for dep in paso.dependencia_ids:
                if dep not in nombres_set:
                    errores.append(f"'{paso.nombre}' depende de '{dep}' que no existe")

        if self._detectar_ciclos(plan):
            errores.append("El plan contiene ciclos en las dependencias")

        for agente in plan.agentes_generados:
            valido, msg = agente.validar_configuracion()
            if not valido:
                errores.append(f"{agente.nombre}: {msg}")

        return len(errores) == 0, errores

    def _detectar_ciclos(self, plan: ExecutionPlan) -> bool:
        """Detecta ciclos en el DAG usando DFS."""
        nombre_a_paso = {p.nombre: p for p in plan.pasos}
        visitados = set()
        pila = set()

        def dfs(nombre: str) -> bool:
            if nombre in pila:
                return True
            if nombre in visitados:
                return False
            visitados.add(nombre)
            pila.add(nombre)
            paso = nombre_a_paso.get(nombre)
            if paso:
                for dep in paso.dependencia_ids:
                    if dfs(dep):
                        return True
            pila.remove(nombre)
            return False

        for paso in plan.pasos:
            if paso.nombre not in visitados:
                if dfs(paso.nombre):
                    return True
        return False

    # ============================================================
    # UTILIDADES
    # ============================================================

    def estimar_complejidad(self, plan: ExecutionPlan) -> str:
        """Estima la complejidad del plan."""
        n_pasos = len(plan.pasos)
        if n_pasos == 0:
            return PlanComplexity.SIMPLE.value

        n_deps = sum(len(p.dependencia_ids) for p in plan.pasos)
        tipos = {p.tipo_agente for p in plan.pasos}
        max_deps = max((len(p.dependencia_ids) for p in plan.pasos), default=0)

        score = n_pasos * 2 + n_deps * 1 + len(tipos) * 3 + max_deps * 2

        if score <= 10:
            return PlanComplexity.SIMPLE.value
        elif score <= 20:
            return PlanComplexity.MODERATE.value
        elif score <= 35:
            return PlanComplexity.COMPLEX.value
        else:
            return PlanComplexity.VERY_COMPLEX.value

    def estimar_tiempo(self, plan: ExecutionPlan) -> float:
        """Estima el tiempo total de ejecución en segundos."""
        tiempos = {'Python': 5, 'Shell': 3, 'HTTP': 8, 'LLM': 12, 'File': 2, 'Loop': 15}
        total = sum(
            tiempos.get(p.tipo_agente, 5) * (1 + len(p.dependencia_ids) * 0.2)
            for p in plan.pasos
        )
        return round(total + len(plan.pasos) * 0.5, 1)

    def generar_dsl(self, plan: ExecutionPlan) -> str:
        """Genera el DSL a partir del plan."""
        lines = []
        lines.append(f"# 📋 Plan: {plan.titulo}")
        lines.append(f"# Análisis: {plan.analisis}")
        lines.append(f"# Complejidad: {self.estimar_complejidad(plan)}")
        lines.append(f"# Tiempo estimado: {self.estimar_tiempo(plan)}s")
        lines.append("")

        nombre_a_paso = {p.nombre: p for p in plan.pasos}
        ejecutados = set()
        ordenados = []

        def dfs(nombre: str):
            if nombre in ejecutados:
                return
            paso = nombre_a_paso.get(nombre)
            if not paso:
                return
            for dep in paso.dependencia_ids:
                if dep not in ejecutados:
                    dfs(dep)
            ordenados.append(nombre)
            ejecutados.add(nombre)

        for paso in plan.pasos:
            if paso.nombre not in ejecutados:
                dfs(paso.nombre)

        for nombre in ordenados:
            paso = nombre_a_paso.get(nombre)
            if not paso:
                continue

            lines.append(f"@agente {paso.nombre}")
            lines.append(f"tipo: {paso.tipo_agente}")
            if paso.descripcion:
                lines.append(f"descripcion: {paso.descripcion}")
            if paso.dependencia_ids:
                lines.append(f"dependencias: {', '.join(paso.dependencia_ids)}")

            config = paso.configuracion
            tipo = paso.tipo_agente

            if tipo == 'Python':
                if config.get('codigo'):
                    lines.append("codigo: |")
                    for line in config['codigo'].split('\n'):
                        lines.append(f"    {line}")
                if config.get('timeout'):
                    lines.append(f"timeout: {config['timeout']}")

            elif tipo == 'HTTP':
                if config.get('url'):
                    lines.append(f"url: {config['url']}")
                if config.get('metodo'):
                    lines.append(f"metodo: {config['metodo']}")
                if config.get('timeout'):
                    lines.append(f"timeout: {config['timeout']}")
                if config.get('headers'):
                    lines.append(f"headers: {json.dumps(config['headers'], ensure_ascii=False)}")

            elif tipo == 'LLM':
                if config.get('prompt'):
                    lines.append("prompt: |")
                    for line in config['prompt'].split('\n'):
                        lines.append(f"    {line}")
                if config.get('modelo'):
                    lines.append(f"modelo: {config['modelo']}")
                if config.get('temperatura'):
                    lines.append(f"temperatura: {config['temperatura']}")
                if config.get('max_tokens'):
                    lines.append(f"max_tokens: {config['max_tokens']}")

            elif tipo == 'Shell':
                if config.get('comando'):
                    lines.append(f"comando: {config['comando']}")
                if config.get('timeout'):
                    lines.append(f"timeout: {config['timeout']}")
                if config.get('working_dir'):
                    lines.append(f"working_dir: {config['working_dir']}")

            elif tipo == 'File':
                if config.get('operacion'):
                    lines.append(f"operacion: {config['operacion']}")
                if config.get('archivo_origen'):
                    lines.append(f"archivo_origen: {config['archivo_origen']}")
                if config.get('archivo_destino'):
                    lines.append(f"archivo_destino: {config['archivo_destino']}")
                if config.get('modo_salida_file'):
                    lines.append(f"modo_salida_file: {config['modo_salida_file']}")

            elif tipo == 'Loop':
                if config.get('fuente_items'):
                    lines.append(f"fuente: {config['fuente_items']}")
                if config.get('codigo_por_item'):
                    lines.append("codigo: |")
                    for line in config['codigo_por_item'].split('\n'):
                        lines.append(f"    {line}")
                if config.get('max_iteraciones'):
                    lines.append(f"max_iteraciones: {config['max_iteraciones']}")
                if config.get('timeout_loop'):
                    lines.append(f"timeout_loop: {config['timeout_loop']}")

            lines.append("")
            if paso.justificacion:
                lines.append(f"# Justificación: {paso.justificacion}")
                lines.append("")

        return '\n'.join(lines)


# ============================================================
# FUNCIONES DE AYUDA
# ============================================================

def ejecutar_plan_prueba(plan: ExecutionPlan, mostrar_detalle: bool = True):
    """Ejecuta un plan generado para pruebas."""
    from core.scheduler import Scheduler

    print("\n" + "=" * 70)
    print("🚀 EJECUTANDO PLAN DE PRUEBA")
    print("=" * 70)

    if not plan.agentes_generados:
        print("❌ No hay agentes para ejecutar")
        return None

    scheduler = Scheduler(max_concurrent=2)

    print(f"\n📦 Registrando {len(plan.agentes_generados)} agente(s):")
    for agente in plan.agentes_generados:
        scheduler.agregar_agente(agente)
        print(f"   ✅ {agente.nombre} ({agente.tipo.value})")
        if agente.dependencias_nombres:
            print(f"      ↳ Depende de: {', '.join(agente.dependencias_nombres)}")

    print("\n🔗 Resolviendo dependencias...")
    scheduler.resolver_dependencias()

    tiene_ciclos, ciclos = scheduler.detectar_ciclos()
    if tiene_ciclos:
        print(f"❌ Se detectaron ciclos: {ciclos}")
        return None

    print("\n▶️ Iniciando ejecución...\n")
    scheduler.iniciar()

    inicio = time.time()
    ultimo_progreso = -1

    while scheduler.ejecutando:
        time.sleep(0.3)
        stats = scheduler.obtener_estadisticas()
        total = stats.get('total', 0)
        completados = stats.get('completados', 0)
        errores = stats.get('errores', 0)
        cancelados = stats.get('cancelados', 0)
        ejecutando = stats.get('ejecutando', 0)

        if total > 0:
            progreso = completados + errores + cancelados
            if progreso != ultimo_progreso:
                ultimo_progreso = progreso
                elapsed = time.time() - inicio
                barra = "█" * int((progreso / total) * 20) + "░" * (20 - int((progreso / total) * 20))
                print(
                    f"\r   [{barra}] {progreso}/{total} | "
                    f"✅{completados} ❌{errores} ⚡{ejecutando} ⏱{elapsed:.1f}s",
                    end=''
                )

    print("\n")

    elapsed = time.time() - inicio
    stats = scheduler.obtener_estadisticas()

    print("=" * 70)
    print("📊 RESUMEN DE EJECUCIÓN")
    print("=" * 70)
    print(f"⏱ Tiempo total: {elapsed:.2f}s")
    print(f"📊 Total agentes: {stats['total']}")
    print(f"   ✅ Completados: {stats['completados']}")
    print(f"   ❌ Errores: {stats['errores']}")
    print(f"   ⛔ Cancelados: {stats['cancelados']}")

    if mostrar_detalle:
        print("\n📋 DETALLE POR AGENTE:")
        for agente in scheduler.agentes.values():
            if agente.estado.value == "Completado":
                print(f"   ✅ {agente.nombre}: {agente.mensaje}")
                if agente.resultado:
                    resumen = str(agente.resultado)
                    if len(resumen) > 200:
                        resumen = resumen[:200] + "..."
                    print(f"      📊 Resultado: {resumen}")
            elif agente.estado.value == "Error":
                print(f"   ❌ {agente.nombre}: {agente.error}")
            elif agente.estado.value == "Cancelado":
                print(f"   ⛔ {agente.nombre}: Cancelado")

    print("\n" + "=" * 70)
    print("✅ EJECUCIÓN COMPLETADA")
    print("=" * 70)

    return scheduler


# ============================================================
# EJECUCIÓN DE PRUEBA
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("=" * 70)
    print("🧠 PROBLEM SOLVER - PRUEBA DE CONCEPTO")
    print("=" * 70)

    if not os.environ.get('DEEPSEEK_API_KEY'):
        print("\n⚠️  No se encontró DEEPSEEK_API_KEY en variables de entorno.")
        print("   Configúrala con: export DEEPSEEK_API_KEY='tu-api-key'")
        sys.exit(0)

    problema = input("\n📝 Describe el problema que quieres resolver: ")
    if not problema.strip():
        problema = "Conseguir los programas más forkeados de GitHub y guardar como github10.txt"
        print(f"   Usando problema de ejemplo: {problema}")

    print("\n🚀 Generando plan... (esto puede tomar unos segundos)\n")

    try:
        solver = ProblemSolver()
        plan = solver.resolver_problema(problema, max_pasos=6)

        print("✅ PLAN GENERADO EXITOSAMENTE")
        print(f"📌 Título: {plan.titulo}")
        print(f"📊 Pasos: {len(plan.pasos)}")
        print(f"🤖 Agentes: {len(plan.agentes_generados)}")
        print(f"📈 Complejidad: {solver.estimar_complejidad(plan)}")
        print(f"⏱ Tiempo estimado: {solver.estimar_tiempo(plan)}s")

        if plan.advertencias:
            print("\n⚠️ ADVERTENCIAS:")
            for adv in plan.advertencias:
                print(f"   • {adv}")

        print("\n📋 DETALLE DE PASOS:")
        for paso in plan.pasos:
            print(f"   {paso.orden}. {paso.nombre} ({paso.tipo_agente})")
            if paso.dependencia_ids:
                print(f"      ↳ Depende de: {', '.join(paso.dependencia_ids)}")
            print(f"      📝 {paso.descripcion[:60]}...")
            config = paso.configuracion
            if 'operacion' in config:
                print(f"      ⚙️ Operación: {config.get('operacion')}")
            if 'url' in config:
                print(f"      🔗 URL: {config.get('url')[:60]}...")
            if 'archivo_destino' in config:
                print(f"      📁 Destino: {config.get('archivo_destino')}")

        print("\n📄 DSL GENERADO:")
        print("-" * 50)
        print(solver.generar_dsl(plan))
        print("-" * 50)

        print("\n" + "-" * 50)
        respuesta = input("🔧 ¿Ejecutar el plan ahora? (s/N): ").strip().lower()
        if respuesta in ('s', 'si', 'sí', 'y', 'yes'):
            ejecutar_plan_prueba(plan, mostrar_detalle=True)
        else:
            print("\nℹ️ Puedes ejecutar el plan más tarde desde la interfaz principal.")

        print("\n✅ Prueba completada con éxito.")

    except KeyboardInterrupt:
        print("\n\n⏹ Ejecución interrumpida por el usuario")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Error en la prueba: {e}")
        import traceback
        traceback.print_exc()