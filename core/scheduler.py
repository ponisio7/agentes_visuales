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

from .agent import Agente, EstadoAgente, TipoAgente
from .bridge import SchedulerBridge
from .cancellation import CancellationToken, obtener_gestor_cancelacion
from .event_bus import obtener_bus

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

    def __init__(self, max_concurrent: int = 4):
        super().__init__()

        # ── Plan B (recuperación de fallos críticos) ──
        self.recovery = None
        self._plan_b_intentos = 0
        self._plan_b_en_progreso = False
        self._problema_original = ""
        self._plan_original = None
        self._max_intentos_plan_b = 2

        # ── Estado de agentes ──
        self.agentes: dict[str, Agente] = {}
        self.max_concurrent = max_concurrent
        self.running: set[str] = set()
        self.completed: set[str] = set()

        # ── Estado de ejecución ──
        self.ejecutando = False
        self.pausado = False
        self._terminado_notificado = False
        self._cancelados: set[str] = set()

        # ── Seguimiento de loops ──
        self._loops_activos: set[str] = set()
        self._loop_items_procesados: dict[str, int] = {}

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

    def set_contexto_plan_b(self, recovery, problema_original: str, plan_original):
        self.recovery = recovery
        self._problema_original = problema_original or ""
        self._plan_original = plan_original
        self._plan_b_intentos = 0
        self._plan_b_en_progreso = False
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
        """Resuelve las dependencias por nombre a IDs."""
        with self._lock:
            nombre_a_id = {a.nombre: a.id for a in self.agentes.values()}

            for agente in self.agentes.values():
                if agente.dependencias_nombres:
                    ids_resueltos = []
                    for nombre in agente.dependencias_nombres:
                        if nombre in nombre_a_id:
                            ids_resueltos.append(nombre_a_id[nombre])
                        else:
                            self.log_mensaje.emit(
                                f"⚠️ Dependencia '{nombre}' no encontrada para {agente.nombre}",
                                "#ffc107"
                            )
                    agente.dependencias_ids = ids_resueltos
                    agente.dependencias_nombres = []

    def detectar_ciclos(self) -> tuple[bool, list[list[str]]]:
        """Detecta ciclos en las dependencias usando DFS."""
        with self._lock:
            visitados = set()
            pila = set()
            ciclos = []
            id_a_nombre = {aid: a.nombre for aid, a in self.agentes.items()}

            def dfs(agente_id: str, path: list[str]):
                if agente_id in pila:
                    try:
                        idx = path.index(agente_id)
                    except ValueError:
                        return
                    ciclo_ids = path[idx:] + [agente_id]
                    ciclo_nombres = [id_a_nombre.get(aid, aid) for aid in ciclo_ids]
                    if ciclo_nombres not in ciclos:
                        ciclos.append(ciclo_nombres)
                    return

                if agente_id in visitados:
                    return

                visitados.add(agente_id)
                pila.add(agente_id)
                path.append(agente_id)

                agente = self.agentes.get(agente_id)
                if agente:
                    for dep_id in list(agente.dependencias_ids):
                        if dep_id in self.agentes:
                            dfs(dep_id, path)

                path.pop()
                pila.remove(agente_id)

            for agente_id in list(self.agentes.keys()):
                if agente_id not in visitados:
                    dfs(agente_id, [])

            return bool(ciclos), ciclos

    def _validar_fuentes_loop(self) -> tuple[bool, list[str]]:
        """Valida que las fuentes de items de los loops sean válidas."""
        with self._lock:
            errores = []
            for agente in self.agentes.values():
                if agente.tipo != TipoAgente.LOOP:
                    continue

                nombre_fuente = agente.obtener_nombre_dependencia()
                if not nombre_fuente:
                    errores.append(
                        f"{agente.nombre}: 'fuente_items' debe tener formato 'Dependencia.clave'"
                    )
                    continue

                nombres_deps = {
                    self.agentes[dep_id].nombre
                    for dep_id in agente.dependencias_ids
                    if dep_id in self.agentes
                }

                if nombre_fuente not in nombres_deps:
                    errores.append(
                        f"{agente.nombre}: la fuente '{nombre_fuente}' no es una "
                        f"dependencia declarada (dependencias: {sorted(nombres_deps) or 'ninguna'})"
                    )

            return not errores, errores

    # ============================================================
    # LÓGICA INTERNA DE EJECUCIÓN
    # ============================================================
    def _obtener_dependencias_pendientes(self, agente: Agente) -> list[str]:
        """
        Retorna las dependencias que aún NO están COMPLETADAS.
        Solo COMPLETADO satisface una dependencia.
        ERROR, TIMEOUT, CANCELADO, SALTADO y BLOQUEADO NO la satisfacen.
        """
        pendientes = []
        for dep_id in agente.dependencias_ids:
            dep = self.agentes.get(dep_id)
            if dep and dep.estado != EstadoAgente.COMPLETADO:
                pendientes.append(dep_id)
        return pendientes

    def _tiene_dependencias_fallidas(self, agente: Agente) -> bool:
        """Retorna True si alguna dependencia terminó en estado terminal no exitoso."""
        for dep_id in agente.dependencias_ids:
            dep = self.agentes.get(dep_id)
            if dep and dep.estado in (
                EstadoAgente.ERROR,
                EstadoAgente.TIMEOUT,
                EstadoAgente.CANCELADO,
                EstadoAgente.SALTADO,
                EstadoAgente.BLOQUEADO,
            ):
                return True
        return False

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
        """
        Ejecuta un agente en un hilo worker con soporte para cancelación.
        """
        # ── Import del executor ──
        try:
            from core.executors import AgentExecutor
        except ImportError as e:
            self._manejar_error_importacion(agente, e)
            return

        # ── FASE 1: Crear token de cancelación ──
        token = self._gestor_cancelacion.crear_token({
            'agente_id': agente.id,
            'agente_nombre': agente.nombre,
            'timestamp_inicio': time.time()
        })

        with self._lock:
            self._tokens_activos[agente.id] = token

        try:
            # ── FASE 2: INICIO ──
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
                    return

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

            # ── FASE 3: Construir contexto ──
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
                            return

                        if dep and dep.resultado is not None and dep.estado == EstadoAgente.COMPLETADO:
                            contexto[dep.nombre] = dep.resultado
                            contexto[dep_id] = dep.resultado

            # ── FASE 4: Ejecutar con timeout y token de cancelación ──
            if agente.tipo == TipoAgente.LOOP:
                with self._lock:
                    self._loops_activos.add(agente.id)
                    self._loop_items_procesados[agente.id] = 0

            # ── ✅ NUEVO: Aviso de riesgo basado en aprendizaje ──
            self._avisar_riesgo_aprendizaje(agente)

            try:

                exito, mensaje, resultado = AgentExecutor.ejecutar(
                    agente,
                    contexto,
                    cancellation_token=token  # ← PASAR EL TOKEN
                )
            except Exception as e:
                exito, mensaje, resultado = False, f"Error crítico: {e}", {}
                logger.exception(f"Error ejecutando agente {agente.nombre}")

            # ── FASE 5: FINALIZACIÓN ──
            tiempo_fin = time.time()
            duracion = tiempo_fin - tiempo_inicio

            # Bajo lock SOLO se decide si hay que bloquear dependientes. El
            # bloqueo (que puede llamar al LLM para el Plan B) se ejecuta
            # después, fuera del crítico: con el lock tomado, la llamada de
            # red (~15-20 s) congelaba la UI (obtener_estadisticas usa el
            # mismo lock) y paraba al resto de workers en FASE 5.
            bloquear_razon: str | None = None

            with self._lock:
                agente.tiempo_fin = tiempo_fin
                agente.duracion = duracion  # ← persistir duración real
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
                    bloquear_razon = f"{razon}"

                elif exito:
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

                else:
                    # ── Verificar si fue timeout ──
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
                        bloquear_razon = f"Timeout: {mensaje[:100]}"

                    # ── Verificar reintentos ──
                    elif agente.reintentos < agente.max_reintentos:
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
                        return

                    else:
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
                        bloquear_razon = f"Error: {mensaje[:100]}"

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

        finally:
            # ── Limpiar token ──
            token.completar()
            with self._lock:
                # Solo borrar si el token registrado sigue siendo el nuestro:
                # en la ruta de reintento el mismo agente puede tener ya otro
                # worker con un token nuevo, y borrarlo lo dejaba fuera del
                # mapa de cancelación (detener() ya no podría pararlo).
                if self._tokens_activos.get(agente.id) is token:
                    self._tokens_activos.pop(agente.id, None)
            self._gestor_cancelacion.eliminar_token(token.id)

        # ── FASE 6: Emitir señales (fuera del lock) ──
        self.agente_actualizado.emit(agente.id)

        # ── FASE 7: Verificar si terminó todo y lanzar más ──
        with self._lock:
            self._verificar_terminado_internal()
            self._intentar_lanzar_internal()

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
            if (self.recovery is None
                    or self._plan_b_en_progreso
                    or self._plan_b_intentos >= self._max_intentos_plan_b):
                return False
            self._plan_b_en_progreso = True
            return True

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
        if agente_fallido_pre is not None and self._reclamar_plan_b():
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
        """
        try:
            logger.info(
                f"🔧 [Plan B #{self._plan_b_intentos + 1}] "
                f"'{agente_fallido.nombre}' falló: {razon}"
            )
            self.log_mensaje.emit(
                f"🔧 Plan B #{self._plan_b_intentos + 1}: "
                f"'{agente_fallido.nombre}' falló. "
                f"Consultando al LLM... (puede tardar ~20s)",
                "#ffc107"
            )

            # El turno ya está reclamado (``_plan_b_en_progreso=True``) por
            # ``_reclamar_plan_b`` antes de entrar aquí.
            self._plan_b_intentos += 1

            # 1. Pedir plan B al LLM (bloqueante ~5-15s, SIN el lock del
            #    scheduler: por eso se reclama el turno antes de entrar).
            plan_b = self.recovery.generar_plan_b(
                problema_original=self._problema_original,
                plan_fallido=self._plan_original,
                agente_fallido=agente_fallido,
                error=razon,
            )

            if plan_b is None or not getattr(plan_b, "agentes_generados", None):
                self.log_mensaje.emit(
                    "⚠️ Plan B descartado: el LLM no devolvió un plan válido. "
                    "Bloqueando dependientes.",
                    "#ffc107"
                )
                logger.warning("Plan B no disponible, bloqueando como antes")
                self._plan_b_en_progreso = False
                # ⬇️ NUEVO: marcar todos los agentes no-terminales como BLOQUEADOS
                # para que el recuento sea coherente y la ejecución termine.
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

            # ⬇️ NUEVO: mensaje de éxito tras generar plan_b
            self.log_mensaje.emit(
                f"✅ Plan B #{self._plan_b_intentos}: "
                f"{len(plan_b.agentes_generados)} agentes generados. "
                f"Cargando y reiniciando ejecución...",
                "#28a745"
            )

            # 2. Detener ejecución actual (cancela tokens, workers)
            self.detener()

            # 3. Limpiar TODO el estado
            with self._lock:
                self.agentes.clear()
                self.completed.clear()
                self.running.clear()
                self._cancelados.clear()
                self._loops_activos.clear()
                self._loop_items_procesados.clear()
                self._terminado_notificado = False
                self._tiempo_inicio_ejecucion = None
                self._invalidar_stats_cache()

            # 4. Cargar los nuevos agentes
            for agente in plan_b.agentes_generados:
                self.agregar_agente(agente)
            self.resolver_dependencias()

            # 5. Actualizar el plan original para futuros Plan B
            self._plan_original = plan_b

            # 6. Notificar a la UI
            self.log_mensaje.emit(
                f"✅ Plan B #{self._plan_b_intentos}: "
                f"{len(plan_b.agentes_generados)} agentes cargados. "
                f"Reiniciando ejecución...",
                "#28a745"
            )

            # 7. Arrancar el nuevo plan
            self._plan_b_en_progreso = False
            self.iniciar()

            #log temporal al final de _intentar_plan_b ---> logger.info(f"FIN _intentar_plan_b: intentos={self._plan_b_intentos}, m...")
            #logger.info(f"FIN _intentar_plan_b: intentos={self._plan_b_intentos}, max={self._max_intentos_plan_b}")
            return True

        except Exception as e:
            logger.exception(f"Error en Plan B: {e}")
            self._plan_b_en_progreso = False
            return False


    # ============================================================
    # HELPERS PARA LOG DE RESULTADOS
    # ============================================================

    # core/scheduler.py - ACTUALIZAR _resumir_resultado_log (sección HTTP)

    def _resumir_resultado_log(self, agente: Agente, resultado: Any) -> str:
        """
        Genera un resumen legible del resultado para el log.
        """
        max_len = self._MAX_RESULTADO_LOG
        tipo = agente.tipo

        if resultado is None:
            return "sin resultado"

        try:
            # ── HTTP (NUEVO FORMATO UNIFICADO) ──
            if tipo == TipoAgente.HTTP and isinstance(resultado, dict):
                status = resultado.get('status_code', '?')
                url = resultado.get('url', '')
                json_data = resultado.get('json')  # ← SIEMPRE presente, puede ser None
                body = resultado.get('body', '')

                # Si hay JSON, mostrar resumen del JSON
                if json_data is not None:
                    if isinstance(json_data, dict):
                        # Buscar claves de interés
                        claves_interes = [k for k in ('data', 'results', 'items', 'message', 'status') if k in json_data]
                        if claves_interes:
                            preview = {k: json_data.get(k) for k in claves_interes[:3]}
                            return f"HTTP {status} | {url[:40]} | JSON: {str(preview)[:max_len]}"
                        claves = list(json_data.keys())[:3]
                        return f"HTTP {status} | {url[:40]} | JSON claves: {claves}"
                    elif isinstance(json_data, list):
                        return f"HTTP {status} | {url[:40]} | JSON array: {len(json_data)} items"
                    else:
                        return f"HTTP {status} | {url[:40]} | JSON: {str(json_data)[:max_len]}"

                # Si no hay JSON pero hay body
                if body:
                    body_preview = body[:max_len].replace('\n', ' ').strip()
                    if body_preview:
                        return f"HTTP {status} | {url[:40]} | body: {body_preview}"

                # Si hay error
                if resultado.get('error'):
                    return f"HTTP {status} | {url[:40]} | error: {resultado['error'][:max_len]}"

                return f"HTTP {status} | {url[:50]}"

            # ── LLM ──
            elif tipo == TipoAgente.LLM and isinstance(resultado, dict):
                respuesta = resultado.get('respuesta', '')
                tokens = resultado.get('tokens_uso', {})
                token_info = f" ({tokens.get('total', '?')} tokens)" if tokens else ""

                if len(respuesta) > max_len:
                    return f"{respuesta[:max_len]}...{token_info}"
                return f"{respuesta}{token_info}" if respuesta else "sin respuesta"

            # ── Shell ──
            elif tipo == TipoAgente.SHELL and isinstance(resultado, dict):
                stdout = resultado.get('stdout', '')
                stderr = resultado.get('stderr', '')
                codigo = resultado.get('codigo', '?')

                if stdout:
                    preview = stdout[:max_len] + "..." if len(stdout) > max_len else stdout
                    preview = preview.replace('\n', ' ').strip()
                    return f"exit={codigo} | {preview}"
                elif stderr:
                    preview = stderr[:max_len] + "..." if len(stderr) > max_len else stderr
                    preview = preview.replace('\n', ' ').strip()
                    return f"exit={codigo} | stderr: {preview}"
                else:
                    return f"exit={codigo} | sin salida"

            # ── File ──
            elif tipo == TipoAgente.FILE and isinstance(resultado, dict):
                archivo = resultado.get('archivo', '')
                tamaño = resultado.get('tamaño', resultado.get('caracteres_escritos', 0))
                operacion = getattr(agente, 'operacion_file', '')
                if operacion:
                    return f"{operacion} | {archivo} | {tamaño} bytes"
                return f"{archivo} | {tamaño} bytes"

            # ── Loop ──
            elif tipo == TipoAgente.LOOP and isinstance(resultado, dict):
                total = resultado.get('total_items', 0)
                exitos = resultado.get('exitos', 0)
                errores = resultado.get('errores', 0)
                duracion = resultado.get('duracion_total', 0)
                return f"{total} items | ✅{exitos} ❌{errores} | {duracion:.1f}s"

            # ── Browser ──
            elif tipo == TipoAgente.BROWSER and isinstance(resultado, dict):
                # Modo multi-URL ('urls_desde')
                if 'urls_navegadas' in resultado:
                    navegadas = resultado.get('urls_navegadas', 0)
                    errores = resultado.get('errores') or []
                    urls = [
                        (r.get('url') or '')[:40]
                        for r in (resultado.get('resultados_por_url') or [])[:2]
                        if isinstance(r, dict)
                    ]
                    preview = f"{navegadas} URLs | {urls} | errores: {len(errores)}"
                    if resultado.get('error'):
                        preview += f" | error: {str(resultado['error'])[:max_len]}"
                    return preview

                titulo = (resultado.get('titulo') or '')[:40]
                url = (resultado.get('url_final') or '')[:40]
                datos = resultado.get('datos_extraidos') or {}
                acciones = resultado.get('acciones_ejecutadas') or []
                ok_acciones = sum(1 for a in acciones if isinstance(a, dict) and a.get('ok'))
                preview = f"{titulo} | {url}"
                if datos:
                    preview += f" | extraído: {list(datos)[:3]}"
                if acciones:
                    preview += f" | acciones {ok_acciones}/{len(acciones)}"
                if resultado.get('error'):
                    preview += f" | error: {str(resultado['error'])[:max_len]}"
                return preview

            # ── Search ──
            elif tipo == TipoAgente.SEARCH and isinstance(resultado, dict):
                query = (resultado.get('query') or '')[:40]
                if resultado.get('error'):
                    return f"'{query}' | error: {str(resultado['error'])[:max_len]}"
                total = resultado.get('total', 0)
                urls = [
                    (r.get('href') or '')[:40]
                    for r in (resultado.get('resultados') or [])[:2]
                    if isinstance(r, dict)
                ]
                return f"'{query}' | {total} resultados | {urls}"

            # ── Python ──
            elif tipo == TipoAgente.PYTHON:
                if isinstance(resultado, dict):
                    claves_interes = [k for k in ('status', 'mensaje', 'data', 'resultado', 'output', 'result')
                                    if k in resultado]
                    if claves_interes:
                        partes = []
                        for k in claves_interes[:3]:
                            v = resultado[k]
                            if isinstance(v, (dict, list)):
                                v_str = f"<{type(v).__name__} len={len(v)}>"
                            else:
                                v_str = str(v)
                                if len(v_str) > 60:
                                    v_str = v_str[:57] + "..."
                            partes.append(f"{k}={v_str}")
                        return " | ".join(partes)

                    claves = list(resultado.keys())[:4]
                    preview = {k: resultado[k] for k in claves}
                    texto = str(preview)
                    return texto[:max_len] + "..." if len(texto) > max_len else texto

                texto = str(resultado)
                return texto[:max_len] + "..." if len(texto) > max_len else texto

            # ── Fallback ──
            if isinstance(resultado, dict):
                claves = list(resultado.keys())[:4]
                preview = {k: resultado[k] for k in claves}
                texto = str(preview)
                return texto[:max_len] + "..." if len(texto) > max_len else texto

            texto = str(resultado)
            return texto[:max_len] + "..." if len(texto) > max_len else texto

        except Exception as e:
            logger.debug(f"Error formateando resultado: {e}")
            return f"(error al formatear: {str(e)[:50]})"

    def _resumir_error_log(self, agente: Agente, mensaje: str, resultado: Any) -> str:
        """
        Genera un resumen del error para el log.

        Args:
            agente: El agente que produjo el error
            mensaje: Mensaje de error
            resultado: Resultado parcial (si existe)

        Returns:
            str: Resumen del error
        """
        max_len = self._MAX_RESULTADO_LOG

        # Usar el mensaje de error
        resumen = str(mensaje) if mensaje else "Error desconocido"
        resumen = resumen.replace('\n', ' ').strip()

        # Extraer información adicional del resultado
        if resultado and isinstance(resultado, dict):
            extras = []

            # Buscar información de error en el resultado
            if 'stderr' in resultado and resultado['stderr']:
                stderr = str(resultado['stderr'])[:100].replace('\n', ' ').strip()
                extras.append(f"stderr: {stderr}")

            if 'error' in resultado and resultado['error']:
                error_detail = str(resultado['error'])[:100].replace('\n', ' ').strip()
                extras.append(f"error: {error_detail}")

            if 'stdout' in resultado and resultado['stdout']:
                stdout = str(resultado['stdout'])[:80].replace('\n', ' ').strip()
                extras.append(f"stdout: {stdout}")

            if 'status_code' in resultado:
                extras.append(f"HTTP {resultado['status_code']}")

            if extras:
                resumen = f"{resumen[:100]} | " + " | ".join(extras)

        # Truncar si es necesario
        return resumen[:max_len] + "..." if len(resumen) > max_len else resumen

    def _manejar_error_importacion(self, agente: Agente, error: Exception):
        """Maneja errores de importación del executor."""
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
            self.ejecutando = True
            self.pausado = False
            self._terminado_notificado = False
            self._tiempo_inicio_ejecucion = time.time()

            # Resetear agentes en cualquier estado terminal (incluye TIMEOUT,
            # SALTADO y BLOQUEADO, no solo COMPLETADO/ERROR/CANCELADO).
            for agente in self.agentes.values():
                if EstadoAgente.es_terminal(agente.estado):
                    agente.resetear_estado()
                    agente.mensaje = "Reiniciado"

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
        """
        # ── Fase 1: Cancelar tokens activos ──
        with self._lock:
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
        old_executor.shutdown(wait=False, cancel_futures=True)

        # ── Fase 4: Verificar terminado ──
        with self._lock:
            self._verificar_terminado_internal()

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
