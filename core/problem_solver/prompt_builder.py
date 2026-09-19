# core/problem_solver/prompt_builder.py
"""
PromptBuilder: construcción modular de prompts (system/user) para el LLM.
Extraído literalmente de core/problem_solver.py (monolito) — Paso 3.

⚠️ NO modificado respecto al original. Los strings generados por
build_system_prompt() / build_user_prompt() deben ser byte-idénticos
a los del monolito (verifica con diff, ver plan de refactor).
"""
import json


class PromptBuilder:
    """
    Construye prompts para el LLM de forma modular y configurable.
    """
    # Reglas para generación de código Python
    PYTHON_RULES = [
        "**CONTRATO DE SALIDA LLM — CÓMO LEERLO**: El resultado de un agente LLM NO es la respuesta directa. Es un dict con:",
        "  - `contexto['NombreLLM']['json']`: el JSON parseado de la respuesta del LLM (si el LLM devolvió JSON válido)",
        "  - `contexto['NombreLLM']['respuesta_limpia']`: la respuesta como string, sin fences markdown",
        "  - `contexto['NombreLLM']['respuesta']`: la respuesta cruda (puede tener ```json ...```)",
        "  - `contexto['NombreLLM']['tokens_uso']`, `['modelo']`, `['tiempo_respuesta']`: metadatos",
        "",
        "⚠️ REGLA: Si necesitas el valor útil que produjo el LLM, USA `contexto['LLM']['json']`.",
        "NO pases `contexto['LLM']` completo al siguiente paso. Extrae solo lo que necesitas.",
        "",
        "❌ INCORRECTO (pasa el dict completo):",
        "```python",
        "resultado = {'resumen': contexto.get('ResumirPerfil', {})}",
        "```",
        "",
        "✅ CORRECTO (extrae el campo útil):",
        "```python",
        "llm = contexto.get('ResumirPerfil', {})",
        "datos = llm.get('json', {}) if isinstance(llm.get('json'), dict) else {}",
        "resultado = {'resumen': datos.get('resumen', '')}",
        "```",
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
        "**PARA LOOPS — CONTRATO DE FUENTE ITEMS**:",
        "  - `fuente_items` debe tener la forma `'NombreDependencia.clave'`.",
        "  - La dependencia DEBE producir un resultado con esa clave exacta.",
        "  - Si el productor devuelve `{'lotes': [...]}`, entonces usa `'X.lotes'`.",
        "  - Si el productor devuelve `{'items': [...]}`, entonces usa `'X.items'`.",
        "  - ⚠️ NUNCA uses `fuente_items: 'X.y'` si X no produce la clave `y`.",
        "  - ⚠️ ANTES de declarar el Loop, comprueba mentalmente que el código",
        "    del productor incluye una clave con ese nombre en su `resultado`.",
        "",
        "**NUNCA uses nombres de agentes como variables Python**:",
        "  - ❌ INCORRECTO: `entrada = GenerarListaItems`",
        "  - ✅ CORRECTO: `entrada = contexto.get('GenerarListaItems', {})`",
        "  - Esto es CRÍTICO: los nombres de agentes NO existen como variables",
        "    en el sandbox. Solo existen dentro del dict `contexto`.",
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
        "**CUÁNDO USAR LOOP vs PYTHON PURO**:",
        "  Elige la herramienta según el caso. NO uses Loop por defecto:",
        "",
        "  ✅ USA `Loop` cuando:",
        "    - Cada item puede fallar de forma independiente y quieres aislar los fallos.",
        "    - Cada item hace una operación lenta (HTTP, IO pesado) y quieres timeout por item.",
        "    - Necesitas cancelación granular.",
        "    - El código por item es complejo o tiene side effects.",
        "",
        "  ✅ USA `Python` puro cuando:",
        "    - El procesamiento es rápido y no necesita aislamiento por item.",
        "    - Todos los items se procesan en la misma pasada (agregaciones, sumas).",
        "    - El código es trivial (escribir a archivo, contar, filtrar).",
        "    - El número de items es alto (100+) y el overhead del Loop sería grande.",
        "",
          "⚠️ En caso de duda, prefiere `Python` puro. El Loop añade overhead.",
        # ✅ Fix 4: regla de indentación estricta
        "**INDENTACIÓN OBLIGATORIA**: El código Python DEBE usar SIEMPRE "
        "4 espacios por nivel de indentación. NUNCA uses tabulaciones, "
        "2 espacios, ni mezcles espacios y tabulaciones. El sandbox "
        "rechaza cualquier código cuya indentación no sea múltiplo de 4.",
        "Ejemplo CORRECTO:",
        "  ```python",
        "  def suma(a, b):",
        "      return a + b",
        "  ",
        "  resultado = {'total': suma(2, 3)}",
        "  ```",
        "Ejemplo INCORRECTO (NO HACER):",
        "  ```python",
        "  def suma(a, b):",
        "    return a + b   # ⚠ 2 espacios, MAL",
        "  ```",
    ]

    FILE_RULES = [
        "**CONTRATO DE File 'escribir'**: El contenido viene del resultado de",
        "una dependencia. Si no hay dependencia, falla con 'no_content'.",
        "",
        "**CONTRATO DE File 'leer'**: Devuelve el contenido del archivo en la",
        "clave 'contenido'. Si el archivo no existe, falla.",
        "",
        "**CONTRATO DE File 'copiar'/'mover'**: El origen debe existir en disco.",
        "La dependencia solo garantiza el orden de ejecución.",
        "",
        "**PATRÓN CORRECTO para crear un archivo con contenido fijo**:",
        "  Paso 1 (Python): escribe el archivo con open() y devuelve el contenido.",
        "  NO uses un agente File 'escribir' sin dependencia.",
        "",
        "**PATRÓN CORRECTO para crear un archivo con contenido generado**:",
        "  Paso 1 (Python o LLM): produce `resultado = {'contenido': '...'}`.",
        "  Paso 2 (File 'escribir', depende del paso 1): escribe el archivo.",
    ]

    # Tipos de agentes disponibles
    AGENT_TYPES = {
        "Python": "Procesamiento de datos, transformaciones, lógica compleja, cálculos.",
        "Shell": "Comandos de terminal, operaciones de sistema, scripts.",
        "HTTP": "Obtener datos de APIs REST que devuelven JSON. NO sirve para páginas HTML normales: para eso usa Browser.",
        "LLM": "Análisis de texto, clasificación, resumen, generación de contenido, traducción.",
        "File": "Lectura/escritura de archivos, persistencia de resultados.",
        "Loop": "Procesamiento en lote sobre una lista de items.",
        "Browser": (
            "Navega una URL real con un navegador (Playwright/Chromium) y ejecuta "
            "acciones declarativas: esperar, extraer, click, rellenar, scroll, "
            "screenshot, ejecutar_js, navegar. En 'extraer', el formato "
            "'texto_principal' devuelve el contenido útil de la página sin "
            "menús, cabeceras, pies ni avisos de cookies (recomendado para "
            "artículos y páginas de contenido). Devuelve el HTML final, el texto "
            "visible, el título y los datos extraídos. Úsalo cuando la información "
            "esté en una página HTML (no en una API JSON) o requiera interacción."
        ),
        "Search": (
            "Busca información en la web (DuckDuckGo, sin API key) y devuelve una "
            "lista de resultados con título, URL y snippet. Úsalo para descubrir "
            "URLs o datos actuales que no conoces de antemano; después puedes "
            "navegar una de esas URLs con Browser."
        ),
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
        "Browser": {
            "descripcion": (
                "Con 'url' navega UNA página. Con 'urls_desde' navega varias "
                "URLs de una lista del contexto y devuelve un resultado agregado."
            ),
            "claves": {
                # Modo una URL
                "url_final": "str - URL tras redirecciones y acciones (modo 'url')",
                "titulo": "str - título (<title>) de la página (modo 'url')",
                "html": "str - HTML final de la página (modo 'url')",
                "texto": "str - texto visible del body (modo 'url')",
                "datos_extraidos": "dict - {nombre: valor} de 'extraer'/'ejecutar_js' (modo 'url')",
                "acciones_ejecutadas": "list - [{tipo, ok, error|detalle}] (modo 'url')",
                "screenshots": "list - rutas de las capturas guardadas",
                "html_truncado": "bool - True si 'html' se recortó por tamaño (modo 'url')",
                # Modo multi-URL ('urls_desde')
                "urls_navegadas": "int - nº de URLs procesadas (modo 'urls_desde')",
                "resultados_por_url": (
                    "list - [{url, titulo, datos_extraidos, acciones_ejecutadas, error}] "
                    "(modo 'urls_desde')"
                ),
                "errores": "list - [{url, error}] de las URLs que fallaron (modo 'urls_desde')",
                "error": "str|None - None si todo fue bien",
            },
        },
        "Search": {
            "descripcion": "El executor busca en la web y devuelve una lista de resultados.",
            "claves": {
                "query": "str - consulta ejecutada",
                "resultados": "list - [{title, href, body}]",
                "total": "int - número de resultados devueltos",
                "error": "str|None - None si todo fue bien",
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
        "**ACCESO A DEPENDENCIAS DESDE SHELL**: Los agentes Shell NO tienen",
        "acceso directo al `contexto` de Python. Si necesitas usar un valor",
        "calculado por un agente anterior (por ejemplo, un nombre de servicio,",
        "un ID, una ruta), el patrón correcto es:",
        "  1. Un agente Python previo calcula el valor y lo devuelve en su `resultado`.",
        "  2. El agente Shell usa la sintaxis `{NombreDependencia.clave}` en el",
        "     comando, que el sistema sustituye automáticamente antes de ejecutarlo.",
        "",
        "Ejemplo CORRECTO:",
        "  - `SeleccionarServicio` (Python) → `resultado = {'servicio_objetivo': 'nginx.service'}`",
        "  - `EjecutarAccionServicio` (Shell) → `comando: 'systemctl start {SeleccionarServicio.servicio_objetivo}'`",
        "",
        "El sistema sustituye `{SeleccionarServicio.servicio_objetivo}` por el valor",
        "real antes de ejecutar el comando.",
        "",
        "❌ INCORRECTO: volver a ejecutar `systemctl list-units` dentro del paso 5",
        "   para obtener el servicio. Duplica trabajo y rompe la lógica del DAG.",
        "❌ INCORRECTO: intentar usar `contexto.get(...)` dentro de un comando Shell.",
        "   Eso es sintaxis Python, no Shell.",
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
            "timeout": 60,
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
            "modelo": "deepseek-v4-pro",
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
        "Browser": {
            "url": "URL a navegar (una sola página)",
            "acciones": [
                {"tipo": "esperar", "selector": "CSS", "timeout": 10000},
                {"tipo": "extraer", "selector": "CSS",
                 "formato": "html|text|attr|texto_principal",
                 "nombre": "clave", "atributo": "href", "multiple": False},
                {"tipo": "click", "selector": "CSS"},
                {"tipo": "rellenar", "selector": "CSS", "valor": "texto"},
                {"tipo": "scroll", "hasta": "bottom|top|CSS"},
                {"tipo": "screenshot", "nombre": "captura.png", "full_page": False},
                {"tipo": "ejecutar_js", "script": "expresión JS", "nombre": "clave"},
                {"tipo": "navegar", "url": "URL"},
            ],
            "urls_desde": "NombreAgente.clave (lista de URLs; alternativa a 'url')",
            "max_urls": 5,
            "acciones_por_url": [
                {"tipo": "extraer", "selector": "article, main",
                 "formato": "texto_principal", "nombre": "contenido",
                 "multiple": False},
            ],
            "timeout": 30,
            "timeout_accion": 10000,
            "headless": True,
            "bloquear_recursos": False,
            "user_agent": "",
        },
        "Search": {
            "query": "consulta de búsqueda",
            "max_resultados": 5,
            "region": "wt-wt",
            "timeout": 30,
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
        "**Sin Repeticiones**: NUNCA repitas la misma sección, lista o párrafo dos veces. "
        "Si el contenido generado no llega a la extensión pedida, AMPLÍA con nuevo material, "
        "no recicles el anterior. Un documento con secciones duplicadas es un documento roto.",
        # ✅ NUEVO: evitar pasos masivos
        "**Paso atómico, no masivo**: Un solo agente Python NO debe procesar "
        "decenas de archivos a la vez si cada uno requiere IO pesado. "
        "Divide en: (1) un paso que lista, (2) un Loop que procesa cada item, "
        "o (3) limita el número de archivos a un máximo razonable (10-20). "
        "El sandbox tiene timeout por paso; un paso que hace demasiado falla.",
        "**Loop para IO pesado**: Si vas a leer/analizar N archivos o "
        "hacer N peticiones HTTP, usa un agente Loop con `continuar_en_error=true` "
        "y un timeout por item. NO hagas un bucle implícito dentro de un "
        "agente Python: bloqueará el sandbox.",
        "**RESPETA EL FORMATO PEDIDO**: Si el problema especifica un formato de "
        "archivo concreto (por ejemplo .html, .docx, .pdf, .md, .csv, .json, .svg, "
        ".xml), el paso File de escritura DEBE usar esa extensión en "
        "'archivo_destino'. NUNCA uses nombres genéricos como 'salida.txt', "
        "'output.txt' o 'resultado.txt' si el problema especifica un formato.",
        "**CUBRE TODOS LOS REQUISITOS EXPLÍCITOS**: Si el problema menciona "
        "requisitos explícitos (N elementos, formato concreto, características "
        "específicas), asegúrate de que el plan incluye pasos para cumplirlos. "
        "Antes de responder, repasa el enunciado y comprueba que cada requisito "
        "tiene al menos un paso que lo produce.",
        "**CÓMO ELEGIR ENTRE HTTP Y BROWSER**:\n"
        "  - HTTP solo sirve para URLs que devuelven JSON (APIs REST).\n"
        "  - Si la URL devuelve HTML (una página web), usa Browser.\n"
        "  - Si no sabes si devuelve JSON o HTML, usa Browser (siempre funciona).\n"
        "  - NUNCA hagas json.loads() sobre el body de una URL que devuelve HTML.",
        "**PATRÓN PARA BUSCAR Y EXTRAER DATOS DE LA WEB**:\n"
        "  - Si necesitas información actual de la web, sigue este patrón:\n"
        "    1. `Search` para descubrir URLs relevantes.\n"
        "    2. `Browser` para navegar a esas URLs y extraer el contenido REAL "
        "(títulos, párrafos, tablas, etc.).\n"
        "    3. `LLM` si necesitas traducir, resumir o estructurar el contenido.\n"
        "    4. `File` para guardar el resultado.\n"
        "  - Los snippets de `Search` son SOLO pistas (título + descripción corta "
        "del buscador). NO son el contenido real. Para obtener el contenido real, "
        "navega a la URL con `Browser`.\n"
        "  - Si el problema pide 'traducir', el texto a traducir DEBE estar en el "
        "idioma original. Si el texto ya está en el idioma destino, no hay nada que "
        "traducir. Asegúrate de obtener el texto en su idioma original navegando a "
        "la fuente original, no a un resumen localizado.",
        "**NAVEGAR VARIAS URLs CON UN SOLO PASO**: Para navegar N URLs de un "
        "`Search`, usa `urls_desde` en el Browser en lugar de un Loop. Ejemplo:\n"
        "  tipo: Browser\n"
        "  configuracion:\n"
        "    urls_desde: 'Buscar.resultados'\n"
        "    max_urls: 5\n"
        "    acciones_por_url:\n"
        "      - tipo: extraer\n"
        "        selector: 'article, main'\n"
        "        formato: texto_principal\n"
        "        nombre: contenido\n"
        "  NO uses un Loop para navegar: el código Python de un Loop no puede "
        "invocar Browser. Un solo paso Browser con `urls_desde` recorre la lista "
        "y devuelve `resultados_por_url`.",
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
          - Contrato genérico de cruce de datos
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
        file_rules = "\n".join(f"  - {rule}" for rule in cls.FILE_RULES)
        design_rules = "\n".join(
            f"{i+1}. {rule}" for i, rule in enumerate(cls.DESIGN_RULES)
        )
        field_rules = "\n".join(
            f"{i+1}. {rule}" for i, rule in enumerate(cls.FIELD_RULES)
        )
        # ── 4. Contratos de salida por tipo (claves exactas) ──
        contratos = cls._formatear_contratos()

        # ── 5. Contrato genérico de cruce de datos entre agentes ──
        contrato_cruce_datos = """
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

## ⚠️ REGLAS CRÍTICAS PARA CÓDIGO PYTHON ⚠️

{python_rules}

## ⚠️ REGLAS CRÍTICAS PARA INDENTACIÓN DE PYTHON ⚠️

- El código Python en `configuracion.codigo` DEBE tener indentación con 4 espacios por nivel.
- NUNCA uses tabulaciones ni 2 espacios.
- El sandbox rechaza cualquier código con indentación incorrecta.
- Si dudas, deja el código Python SIN indentación (una sola línea o sin bloques).

## ⚠️ REGLAS CRÍTICAS PARA COMANDOS SHELL ⚠️

{shell_rules}

## ⚠️ REGLAS CRÍTICAS PARA AGENTES File ⚠️

{file_rules}

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

{contrato_cruce_datos}

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
        contexto_extra: dict | None = None,
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
            2. ¿El paso final de File (si aplica) recibe un dict con las claves correctas?
            3. ¿Las dependencias forman un DAG válido (sin ciclos, sin nombres inexistentes)?
            4. ¿El JSON es válido y no tiene comas finales ni texto adicional?

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