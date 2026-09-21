# core/scheduler.py - VERSIÓN REFACTORIZADA CON RESULTADOS EN LOG
"""
Orquestador de agentes basado en dependencias DAG — VERSIÓN SEGURA PARA HILOS

MEJORAS IMPLEMENTADAS:
- ✅ Logs claros al inicio/fin de cada agente con duración
- ✅ Resumen detallado al completar toda la ejecución
- ✅ Señal _reintentar_agente correctamente declarada
- ✅ Formato consistente: [Nombre] con emojis y colores
- ✅ Manejo robusto de locks (sin deadlocks)
- ✅ Throttling de estadísticas para reducir CPU
- ✅ Documentación mejorada y secciones claras
- ✅ API pública vs interna bien delimitada
- ✅ LOG DE RESULTADO DE CADA AGENTE al finalizar
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from PyQt6.QtCore import QObject, Qt, pyqtSignal, pyqtSlot

from . import dependency_manager
from .acceptance_manager import calcular_aceptacion
from .agent import Agente, EstadoAgente, TipoAgente
from .bridge import SchedulerBridge
from .budget_manager import BudgetManager
from .cancellation import CancellationToken, obtener_gestor_cancelacion
from .event_bus import obtener_bus
from .execution_log import resumir_error, resumir_resultado
from .recovery_manager import RecoveryDecision, RecoveryManager

# ============================================================
# MÁQUINA DE ESTADOS - TRANSICIONES VÁLIDAS
# ============================================================

TRANSICIONES_VALIDAS = {
    # Desde PENDIENTE
    EstadoAgente.PENDIENTE: {
        EstadoAgente.EN_COLA,      # → En cola
        EstadoAgente.ESPERANDO,    # → Esperando dependencias
        EstadoAgente.CANCELADO,    # → Cancelado (por usuario)
        EstadoAgente.BLOQUEADO,    # → Bloqueado (dependencia eliminada)
    },

    # Desde EN_COLA
    EstadoAgente.EN_COLA: {
        EstadoAgente.LISTO,        # → Listo para ejecutar
        EstadoAgente.CANCELADO,    # → Cancelado
        EstadoAgente.BLOQUEADO,    # → Bloqueado
    },

    # Desde ESPERANDO
    EstadoAgente.ESPERANDO: {
        EstadoAgente.LISTO,        # → Listo (dependencias resueltas)
        EstadoAgente.CANCELADO,    # → Cancelado
        EstadoAgente.BLOQUEADO,    # → Bloqueado
        EstadoAgente.SALTADO,      # → Saltado (dependencia fallida)
    },

    # Desde LISTO
    EstadoAgente.LISTO: {
        EstadoAgente.EJECUTANDO,   # → Ejecutando
        EstadoAgente.CANCELADO,    # → Cancelado
        EstadoAgente.BLOQUEADO,    # → Bloqueado
    },

    # Desde EJECUTANDO
    EstadoAgente.EJECUTANDO: {
        EstadoAgente.COMPLETADO,   # → Completado (éxito)
        EstadoAgente.ERROR,        # → Error (falló)
        EstadoAgente.TIMEOUT,      # → Timeout (excedió tiempo)
        EstadoAgente.REINTENTANDO, # → Reintentando (falló pero hay reintentos)
        EstadoAgente.CANCELADO,    # → Cancelado (por usuario)
    },

    # Desde REINTENTANDO
    EstadoAgente.REINTENTANDO: {
        EstadoAgente.EN_COLA,      # → En cola (reintento programado)
        EstadoAgente.ERROR,        # → Error (sin más reintentos)
        EstadoAgente.TIMEOUT,      # → Timeout
        EstadoAgente.CANCELADO,    # → Cancelado
    },

    # Estados terminales (no tienen transiciones salientes)
    EstadoAgente.COMPLETADO: set(),
    EstadoAgente.ERROR: set(),
    EstadoAgente.TIMEOUT: set(),
    EstadoAgente.CANCELADO: set(),
    EstadoAgente.SALTADO: set(),
    EstadoAgente.BLOQUEADO: set(),
}


logger = logging.getLogger(__name__)


# ============================================================
# CLASE PRINCIPAL: SCHEDULER
# ============================================================
class Scheduler(QObject):
    """
    Orquestador de agentes basado en dependencias DAG.
    Thread-safe con señales Qt para comunicación con la UI.
    """

    # ── Señales para comunicación con la UI ──
    agente_actualizado = pyqtSignal(str)           # ID del agente actualizado
    log_mensaje = pyqtSignal(str, str)             # mensaje, color
    ejecucion_terminada = pyqtSignal()             # Todos los agentes terminaron
    estado_cambiado = pyqtSignal(bool)             # ejecutando/pausado
    # ✅ FIX: solo ID + contexto (no el objeto Agente vivo). QueuedConnection
    # no copia argumentos Python; pasar el Agente desde el worker provoca
    # data race con el hilo principal en reintentos.
    _reintentar_agente = pyqtSignal(str, object)  # agente_id, contexto_extra

    # ── Constantes ──
    _AGENT_TIMEOUT = 3600          # Timeout global por agente (1 hora)
    _STATS_CACHE_TTL = 0.5         # TTL del cache de estadísticas (500ms)
    _MAX_RESULTADO_LOG = 200       # Caracteres máximos para mostrar en log

    def __init__(
        self,
        max_concurrent: int = 4,
        *,
        max_intentos_plan_b: int | None = None,
        presupuesto_plan_b_seg: float | None = None,
        presupuesto: BudgetManager | None = None,
    ):
        super().__init__()

        # ── Plan B (recuperación de fallos críticos) ──
        # H7: límites configurables (argumento > entorno > default). Antes
        # estaban fijos en 2 sin presupuesto de tiempo.
        from .plan_recovery import configuracion_plan_b

        _cfg_plan_b = configuracion_plan_b()
        self.recovery = None
        self._plan_b_intentos = 0
        self._plan_b_en_progreso = False
        self._problema_original = ""
        self._plan_original = None
        self._max_intentos_plan_b = (
            _cfg_plan_b["max_intentos"]
            if max_intentos_plan_b is None else int(max_intentos_plan_b)
        )
        self._presupuesto_plan_b_seg = (
            _cfg_plan_b["presupuesto_seg"]
            if presupuesto_plan_b_seg is None else float(presupuesto_plan_b_seg)
        )
        self._plan_b_inicio: float | None = None
        # H7: anti-repetición y traza de intentos.
        self._firmas_plan_fallidas: set[str] = set()
        self._reparaciones_intentadas: list[dict] = []
        # V3.8: paradas duras. El manager decide si un fallo merece Plan B;
        # ``_parada_dura`` guarda la última parada para exponerla en el
        # resultado de aceptación (API/GUI) y para no gastar intentos.
        # El presupuesto (tiempo/llamadas/tokens/coste) alimenta al manager:
        # si se agota, la recuperación es una parada dura BUDGET_EXCEEDED.
        self.presupuesto = presupuesto or BudgetManager.desde_entorno()
        self.recovery_manager = RecoveryManager(budget=self.presupuesto)
        self._parada_dura: dict | None = None

        # ── Estado de agentes ──
        self.agentes: dict[str, Agente] = {}
        self.max_concurrent = max_concurrent
        self.running: set[str] = set()
        self.completed: set[str] = set()

        # ── Estado de ejecución ──
        self.ejecutando = False
        self.pausado = False
        self._terminado_notificado = False
        # ⛔ B2: True desde la primera llamada a ``detener()``. Lo consulta
        # ``_intentar_plan_b`` para no volver a arrancar (submit) un plan
        # después de un shutdown (executor o intérprete). ``iniciar()`` lo
        # vuelve a poner a False porque es un arranque explícito.
        self._detenido = False
        self._cancelados: set[str] = set()

        # ── Seguimiento de loops ──
        self._loops_activos: set[str] = set()
        self._loop_items_procesados: dict[str, int] = {}

        # ── Aceptación de la salida (H6) ──
        # agente_id -> ResultadoVerificacion (dict serializable)
        self._verificaciones: dict[str, dict] = {}
        self._ultima_aceptacion: dict | None = None

        # ── Cache de estadísticas ──
        self._stats_cache: dict | None = None
        self._stats_cache_time: float = 0.0

        # ── Sincronización ──
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=max_concurrent)

        # ── Bridge para comunicación con agentes ──
        self.bridge = SchedulerBridge()
        self._conectar_bridge()

        # ── Conexión de señal de reintentos (hilo principal) ──
        self._reintentar_agente.connect(
            self._on_reintentar_agente,
            Qt.ConnectionType.QueuedConnection
        )

        # ── Timestamp de inicio de ejecución ──
        self._tiempo_inicio_ejecucion: float | None = None

        # ── Event Bus ──
        self._bus = obtener_bus()

        # ── Gestor de cancelación ──
        self._gestor_cancelacion = obtener_gestor_cancelacion()
        self._tokens_activos: dict[str, CancellationToken] = {}  # agente_id -> token

        #logger.debug(f"Scheduler inicializado (max_concurrent={max_concurrent})")

    def set_contexto_plan_b(
        self, recovery, problema_original: str, plan_original,
        reiniciar_presupuesto: bool = True,
    ):
        """Inyecta el contexto del Plan B.

        ``reiniciar_presupuesto`` (3.12): en ``run`` cada ejecución arranca con
        el presupuesto a cero, pero en ``resolve`` el MISMO presupuesto cubre
        todos los intentos, así que reiniciarlo aquí lo dejaría sin efecto. El
        modo ``resolve`` pasa ``False``.
        """
        self.recovery = recovery
        self._problema_original = problema_original or ""
        self._plan_original = plan_original
        self._plan_b_intentos = 0
        self._plan_b_en_progreso = False
        self._plan_b_inicio = None
        self._firmas_plan_fallidas = set()
        self._reparaciones_intentadas = []
        # V3.8: cada ejecución arranca sin paradas duras previas y con el
        # presupuesto a cero (el reloj empieza en ``iniciar()``).
        self._parada_dura = None
        if getattr(self, "recovery_manager", None) is not None:
            self.recovery_manager.reset()
        if getattr(self, "presupuesto", None) is not None and reiniciar_presupuesto:
            self.presupuesto.reset()
        logger.debug("Plan B contexto inyectado en Scheduler")

    # ============================================================
    # CONEXIÓN DEL BRIDGE
    # ============================================================
    def _conectar_bridge(self):
        """Conecta las señales del bridge a las señales locales."""
        self.bridge.agente_actualizado.connect(
            self.agente_actualizado.emit,
            Qt.ConnectionType.QueuedConnection
        )
        self.bridge.log_mensaje.connect(
            self.log_mensaje.emit,
            Qt.ConnectionType.QueuedConnection
        )
        self.bridge.ejecucion_terminada.connect(
            self.ejecucion_terminada.emit,
            Qt.ConnectionType.QueuedConnection
        )

    # ============================================================
    # API PÚBLICA — Gestión de agentes
    # ============================================================
    def eliminar_agente(self, agente_id: str) -> bool:
        """
        Elimina un agente del scheduler de forma thread-safe.
        Limpia referencias en dependencias y estructuras internas.
        Retorna True si se eliminó correctamente.
        """
        with self._lock:
            agente = self.agentes.get(agente_id)
            if not agente:
                return False

            # No permitir eliminar en ejecución
            if agente.estado == EstadoAgente.EJECUTANDO or agente_id in self.running:
                return False

            # Eliminar de estructuras internas
            del self.agentes[agente_id]
            self.running.discard(agente_id)
            self.completed.discard(agente_id)
            self._cancelados.discard(agente_id)
            self._loops_activos.discard(agente_id)
            self._loop_items_procesados.pop(agente_id, None)

            # Limpiar referencias en otros agentes (por ID y por nombre)
            for otro in self.agentes.values():
                if agente_id in (otro.dependencias_ids or []):
                    otro.dependencias_ids = [d for d in otro.dependencias_ids if d != agente_id]
                if agente.nombre in (otro.dependencias_nombres or []):
                    otro.dependencias_nombres = [n for n in otro.dependencias_nombres if n != agente.nombre]

            self._invalidar_stats_cache()

        self.log_mensaje.emit(f"🗑 Agente eliminado: {agente.nombre}", "#dc3545")
        return True

    def agregar_agente(self, agente: Agente):
        """Agrega un agente al scheduler de forma thread-safe."""
        with self._lock:
            self.agentes[agente.id] = agente
            agente.progreso = 10
            agente_id = agente.id

        # Emitir señal FUERA del lock
        self.agente_actualizado.emit(agente_id)

    def agregar_agentes(self, agentes: list[Agente]):
        """Agrega múltiples agentes."""
        for agente in agentes:
            self.agregar_agente(agente)

    def obtener_agente(self, agente_id: str) -> Agente | None:
        """Obtiene un agente por ID."""
        with self._lock:
            return self.agentes.get(agente_id)

    def obtener_agente_por_nombre(self, nombre: str) -> Agente | None:
        """Obtiene un agente por nombre."""
        with self._lock:
            for agente in self.agentes.values():
                if agente.nombre == nombre:
                    return agente
            return None

    # ============================================================
    # DEPENDENCIAS Y VALIDACIÓN
    # ============================================================
    def resolver_dependencias(self):
        """Resuelve las dependencias por nombre a IDs (V3.8-6: delegado)."""
        with self._lock:
            no_encontradas = dependency_manager.resolver_dependencias(self.agentes)
        for agente_nombre, nombre in no_encontradas:
            self.log_mensaje.emit(
                f"⚠️ Dependencia '{nombre}' no encontrada para {agente_nombre}",
                "#ffc107",
            )

    def detectar_ciclos(self) -> tuple[bool, list[list[str]]]:
        """Detecta ciclos en las dependencias usando DFS (V3.8-6: delegado)."""
        with self._lock:
            return dependency_manager.detectar_ciclos(self.agentes)

    def _validar_fuentes_loop(self) -> tuple[bool, list[str]]:
        """Valida que las fuentes de items de los loops sean válidas (V3.8-6)."""
        with self._lock:
            return dependency_manager.validar_fuentes_loop(self.agentes)

    # ============================================================
    # LÓGICA INTERNA DE EJECUCIÓN
    # ============================================================
    def _obtener_dependencias_pendientes(self, agente: Agente) -> list[str]:
        """Dependencias que aún no están COMPLETADAS (V3.8-6: delegado).

        Solo COMPLETADO satisface una dependencia. ERROR, TIMEOUT, CANCELADO,
        SALTADO y BLOQUEADO NO la satisfacen.
        """
        return dependency_manager.obtener_dependencias_pendientes(
            agente, self.agentes
        )

    def _tiene_dependencias_fallidas(self, agente: Agente) -> bool:
        """True si alguna dependencia falló (V3.8-6: delegado)."""
        return dependency_manager.tiene_dependencias_fallidas(agente, self.agentes)

    def _puede_ejecutar_internal(self, agente: Agente) -> bool:
        """Verifica si un agente puede ejecutarse (bajo lock)."""
        if self.pausado:
            return False
        if not EstadoAgente.puede_ejecutarse(agente.estado):
            return False
        if agente.id in self.running:
            return False
        if self._obtener_dependencias_pendientes(agente):
            return False
        if len(self.running) >= self.max_concurrent:
            return False
        return True

    def _intentar_lanzar_internal(self):
        """Intenta lanzar agentes pendientes (bajo lock)."""
        if not self.ejecutando:
            return

        agentes_a_lanzar = []
        agentes_a_bloquear = []

        for agente in self.agentes.values():
            # Si está en estado terminal, no procesar
            if EstadoAgente.es_terminal(agente.estado):
                continue

            # Si tiene dependencias fallidas → BLOQUEAR (no ejecutar)
            if self._tiene_dependencias_fallidas(agente):
                agentes_a_bloquear.append(agente)
                continue

            # Si todas las deps están COMPLETADAS y puede ejecutarse
            if self._puede_ejecutar_internal(agente):
                agentes_a_lanzar.append(agente)

            if len(agentes_a_lanzar) + len(self.running) >= self.max_concurrent:
                break

        # Bloquear primero (fuera del loop de lanzamiento)
        for agente in agentes_a_bloquear:
            if agente.id in self.running:
                continue
            deps_fallidas = [
                self.agentes[d].nombre
                for d in agente.dependencias_ids
                if d in self.agentes
                and self.agentes[d].estado in (
                    EstadoAgente.ERROR, EstadoAgente.TIMEOUT,
                    EstadoAgente.CANCELADO, EstadoAgente.SALTADO,
                    EstadoAgente.BLOQUEADO,
                )
            ]
            razon = f"Dependencia(s) fallida(s): {', '.join(deps_fallidas)}"
            try:
                agente.transicionar_a(EstadoAgente.BLOQUEADO, f"🚫 {razon}")
            except ValueError:
                agente.estado = EstadoAgente.BLOQUEADO
                agente.mensaje = f"🚫 {razon}"
            agente.progreso = 100
            self.completed.add(agente.id)
            self._invalidar_stats_cache()
            self.agente_actualizado.emit(agente.id)
            self.log_mensaje.emit(
                f"🚫 [{agente.nombre}] {razon}",
                "#8b0000"
            )
            self._bus.publicar_agente_actualizado(agente.id, origen="scheduler")

        # Lanzar los que sí pueden
        for agente in agentes_a_lanzar:
            agente.estado = EstadoAgente.LISTO
            self.running.add(agente.id)
            self._executor.submit(self._ejecutar_agente, agente)

    def _intentar_lanzar(self):
        """Versión pública con lock."""
        with self._lock:
            self._intentar_lanzar_internal()

    # ============================================================
    # EJECUCIÓN DE UN AGENTE (hilo worker)
    # ============================================================
    # core/scheduler.py - MÉTODO COMPLETO _ejecutar_agente

    # core/scheduler.py - MÉTODO _ejecutar_agente COMPLETO CON CANCELACIÓN

    def _ejecutar_agente(self, agente: Agente, contexto_extra: dict = None):
        """Ejecuta un agente en un hilo worker con soporte para cancelación.

        Orquestador de fases (ROADMAP 4.1). Antes eran 380 líneas seguidas; cada
        fase vive ahora en su propio método, para poder leerlas y probarlas por
        separado:

          FASE 1  ``_abrir_token_cancelacion``
          FASE 2  ``_marcar_en_ejecucion``
          FASE 3  ``_construir_contexto_o_saltar``
          FASE 4  ``_ejecutar_con_executor``
          FASE 4b ``_verificar_aceptacion``
          FASE 5  ``_cerrar_intento`` (duración + estado + política de reintento)
          finally ``_cerrar_token_cancelacion``

        La extracción es mecánica: los ``return`` tempranos se han convertido en
        valores centinela (``None`` / ``False``) y el ``return`` de la rama de
        reintento en el flag ``reintentar``.
        """
        # ── Import del executor ──
        if not self._preparar_executor(agente):
            return

        # ── FASE 1: Crear token de cancelación ──
        token = self._abrir_token_cancelacion(agente)

        try:
            # ── FASE 2: INICIO ──
            tiempo_inicio = self._marcar_en_ejecucion(agente)
            if tiempo_inicio is None:
                return

            # ── FASE 3: Construir contexto ──
            contexto = self._construir_contexto_o_saltar(agente, contexto_extra)
            if contexto is None:
                return

            # ── FASE 4: Ejecutar con timeout y token de cancelación ──
            if agente.tipo == TipoAgente.LOOP:
                with self._lock:
                    self._loops_activos.add(agente.id)
                    self._loop_items_procesados[agente.id] = 0

            # ── ✅ NUEVO: Aviso de riesgo basado en aprendizaje ──
            self._avisar_riesgo_aprendizaje(agente)

            exito, mensaje, resultado = self._ejecutar_con_executor(
                agente, contexto, token
            )

            # ── FASE 4b: VERIFICACIÓN DE ACEPTACIÓN (H6) ──
            exito, mensaje, fallo_verificacion = self._verificar_aceptacion(
                agente, exito, mensaje, resultado
            )

            # ── FASE 5: FINALIZACIÓN ──
            bloquear_razon, reintentar = self._cerrar_intento(
                agente, token, exito, mensaje, resultado,
                tiempo_inicio, fallo_verificacion, contexto_extra,
            )
            if reintentar:
                return

            # ── Plan B / bloqueo de dependientes (FUERA del lock) ──
            # ``_bloquear_dependientes`` puede llamar al LLM para generar el
            # Plan B (red, ~15-20 s). Se ejecuta aquí, sin el lock, y la
            # decisión de reclamar el turno de Plan B se toma de forma atómica
            # dentro del propio método (``_reclamar_plan_b``).
            if bloquear_razon is not None:
                if self._bloquear_dependientes(agente.id, bloquear_razon):
                    # Plan B lanzado: ya reemplazó los agentes y relanzó la
                    # ejecución. Mutar aquí el estado del plan anterior solo
                    # ensuciaría el nuevo (p. ej. añadiría su id a completed).
                    return

            self._actualizar_conjuntos_finales(agente)

        finally:
            self._cerrar_token_cancelacion(agente, token)

        # ── FASE 6: Emitir señales (fuera del lock) ──
        self.agente_actualizado.emit(agente.id)

        # ── FASE 7: Verificar si terminó todo y lanzar más ──
        with self._lock:
            self._verificar_terminado_internal()
            self._intentar_lanzar_internal()

    def _preparar_executor(self, agente: Agente) -> bool:
        """Comprueba que ``AgentExecutor`` se puede importar.

        Si no, marca el error del agente y devuelve ``False``: el original
        volvía ANTES de crear el token de cancelación, y eso se conserva.
        """
        try:
            from core.executors import AgentExecutor  # noqa: F401
        except ImportError as e:
            self._manejar_error_importacion(agente, e)
            return False
        return True

    def _abrir_token_cancelacion(self, agente: Agente):
        """FASE 1: registra un token de cancelación para este agente."""
        token = self._gestor_cancelacion.crear_token({
            'agente_id': agente.id,
            'agente_nombre': agente.nombre,
            'timestamp_inicio': time.time()
        })
        with self._lock:
            self._tokens_activos[agente.id] = token
        return token

    def _marcar_en_ejecucion(self, agente: Agente) -> float | None:
        """FASE 2: transiciona a EJECUTANDO y devuelve ``tiempo_inicio``.

        Devuelve ``None`` si la máquina de estados rechaza la transición (antes
        hacía ``return`` directamente).
        """
        tiempo_inicio = time.time()
        with self._lock:
            # Transición: PENDIENTE/EN_COLA/REINTENTANDO → EJECUTANDO
            try:
                agente.transicionar_a(
                    EstadoAgente.EJECUTANDO,
                    "⚡ Iniciando ejecución..."
                )
            except ValueError as e:
                self.log_mensaje.emit(
                    f"⚠️ [{agente.nombre}] No se puede ejecutar: {e}",
                    "#ffc107"
                )
                return None

            agente.tiempo_inicio = tiempo_inicio
            agente.progreso = 10
            agente._bridge = self.bridge

            if self._tiempo_inicio_ejecucion is None:
                self._tiempo_inicio_ejecucion = tiempo_inicio

        self._bus.publicar_agente_actualizado(agente.id, origen="scheduler")
        self._bus.publicar_log(
            f"▶️ [{agente.nombre}] Iniciando ejecución ({agente.tipo.value})",
            "#007bff",
            origen="scheduler"
        )
        return tiempo_inicio

    def _construir_contexto_o_saltar(
        self, agente: Agente, contexto_extra: dict | None
    ) -> dict | None:
        """FASE 3: construye el contexto de dependencias.

        Devuelve ``None`` si alguna dependencia falló: en ese caso el agente
        queda SALTADO, se cierra su contabilidad y se relanza el plan (antes
        hacía ``return`` directamente).
        """
        contexto = contexto_extra or {}
        with self._lock:
            for dep_id in agente.dependencias_ids:
                dep = self.agentes.get(dep_id)
                if dep:
                    if dep.estado in (EstadoAgente.ERROR, EstadoAgente.TIMEOUT, EstadoAgente.SALTADO):
                        try:
                            agente.transicionar_a(
                                EstadoAgente.SALTADO,
                                f"⏭️ Dependencia '{dep.nombre}' falló ({dep.estado.value})"
                            )
                        except ValueError:
                            agente.estado = EstadoAgente.SALTADO
                            agente.mensaje = f"⏭️ Dependencia '{dep.nombre}' falló"

                        agente.progreso = 100
                        self.log_mensaje.emit(
                            f"⏭️ [{agente.nombre}] Saltado: dependencia '{dep.nombre}' falló",
                            "#6c757d"
                        )
                        self.agente_actualizado.emit(agente.id)

                        with self._lock:
                            self.completed.add(agente.id)
                            self.running.discard(agente.id)
                            self._invalidar_stats_cache()
                            self._verificar_terminado_internal()
                            self._intentar_lanzar_internal()
                        return None

                    if dep and dep.resultado is not None and dep.estado == EstadoAgente.COMPLETADO:
                        contexto[dep.nombre] = dep.resultado
                        contexto[dep_id] = dep.resultado
        return contexto

    def _ejecutar_con_executor(
        self, agente: Agente, contexto: dict, token
    ) -> tuple[bool, str, dict]:
        """FASE 4: llama al executor. Un error inesperado no tumba el worker."""
        from core.executors import AgentExecutor

        try:
            return AgentExecutor.ejecutar(
                agente,
                contexto,
                cancellation_token=token  # ← PASAR EL TOKEN
            )
        except Exception as e:
            logger.exception(f"Error ejecutando agente {agente.nombre}")
            return False, f"Error crítico: {e}", {}

    def _verificar_aceptacion(
        self, agente: Agente, exito: bool, mensaje: str, resultado
    ) -> tuple[bool, str, bool]:
        """FASE 4b: VERIFICACIÓN DE ACEPTACIÓN (H6).

        Se comprueba el ARTEFACTO real (disco/bytes), no lo que el ejecutor dice
        haber producido. Se hace fuera del lock porque toca disco. Un paso
        crítico con resultado sospechoso (Nivel 1) o un contrato incumplido
        (Nivel 2) convierten el éxito en fallo con motivo, y ese motivo alimenta
        al Plan B.

        Devuelve ``(exito, mensaje, fallo_verificacion)``.
        """
        if not exito:
            return exito, mensaje, False

        try:
            from .verification import verificar_agente

            verificacion = verificar_agente(agente, resultado)
        except Exception as e:
            # Un verificador roto NUNCA debe tumbar la ejecución, pero
            # tampoco puede dar por bueno lo que no comprobó.
            logger.warning(
                f"Verificación de '{agente.nombre}' falló: {e}"
            )
            return exito, mensaje, False

        if verificacion is None:
            return exito, mensaje, False

        with self._lock:
            self._verificaciones[agente.id] = verificacion.to_dict()

        if verificacion.aceptado:
            return exito, mensaje, False

        mensaje = f"Aceptación fallida: {verificacion.motivo()}"
        self.log_mensaje.emit(
            f"🔎 [{agente.nombre}] Aceptación fallida → "
            f"{verificacion.motivo()}",
            "#dc3545"
        )
        return False, mensaje, True

    def _preparar_cierre(
        self, agente: Agente, tiempo_fin: float, duracion: float, resultado
    ) -> None:
        """FASE 5a: persiste tiempos, duración acumulada y log de loops."""
        agente.tiempo_fin = tiempo_fin
        # 3.7: ACUMULATIVA entre reintentos. Antes se sobrescribía, así
        # que con intentos de 5 + 7 + 4 s `duracion` acababa en 4 s (el
        # último) en vez de 16 s. Se acumula en un atributo propio para
        # no arrastrar el 0.1 de relleno que fija ``Agente.__post_init__``.
        _acumulada = (
            float(getattr(agente, "_duracion_acumulada", 0.0) or 0.0)
            + duracion
        )
        agente._duracion_acumulada = _acumulada
        agente.duracion = _acumulada
        agente.progreso = 80
        agente.mensaje = "Procesando resultado..."

        # ── Log especial para loops ──
        if agente.tipo == TipoAgente.LOOP:
            self._loops_activos.discard(agente.id)
            self._loop_items_procesados.pop(agente.id, None)
            if resultado and isinstance(resultado, dict):
                total_items = resultado.get('total_items', 0)
                exitos = resultado.get('exitos', 0)
                errores_loop = resultado.get('errores', 0)
                no_ejecutados = resultado.get('no_ejecutados', 0)
                duracion_loop = resultado.get('duracion_total', 0)
                self.log_mensaje.emit(
                    f"📊 [{agente.nombre}] Loop: {total_items} items, "
                    f"✅{exitos} ❌{errores_loop} ⏭{no_ejecutados} no ejec | {duracion_loop:.2f}s",
                    "#6f42c1"
                )

    def _cerrar_intento(
        self,
        agente: Agente,
        token,
        exito: bool,
        mensaje: str,
        resultado,
        tiempo_inicio: float,
        fallo_verificacion: bool,
        contexto_extra: dict | None,
    ) -> tuple[str | None, bool]:
        """FASE 5: duración, estado final y política de reintento.

        Devuelve ``(razon_para_bloquear_dependientes, hay_que_reintentar)``. La
        rama de reintento era un ``return`` a mitad del método; ahora se señala
        con el segundo elemento.
        """
        tiempo_fin = time.time()
        duracion = tiempo_fin - tiempo_inicio

        with self._lock:
            self._preparar_cierre(agente, tiempo_fin, duracion, resultado)

            # ── Determinar estado final usando la máquina de estados ──
            # ``razon`` debe estar siempre definida: fue_cancelado puede
            # venir de self._cancelados sin que el token esté cancelado
            # (p. ej. si el agente se marcó cancelado mientras un reintento
            # registraba un token nuevo). Antes eso provocaba un
            # UnboundLocalError silencioso en el hilo worker.
            razon = token.obtener_metadata(
                'razon_cancelacion', 'Cancelado por el usuario'
            )
            fue_cancelado = agente.id in self._cancelados
            # Verificar si el token fue cancelado
            if token.esta_cancelado():
                fue_cancelado = True

            self._cancelados.discard(agente.id)

            if fue_cancelado:
                try:
                    agente.transicionar_a(
                        EstadoAgente.CANCELADO,
                        f"⛔ {razon}"
                    )
                except ValueError:
                    agente.estado = EstadoAgente.CANCELADO
                    agente.mensaje = f"⛔ {razon}"
                agente.progreso = 100
                self.log_mensaje.emit(
                    f"⛔ [{agente.nombre}] {razon}",
                    "#6c757d"
                )
                return f"{razon}", False

            if exito:
                try:
                    agente.transicionar_a(
                        EstadoAgente.COMPLETADO,
                        "✅ Completado"
                    )
                except ValueError:
                    agente.estado = EstadoAgente.COMPLETADO
                    agente.mensaje = "✅ Completado"
                agente.progreso = 100
                agente.salida = mensaje
                agente.resultado = resultado

                resumen_resultado = self._resumir_resultado_log(agente, resultado)
                self.log_mensaje.emit(
                    f"✅ [{agente.nombre}] Completado en {duracion:.2f}s → {resumen_resultado}",
                    "#28a745"
                )
                return None, False

            # ── Fallo: verificar si fue timeout ──
            es_timeout = (
                "timeout" in mensaje.lower() or
                "excedió" in mensaje.lower() or
                "timed out" in mensaje.lower()
            )

            if es_timeout:
                try:
                    agente.transicionar_a(
                        EstadoAgente.TIMEOUT,
                        f"⏱️ Timeout: {mensaje[:100]}"
                    )
                except ValueError:
                    agente.estado = EstadoAgente.TIMEOUT
                    agente.mensaje = "⏱️ Timeout"
                agente.progreso = 100
                agente.error = mensaje
                self.log_mensaje.emit(
                    f"⏱️ [{agente.nombre}] Timeout después de {duracion:.2f}s",
                    "#dc3545"
                )
                return f"Timeout: {mensaje[:100]}", False

            # ── Fallo de aceptación: error final SIN reintentos ──
            # El artefacto no cumple el contrato y el contrato es
            # determinista: reintentar el mismo paso daría el mismo
            # resultado. Se falla ya y el motivo va al Plan B.
            if fallo_verificacion:
                try:
                    agente.transicionar_a(
                        EstadoAgente.ERROR,
                        f"❌ {mensaje[:150]}"
                    )
                except ValueError:
                    agente.estado = EstadoAgente.ERROR
                    agente.mensaje = f"❌ {mensaje[:150]}"
                agente.progreso = 100
                agente.error = mensaje
                self.log_mensaje.emit(
                    f"❌ [{agente.nombre}] Rechazado por aceptación "
                    f"tras {duracion:.2f}s → {mensaje[:200]}",
                    "#dc3545"
                )
                # ``mensaje`` ya empieza por "Aceptación fallida:"; ese
                # motivo es el que recibe el Plan B.
                return mensaje, False

            # ── Verificar reintentos ──
            if agente.reintentos < agente.max_reintentos:
                agente.reintentos += 1
                try:
                    agente.transicionar_a(
                        EstadoAgente.REINTENTANDO,
                        f"🔄 Reintento {agente.reintentos}/{agente.max_reintentos}"
                    )
                except ValueError:
                    agente.estado = EstadoAgente.REINTENTANDO
                    agente.mensaje = f"🔄 Reintento {agente.reintentos}/{agente.max_reintentos}"
                agente.progreso = 0
                self.running.discard(agente.id)
                self._invalidar_stats_cache()
                self.log_mensaje.emit(
                    f"🔄 [{agente.nombre}] Reintentando "
                    f"({agente.reintentos}/{agente.max_reintentos})",
                    "#ffc107"
                )
                try:
                    agente.transicionar_a(
                        EstadoAgente.EN_COLA,
                        f"📋 En cola para reintento {agente.reintentos}"
                    )
                except ValueError:
                    agente.estado = EstadoAgente.EN_COLA
                self._reintentar_agente.emit(agente.id, contexto_extra)
                self.agente_actualizado.emit(agente.id)
                self._intentar_lanzar_internal()
                return None, True

            # ── Error final (sin más reintentos) ──
            try:
                agente.transicionar_a(
                    EstadoAgente.ERROR,
                    f"❌ Error tras {agente.reintentos} reintentos"
                )
            except ValueError:
                agente.estado = EstadoAgente.ERROR
                agente.mensaje = f"❌ Error tras {agente.reintentos} reintentos"
            agente.progreso = 100
            agente.error = mensaje

            error_resumen = self._resumir_error_log(agente, mensaje, resultado)
            self.log_mensaje.emit(
                f"❌ [{agente.nombre}] Error después de {duracion:.2f}s → {error_resumen}",
                "#dc3545"
            )
            return f"Error: {mensaje[:100]}", False

    def _actualizar_conjuntos_finales(self, agente: Agente) -> None:
        """Mueve el agente de ``running`` a ``completed`` si está en estado final."""
        with self._lock:
            # ── Actualizar conjuntos de estado ──
            self.running.discard(agente.id)
            if agente.estado in (
                EstadoAgente.COMPLETADO,
                EstadoAgente.ERROR,
                EstadoAgente.TIMEOUT,
                EstadoAgente.CANCELADO,
                EstadoAgente.SALTADO,
                EstadoAgente.BLOQUEADO
            ):
                self.completed.add(agente.id)
                self._invalidar_stats_cache()

    def _cerrar_token_cancelacion(self, agente: Agente, token) -> None:
        """``finally``: completa y desregistra el token de cancelación."""
        token.completar()
        with self._lock:
            # Solo borrar si el token registrado sigue siendo el nuestro:
            # en la ruta de reintento el mismo agente puede tener ya otro
            # worker con un token nuevo, y borrarlo lo dejaba fuera del
            # mapa de cancelación (detener() ya no podría pararlo).
            if self._tokens_activos.get(agente.id) is token:
                self._tokens_activos.pop(agente.id, None)
        self._gestor_cancelacion.eliminar_token(token.id)

    def _avisar_riesgo_aprendizaje(self, agente: Agente):
        """
        Consulta al LearningEngine si el agente tiene riesgo alto de fallo.
        Solo AVISA en el log, no bloquea la ejecución.

        Si el sistema de aprendizaje no está disponible, no hace nada.
        """
        try:
            from learning import obtener_learning_engine

            engine = obtener_learning_engine()
            if engine is None:
                return

            riesgo = engine.predecir_riesgo_agente(agente)
            logger.info(
                f"🧠 [{agente.nombre}] riesgo: "
                f"p={riesgo['probabilidad_exito']:.2f} "
                f"conf={riesgo['confianza']} "
                f"n={riesgo['n_muestras']}"
            )

            # Solo avisamos si el modelo tiene confianza suficiente
            # y la probabilidad de éxito es baja.
            if (
                riesgo.get("confianza") in ("media", "alta") and
                riesgo.get("probabilidad_exito", 1.0) < 0.4
            ):
                self.log_mensaje.emit(
                    f"⚠️ [{agente.nombre}] Riesgo de fallo estimado: "
                    f"{1 - riesgo['probabilidad_exito']:.0%} "
                    f"(basado en {riesgo['n_muestras']} ejecuciones previas)",
                    "#ffc107"
                )
                self._bus.publicar_log(
                    f"⚠️ Riesgo alto para '{agente.nombre}'",
                    "#ffc107",
                    origen="scheduler.learning"
                )
        except Exception as e:
            # Silencioso: el aprendizaje nunca debe romper la ejecución
            logger.debug(f"Aviso de aprendizaje falló: {e}")

    # core/scheduler.py - MÉTODO _bloquear_dependientes COMPLETO

    def _reclamar_plan_b(self) -> bool:
        """Reclama de forma atómica el turno de Plan B.

        Antes la comprobación del turno vivía dentro del ``with self._lock``
        de FASE 5, que serializaba a los workers. Al sacar la llamada al LLM
        fuera del crítico hay que reservar el turno con el lock tomado y
        soltarlo antes de la llamada de red, para que dos fallos simultáneos
        no generen dos Plan B en paralelo.

        Devuelve True si este worker se queda el turno (deja
        ``_plan_b_en_progreso`` a True); False si ya hay un Plan B en curso o
        se agotaron los intentos.
        """
        with self._lock:
            if self.recovery is None or self._plan_b_en_progreso:
                return False
            if self._plan_b_intentos >= self._max_intentos_plan_b:
                logger.info(
                    f"Plan B agotado: {self._plan_b_intentos}/"
                    f"{self._max_intentos_plan_b} intentos"
                )
                return False
            # Presupuesto de tiempo (H7): un Plan B no puede estirarse sin fin.
            if self._plan_b_inicio is None:
                self._plan_b_inicio = time.time()
            elif (time.time() - self._plan_b_inicio) > self._presupuesto_plan_b_seg:
                logger.warning(
                    f"Plan B agotado por tiempo: "
                    f"{time.time() - self._plan_b_inicio:.0f}s > "
                    f"{self._presupuesto_plan_b_seg:.0f}s"
                )
                return False
            self._plan_b_en_progreso = True
            return True

    def _decidir_recuperacion(
        self,
        agente: Agente,
        razon: str,
        origen: str = "ejecucion",
    ) -> RecoveryDecision:
        """Pregunta al RecoveryManager si el fallo merece un Plan B (V3.8).

        Nunca debe romper la ejecución: si el manager falla, se permite el
        reintento (como antes de V3.8).
        """
        try:
            return self.recovery_manager.decidir(
                error=razon,
                agente=agente,
                intento=self._plan_b_intentos + 1,
                origen=origen,
                presupuesto_agotado=self.presupuesto.agotado(),
            )
        except Exception as e:  # nunca romper la recuperación por el manager
            logger.warning(
                f"RecoveryManager falló; se permite el reintento por defecto: {e}"
            )
            return RecoveryDecision(
                permitir_reintento=True,
                motivo=f"RecoveryManager no disponible: {e}",
            )

    def _registrar_parada_dura(self, decision: RecoveryDecision, agente: Agente) -> None:
        """Anota y anuncia una parada dura: no se gasta Plan B."""
        self._parada_dura = decision.to_dict()
        nombre = getattr(agente, "nombre", "?")
        self.log_mensaje.emit(
            f"🛑 Parada dura [{decision.codigo}]: {decision.motivo} "
            f"(no se intenta Plan B)",
            "#dc3545",
        )
        try:
            self._bus.publicar_log(
                f"🛑 Parada dura [{decision.codigo}] en '{nombre}': "
                f"{decision.motivo}",
                "#dc3545",
                origen="scheduler.recovery",
            )
        except Exception:
            pass
        logger.error(
            f"Parada dura [{decision.codigo}] en '{nombre}': {decision.motivo}"
        )
        # Auditoría best-effort en ``reparaciones_plan`` (nunca rompe el flujo).
        try:
            db_path = getattr(self.recovery, "db_path", "")
            if db_path:
                from .plan_recovery import registrar_reparacion

                registrar_reparacion(
                    db_path,
                    problema=self._problema_original,
                    intento=self._plan_b_intentos + 1,
                    agente=nombre,
                    error=decision.motivo,
                    estrategia="parada_dura",
                    resultado=f"parada_dura:{decision.codigo}",
                    exito=False,
                )
        except Exception as e:
            logger.debug(f"No se pudo auditar la parada dura: {e}")

    def _bloquear_dependientes(self, agente_id: str, razon: str) -> bool:
        """
        Bloquea a todos los agentes que dependen directamente del agente que falló.
        Incluye cancelación de tokens activos.
        Intenta Plan B antes de bloquear si hay recovery inyectado.

        Devuelve True si el Plan B se lanzó (la ejecución se reinició con un
        plan nuevo) y False si se aplicó el bloqueo normal.

        ⚠️ Puede llamar al LLM (Plan B) y por eso debe invocarse SIEMPRE
        fuera de ``self._lock``.
        """
        # ⬇ PARCHE PLAN B: intentar recuperación antes de bloquear.
        # El turno se reclama con el lock tomado, pero la llamada al LLM se
        # hace ya sin él.
        with self._lock:
            agente_fallido_pre = self.agentes.get(agente_id)
        if agente_fallido_pre is not None:
            # V3.8: antes de gastar un Plan B, el RecoveryManager decide si el
            # fallo es recuperable. Las paradas duras (API key ausente,
            # dependencia que falta, problema inválido, bloqueo de seguridad,
            # contrato imposible, presupuesto agotado, timeout global) abortan
            # SIN llamar al LLM ni consumir intentos.
            decision = self._decidir_recuperacion(agente_fallido_pre, razon)
            if not decision.permitir_reintento:
                self._registrar_parada_dura(decision, agente_fallido_pre)
            elif self._reclamar_plan_b():
                if self._intentar_plan_b(agente_fallido_pre, razon):
                    return True  # Plan B lanzado con éxito, no bloquear nada

        with self._lock:
            dependientes_bloqueados = []
            agente_fallido = self.agentes.get(agente_id)
            nombre_fallido = agente_fallido.nombre if agente_fallido else "desconocido"

            # Buscar todos los agentes que dependen de 'agente_id'
            for otro_agente in self.agentes.values():
                if agente_id in (otro_agente.dependencias_ids or []):
                    if not EstadoAgente.es_terminal(otro_agente.estado):
                        # Marcar como BLOQUEADO
                        otro_agente.estado = EstadoAgente.BLOQUEADO
                        otro_agente.mensaje = f"🚫 Bloqueado porque '{nombre_fallido}' {razon}"
                        otro_agente.progreso = 100
                        dependientes_bloqueados.append(otro_agente.nombre)

                        # Cancelar token si existe
                        if otro_agente.id in self._tokens_activos:
                            token = self._tokens_activos[otro_agente.id]
                            try:
                                if hasattr(token, "esta_activo") and token.esta_activo():
                                    token.cancelar(f"Bloqueado por dependencia '{nombre_fallido}'")
                                elif hasattr(token, "cancel"):
                                    token.cancel()
                            except Exception:
                                pass

                        self.running.discard(otro_agente.id)
                        self.completed.add(otro_agente.id)
                        self.agente_actualizado.emit(otro_agente.id)

            # ── Log de bloqueos ──
            if dependientes_bloqueados:
                nombres = ', '.join(dependientes_bloqueados)
                self.log_mensaje.emit(
                    f"🚫 [{nombre_fallido}] Bloqueó a: {nombres}",
                    "#8b0000"
                )
                logger.warning(
                    f"Agente {nombre_fallido} bloqueó a {len(dependientes_bloqueados)} dependientes"
                )
            else:
                self.log_mensaje.emit(
                    f"ℹ [{nombre_fallido}] No tiene dependientes activos",
                    "#6c757d"
                )
            # ✅ FIX BUG 3: notificar término si ya no quedan agentes activos.
            # Sin esto, cuando el Plan B falla y se bloquean dependientes,
            # la UI queda esperando `ejecucion_terminada` para siempre.
            self._verificar_terminado_internal()

        return False

    def _intentar_plan_b(self, agente_fallido, razon: str) -> bool:
        """
        Intenta generar y ejecutar un plan alternativo.
        Devuelve True si lo lanzó con éxito, False si hay que bloquear como antes.

        Debe llamarse SOLO tras un ``_reclamar_plan_b()`` que devolvió True:
        el turno (``_plan_b_en_progreso``) ya viene reservado y aquí se hace
        la llamada al LLM sin el lock del scheduler.

        V4.0-5: el método era un bloque de ~220 líneas con siete fases
        entrelazadas. Ahora es un ORQUESTADOR que delega en ``_pb_*``. La
        extracción es mecánica: el orden, los efectos y los mensajes son los
        mismos (lo fijan los tests de caracterización de
        ``tests/test_scheduler_plan_b_caracterizacion.py``).
        """
        # ⛔ B2: si el scheduler ya se detuvo, no se arranca otro plan. Sin
        # este guard, un Plan B en vuelo durante el cierre del proceso llegaba
        # a ``iniciar()`` y fallaba con "cannot schedule new futures after
        # interpreter shutdown" (RuntimeError en scheduler.py).
        if self._detenido:
            logger.info(
                "Plan B descartado: el scheduler está detenido (_detenido=True)"
            )
            self._plan_b_en_progreso = False
            return False

        try:
            intento, estrategia = self._pb_preparar_intento(agente_fallido, razon)

            # 1. Pedir plan B al LLM (bloqueante ~5-15s, SIN el lock del
            #    scheduler: por eso se reclama el turno antes de entrar).
            plan_b = self._pb_pedir_plan_al_llm(agente_fallido, razon, intento, estrategia)

            if plan_b is None or not getattr(plan_b, "agentes_generados", None):
                return self._pb_abortar_sin_plan(
                    agente_fallido, razon, intento, estrategia
                )

            self._pb_anunciar_plan_generado(plan_b)

            # 2. Detener ejecución actual (cancela tokens, workers)
            #    Antes de tirar el estado, se guardan los agentes COMPLETADOS
            #    cuyo artefacto pasó la verificación: H7 evita repetir trabajo
            #    bueno (A→B→C→D con D fallido no reejecuta A/B/C).
            reutilizables = self._pb_recoger_reutilizables()
            self.detener()

            # 3. Limpiar TODO el estado y cargar los nuevos agentes
            self._pb_limpiar_estado()
            self._pb_cargar_plan(plan_b)

            # 4. Reutilizar los agentes idénticos ya completados y verificados.
            reutilizados = self._pb_reutilizar_agentes(plan_b, reutilizables)

            # 5. Actualizar el plan original para futuros Plan B y auditar.
            self._pb_registrar_exito(
                agente_fallido, razon, intento, estrategia, plan_b, reutilizados
            )

            # 6. Notificar a la UI y arrancar el nuevo plan.
            self._pb_anunciar_plan_cargado(plan_b)
            self._plan_b_en_progreso = False
            self.iniciar()
            return True

        except Exception as e:
            logger.exception(f"Error en Plan B: {e}")
            self._plan_b_en_progreso = False
            return False

    # ------------------------------------------------------------
    # Fases de _intentar_plan_b (V4.0-5: extracción mecánica)
    # ------------------------------------------------------------
    def _pb_preparar_intento(self, agente_fallido, razon: str) -> tuple[int, str]:
        """Reserva el número de intento, elige estrategia y anota la traza.

        Devuelve ``(intento, estrategia)``. Consume el intento
        (``_plan_b_intentos += 1``) antes de la llamada al LLM: si la llamada
        falla, el intento ya está gastado (comportamiento preexistente).
        """
        from .plan_recovery import estrategia_para_intento, firma_plan

        intento = self._plan_b_intentos + 1
        estrategia = estrategia_para_intento(intento)

        # H7: la firma del plan que acaba de fallar entra en el conjunto
        # anti-repetición para que el LLM no genere un plan equivalente.
        firma_fallida = firma_plan(self._plan_original) if self._plan_original else ""
        if firma_fallida:
            self._firmas_plan_fallidas.add(firma_fallida)
        self._reparaciones_intentadas.append({
            "intento": intento,
            "estrategia": estrategia,
            "agente": getattr(agente_fallido, "nombre", "?"),
            "error": (razon or "")[:300],
        })

        logger.info(
            f"🔧 [Plan B #{intento}] estrategia={estrategia} "
            f"'{agente_fallido.nombre}' falló: {razon}"
        )
        self.log_mensaje.emit(
            f"🔧 Plan B #{intento} ({estrategia}): "
            f"'{agente_fallido.nombre}' falló. "
            f"Consultando al LLM... (puede tardar ~20s)",
            "#ffc107"
        )

        # El turno ya está reclamado (``_plan_b_en_progreso=True``) por
        # ``_reclamar_plan_b`` antes de entrar aquí.
        self._plan_b_intentos += 1
        return intento, estrategia

    def _pb_pedir_plan_al_llm(self, agente_fallido, razon: str, intento: int, estrategia: str):
        """Consulta al LLM (bloqueante). No toma el lock del scheduler."""
        return self.recovery.generar_plan_b(
            problema_original=self._problema_original,
            plan_fallido=self._plan_original,
            agente_fallido=agente_fallido,
            error=razon,
            estrategia=estrategia,
            errores_previos=list(self._reparaciones_intentadas),
            firmas_fallidas=set(self._firmas_plan_fallidas),
            intento=intento,
        )

    def _pb_abortar_sin_plan(self, agente_fallido, razon: str, intento: int, estrategia: str) -> bool:
        """No hay plan B: auditar, bloquear lo no terminal y terminar. → False."""
        from .plan_recovery import registrar_reparacion

        self.log_mensaje.emit(
            "⚠️ Plan B descartado: sin plan válido o plan repetido. "
            "Bloqueando dependientes.",
            "#ffc107"
        )
        logger.warning("Plan B no disponible, bloqueando como antes")
        registrar_reparacion(
            getattr(self.recovery, "db_path", ""),
            problema=self._problema_original,
            intento=intento,
            agente=getattr(agente_fallido, "nombre", ""),
            error=razon,
            estrategia=estrategia,
            resultado="sin_plan",
            exito=False,
        )
        self._plan_b_en_progreso = False
        # ⬇️ Marcar todos los agentes no-terminales como BLOQUEADOS para que el
        # recuento sea coherente y la ejecución termine.
        with self._lock:
            for ag in self.agentes.values():
                if EstadoAgente.es_terminal(ag.estado):
                    continue
                if ag.id in self.running:
                    continue  # dejar que terminen los que están corriendo
                ag.estado = EstadoAgente.BLOQUEADO
                ag.mensaje = "🚫 Plan B agotado: no se pudo recuperar la ejecución"
                ag.progreso = 100
                self.completed.add(ag.id)
                self.agente_actualizado.emit(ag.id)
            # ✅ FIX BUG 3: notificar término tras bloquear.
            self._verificar_terminado_internal()
        return False

    def _pb_anunciar_plan_generado(self, plan_b) -> None:
        self.log_mensaje.emit(
            f"✅ Plan B #{self._plan_b_intentos}: "
            f"{len(plan_b.agentes_generados)} agentes generados. "
            f"Cargando y reiniciando ejecución...",
            "#28a745"
        )

    def _pb_recoger_reutilizables(self) -> dict[str, dict]:
        """Agentes completados cuyo artefacto pasó la verificación (H7)."""
        from .plan_recovery import firma_agente

        reutilizables: dict[str, dict] = {}
        with self._lock:
            for ag in self.agentes.values():
                if ag.estado != EstadoAgente.COMPLETADO or ag.resultado is None:
                    continue
                verificacion = self._verificaciones.get(ag.id)
                if verificacion is not None and not verificacion.get("aceptado", True):
                    continue
                reutilizables[firma_agente(ag)] = {
                    "resultado": ag.resultado,
                    "salida": ag.salida,
                    "verificacion": verificacion,
                }
        return reutilizables

    def _pb_limpiar_estado(self) -> None:
        """Deja el scheduler como recién construido (salvo configuración)."""
        with self._lock:
            self.agentes.clear()
            self.completed.clear()
            self.running.clear()
            self._cancelados.clear()
            self._loops_activos.clear()
            self._loop_items_procesados.clear()
            self._terminado_notificado = False
            self._tiempo_inicio_ejecucion = None
            self._verificaciones.clear()
            self._ultima_aceptacion = None
            self._invalidar_stats_cache()

    def _pb_cargar_plan(self, plan_b) -> None:
        for agente in plan_b.agentes_generados:
            self.agregar_agente(agente)
        self.resolver_dependencias()

    def _pb_reutilizar_agentes(self, plan_b, reutilizables: dict[str, dict]) -> list[str]:
        """Marca como completados los agentes idénticos ya verificados."""
        from .plan_recovery import firma_agente

        reutilizados: list[str] = []
        with self._lock:
            for agente in plan_b.agentes_generados:
                datos = reutilizables.get(firma_agente(agente))
                if not datos:
                    continue
                agente.estado = EstadoAgente.COMPLETADO
                agente.resultado = datos["resultado"]
                agente.salida = datos.get("salida", "")
                agente.progreso = 100
                agente.mensaje = "♻ reutilizado del plan anterior (artefacto válido)"
                self.completed.add(agente.id)
                if datos.get("verificacion") is not None:
                    self._verificaciones[agente.id] = datos["verificacion"]
                reutilizados.append(agente.nombre)
            if reutilizados:
                self._invalidar_stats_cache()

        if reutilizados:
            self.log_mensaje.emit(
                f"♻ Plan B reutiliza {len(reutilizados)} agente(s) ya "
                f"válidos: {', '.join(reutilizados)}",
                "#28a745"
            )
            logger.info(
                f"Plan B: {len(reutilizados)} agentes reutilizados sin "
                f"reejecutar: {reutilizados}"
            )
        return reutilizados

    def _pb_registrar_exito(
        self, agente_fallido, razon: str, intento: int, estrategia: str,
        plan_b, reutilizados: list[str],
    ) -> None:
        """Actualiza el plan de referencia y audita el intento (H7)."""
        from .plan_recovery import firma_plan, registrar_reparacion

        # El plan B pasa a ser el plan de referencia para futuros Plan B.
        self._plan_original = plan_b
        registrar_reparacion(
            getattr(self.recovery, "db_path", ""),
            problema=self._problema_original,
            intento=intento,
            agente=getattr(agente_fallido, "nombre", ""),
            error=razon,
            estrategia=estrategia,
            plan_firma=firma_plan(plan_b),
            resultado=(
                f"plan_generado ({len(plan_b.agentes_generados)} agentes, "
                f"{len(reutilizados)} reutilizados)"
            ),
            exito=True,
        )

    def _pb_anunciar_plan_cargado(self, plan_b) -> None:
        self.log_mensaje.emit(
            f"✅ Plan B #{self._plan_b_intentos}: "
            f"{len(plan_b.agentes_generados)} agentes cargados. "
            f"Reiniciando ejecución...",
            "#28a745"
        )


    # ============================================================
    # HELPERS PARA LOG DE RESULTADOS
    # ============================================================

    def _resumir_resultado_log(self, agente: Agente, resultado: Any) -> str:
        """Resumen legible del resultado (V3.8-6: lógica en execution_log)."""
        return resumir_resultado(
            agente, resultado, max_len=self._MAX_RESULTADO_LOG
        )

    def _resumir_error_log(self, agente: Agente, mensaje: str, resultado: Any) -> str:
        """Resumen del error para el log (V3.8-6: lógica en execution_log)."""
        return resumir_error(
            agente, mensaje, resultado, max_len=self._MAX_RESULTADO_LOG
        )

    def _manejar_error_importacion(self, agente: Agente, error: Exception):
        """Maneja errores de importación del executor."""
        # V3.8: que el executor no se pueda importar es falta de dependencia
        # del entorno: parada dura, no se intenta Plan B ni se gasta dinero.
        try:
            decision = self.recovery_manager.decidir(
                error=str(error), agente=agente, origen="importacion"
            )
            self._registrar_parada_dura(decision, agente)
        except Exception as e:  # el registro de la parada nunca rompe el flujo
            logger.debug(f"No se pudo registrar la parada dura de importación: {e}")

        with self._lock:
            agente.estado = EstadoAgente.ERROR
            agente.progreso = 100
            agente.mensaje = f"Error de importación: {error}"
            agente.error = str(error)
            agente.tiempo_fin = time.time()
            self.running.discard(agente.id)
            self.completed.add(agente.id)
            self._invalidar_stats_cache()

        self.log_mensaje.emit(
            f"❌ [{agente.nombre}] Error de importación: {error}",
            "#dc3545"
        )
        self.agente_actualizado.emit(agente.id)

        with self._lock:
            self._verificar_terminado_internal()
            self._intentar_lanzar_internal()

    # ============================================================
    # SLOT PARA REINTENTOS
    # ============================================================
    @pyqtSlot(str, object)
    def _on_reintentar_agente(self, agente_id: str, contexto_extra: dict):
        """
        Slot para reintentar un agente desde el hilo principal.
        Recibe solo el ID (no el objeto vivo) para evitar data race con el worker.
        """
        with self._lock:
            agente = self.agentes.get(agente_id)
            if agente is None:
                return
            # Si el lanzamiento inmediato del reintento ya funcionó, el agente
            # está en ``running``: esta señal encolada es un duplicado.
            if agente.id in self.running:
                return
            if not EstadoAgente.puede_ejecutarse(agente.estado):
                return
            # _ejecutar_agente solo acepta LISTO → EJECUTANDO: las aristas
            # EN_COLA/REINTENTANDO → EJECUTANDO no existen, así que el worker
            # abortaba con "No se puede ejecutar" y el reintento se perdía
            # (y, al no sumarse a running, no contaba para max_concurrent).
            agente.estado = EstadoAgente.LISTO
            self.running.add(agente.id)
            self._invalidar_stats_cache()
            self._executor.submit(self._ejecutar_agente, agente, contexto_extra)

    # ============================================================
    # VERIFICACIÓN DE TÉRMINO
    # ============================================================
    def _verificar_terminado_internal(self):
        # ⬇️ PARCHE PLAN B: si hay un Plan B en progreso, no dar por terminado
        if self._plan_b_en_progreso:
            return
        stats = self._calcular_estadisticas_internal()
        total_terminados = (
            stats['completados']
            + stats['errores']
            + stats['cancelados']
            + stats['bloqueados']
            + stats['timeout']
            + stats['saltados']
        )

        if total_terminados == stats['total'] and stats['total'] > 0:
            if not self._terminado_notificado:
                self.ejecutando = False
                self.pausado = False
                self._terminado_notificado = True
                # Veredicto de aceptación (H6): «terminado» no es «aceptado».
                aceptacion = self._calcular_aceptacion_internal()
                if aceptacion["aceptada"]:
                    self.log_mensaje.emit(
                        f"🔎 Aceptación de la salida: OK "
                        f"({len(aceptacion['pasos'])} paso(s) verificado(s))",
                        "#28a745"
                    )
                else:
                    self.log_mensaje.emit(
                        f"🔎 Aceptación de la salida: {aceptacion['resumen']}",
                        "#dc3545"
                    )
                self.ejecucion_terminada.emit()
                self.estado_cambiado.emit(False)
            # logger temporal logger.info(f"TERMINADO: intentos_plan_b={self._plan_b_intentos}")
            #logger.info(f"TERMINADO: intentos_plan_b={self._plan_b_intentos}")

    # ============================================================
    # CONTROL DE EJECUCIÓN (API pública)
    # ============================================================
    def iniciar(self):
        """Inicia la ejecución de todos los agentes."""
        if self.ejecutando:
            return

        # Validaciones previas
        self.resolver_dependencias()

        fuentes_validas, errores_fuente = self._validar_fuentes_loop()
        if not fuentes_validas:
            for error in errores_fuente:
                self.log_mensaje.emit(f"⚠️ LOOP inválido: {error}", "#dc3545")
            return

        tiene_ciclos, ciclos = self.detectar_ciclos()
        if tiene_ciclos:
            self.log_mensaje.emit(
                f"⚠️ CICLOS DETECTADOS: {ciclos}. La ejecución no puede comenzar.",
                "#dc3545"
            )
            return

        with self._lock:
            # Arranque explícito: se limpia la marca de parada de B2 para que
            # un Plan B posterior vuelva a estar permitido.
            self._detenido = False
            self.ejecutando = True
            self.pausado = False
            self._terminado_notificado = False
            self._tiempo_inicio_ejecucion = time.time()
            self._verificaciones.clear()
            self._ultima_aceptacion = None

            # Resetear agentes en cualquier estado terminal (incluye TIMEOUT,
            # SALTADO y BLOQUEADO, no solo COMPLETADO/ERROR/CANCELADO).
            for agente in self.agentes.values():
                if EstadoAgente.es_terminal(agente.estado):
                    agente.resetear_estado()
                    agente.mensaje = "Reiniciado"

        # V3.8: presupuesto de la ejecución. NO se reinicia aquí: ``iniciar()``
        # también relanza un Plan B dentro de la MISMA ejecución, y el
        # presupuesto es del conjunto (tiempo/llamadas/tokens/coste). El reloj
        # arranca la primera vez y la contabilidad se suscribe solo si hay
        # algún límite configurado (opt-in; sin límites nada cambia).
        try:
            if not self.presupuesto.iniciado:
                self.presupuesto.iniciar()
            if self.presupuesto.hay_limites():
                self.presupuesto.conectar()
        except Exception as e:
            logger.debug(f"No se pudo arrancar el presupuesto: {e}")

        # ✅ Log de inicio
        self.log_mensaje.emit(
            f"▶️ Ejecución iniciada ({len(self.agentes)} agentes)",
            "#28a745"
        )
        self.estado_cambiado.emit(True)
        self._intentar_lanzar()

    def pausar(self) -> bool:
        """Pausa o reanuda la ejecución. Retorna el nuevo estado."""
        with self._lock:
            self.pausado = not self.pausado
            estado = "Pausado" if self.pausado else "Reanudado"

        self.log_mensaje.emit(f"⏸ {estado}", "#ffc107")
        self.estado_cambiado.emit(not self.pausado)

        if not self.pausado:
            self._intentar_lanzar()

        return self.pausado

    # core/scheduler.py - MÉTODO detener COMPLETO CON CANCELACIÓN REAL

    def detener(self):
        """
        Detiene la ejecución de forma segura con cancelación real de workers.

        Idempotente: la primera llamada marca ``_detenido`` (para que
        ``_intentar_plan_b`` no vuelva a arrancar) y sustituye el executor
        por uno nuevo; las siguientes no hacen nada si ya no queda trabajo.
        """
        # ── Fase 1: Cancelar tokens activos ──
        with self._lock:
            # ⛔ B2: marca de parada consultada por ``_intentar_plan_b``.
            ya_detenido = self._detenido
            self._detenido = True
            if ya_detenido and not self.ejecutando and not self.running:
                return

            self.ejecutando = False
            self.pausado = False

            # Cancelar TODOS los tokens activos
            tokens_a_cancelar = list(self._tokens_activos.values())
            cancelados = 0
            for token in tokens_a_cancelar:
                if token.esta_activo():
                    token.cancelar("Ejecución detenida por usuario")
                    cancelados += 1

            if cancelados > 0:
                self.log_mensaje.emit(
                    f"⏹ Cancelados {cancelados} workers en ejecución",
                    "#dc3545"
                )

            agentes_a_cancelar = []
            for agente in self.agentes.values():
                if agente.estado == EstadoAgente.EJECUTANDO:
                    agente.estado = EstadoAgente.CANCELADO
                    agente.mensaje = "Cancelado por usuario"
                    self._cancelados.add(agente.id)
                    agentes_a_cancelar.append(agente.id)

                if agente.tipo == TipoAgente.LOOP:
                    self._loops_activos.discard(agente.id)
                    self._loop_items_procesados.pop(agente.id, None)

            self._invalidar_stats_cache()
            # Los futuros encolados cancelados por cancel_futures=True nunca
            # ejecutan su 'finally' que descarta el ID de running; limpiarlo
            # aquí evita que un iniciar() posterior los considere en marcha.
            self.running.clear()
            old_executor = self._executor
            self._executor = ThreadPoolExecutor(max_workers=self.max_concurrent)

        # ── Fase 2: Emitir señales (fuera del lock) ──
        for agente_id in agentes_a_cancelar:
            self.agente_actualizado.emit(agente_id)

        self.log_mensaje.emit("⏹ Ejecución detenida", "#dc3545")
        self.estado_cambiado.emit(False)

        # ── Fase 3: Shutdown del executor (FUERA del lock) ──
        # ``wait=True`` garantiza que al volver no queda ningún worker capaz
        # de hacer ``submit`` (era el origen de B2: "cannot schedule new
        # futures after interpreter shutdown"). Si ``detener()`` se invoca
        # desde un worker de ESE executor (lo hace ``_intentar_plan_b``), un
        # ``join`` del hilo actual lanzaría "cannot join current thread": en
        # ese caso se cierra sin esperar.
        es_worker_propio = threading.current_thread() in tuple(
            getattr(old_executor, "_threads", ())
        )
        old_executor.shutdown(wait=not es_worker_propio, cancel_futures=True)

        # ── Fase 4: Verificar terminado ──
        with self._lock:
            self._verificar_terminado_internal()

        # V3.8: dejar de contabilizar gasto cuando ya no hay ejecución (se
        # vuelve a suscribir en ``iniciar()`` si el presupuesto tiene límites).
        try:
            self.presupuesto.desconectar()
        except Exception as e:
            logger.debug(f"No se pudo cerrar el presupuesto: {e}")

    def limpiar(self):
        """
        Limpia todo el estado Y elimina los agentes.

        Se llama al inicio de cada ejecución desde la GUI para garantizar
        que no se arrastran agentes de ejecuciones previas.
        """
        if self.ejecutando or self.running:
            self.detener()

        # Emitir actualización para cada agente ANTES de vaciar (la UI
        # necesita saber que estos agentes ya no existen)
        with self._lock:
            for agente_id in list(self.agentes.keys()):
                self.agente_actualizado.emit(agente_id)

        with self._lock:
            self.agentes.clear()
            self.completed.clear()
            self.running.clear()
            self._cancelados.clear()
            self._loops_activos.clear()
            self._loop_items_procesados.clear()
            self._terminado_notificado = False
            self._tiempo_inicio_ejecucion = None
            self._plan_b_intentos = 0
            self._plan_b_en_progreso = False
            self._plan_b_inicio = None
            self._firmas_plan_fallidas = set()
            self._reparaciones_intentadas = []
            self._verificaciones.clear()
            self._ultima_aceptacion = None
            self._invalidar_stats_cache()

        self.ejecutando = False
        self.pausado = False
        self.log_mensaje.emit("🧹 Estado limpiado", "#6c757d")
        self.estado_cambiado.emit(False)

    # ============================================================
    # ESTADÍSTICAS (thread-safe con cache)
    # ============================================================
    def _invalidar_stats_cache(self):
        """Invalida el cache de estadísticas."""
        self._stats_cache = None
        self._stats_cache_time = 0.0

    def _calcular_estadisticas_internal(self) -> dict:
        """Calcula las estadísticas (debe llamarse bajo lock)."""
        agentes = list(self.agentes.values())
        total = len(agentes)

        return {
            "total": total,
            "completados": sum(1 for a in agentes if a.estado == EstadoAgente.COMPLETADO),
            "ejecutando": sum(1 for a in agentes if a.estado == EstadoAgente.EJECUTANDO),
            "listos": sum(1 for a in agentes if a.estado == EstadoAgente.LISTO),
            "esperando": sum(1 for a in agentes if a.estado in (EstadoAgente.PENDIENTE, EstadoAgente.ESPERANDO)),
            "errores": sum(1 for a in agentes if a.estado == EstadoAgente.ERROR),
            "cancelados": sum(1 for a in agentes if a.estado == EstadoAgente.CANCELADO),
            "bloqueados": sum(1 for a in agentes if a.estado == EstadoAgente.BLOQUEADO),  # ← NUEVO
            "timeout": sum(1 for a in agentes if a.estado == EstadoAgente.TIMEOUT),
            "saltados": sum(1 for a in agentes if a.estado == EstadoAgente.SALTADO),
            "loops_activos": len(self._loops_activos),
            "loops_total": sum(1 for a in agentes if a.tipo == TipoAgente.LOOP),
        }

    def obtener_estadisticas(self) -> dict:
        """Obtiene estadísticas con cache para reducir CPU."""
        now = time.time()
        with self._lock:
            if self._stats_cache is not None and (now - self._stats_cache_time) < self._STATS_CACHE_TTL:
                return self._stats_cache

            self._stats_cache = self._calcular_estadisticas_internal()
            self._stats_cache_time = now
            return self._stats_cache

    # ============================================================
    # ACEPTACIÓN DE LA SALIDA (H6)
    # ============================================================
    def _calcular_aceptacion_internal(self) -> dict:
        """Veredicto de aceptación de la ejecución (V3.8-6: delegado).

        Debe llamarse bajo lock; el cálculo puro vive en
        ``core/acceptance_manager.py``.
        """
        resultado = calcular_aceptacion(
            self.agentes,
            self._verificaciones,
            parada_dura=self._parada_dura,
            presupuesto=self.presupuesto,
        )
        self._ultima_aceptacion = resultado
        return resultado

    def obtener_resultado_aceptacion(self) -> dict:
        """Resultado de aceptación de la última ejecución (API pública)."""
        with self._lock:
            return self._calcular_aceptacion_internal()

    # ============================================================
    # EJECUCIÓN INDIVIDUAL
    # ============================================================
    def ejecutar_agente_individual(self, agente_id: str, contexto: dict = None) -> bool:
        """Ejecuta un agente individualmente (para pruebas)."""
        with self._lock:
            agente = self.agentes.get(agente_id)
            if not agente:
                self.log_mensaje.emit(f"⚠️ Agente {agente_id} no encontrado", "#ffc107")
                return False

            if agente.estado == EstadoAgente.EJECUTANDO:
                self.log_mensaje.emit(
                    f"⏳ El agente {agente.nombre} ya está ejecutándose",
                    "#ffc107"
                )
                return False

            if agente.estado in (EstadoAgente.COMPLETADO, EstadoAgente.ERROR, EstadoAgente.CANCELADO):
                agente.resetear_estado()

            ctx = dict(contexto) if contexto else {}
            for dep_id in agente.dependencias_ids:
                dep = self.agentes.get(dep_id)
                if dep and dep.estado == EstadoAgente.COMPLETADO and dep.resultado is not None:
                    ctx[dep.nombre] = dep.resultado
                    ctx[dep_id] = dep.resultado

            agente.estado = EstadoAgente.LISTO
            self.running.add(agente.id)
            self._invalidar_stats_cache()
            self._executor.submit(self._ejecutar_agente, agente, ctx)

        self.log_mensaje.emit(
            f"▶️ Ejecutando agente individual: {agente.nombre}",
            "#007bff"
        )
        return True

    # ============================================================
    # SEGUIMIENTO DE LOOPS
    # ============================================================
    def obtener_progreso_loop(self, agente_id: str) -> dict | None:
        """Obtiene el progreso de un loop activo."""
        with self._lock:
            if agente_id not in self._loops_activos:
                return None
            return {
                "activo": True,
                "items_procesados": self._loop_items_procesados.get(agente_id, 0),
            }

    def obtener_loops_activos(self) -> list[str]:
        """Retorna la lista de IDs de loops activos."""
        with self._lock:
            return list(self._loops_activos)

    def esta_loop_activo(self, agente_id: str) -> bool:
        """Verifica si un loop está activo."""
        with self._lock:
            return agente_id in self._loops_activos

    # ============================================================
    # CONSULTAS DE ESTADO
    # ============================================================
    def esta_ejecutando(self) -> bool:
        """Retorna True si hay una ejecución en curso."""
        return self.ejecutando

    def esta_pausado(self) -> bool:
        """Retorna True si la ejecución está pausada."""
        return self.pausado

    # ============================================================
    # LIMPIEZA FINAL
    # ============================================================
    def __del__(self):
        """Limpieza al destruir el scheduler."""
        try:
            if hasattr(self, '_executor'):
                self._executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
