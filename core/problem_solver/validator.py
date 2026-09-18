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
import logging
import re

from core.agent import TipoAgente

from .constants import CAMPOS_VALIDOS_POR_TIPO
from .models import ExecutionPlan, StepPlan

logger = logging.getLogger(__name__)


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
                config['prompt'] = "Analiza el siguiente contexto:\n{contexto}"
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
        for paso in plan.pasos:
            if getattr(paso, "tipo_agente", None) == "Python":
                errores.extend(self._validar_codigo_python(paso, nombres_agentes))

        return len(errores) == 0, errores

    @staticmethod
    def _validar_codigo_python_ast(
        codigo: str,
        nombre: str,
        nombres_agentes: set,
    ) -> list[str]:
        """
        Función pura. Valida un bloque de código. Misma regla para PlanValidator y PlanRecovery.
        Detecta: SyntaxError, NameError por agente, json.loads('{X}'), {{X}}
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

        # 2. NameError - agente usado como variable suelta
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Name) and nodo.id in nombres_agentes:
                errores.append(
                    f"BLOQUEANTE: {nombre}: usa '{nodo.id}' como variable Python "
                    f"(provocará NameError). Debería ser: "
                    f"contexto.get('{nodo.id}', {{}})"
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

        # 4. {{X}} - Bloque A.2 integrado
        if "{{" in codigo:
            idx = codigo.find("{{")
            fragmento = codigo[max(0, idx - 20):idx + 40].replace("\n", " ")
            errores.append(
                f"BLOQUEANTE: {nombre}: usa sintaxis de plantilla '{{{{...}}}}' "
                f"en lugar de contexto.get(...). Fragmento: '...{fragmento}...'"
            )

        return errores

    def _validar_codigo_python(self, paso, nombres_agentes: set) -> list:
        """
        Delega en la función pura para tener una única fuente de verdad.
        """
        codigo = (paso.configuracion or {}).get("codigo", "") or ""
        return self._validar_codigo_python_ast(
            codigo=codigo,
            nombre=paso.nombre,
            nombres_agentes=nombres_agentes,
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
