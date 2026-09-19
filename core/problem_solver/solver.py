# core/problem_solver/solver.py
"""
ProblemSolver: orquestador delgado. Une los submódulos:
    - PromptBuilder      (construcción de prompts)
    - PlanParser         (parsing de la respuesta del LLM)
    - PlanValidator      (validación de planes y pasos)
    - PlanBuilder        (construcción de Agentes desde StepPlan)
    - PythonCodeCorrector (corrección de código Python)
    - FileNameNormalizer (normalización de nombres de archivo)

Fragmentado desde el monolito core/problem_solver.py (2247 líneas).
La API pública se mantiene intacta: resolver_problema, refinar_plan,
obtener_plan, listar_planes, estimar_complejidad, estimar_tiempo,
generar_dsl.

⚠️ El antigo atributo `self._plan_actual` (estado de instancia usado
para propagar advertencias desde `_validar_campos_configuracion` al plan
en curso) se mantiene aquí, pero ahora se pasa EXPLÍCITAMENTE a los
submódulos (PlanBuilder.paso_a_agente, PlanValidator.validar_campos_configuracion).
Antes de llamar a `builder.construir_plan()`, `self._plan_actual` es None;
se asigna justo antes de `builder.generar_agentes(plan)`, igual que en
el monolito.
"""
import json
import logging
import os
import re
import time

from core.llm_client import LLMClient

# ─────────────────────────────────────────────────────────────
# Post-procesado del plan: reemplazo determinista de pasos
# Python que ensamblan HTML por la utilidad del proyecto.
# ─────────────────────────────────────────────────────────────
_PATRON_ENSAMBLAR_HTML = re.compile(r'ensambl.*html|construir.*html|combinar.*html', re.I)

_CODIGO_ENSAMBLAR_HTML = '''from core.utils.ensamblar_html import ensamblar_calculadora_html

# Detectar las claves de los pasos previos por nombre.
_nombres = list(contexto.keys())
_html_key = next((n for n in _nombres if 'estructura' in n.lower() or 'html' in n.lower() and 'ensambl' not in n.lower()), None)
_css_key = next((n for n in _nombres if 'css' in n.lower() or 'estilo' in n.lower()), None)
_js_key = next((n for n in _nombres if 'js' in n.lower() or 'javascript' in n.lower() or 'script' in n.lower()), None)

if not _html_key or not _css_key or not _js_key:
    raise ValueError(
        'EnsamblarHTML: no se pudieron identificar las claves de las partes. '
        'Claves del contexto: %s' % _nombres
    )

html_completo = ensamblar_calculadora_html(
    contexto.get(_html_key, {}),
    contexto.get(_css_key, {}),
    contexto.get(_js_key, {}),
)
resultado = {'html': html_completo}
'''
# ─────────────────────────────────────────────────────────────
# Post-procesado: inyectar paso ValidarCoherencia tras EnsamblarHTML
# ─────────────────────────────────────────────────────────────
_PATRON_VALIDAR_COHERENCIA = re.compile(
    r'validar.*coherencia|validar.*html.*generado|coherencia.*html',
    re.I
)

# ⚠️ Raw string (r'''...''') para que \b y \s lleguen literales al sandbox.
_CODIGO_VALIDAR_COHERENCIA = r'''import re
html = ''
# Buscar el HTML en el contexto. El paso EnsamblarHTML lo deja en 'html'.
for clave in list(contexto.keys()):
    valor = contexto.get(clave, {})
    if isinstance(valor, dict) and isinstance(valor.get('html'), str):
        html = valor['html']
        break
if not html:
    raise ValueError('ValidarCoherencia: no se encontro HTML en el contexto')
# 1) Detectar doble contenedor .calculadora
n_calc = len(re.findall(r'class="[^"]*\bcalculadora\b[^"]*"', html))
if n_calc > 1:
    raise ValueError(
        'Doble contenedor .calculadora detectado (%d ocurrencias). '
        'El fragmento HTML no debe incluir el contenedor .calculadora; '
        'el ensamblador ya lo anade.' % n_calc
    )
# 2) data-accion del HTML vs case del JS
acciones = set(re.findall(r'data-accion="([^"]+)"', html))
cases = set(re.findall(r"case\s+'([^']+)'", html))
faltan = acciones - cases
if faltan:
    raise ValueError(
        'Acciones del HTML sin case en el JS: %s. '
        'Anade los case correspondientes al switch del JS.'
        % sorted(faltan)
    )
# 3) Clases del HTML vs CSS
clases_html = set()
for c in re.findall(r'class="([^"]+)"', html):
    clases_html.update(c.split())
css_match = re.search(r'<style[^>]*>(.*?)</style>', html, re.S)
if css_match:
    clases_css = set(re.findall(r'\.([a-zA-Z][\w-]*)', css_match.group(1)))
    # Excluir clases que se inyectan por defecto
    ignorar = {'calculadora'}
    huerfanas = clases_html - clases_css - ignorar
    if huerfanas:
        raise ValueError(
            'Clases del HTML sin definicion en el CSS: %s. '
            'Anade las reglas CSS correspondientes o corrige los nombres.'
            % sorted(huerfanas)
        )
resultado = {
    'valido': True,
    'acciones_html': len(acciones),
    'clases_html': len(clases_html),
    'mensaje': 'Coherencia HTML/CSS/JS verificada',
}
'''


from .builder import PlanBuilder
from .code_corrector import PythonCodeCorrector
from .constants import PlanComplexity
from .file_normalizer import FileNameNormalizer
from .models import ExecutionPlan
from .parser import PlanParser
from .prompt_builder import PromptBuilder
from .validator import PlanValidator

logger = logging.getLogger(__name__)


class ProblemSolver:
    """
    Descompone problemas complejos en planes de ejecución de agentes.
    """

    # Modelos a probar en orden (configurable)
    DEFAULT_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro"]
    DEFAULT_TEMPERATURE = 0.1
    DEFAULT_MAX_TOKENS = 5000
    DEFAULT_REASONING_EFFORT = "low"
    DEFAULT_THINKING_ENABLED = False

    def __init__(self, llm_client: LLMClient | None = None):
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

        self._plan_cache: dict[str, ExecutionPlan] = {}
        self._ultimo_problema: str = ""
        self._plan_actual: ExecutionPlan | None = None
        self.logger = logger

        # Componentes modulares
        self.prompt_builder = PromptBuilder()
        self.code_corrector = PythonCodeCorrector()
        self.file_normalizer = FileNameNormalizer()
        self.parser = PlanParser(logger)
        self.validator = PlanValidator(logger)
        self.builder = PlanBuilder(logger, self.code_corrector, self.validator)

        self.logger.info("ProblemSolver inicializado correctamente")

    @staticmethod
    def _postprocesar_plan(plan: dict) -> dict:
        """
        Post-procesado determinista:
        1. Reemplaza el código de pasos Python que ensamblan HTML por
            la utilidad `core.utils.ensamblar_html`.
        2. Inyecta un paso `ValidarCoherencia` tras `EnsamblarHTML`
            si el plan no lo tiene ya.
        """
        pasos = plan.get('pasos') or plan.get('steps') or []
        if not isinstance(pasos, list):
            return plan

        reemplazados = []
        indice_ensamblar = None

        # ── Pase 1: reemplazar código de EnsamblarHTML ──
        for i, paso in enumerate(pasos):
            if not isinstance(paso, dict):
                continue
            if paso.get('tipo') != 'Python':
                continue
            nombre = paso.get('nombre', '') or ''
            if not _PATRON_ENSAMBLAR_HTML.search(nombre):
                continue

            config = paso.setdefault('configuracion', {})
            config['codigo'] = _CODIGO_ENSAMBLAR_HTML
            reemplazados.append(nombre)
            indice_ensamblar = i

        if reemplazados:
            logger.info(
                "Post-procesado del plan: código de ensamblado HTML reemplazado "
                "en: %s", ', '.join(reemplazados)
            )

        # ── Pase 2: inyectar ValidarCoherencia tras EnsamblarHTML ──
        if indice_ensamblar is not None:
            ya_existe = any(
                isinstance(p, dict)
                and _PATRON_VALIDAR_COHERENCIA.search(p.get('nombre', '') or '')
                for p in pasos
            )
            if not ya_existe:
                nombre_ensamblar = pasos[indice_ensamblar].get('nombre', 'EnsamblarHTML')
                nuevo_paso = {
                    'orden': pasos[indice_ensamblar].get('orden', 0) + 1,
                    'nombre': 'ValidarCoherencia',
                    'descripcion': (
                        'Verifica que el HTML, CSS y JS ensamblados sean '
                        'coherentes: data-accion con case en JS, clases del HTML '
                        'definidas en CSS, sin doble contenedor .calculadora.'
                    ),
                    'tipo': 'Python',
                    'dependencias': [nombre_ensamblar],
                    'configuracion': {
                        'codigo': _CODIGO_VALIDAR_COHERENCIA,
                    },
                    'justificacion': (
                        'Detección temprana de incoherencias HTML/CSS/JS '
                        'antes de escribir el archivo final.'
                    ),
                }
                pasos.insert(indice_ensamblar + 1, nuevo_paso)
                # Reasignar 'orden' a todos los pasos
                for i, p in enumerate(pasos):
                    if isinstance(p, dict):
                        p['orden'] = i + 1
                logger.info(
                    "Post-procesado del plan: paso 'ValidarCoherencia' "
                    "inyectado tras '%s'", nombre_ensamblar
                )

        return plan


    # ============================================================
    # API PÚBLICA
    # ============================================================

    def resolver_problema(
        self,
        problema: str,
        contexto_extra: dict | None = None,
        max_pasos: int = 10,
        nivel_detalle: str = "normal",
        _es_regeneracion: bool = False,
        _instruccion_extra: str = "",
        _intento_validacion: int = 0,
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

        # 2.1 ✅ Post-procesado determinista del plan (ensamblar HTML)
        plan_dict = self._postprocesar_plan(plan_dict)

        # 3. Normalizar nombres de archivos
        plan_dict = self.file_normalizer.normalizar(plan_dict)

        # 4. Construir ExecutionPlan
        plan = self.builder.construir_plan(problema, plan_dict)

        # ✅ 5. Guardar referencia (para que el validator pueda propagar
        #       advertencias al plan).
        self._plan_actual = plan

        try:
            # 6. Detectar requisitos no cumplidos
            from core.plan_repairs import (
                aplicar_parche,
                construir_instruccion_regeneracion,
                detectar_requisitos_no_cumplidos,
            )
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
                        self._registrar_reparacion(problema, parche_aplicado)

            # 9. Generar objetos Agente
            plan.agentes_generados = self.builder.generar_agentes(plan)

            # 10. Validar
            es_valido, errores = self.validator.validar_plan(plan)

            errores_graves = [
                e for e in errores
                if e.startswith("BLOQUEANTE:")
            ]

            advertencias = [
                e for e in errores
                if not e.startswith("BLOQUEANTE:")
            ]

            if advertencias:
                plan.advertencias.extend(
                    a for a in advertencias
                    if a not in plan.advertencias
                )
                self.logger.warning(
                    f"⚠️ Plan con advertencias: {advertencias}"
                )

            if errores_graves:
                MAX_INTENTOS_VALIDACION = 2

                if _intento_validacion < MAX_INTENTOS_VALIDACION:
                    self.logger.error(
                        f"❌ Plan con {len(errores_graves)} errores bloqueantes. "
                        f"Reintentando generación "
                        f"({_intento_validacion + 1}/{MAX_INTENTOS_VALIDACION})..."
                    )

                    instruccion = (
                        "El plan anterior tenía errores bloqueantes "
                        "en el código Python. Corrígelos.\n\n"
                        "ERRORES DETECTADOS:\n"
                        + "\n".join(
                            f"- {e}" for e in errores_graves
                        )
                    )

                    self._plan_actual = None

                    return self.resolver_problema(
                        problema=problema,
                        contexto_extra=contexto_extra,
                        max_pasos=max_pasos,
                        nivel_detalle=nivel_detalle,
                        _es_regeneracion=True,
                        _instruccion_extra=instruccion,
                        _intento_validacion=_intento_validacion + 1,
                    )

                else:
                    self.logger.error(
                        f"❌ Plan con errores bloqueantes tras "
                        f"{MAX_INTENTOS_VALIDACION} reintentos. "
                        f"Se dejará pasar; Plan B en ejecución decidirá."
                    )

                    plan.advertencias.extend(
                        e for e in errores_graves
                        if e not in plan.advertencias
                    )

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
            self.logger.info(f"📄 Respuesta cruda (primeros 500 chars):\n{respuesta[:500]}")

            plan_dict = self.parser.parsear(respuesta)
            plan_dict = self.file_normalizer.normalizar(plan_dict)
            nuevo_plan = self.builder.construir_plan(plan.problema_original, plan_dict)

            self._plan_actual = nuevo_plan
            try:
                nuevo_plan.agentes_generados = self.builder.generar_agentes(nuevo_plan)

                es_valido, errores = self.validator.validar_plan(nuevo_plan)

                errores_graves = [e for e in errores if e.startswith("BLOQUEANTE:")]
                advertencias = [e for e in errores if not e.startswith("BLOQUEANTE:")]

                if advertencias:
                    nuevo_plan.advertencias.extend(
                        a for a in advertencias if a not in nuevo_plan.advertencias
                    )

                if errores_graves:
                    self.logger.warning(
                        f"⚠️ Plan refinado con errores bloqueantes: {errores_graves}"
                    )
                    nuevo_plan.advertencias.extend(
                        e for e in errores_graves if e not in nuevo_plan.advertencias
                    )
            finally:
                self._plan_actual = None

            self._plan_cache[nuevo_plan.id] = nuevo_plan
            return nuevo_plan

        except Exception as e:
            self.logger.error(f"Error refinando plan: {e}")
            raise ValueError(f"No se pudo refinar el plan: {e}")

    def obtener_plan(self, plan_id: str) -> ExecutionPlan | None:
        """Obtiene un plan de la caché por su ID."""
        return self._plan_cache.get(plan_id)

    def listar_planes(self) -> list[dict]:
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

    def _consultar_llm_con_reintentos(self, user_prompt: str) -> dict:
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

                respuesta_limpia = self.parser.limpiar_respuesta_agresivamente(respuesta)

                try:
                    plan_dict = json.loads(respuesta_limpia)
                    if plan_dict and plan_dict.get('pasos'):
                        self.logger.info(f"✅ Plan obtenido con modelo {modelo}")
                        return plan_dict
                except json.JSONDecodeError as e:
                    self.logger.warning(f"⚠️ JSON inválido con modelo {modelo}: {e}")

                    reparado = self.parser.reparar_json(respuesta_limpia)
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

    def _guardar_respuesta_debug(self, respuesta: str, modelo: str):
        """Guarda la respuesta del LLM para depuración."""
        try:
            os.makedirs("logs", exist_ok=True)
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            # Microsegundos para no sobrescribir dos respuestas del mismo
            # modelo en el mismo segundo.
            sufijo_us = f"{time.time_ns() % 1_000_000:06d}"
            filename = f"logs/llm_response_{timestamp}_{sufijo_us}_{modelo}.txt"
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

    def _crear_plan_fallback(self, problema: str) -> dict:
        """Crea un plan de fallback con código Python CORRECTO."""
        self.logger.warning(f"Creando plan de fallback para: {problema[:50]}...")

        import re
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