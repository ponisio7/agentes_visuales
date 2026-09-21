# core/problem_solver/validator.py
"""
PlanValidator: validación de planes y configuración de pasos.
Extraído de los métodos privados de ProblemSolver (monolito) — Paso 5,
Opción B (clase con estado, instanciada con `logger` en el `__init__`
de ProblemSolver).

Métodos migrados desde el monolito (misma lógica; solo cambia el "self"
de ProblemSolver por el de PlanValidator, y se hacen públicos los que
`solver.py` necesita llamar desde fuera):

    _validar_configuracion_paso  -> validar_configuracion_paso   (público)
    _validar_campos_configuracion -> validar_campos_configuracion (público)
    _validar_plan                -> validar_plan                 (público)
    _detectar_ciclos             -> _detectar_ciclos              (privado, solo lo usa validar_plan)

⚠️ Único cambio real de comportamiento (necesario para desacoplar de
ProblemSolver): el monolito usaba `self._plan_actual` (estado de instancia
de ProblemSolver, asignado en `resolver_problema`) dentro de
`_validar_campos_configuracion` para poder anexarle advertencias al plan
en curso. Aquí se recibe como parámetro explícito `plan_actual`
(por defecto None) — `solver.py` debe pasar su propio `self._plan_actual`.

⚠️ Preservada literalmente una inconsistencia del original: en
`validar_configuracion_paso`, la advertencia de `max_tokens` insuficiente
usa el logger de MÓDULO (`logger`, definido con `logging.getLogger(__name__)`
a nivel de archivo en el monolito), no `self.logger` como el resto de
advertencias de este mismo método. Se replica tal cual para no alterar
el comportamiento (incluye el nombre del logger que queda en los logs).
"""
import ast
import builtins
import json
import logging
import os
import re

from core.agent import TipoAgente
from core.ia_config import modelo_por_defecto
from core.sandbox_contract import NOMBRES_INYECTADOS
from core.utils import LITERALES_FABRICADOS

from .constants import CAMPOS_VALIDOS_POR_TIPO
from .models import ExecutionPlan, StepPlan
from .prompt_builder import PromptBuilder

logger = logging.getLogger(__name__)

# Alias que el LLM inventa con frecuencia para referirse a las dependencias.
# En el sandbox la ÚNICA variable disponible es ``contexto`` (ver
# ``core/problem_solver/prompt_builder.py`` y ``core/sandbox.py``), así que
# leer cualquiera de estos nombres provoca ``NameError`` en tiempo de
# ejecución. Se detectan como variable suelta igual que los nombres de agente.
ALIAS_CONTEXTO_PROHIBIDOS = {"dependencias"}

# Nombres que Python y el sandbox ya definen: usarlos NO es un error.
_DUNDERS = frozenset({
    "__name__", "__file__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__debug__", "__class__", "__qualname__",
    "__module__", "__annotations__", "__dict__",
})
NOMBRES_CONOCIDOS = (
    frozenset(builtins.dir(builtins)) | set(NOMBRES_INYECTADOS) | _DUNDERS
)


def _construir_claves_salida() -> dict[str, frozenset]:
    """Claves que cada tipo de agente puede devolver en su resultado.

    Fuente única de verdad: el contrato documentado del prompt más las
    claves extra que los ejecutores devuelven de hecho.
    """
    salida: dict[str, set] = {}
    for tipo, info in PromptBuilder.CONTRATOS_SALIDA.items():
        claves = info.get("claves")
        if isinstance(claves, dict):
            salida[tipo] = set(claves)
    for tipo, extra in getattr(PromptBuilder, "CLAVES_EXTRA_SALIDA", {}).items():
        salida.setdefault(tipo, set()).update(extra)
    return {tipo: frozenset(claves) for tipo, claves in salida.items()}


CLAVES_SALIDA_POR_TIPO = _construir_claves_salida()

# Unión de todas las claves conocidas. La regla 7 solo actúa si la clave
# pertenece a OTRO tipo de agente (p. ej. ``body`` es de HTTP y se lee de un
# LLM). Así una clave válida pero no documentada no genera falsos positivos.
UNION_CLAVES_SALIDA = frozenset().union(*CLAVES_SALIDA_POR_TIPO.values())


def _texto_constante(nodo) -> str | None:
    """Valor de un literal str (o None si no lo es)."""
    if isinstance(nodo, ast.Constant) and isinstance(nodo.value, str):
        return nodo.value
    return None


def _nombre_dependencia(expr, nombres_agentes: set) -> str | None:
    """Nombre de agente leído del contexto en ``expr``.

    Reconoce ``contexto.get('Dep', ...)`` y ``contexto['Dep']``.
    """
    if isinstance(expr, ast.Call):
        funcion = expr.func
        if (
            isinstance(funcion, ast.Attribute)
            and funcion.attr == "get"
            and isinstance(funcion.value, ast.Name)
            and funcion.value.id == "contexto"
            and expr.args
        ):
            nombre = _texto_constante(expr.args[0])
            if nombre in nombres_agentes:
                return nombre
        return None
    if isinstance(expr, ast.Subscript):
        if (
            isinstance(expr.value, ast.Name)
            and expr.value.id == "contexto"
        ):
            nombre = _texto_constante(expr.slice)
            if nombre in nombres_agentes:
                return nombre
    return None


class PlanValidator:
    """Valida y completa configuración de pasos, y valida el plan completo."""

    CAMPOS_VALIDOS_POR_TIPO = CAMPOS_VALIDOS_POR_TIPO

    def __init__(self, logger):
        self.logger = logger

    def validar_configuracion_paso(self, paso: StepPlan):
        """
        Valida y completa la configuración mínima según el tipo.

        ✅ ADEMÁS: elimina campos desconocidos de 'configuracion' para evitar
        que lleguen al constructor de Agente o queden ocultos en el plan.
        """
        tipo = paso.tipo_agente
        # El LLM puede devolver "configuracion": null; normalizar antes de
        # mutar para no lanzar AttributeError y abortar todo el plan.
        config = paso.configuracion or {}
        paso.configuracion = config

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
                config['prompt'] = "Analiza el siguiente contexto:\n{contexto}"
            if 'modelo' not in config:
                # H1: respeta el modelo configurado por el usuario
                # (DEEPSEEK_MODEL / archivo de configuración).
                config['modelo'] = modelo_por_defecto()
            if 'temperatura' not in config:
                config['temperatura'] = 0.7

            # ✅ Valor por defecto más alto: 4000 tokens
            if 'max_tokens' not in config:
                config['max_tokens'] = 4000

            # ✅ Blindaje: subir a un mínimo seguro si viene bajo
            MIN_TOKENS_SEGUROS = 4000
            try:
                max_tokens_actual = int(float(config['max_tokens']))
            except (TypeError, ValueError):
                max_tokens_actual = 0
            if max_tokens_actual < MIN_TOKENS_SEGUROS:
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
                config['timeout'] = 60

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

        elif tipo == 'Browser':
            # No se inventan 'url' ni 'acciones': si faltan, la validación
            # del agente lo reportará como advertencia (mejor transparente
            # que ejecutar una navegación a una URL inventada).
            if not isinstance(config.get('acciones'), list):
                config['acciones'] = []
            if 'timeout' not in config:
                config['timeout'] = 30
            if 'timeout_accion' not in config:
                config['timeout_accion'] = 10000
            if 'headless' not in config:
                config['headless'] = True
            if 'bloquear_recursos' not in config:
                config['bloquear_recursos'] = False
            if 'user_agent' not in config:
                config['user_agent'] = ''

        elif tipo == 'Search':
            # Igual que en Browser: 'query' no se inventa.
            if 'max_resultados' not in config:
                config['max_resultados'] = 5
            if 'region' not in config:
                config['region'] = 'wt-wt'
            if 'timeout' not in config:
                config['timeout'] = 30

    def validar_campos_configuracion(
        self,
        paso: StepPlan,
        tipo: TipoAgente,
        plan_actual: ExecutionPlan | None = None,
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

        if plan_actual is not None:
            plan_actual.advertencias.append(advertencia)

    def validar_plan(self, plan: ExecutionPlan) -> tuple[bool, list[str]]:
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

        # ── NUEVO: validar código Python de cada paso ──
        nombres_agentes = {p.nombre for p in plan.pasos}
        tipos_agentes = {p.nombre: p.tipo_agente for p in plan.pasos}
        for paso in plan.pasos:
            if getattr(paso, "tipo_agente", None) == "Python":
                errores.extend(
                    self._validar_codigo_python(paso, nombres_agentes, tipos_agentes)
                )

        # ── NUEVO: contratos de los pasos File ──
        errores.extend(self._validar_pasos_file(plan))

        # ── NUEVO: rechazo estático de contratos de aceptación imposibles ──
        errores.extend(self._validar_contratos_aceptacion(plan))

        return len(errores) == 0, errores

    @staticmethod
    def _validar_pasos_file(plan: ExecutionPlan) -> list[str]:
        """Valida contratos de los pasos File que bloquean en runtime.

        - ``copiar``/``mover`` con ``archivo_origen == archivo_destino``:
          revienta con ``SameFileError`` y dispara un Plan B inútil.
        - ``escribir`` sin dependencias: el contenido de un agente File sale
          del resultado de su dependencia; sin ella no hay fuente y falla con
          ``no_content``.
        """
        errores = []
        for paso in plan.pasos:
            if getattr(paso, "tipo_agente", None) != "File":
                continue
            config = paso.configuracion or {}
            operacion = (config.get("operacion") or "").lower()
            origen = config.get("archivo_origen") or ""
            destino = config.get("archivo_destino") or ""

            if (
                operacion in ("copiar", "mover")
                and origen
                and destino
                and os.path.normpath(origen) == os.path.normpath(destino)
            ):
                errores.append(
                    f"BLOQUEANTE: '{paso.nombre}': operación '{operacion}' con "
                    f"archivo_origen == archivo_destino ('{destino}'); no aporta "
                    f"nada y falla con SameFileError. Elimina el paso o cambia "
                    f"el destino."
                )

            if operacion == "escribir" and not paso.dependencia_ids:
                errores.append(
                    f"BLOQUEANTE: '{paso.nombre}': File 'escribir' sin "
                    f"dependencias. El contenido se toma del resultado de una "
                    f"dependencia; sin ella no hay fuente de contenido."
                )
        return errores

    @staticmethod
    def _validar_contratos_aceptacion(plan: ExecutionPlan) -> list[str]:
        """Rechaza de forma estática los contratos de aceptación imposibles.

        Solo se comprueba lo comprobable sin ejecutar:

        - un archivo exigido por el contrato que ningún paso del plan produce;
        - un contrato que exige imágenes incrustadas cuando ni el paso ni sus
          dependencias pueden producir una imagen raster.

        Es preferible fallar al validar (y regenerar el plan con el motivo)
        que arrastrar una ejecución condenada a fallar la aceptación.
        """
        errores: list[str] = []
        pasos_por_nombre = {p.nombre: p for p in plan.pasos}
        texto_plan = PlanValidator._texto_plan(plan)

        for paso in plan.pasos:
            contrato = getattr(paso, "aceptacion", None)
            if contrato is None or getattr(contrato, "es_vacio", lambda: True)():
                continue

            # ── 1. El artefacto declarado debe producirlo algún paso ──
            for ruta in list(getattr(contrato, "archivos", []) or []) + list(
                getattr(contrato, "imagenes", []) or []
            ):
                base = os.path.basename(str(ruta)).lower().strip()
                if base and base not in texto_plan:
                    errores.append(
                        f"BLOQUEANTE: '{paso.nombre}': el contrato de aceptación "
                        f"exige '{ruta}', pero ningún paso del plan produce ese "
                        f"archivo (ninguno lo menciona)."
                    )

            # ── 2. Imágenes exigidas: debe existir una fuente raster ──
            min_imagenes = int(getattr(contrato, "min_imagenes", 0) or 0)
            if getattr(contrato, "requiere_imagen", False) and min_imagenes <= 0:
                min_imagenes = 1
            if min_imagenes > 0:
                fuentes = [paso]
                fuentes.extend(
                    pasos_por_nombre[nombre]
                    for nombre in (paso.dependencia_ids or [])
                    if nombre in pasos_por_nombre
                )
                if not any(
                    PlanValidator._menciona_imagen(fuente) for fuente in fuentes
                ):
                    errores.append(
                        f"BLOQUEANTE: '{paso.nombre}': el contrato exige "
                        f"{min_imagenes} imagen(es) raster incrustada(s), pero ni "
                        f"el paso ni sus dependencias producen ninguna imagen "
                        f"(usa 'preparar_imagen' con bytes raster reales o quita "
                        f"el invariante)."
                    )

        return errores

    @staticmethod
    def _texto_plan(plan: ExecutionPlan) -> str:
        """Todo el texto comprobable del plan, en minúsculas."""
        partes: list[str] = []
        for paso in plan.pasos:
            partes.append(str(paso.nombre))
            partes.append(str(paso.descripcion or ""))
            try:
                partes.append(json.dumps(paso.configuracion or {}, default=str))
            except (TypeError, ValueError):
                partes.append(str(paso.configuracion or {}))
        return "\n".join(partes).lower()

    @staticmethod
    def _menciona_imagen(paso: StepPlan) -> bool:
        """Heurística estática: ¿este paso puede producir una imagen raster?"""
        tokens = (
            "imagen", "imágenes", "image", "ilustracion", "ilustración",
            "foto", "dibujo", "grafico", "gráfico", "preparar_imagen",
            "png", "jpg", "jpeg", "webp", "tiff", "bmp",
            "pil", "pillow", "bytesio", "matplotlib", "plot",
        )
        try:
            texto = json.dumps(paso.configuracion or {}, default=str).lower()
        except (TypeError, ValueError):
            texto = str(paso.configuracion or {}).lower()
        texto += "\n" + str(paso.descripcion or "").lower()
        contrato = getattr(paso, "aceptacion", None)
        if contrato is not None and getattr(contrato, "imagenes", None):
            texto += "\n" + " ".join(contrato.imagenes).lower()
        return any(token in texto for token in tokens)

    @staticmethod
    def _nombres_ligados(arbol: ast.AST) -> set:
        """Nombres que el propio código define antes de usarlos.

        Cubre asignaciones, parámetros, bucles, ``with ... as``,
        ``except ... as``, imports, walrus y ``global``/``nonlocal``. Se usa
        para no marcar como error un alias de contexto que el código
        redefine localmente (``dependencias = contexto`` es legítimo).
        """
        ligados: set = set()
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Name) and isinstance(nodo.ctx, (ast.Store, ast.Del)):
                ligados.add(nodo.id)
            elif isinstance(nodo, ast.arg):
                ligados.add(nodo.arg)
            elif isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                ligados.add(nodo.name)
            elif isinstance(nodo, ast.ExceptHandler) and nodo.name:
                ligados.add(nodo.name)
            elif isinstance(nodo, ast.alias):
                ligados.add((nodo.asname or nodo.name).split(".")[0])
            elif isinstance(nodo, (ast.Global, ast.Nonlocal)):
                ligados.update(nodo.names)
        return ligados

    @staticmethod
    def _validar_codigo_python_ast(
        codigo: str,
        nombre: str,
        nombres_agentes: set,
        tipos_agentes: dict | None = None,
    ) -> list[str]:
        """
        Función pura. Valida un bloque de código. Misma regla para PlanValidator y PlanRecovery.
        Detecta: SyntaxError, NameError por agente, json.loads('{X}'), {{X}},
        nombre inventado usado como literal (N2), nombre libre sin definir y
        claves inexistentes en el contrato del productor.
        """
        errores = []
        if not codigo or not isinstance(codigo, str) or not codigo.strip():
            return errores

        # 1. SyntaxError
        try:
            arbol = ast.parse(codigo)
        except SyntaxError as e:
            errores.append(
                f"BLOQUEANTE: {nombre}: SyntaxError en línea {e.lineno}: {e.msg}"
            )
            return errores

        # 2. NameError - agente usado como variable suelta, o alias de
        #    contexto inventado (``dependencias``) que el sandbox no define.
        nombres_ligados = PlanValidator._nombres_ligados(arbol)
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Name):
                continue
            if nodo.id in nombres_agentes:
                errores.append(
                    f"BLOQUEANTE: {nombre}: usa '{nodo.id}' como variable Python "
                    f"(provocará NameError). Debería ser: "
                    f"contexto.get('{nodo.id}', {{}})"
                )
            elif (
                nodo.id in ALIAS_CONTEXTO_PROHIBIDOS
                and nodo.id not in nombres_ligados
            ):
                errores.append(
                    f"BLOQUEANTE: {nombre}: usa '{nodo.id}' como variable Python "
                    f"(provocará NameError: en el sandbox solo existe "
                    f"'contexto'). Debería ser: "
                    f"contexto.get('NombreDependencia', {{}})"
                )

        # 3. json.loads con placeholder literal
        patron_json = re.compile(
            r"json\.loads\s*\(\s*(['\"]{2,3})\s*\{([a-zA-Z_]\w*)\}\s*\1\s*\)"
        )
        for m in patron_json.finditer(codigo):
            errores.append(
                f"BLOQUEANTE: {nombre}: json.loads con placeholder literal "
                f"'{{{m.group(2)}}}' sin sustituir"
            )

        # 4. {{X}} - Bloque A.2 integrado.
        #    OJO: '{{' y '}}' son también el escape legítimo de una llave
        #    literal dentro de un f-string (p. ej. CSS embebido:
        #    f"body {{ margin: 0 }}"), que es código Python correcto. Solo
        #    se marca como error si el placeholder referencia a un agente
        #    del plan, que es el anti-patrón real ({{Dependencia.clave}}).
        patron_plantilla = re.compile(
            r"\{\{\s*([A-Za-z_]\w*)\s*(?:\.[^}]*)?\}\}"
        )
        for m in patron_plantilla.finditer(codigo):
            if m.group(1) not in nombres_agentes:
                continue
            fragmento = m.group(0).replace("\n", " ")
            errores.append(
                f"BLOQUEANTE: {nombre}: usa sintaxis de plantilla '{{{{...}}}}' "
                f"en lugar de contexto.get(...). Fragmento: '...{fragmento}...'"
            )

        # 5. N2: el LLM se INVENTA UN NOMBRE para el contenido y lo usa como
        #    literal en vez de construir el valor:
        #
        #        farsi = json.loads("__FARSI_JSON__")
        #
        #    Ese nombre inventado llega al AST como ``ast.Constant`` de tipo
        #    ``str`` y ninguna regla anterior lo miraba, así que se colaba
        #    hasta el sandbox (donde ``json.loads`` revienta).
        #
        #    El criterio es la FORMA del nombre, no una lista de nombres
        #    conocidos ni un caso concreto: MAYÚSCULAS_CON_GUIONES_BAJOS, con
        #    o sin envoltura ``__...__``. Así cubre por igual
        #    ``__FARSI_JSON__``, ``__UCRAINIAN_JSON__``, ``__NEWS_HTML__``,
        #    ``__CUENTO_DRAGON__``, ``__CALCULADORA_JS__`` o ``FARSI_JSON``
        #    sin nombrar ninguno.
        #
        #    Se exige al menos un guion bajo para no marcar constantes de una
        #    sola palabra perfectamente legítimas en código generado
        #    (``"GET"``, ``"POST"``, ``"CSV"``, ``"OK"``). Los dunders
        #    legítimos (``__name__``, ``__file__``, ``__main__``, ``__all__``)
        #    van en minúsculas y tampoco casan.
        #
        #    Nota sobre ``_nombres_ligados``: no aplica aquí. Un literal de
        #    cadena nunca puede ser el LHS de una asignación, y excluir los
        #    literales cuyo texto coincida con un nombre ligado abriría un
        #    falso negativo (p. ej. ``__X__ = 1`` junto a
        #    ``json.loads("__X__")``). Lo que sí se excluye es la constante
        #    que es OBJETIVO de una asignación por subíndice
        #    (``contexto["__X__"] = ...``): ahí es un nombre de clave elegido
        #    por el código, no un placeholder que se consuma.
        objetivos_asignacion = {
            id(nodo.slice)
            for nodo in ast.walk(arbol)
            if isinstance(nodo, ast.Subscript)
            and isinstance(nodo.ctx, (ast.Store, ast.Del))
        }
        patron_nombre_inventado = re.compile(r"[A-Z][A-Z0-9_]*")
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Constant):
                continue
            if not isinstance(nodo.value, str):
                continue
            if "_" not in nodo.value:
                continue
            if not patron_nombre_inventado.fullmatch(nodo.value.strip("_")):
                continue
            if id(nodo) in objetivos_asignacion:
                continue
            errores.append(
                f"BLOQUEANTE: {nombre}: placeholder literal '{nodo.value}' "
                f"sin sustituir (no existe en el sandbox)"
            )

        # 6. Nombres LIBRES que no define el código ni el sandbox.
        #
        #    Es la generalización del bug de binding: cualquier alias que el
        #    corrector no haya podido resolver (p. ej. ``respuesta`` en un
        #    paso SIN dependencias) llegaba al sandbox y reventaba con
        #    NameError, o peor, se leía un valor por defecto silencioso.
        #    Los nombres de agente y los alias de contexto ya se reportan en
        #    la regla 2, así que aquí se excluyen para no duplicar.
        reportados: set = set()
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Name) or not isinstance(nodo.ctx, ast.Load):
                continue
            identificador = nodo.id
            if identificador in reportados:
                continue
            if identificador in nombres_ligados:
                continue
            if identificador in NOMBRES_CONOCIDOS:
                continue
            if identificador in nombres_agentes:
                continue
            if identificador in ALIAS_CONTEXTO_PROHIBIDOS:
                continue
            reportados.add(identificador)
            errores.append(
                f"BLOQUEANTE: {nombre}: usa '{identificador}' sin definirla. "
                f"No existe en el sandbox; lee las dependencias con "
                f"contexto.get('NombreDependencia', {{}}) o "
                f"dependencia(contexto, 'NombreDependencia')."
            )

        # 7. Clave leída de una dependencia que su tipo no puede producir.
        #
        #    Es el anti-patrón que originó el bug: un paso leía
        #    ``contexto.get('GenerarCuento', {}).get('body', '{}')`` sobre un
        #    agente LLM. ``body`` es una clave de HTTP, no de LLM: el default
        #    ``'{}'`` se colaba como contenido. La lista de claves válidas
        #    sale del contrato de salida documentado, no de una lista ad-hoc.
        if tipos_agentes:
            for nodo in ast.walk(arbol):
                dependencia = None
                clave = None
                if (
                    isinstance(nodo, ast.Call)
                    and isinstance(nodo.func, ast.Attribute)
                    and nodo.func.attr == "get"
                    and nodo.args
                ):
                    dependencia = _nombre_dependencia(
                        nodo.func.value, nombres_agentes
                    )
                    if dependencia is not None:
                        clave = _texto_constante(nodo.args[0])
                elif isinstance(nodo, ast.Subscript):
                    dependencia = _nombre_dependencia(
                        nodo.value, nombres_agentes
                    )
                    if dependencia is not None:
                        clave = _texto_constante(nodo.slice)

                if dependencia is None or clave is None:
                    continue
                tipo_dep = tipos_agentes.get(dependencia)
                claves_validas = CLAVES_SALIDA_POR_TIPO.get(tipo_dep)
                if not claves_validas or clave in claves_validas:
                    continue
                # Solo si la clave es de OTRO tipo (no un simple typo).
                if clave not in UNION_CLAVES_SALIDA:
                    continue
                errores.append(
                    f"BLOQUEANTE: {nombre}: lee la clave '{clave}' de "
                    f"'{dependencia}' (tipo {tipo_dep}), pero esa clave es de "
                    f"otro tipo de agente. Claves válidas para {tipo_dep}: "
                    f"{sorted(claves_validas)}."
                )

        # 8. Contenido FABRICADO en el código (3.3).
        #
        #    El LLM, cuando la dependencia no trae lo esperado, tiende a
        #    escribir un fallback que INVENTA el contenido ('Descripción no
        #    disponible', 'Sin contenido', 'Idea 1'...) en vez de dejar que el
        #    paso falle. El plan "triunfa" y el usuario recibe relleno: es el
        #    bug real que aparece en logs/llm_response_20260913_091705*.txt.
        #    Inventar contenido oculta el fallo y además impide que el Plan B
        #    se active, así que se bloquea el plan entero.
        for nodo in ast.walk(arbol):
            if not isinstance(nodo, ast.Constant):
                continue
            if not isinstance(nodo.value, str):
                continue
            if nodo.value.strip().lower() not in LITERALES_FABRICADOS:
                continue
            errores.append(
                f"BLOQUEANTE: {nombre}: contenido de RELLENO literal "
                f"'{nodo.value}'. No inventes el dato que falta: si la "
                f"dependencia no trae lo esperado, deja que el paso falle para "
                f"que se replanifique."
            )

        return errores

    def _validar_codigo_python(
        self, paso, nombres_agentes: set, tipos_agentes: dict | None = None
    ) -> list:
        """
        Delega en la función pura para tener una única fuente de verdad.
        """
        codigo = (paso.configuracion or {}).get("codigo", "") or ""
        return self._validar_codigo_python_ast(
            codigo=codigo,
            nombre=paso.nombre,
            nombres_agentes=nombres_agentes,
            tipos_agentes=tipos_agentes,
        )

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
