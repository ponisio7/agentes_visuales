# core/problem_solver/builder.py
"""
PlanBuilder: construcción de ExecutionPlan/Agente a partir de la respuesta
del LLM. Extraído de los métodos privados de ProblemSolver (monolito) —
Paso 5, Opción B (clase con estado, instanciada en el `__init__` de
ProblemSolver con `logger`, `code_corrector` y `validator` inyectados).

Métodos migrados desde el monolito (misma lógica; solo cambia el "self"
de ProblemSolver por el de PlanBuilder, y se hacen públicos los que
`solver.py` necesita llamar desde fuera):

    _construir_plan     -> construir_plan      (público)
    _generar_agentes    -> generar_agentes     (público)
    _paso_a_agente      -> paso_a_agente       (público, lo usa generar_agentes internamente)
    _kwargs_python..._kwargs_loop -> _kwargs_* (privados, dispatch interno)
    _endurecer_prompt_llm          -> _endurecer_prompt_llm (privado, solo lo usa _kwargs_llm)

⚠️ Único cambio real de comportamiento (necesario para desacoplar de
ProblemSolver): igual que en `validator.py`, el monolito usaba
`self._plan_actual` (estado de instancia de ProblemSolver) dentro de
`_validar_campos_configuracion`, invocado indirectamente desde
`_paso_a_agente`. Aquí `paso_a_agente` recibe `plan_actual` como parámetro
explícito (por defecto None) y lo reenvía al validator. `generar_agentes`
ya recibe el `plan` que está construyendo, así que se lo pasa directamente
— es el mismo objeto que el monolito guardaba en `self._plan_actual` en
ese punto del flujo (ver `resolver_problema`: `self._plan_actual = plan`
se asigna justo antes de llamar a `_generar_agentes(plan)`).
"""
import uuid
from typing import Any, Dict, List

from core.agent import Agente, TipoAgente

from .models import ExecutionPlan, StepPlan


class PlanBuilder:
    """Construye el ExecutionPlan y los Agente a partir de la respuesta del LLM."""

    def __init__(self, logger, code_corrector, validator):
        self.logger = logger
        self.code_corrector = code_corrector
        self.validator = validator

    def construir_plan(self, problema: str, plan_dict: Dict) -> ExecutionPlan:
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

            self.validator.validar_configuracion_paso(paso)
            plan.pasos.append(paso)

        return plan

    def generar_agentes(self, plan: ExecutionPlan) -> List[Agente]:
        """Genera objetos Agente a partir del plan, corrigiendo código Python."""
        agentes: List[Agente] = []
        nombre_a_id: Dict[str, str] = {}

        for paso in plan.pasos:
            try:
                agente = self.paso_a_agente(paso, plan_actual=plan)
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

    def paso_a_agente(self, paso: StepPlan, plan_actual: ExecutionPlan = None) -> Agente:
        """
        Convierte un StepPlan en un objeto Agente listo para ejecutar.

        RESPONSABILIDADES:
        1. Validar el tipo (con fallback seguro a Python).
        2. Detectar y reportar campos desconocidos en 'configuracion'.
        3. Corregir automáticamente el código Python generado por el LLM.
        4. Construir el Agente con la configuración específica del tipo.

        Args:
            paso: StepPlan con la configuración generada por el LLM.
            plan_actual: plan en curso, para propagar advertencias
                (ver nota de módulo sobre el reemplazo de `self._plan_actual`).

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
        self.validator.validar_campos_configuracion(paso, tipo, plan_actual=plan_actual)

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

        ✅ FASE 4c: A/B testing. En lugar de aplicar siempre la última
        reescritura, consulta las dos versiones vigentes (activo y
        candidato) y elige una con `PromptABEvaluator.elegir_variante`.
        El id elegido se propaga al Agente para que `execution_recorder`
        pueda registrar el uso.
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

        kwargs['reasoning_effort_llm'] = config.get('reasoning_effort', 'low')
        kwargs['thinking_enabled_llm'] = bool(config.get('thinking_enabled', False))

        # 1. Endurecer primero (idempotente).
        prompt_endurecido = self._endurecer_prompt_llm(prompt_original)

        # 2. ✅ FASE 4c: A/B testing.
        #    - La firma se calcula sobre el prompt endurecido (es lo que
        #      persiste en prompts_reescritos).
        #    - consultar_versiones devuelve la activa y la candidata.
        #    - elegir_variante decide con probabilidad (20% candidato).
        prompt_final = prompt_endurecido
        prompt_id_elegido = 0
        try:
            from learning.prompt_ab_evaluator import PromptABEvaluator
            from learning.feedback_processor import FeedbackProcessor

            db_path = "agent_history.db"
            firma = FeedbackProcessor._firmar(prompt_endurecido)
            versiones = PromptABEvaluator.consultar_versiones(db_path, firma)

            activo = versiones.get("activo")
            candidato = versiones.get("candidato")

            if activo or candidato:
                prompt_elegido, prompt_id_elegido = PromptABEvaluator.elegir_variante(
                    prompt_activo=(activo or {}).get("prompt"),
                    prompt_id_activo=(activo or {}).get("id"),
                    prompt_candidato=(candidato or {}).get("prompt"),
                    prompt_id_candidato=(candidato or {}).get("id"),
                )
                if prompt_elegido:
                    prompt_final = prompt_elegido
                    self.logger.info(
                    f"✨ AB: '{paso.nombre}' usa prompt id={prompt_id_elegido} "
                    f"(activo={'sí' if activo else 'no'}, "
                    f"candidato={'sí' if candidato else 'no'})"
                    )
        except Exception as e:
            self.logger.debug(f"AB no disponible: {e}")

        kwargs['prompt_llm'] = prompt_final
        kwargs['prompt_reescrito_id'] = int(prompt_id_elegido or 0)

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