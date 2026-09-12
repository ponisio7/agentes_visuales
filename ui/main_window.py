# ui/main_window.py - VERSIÓN REFACTORIZADA Y COMPLETA
"""
Ventana principal de la aplicación con todas las funcionalidades.

MEJORAS IMPLEMENTADAS:
- Throttling de actualizaciones de UI (reduce carga CPU)
- ✅ Stats con throttle propio (evita saturación)
- ✅ Progreso real de loops (no falso)
- ✅ Eliminado processEvents() peligroso en _limpiar()
- ✅ Manejo de memoria con deleteLater() seguro
- Cache de estadísticas para reducir cálculos
- Manejo de errores granular
- Desconexión de señales para prevenir memory leaks
- Validación de nombres con feedback visual
- Mejora del sistema de temas con persistencia
- Progress bar con estado real de loops
- Diálogos modales con mejor UX
- Logging estructurado con resaltado visual
- Atajos de teclado (Ctrl+N, Ctrl+S, Ctrl+O, etc.)
- Drag & drop para archivos JSON
- Estado de ejecución con colores consistentes
- ✅ CORREGIDO: Conexión directa a señales del scheduler (no al bridge)
- ✅ CORREGIDO: Logs de inicio/fin de agentes visibles
- ✅ CORREGIDO: Resumen detallado al finalizar ejecución
- ✅ NUEVO: Log de resultados de agentes al finalizar
"""

import time
import dataclasses
import math
import logging
import os
import json
from typing import Dict, List, Optional, Set, Any
from pathlib import Path
from datetime import datetime

from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QLineEdit, QComboBox, QScrollArea, QTextEdit,
    QGroupBox, QMessageBox, QSplitter, QTabWidget,
    QInputDialog, QFileDialog, QDialog, QCheckBox,
    QMenu, QSystemTrayIcon, QProgressBar, 
    QFrame, QGridLayout, QListWidget, QListWidgetItem,
    QApplication, QStyle
)
from PyQt6.QtCore import Qt, QTimer, QSettings, pyqtSignal, QEvent
from PyQt6.QtGui import (
    QFont, QIcon, QAction, QKeySequence, QDragEnterEvent,
    QDropEvent, QColor, QShortcut, QPalette
)

from core.agent import Agente, EstadoAgente, TipoAgente
from core.scheduler import Scheduler
from storage.database import Database
from storage.config_manager import ConfigManager, ConfigError, ConfigSecurityError

from ui.agent_widget import AgentWidget
from ui.graph_view import GraphView
from ui.notification_manager import NotificationManager, PLYER_DISPONIBLE
from ui.metrics_dashboard import MetricsDashboard
from ui.agent_config_dialog import AgentConfigDialog
from ui.theme_manager import ThemeManager
from core.event_bus import obtener_bus, Event, EventType
from core.cancellation import obtener_gestor_cancelacion, CancellationToken


# Configurar logger
logger = logging.getLogger(__name__)

# Nombres de los campos válidos del dataclass Agente
_CAMPOS_AGENTE = {f.name for f in dataclasses.fields(Agente)}

# ============================================================
# CONSTANTES DE CONFIGURACIÓN
# ============================================================

UI_UPDATE_INTERVAL_MS = 500  # Actualización de UI en ms
GRAPH_THROTTLE_MS = 150      # Throttle para actualización del grafo
STATS_THROTTLE_MS = 300      # Throttle para actualización de estadísticas
STATS_CACHE_TTL = 0.5        # Segundos de cache para estadísticas
MAX_AGENTS_DISPLAY = 1000    # Máximo de agentes mostrados en UI
MAX_LOG_LINES = 1000         # Máximo de líneas en el log
MAX_RESULTADO_LOG = 150      # Máximo de caracteres para mostrar en log

# ✅ NUEVO: Marcadores para identificar eventos importantes en logs y resaltarlos
LOG_MARKERS_IMPORTANTES = frozenset(["▶️", "✅", "❌", "🏁", "⛔", "⏹", "━", "⏸"])


# ============================================================
# CLASE PRINCIPAL: MAIN WINDOW
# ============================================================

class MainWindow(QMainWindow):
    """Ventana principal de la aplicación con todas las funcionalidades."""

        # Señales para comunicación entre hilos
    signal_agent_updated = pyqtSignal(str)
    signal_log_message = pyqtSignal(str, str)
    signal_execution_finished = pyqtSignal()

    # ✅ NUEVAS: señales hilo-seguras para arrancar timers
    signal_start_stats_timer = pyqtSignal(int)
    signal_start_graph_timer = pyqtSignal(int)

    def __init__(self):
        super().__init__()

        logger.info("Inicializando MainWindow")

        # ── Preferencias persistentes ──
        self.settings = QSettings("AgentesVisuales", "AgentesVisuales")
        self.tema_actual = self.settings.value("tema", "light", type=str)

        # Aplicar tema
        ThemeManager.aplicar_tema(self.tema_actual)

        # ── Core components ──
        self.scheduler = Scheduler(max_concurrent=4)
        self.db = Database()
        self.config_manager = ConfigManager()

        # ── Grafo (creado aquí porque ya existe self.scheduler) ──
        self.graph_view = GraphView(scheduler=self.scheduler)
        self.graph_view.eliminar_agente_solicitado.connect(self._eliminar_agente)

        # ── UI components ──
        self.widgets: Dict[str, AgentWidget] = {}
        self.tiempo_inicio: Optional[float] = None
        self._agent_names: Set[str] = set()  # Cache de nombres para validación

        # ── Throttling ──
        self._graph_update_timer = QTimer()
        self._graph_update_timer.setSingleShot(True)
        self._graph_update_timer.timeout.connect(self._actualizar_grafo_throttled)
        self._graph_update_pending = False

        # ✅ NUEVO: Timer para throttling de estadísticas
        self._stats_update_timer = QTimer()
        self._stats_update_timer.setSingleShot(True)
        self._stats_update_timer.timeout.connect(self._actualizar_stats_throttled)
        self._stats_pending = False

        # ✅ NUEVO: Conectar señales de arranque de timers.
        # Con QueuedConnection, el slot `QTimer.start` se ejecuta SIEMPRE
        # en el hilo donde vive MainWindow (el principal), aunque la señal
        # se emita desde un hilo worker.
        self.signal_start_stats_timer.connect(
            self._stats_update_timer.start,
            Qt.ConnectionType.QueuedConnection
        )
        self.signal_start_graph_timer.connect(
            self._graph_update_timer.start,
            Qt.ConnectionType.QueuedConnection
        )

        self._stats_cache: Optional[Dict] = None
        self._stats_cache_time: float = 0.0

        # ── Estado de ejecución ──
        self._ejecucion_activa = False
        self._limpiando = False  # ✅ Previene re-entrada en _limpiar()

        # ── Notificaciones ──
        self.notificador = NotificationManager(self)
        self.notificador.notificacion_recibida.connect(self._on_notificacion_recibida)

        # ── Conectar señales del scheduler (CORREGIDO: escuchar directamente al scheduler) ──
        self.scheduler.agente_actualizado.connect(self._on_agent_updated)
        self.scheduler.log_mensaje.connect(self._log)
        self.scheduler.ejecucion_terminada.connect(self._on_ejecucion_terminada)

        # ── Inicializar UI ──
        self._init_ui()
        self._setup_shortcuts()
        self._setup_drag_drop()

        # ── Cargar agentes de ejemplo ──
        # ── Cargar agentes de ejemplo (solo si está habilitado) ──
        cargar_ejemplos = self.settings.value("cargar_ejemplos_al_iniciar", False, type=bool)
        if cargar_ejemplos:
            self._crear_agentes_ejemplo()

        # ── Timer para actualización de UI ──
        self.timer = QTimer()
        self.timer.timeout.connect(self._actualizar_tiempos)
        self.timer.start(UI_UPDATE_INTERVAL_MS)

        # ── Restaurar geometría de ventana ──
        self._restore_window_geometry()

        # ── Registro de agentes cuyo resultado ya fue logueado ──
        self._agentes_resultado_logueados: Set[str] = set()

        # ── Event Bus ──
        self._bus = obtener_bus()
        self._suscribir_eventos()

        logger.info("MainWindow inicializada correctamente")

    # ============================================================
    # CONSTRUCCIÓN DE LA UI
    # ============================================================

    def _init_ui(self):
        """Inicializa la interfaz de usuario."""
        self.setWindowTitle("🚀 Visualizador de Agentes con Grafo")
        self.setMinimumSize(1100, 750)
        self.setAcceptDrops(True)

        # Crear widget central
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(8, 8, 8, 8)

        # Agregar componentes
        main_layout.addWidget(self._crear_panel_control())
        main_layout.addWidget(self._crear_panel_principal(), stretch=1)
        main_layout.addWidget(self._crear_panel_logs())

        # Crear barra de menú
        self._create_menu_bar()

        self._log("📌 Aplicación iniciada", "#28a745")
        self._cargar_historial()

    def _create_menu_bar(self):
        """Crea la barra de menú."""
        menubar = self.menuBar()

        # ── Menú Archivo ──
        file_menu = menubar.addMenu("📁 Archivo")

        new_action = QAction("➕ Nuevo Agente", self)
        new_action.setShortcut(QKeySequence("Ctrl+N"))
        new_action.triggered.connect(self._agregar_agente_avanzado)
        file_menu.addAction(new_action)

        file_menu.addSeparator()

        save_action = QAction("💾 Guardar Configuración", self)
        save_action.setShortcut(QKeySequence("Ctrl+S"))
        save_action.triggered.connect(self._guardar_configuracion)
        file_menu.addAction(save_action)

        load_action = QAction("📂 Cargar Configuración", self)
        load_action.setShortcut(QKeySequence("Ctrl+O"))
        load_action.triggered.connect(self._cargar_configuracion)
        file_menu.addAction(load_action)

        file_menu.addSeparator()

        export_action = QAction("📤 Exportar JSON", self)
        export_action.setShortcut(QKeySequence("Ctrl+E"))
        export_action.triggered.connect(self._exportar_json)
        file_menu.addAction(export_action)

        import_action = QAction("📥 Importar JSON", self)
        import_action.setShortcut(QKeySequence("Ctrl+I"))
        import_action.triggered.connect(self._importar_json)
        file_menu.addAction(import_action)

        file_menu.addSeparator()

        exit_action = QAction("🚪 Salir", self)
        exit_action.setShortcut(QKeySequence("Ctrl+Q"))
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        # ── Menú Ejecución ──
        run_menu = menubar.addMenu("▶️ Ejecución")

        start_action = QAction("Iniciar Todos", self)
        start_action.setShortcut(QKeySequence("Ctrl+R"))
        start_action.triggered.connect(self._iniciar)
        run_menu.addAction(start_action)

        pause_action = QAction("Pausar/Reanudar", self)
        pause_action.setShortcut(QKeySequence("Ctrl+P"))
        pause_action.triggered.connect(self._pausar)
        run_menu.addAction(pause_action)

        stop_action = QAction("Detener", self)
        stop_action.setShortcut(QKeySequence("Ctrl+Shift+R"))
        stop_action.triggered.connect(self._detener)
        run_menu.addAction(stop_action)

        run_menu.addSeparator()

        clear_action = QAction("🗑 Limpiar Todo", self)
        clear_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        clear_action.triggered.connect(self._limpiar)
        run_menu.addAction(clear_action)

        # ── Menú Ver ──
        view_menu = menubar.addMenu("👁️ Ver")

        theme_action = QAction("🌓 Alternar Tema", self)
        theme_action.setShortcut(QKeySequence("Ctrl+T"))
        theme_action.triggered.connect(self._alternar_tema)
        view_menu.addAction(theme_action)

        view_menu.addSeparator()

        refresh_action = QAction("🔄 Refrescar", self)
        refresh_action.setShortcut(QKeySequence("F5"))
        refresh_action.triggered.connect(self._refresh_ui)
        view_menu.addAction(refresh_action)

    def _setup_shortcuts(self):
        """Configura atajos de teclado adicionales."""
        # Atajo para abrir notificaciones
        shortcut_notify = QShortcut(QKeySequence("Ctrl+Shift+N"), self)
        shortcut_notify.activated.connect(self._configurar_notificaciones_dialog)

        # Atajo para ayuda
        shortcut_help = QShortcut(QKeySequence("F1"), self)
        shortcut_help.activated.connect(self._show_help)

    def _setup_drag_drop(self):
        """Configura drag & drop para archivos JSON."""
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        """Maneja el evento de drag enter."""
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().endswith('.json'):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        """Maneja el evento de drop."""
        for url in event.mimeData().urls():
            file_path = url.toLocalFile()
            if file_path.endswith('.json'):
                self._importar_desde_drag(file_path)
                event.acceptProposedAction()
                return
        event.ignore()

    def _restore_window_geometry(self):
        """Restaura la geometría de la ventana desde settings."""
        geometry = self.settings.value("window_geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def closeEvent(self, event):
        """Maneja el evento de cierre de la ventana."""
        # Guardar geometría
        self.settings.setValue("window_geometry", self.saveGeometry())

        # Detener ejecución si está activa
        if self.scheduler.ejecutando:
            reply = QMessageBox.question(
                self, "⚠️ Confirmar",
                "Hay una ejecución en curso. ¿Deseas detenerla y salir?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.scheduler.detener()
                # Esperar brevemente a que termine
                QApplication.processEvents()
                time.sleep(0.1)
            else:
                event.ignore()
                return

        # Limpiar recursos
        self.timer.stop()
        self._graph_update_timer.stop()
        self._stats_update_timer.stop()

        # Limpiar widgets
        for widget in list(self.widgets.values()):
            widget.deleteLater()
        self.widgets.clear()

        # Notificar al sistema
        self.notificador.limpiar_notificaciones()

        # Aceptar evento
        event.accept()
        logger.info("Aplicación cerrada correctamente")

    # ============================================================
    # PANEL DE CONTROL
    # ============================================================

    def _crear_panel_control(self) -> QGroupBox:
        """Crea el panel de control completo."""
        control_group = QGroupBox("🎮 Control")
        control_layout = QVBoxLayout(control_group)
        control_layout.setSpacing(6)
        control_layout.setContentsMargins(10, 12, 10, 10)

        control_layout.addLayout(self._crear_fila_creacion())
        control_layout.addLayout(self._crear_fila_botones())
        control_layout.addLayout(self._crear_fila_estadisticas())
        control_layout.addLayout(self._crear_fila_loops())

        return control_group

    def _crear_fila_creacion(self) -> QHBoxLayout:
        """Fila de creación de agentes."""
        layout = QHBoxLayout()
        layout.setSpacing(8)

        # Botón agregar avanzado
        self.btn_agregar_avanzado = QPushButton("➕ Nuevo Agente con Detalle…")
        self.btn_agregar_avanzado.clicked.connect(self._agregar_agente_avanzado)
        self.btn_agregar_avanzado.setStyleSheet("""
            QPushButton {
                background-color: #6f42c1;
                color: white;
                font-weight: bold;
                padding: 8px 20px;
                border-radius: 4px;
                font-size: 13px;
            }
            QPushButton:hover {
                background-color: #5a32a3;
            }
        """)
        layout.addWidget(self.btn_agregar_avanzado)

        self.btn_crear_desde_texto = QPushButton("📝 Agregar Agente Desde Texto")
        self.btn_crear_desde_texto.setToolTip("Crear agentes desde texto estructurado")
        self.btn_crear_desde_texto.clicked.connect(self._crear_desde_texto)
        self.btn_crear_desde_texto.setStyleSheet("""
            QPushButton {
                background-color: #17a2b8;
                color: white;
                font-weight: bold;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #138496; }
        """)
        layout.addWidget(self.btn_crear_desde_texto)

        self.btn_resolver_problema = QPushButton("🧠 Resolver Problema")
        self.btn_resolver_problema.setToolTip(
            "Describe un problema y la IA creará automáticamente "
            "la orquestación de agentes para resolverlo"
        )
        self.btn_resolver_problema.clicked.connect(self._resolver_problema)
        self.btn_resolver_problema.setStyleSheet("""
            QPushButton {
                background-color: #6f42c1;
                color: white;
                font-weight: bold;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #5a32a3; }
        """)
        layout.addWidget(self.btn_resolver_problema)

        # Botón para crear agente Loop rápido
        self.btn_crear_loop = QPushButton("🔄 Nuevo Loop")
        self.btn_crear_loop.setToolTip("Crear un agente Loop rápidamente")
        self.btn_crear_loop.clicked.connect(self._crear_loop_rapido)
        self.btn_crear_loop.setStyleSheet("""
            QPushButton {
                background-color: #fd7e14;
                color: white;
                font-weight: bold;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #e06b0a;
            }
        """)
        layout.addWidget(self.btn_crear_loop)

        # Botón Limpiar Todo (rápido)
        self.btn_limpiar_rapido = QPushButton("🗑 Limpiar")
        self.btn_limpiar_rapido.setToolTip("Limpiar todos los agentes")
        self.btn_limpiar_rapido.clicked.connect(self._limpiar)
        self.btn_limpiar_rapido.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                font-weight: bold;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #5a6268;
            }
        """)
        layout.addWidget(self.btn_limpiar_rapido)

        layout.addStretch()

        # Contador de agentes
        self.lbl_contador = QLabel("👥 0 agentes")
        self.lbl_contador.setStyleSheet("color: #6c757d; font-weight: bold;")
        layout.addWidget(self.lbl_contador)

        return layout

    def _crear_fila_botones(self) -> QHBoxLayout:
        """Fila de botones de control."""
        layout = QHBoxLayout()
        layout.setSpacing(8)

        # Botones de ejecución
        self.btn_iniciar = self._crear_boton("▶ Iniciar", "#28a745", self._iniciar)
        layout.addWidget(self.btn_iniciar)

        self.btn_pausar = self._crear_boton("⏸ Pausar", "#ffc107", self._pausar)
        self.btn_pausar.setEnabled(False)
        layout.addWidget(self.btn_pausar)

        self.btn_detener = self._crear_boton("⏹ Detener", "#dc3545", self._detener)
        self.btn_detener.setEnabled(False)
        layout.addWidget(self.btn_detener)

        layout.addStretch()

        # Botones de configuración
        self.btn_guardar = self._crear_boton("💾 Guardar", "#17a2b8", self._guardar_configuracion)
        layout.addWidget(self.btn_guardar)

        self.btn_cargar = self._crear_boton("📂 Cargar", "#6f42c1", self._cargar_configuracion)
        layout.addWidget(self.btn_cargar)

        self.btn_exportar = self._crear_boton("📤 Exportar", "#28a745", self._exportar_json)
        layout.addWidget(self.btn_exportar)

        self.btn_importar = self._crear_boton("📥 Importar", "#fd7e14", self._importar_json)
        layout.addWidget(self.btn_importar)

        layout.addStretch()

        # Estado
        self.lbl_info = QLabel("✅ Listo")
        self.lbl_info.setFont(QFont("", 10))
        self.lbl_info.setStyleSheet("font-weight: bold; color: #28a745;")
        layout.addWidget(self.lbl_info)

        # Notificaciones
        self.btn_notificaciones = QPushButton("🔔")
        self.btn_notificaciones.setToolTip("Configurar notificaciones")
        self.btn_notificaciones.clicked.connect(self._configurar_notificaciones_dialog)
        self.btn_notificaciones.setFixedWidth(35)
        layout.addWidget(self.btn_notificaciones)

        # Tema claro/oscuro
        self.btn_tema = QPushButton("🌓")
        self.btn_tema.setToolTip("Alternar tema claro/oscuro")
        self.btn_tema.clicked.connect(self._alternar_tema)
        self.btn_tema.setFixedWidth(35)
        layout.addWidget(self.btn_tema)

        # Ayuda
        self.btn_ayuda = QPushButton("❓")
        self.btn_ayuda.setToolTip("Ayuda")
        self.btn_ayuda.clicked.connect(self._show_help)
        self.btn_ayuda.setFixedWidth(35)
        layout.addWidget(self.btn_ayuda)

        return layout

    def _crear_boton(self, texto: str, color: str, callback) -> QPushButton:
        """Crea un botón con estilo consistente."""
        boton = QPushButton(texto)
        boton.clicked.connect(callback)
        boton.setStyleSheet(f"""
            QPushButton {{
                background-color: {color};
                color: white;
                font-weight: bold;
                padding: 8px 20px;
                border-radius: 4px;
                min-width: 80px;
            }}
            QPushButton:hover {{
                background-color: {color}dd;
            }}
            QPushButton:disabled {{
                background-color: #6c757d;
                color: #adb5bd;
            }}
        """)
        return boton

    def _crear_fila_estadisticas(self) -> QHBoxLayout:
        """Fila de estadísticas."""
        layout = QHBoxLayout()
        layout.setSpacing(15)

        self.lbl_stats = QLabel("📊 Total: 0 | ✅ 0 | ⚡ 0 | ⏳ 0 | ❌ 0 | ⛔ 0")
        self.lbl_stats.setFont(QFont("", 10))
        self.lbl_stats.setStyleSheet("color: #495057;")
        layout.addWidget(self.lbl_stats)

        layout.addStretch()

        # Tiempo de ejecución
        self.lbl_tiempo_ejecucion = QLabel("⏱ 0s")
        self.lbl_tiempo_ejecucion.setStyleSheet("color: #6c757d;")
        layout.addWidget(self.lbl_tiempo_ejecucion)

        return layout

    def _crear_fila_loops(self) -> QHBoxLayout:
        """Fila de estado de loops activos."""
        layout = QHBoxLayout()
        layout.setSpacing(8)

        # Etiqueta de estado de loops
        self.lbl_loop_status = QLabel("🔄 Loops: 0 activos")
        self.lbl_loop_status.setFont(QFont("", 10))
        self.lbl_loop_status.setStyleSheet("color: #6f42c1; font-weight: bold;")
        layout.addWidget(self.lbl_loop_status)

        # Barra de progreso de loops
        self.loop_progress_bar = QProgressBar()
        self.loop_progress_bar.setRange(0, 100)
        self.loop_progress_bar.setValue(0)
        self.loop_progress_bar.setVisible(False)
        self.loop_progress_bar.setMaximumWidth(200)
        self.loop_progress_bar.setFormat("Loop: %p%")
        self.loop_progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #d1d5db;
                border-radius: 3px;
                text-align: center;
                height: 20px;
                background-color: white;
            }
            QProgressBar::chunk {
                background-color: #6f42c1;
                border-radius: 3px;
            }
        """)
        layout.addWidget(self.loop_progress_bar)

        # Botón para detener loops
        self.btn_detener_loops = QPushButton("⏹ Detener Loops")
        self.btn_detener_loops.setToolTip("Detener todos los loops activos")
        self.btn_detener_loops.clicked.connect(self._detener_loops)
        self.btn_detener_loops.setEnabled(False)
        self.btn_detener_loops.setStyleSheet("""
            QPushButton {
                background-color: #dc3545;
                color: white;
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 10px;
            }
            QPushButton:hover {
                background-color: #c82333;
            }
            QPushButton:disabled {
                background-color: #6c757d;
            }
        """)
        layout.addWidget(self.btn_detener_loops)

        layout.addStretch()
        return layout

    # ============================================================
    # PANEL PRINCIPAL
    # ============================================================

    def _crear_panel_principal(self) -> QSplitter:
        """Panel principal con splitter."""
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._crear_panel_agentes())
        splitter.addWidget(self._crear_panel_pestanas())
        splitter.setSizes([450, 550])
        return splitter

    def _crear_panel_agentes(self) -> QWidget:
        """Panel con lista de agentes."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        # Barra de búsqueda
        search_layout = QHBoxLayout()
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("🔍 Buscar agente...")
        self.search_input.textChanged.connect(self._filtrar_agentes)
        search_layout.addWidget(self.search_input)

        # Botón de limpiar filtro
        self.btn_clear_filter = QPushButton("✕")
        self.btn_clear_filter.setFixedWidth(30)
        self.btn_clear_filter.setToolTip("Limpiar filtro")
        self.btn_clear_filter.clicked.connect(lambda: self.search_input.clear())
        self.btn_clear_filter.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                color: #6c757d;
            }
            QPushButton:hover {
                color: #dc3545;
            }
        """)
        search_layout.addWidget(self.btn_clear_filter)

        layout.addLayout(search_layout)

        # Scroll area con agentes
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QScrollArea.Shape.NoFrame)

        self.contenedor_agentes = QWidget()
        self.layout_agentes = QVBoxLayout(self.contenedor_agentes)
        self.layout_agentes.setSpacing(6)
        self.layout_agentes.setContentsMargins(4, 4, 4, 4)

        # Mensaje persistente
        self.lbl_sin_agentes = QLabel(
            "📭 No hay agentes creados\n\n"
            "Haz clic en 'Nuevo Agente con Detalle…' para comenzar"
        )
        self.lbl_sin_agentes.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_sin_agentes.setStyleSheet("""
            QLabel {
                color: #999;
                font-size: 14px;
                padding: 40px;
                background-color: #f8f9fa;
                border-radius: 8px;
                border: 2px dashed #dee2e6;
            }
        """)
        self.layout_agentes.addWidget(self.lbl_sin_agentes)
        self.layout_agentes.addStretch()

        self.scroll_area.setWidget(self.contenedor_agentes)
        layout.addWidget(self.scroll_area)

        return widget

    def _crear_panel_pestanas(self) -> QTabWidget:
        """Panel con pestañas."""
        tabs = QTabWidget()

        # Grafo (visualización de ejecución) — reutiliza el creado en __init__
        tabs.addTab(self.graph_view, "📊 Grafo")

        # Dashboard
        self.metrics_dashboard = MetricsDashboard(self.scheduler)
        tabs.addTab(self.metrics_dashboard, "📈 Dashboard")

        # Historial
        self.history_text = QTextEdit()
        self.history_text.setReadOnly(True)
        self.history_text.setFont(QFont("Monospace", 9))
        tabs.addTab(self.history_text, "📜 Historial")

        return tabs

    def _crear_panel_logs(self) -> QGroupBox:
        """Panel de logs."""
        group = QGroupBox("📋 Logs")
        layout = QVBoxLayout(group)
        layout.setContentsMargins(8, 8, 8, 8)

        # Barra de herramientas de logs
        log_toolbar = QHBoxLayout()
        self.btn_clear_logs = QPushButton("🗑 Limpiar Logs")
        self.btn_clear_logs.clicked.connect(self._limpiar_logs)
        self.btn_clear_logs.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 10px;
            }
            QPushButton:hover {
                background-color: #5a6268;
            }
        """)
        log_toolbar.addWidget(self.btn_clear_logs)

        self.btn_exportar_logs = QPushButton("📤 Exportar Logs")
        self.btn_exportar_logs.clicked.connect(self._exportar_logs)
        self.btn_exportar_logs.setStyleSheet("""
            QPushButton {
                background-color: #17a2b8;
                color: white;
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 10px;
            }
            QPushButton:hover {
                background-color: #138496;
            }
        """)
        log_toolbar.addWidget(self.btn_exportar_logs)

        log_toolbar.addStretch()

        self.lbl_log_count = QLabel("0 líneas")
        self.lbl_log_count.setStyleSheet("color: #6c757d; font-size: 10px;")
        log_toolbar.addWidget(self.lbl_log_count)

        layout.addLayout(log_toolbar)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMaximumHeight(150)
        self.log_text.setFont(QFont("Monospace", 9))
        self.log_text.setStyleSheet("background-color: #f8f9fa;")

        layout.addWidget(self.log_text)

        return group

    # ============================================================
    # LOGGING MEJORADO
    # ============================================================

    def _log(self, mensaje: str, color: str = "#333"):
        """Añade un mensaje al log con formato mejorado para eventos importantes."""
        timestamp = time.strftime("%H:%M:%S")
        
        # Detectar si es un evento importante para resaltarlo
        es_importante = any(marcador in mensaje for marcador in LOG_MARKERS_IMPORTANTES)
        
        if es_importante:
            formatted = (
                f'<div style="background-color: {color}15; '
                f'padding: 4px 8px; margin: 2px 0; '
                f'border-left: 3px solid {color}; border-radius: 2px;">'
                f'<span style="color: #888; font-size: 11px;">[{timestamp}]</span> '
                f'<span style="color: {color}; font-weight: bold;">{mensaje}</span>'
                f'</div>'
            )
        else:
            formatted = (
                f'<span style="color: #888;">[{timestamp}]</span> '
                f'<span style="color: {color};">{mensaje}</span>'
            )
        
        # Limitar líneas de log
        lines = self.log_text.toPlainText().split('\n')
        if len(lines) > MAX_LOG_LINES:
            keep_lines = lines[-(MAX_LOG_LINES - 50):]
            self.log_text.setPlainText('\n'.join(keep_lines))
        
        self.log_text.append(formatted)
        self.log_text.verticalScrollBar().setValue(
            self.log_text.verticalScrollBar().maximum()
        )
        
        # Actualizar contador
        self.lbl_log_count.setText(f"{len(lines)} líneas")

    def _log_separador(self, caracter: str = "━", color: str = "#6c757d"):
        """Añade un separador visual al log."""
        self._log(caracter * 60, color)

    def _limpiar_logs(self):
        """Limpia el panel de logs."""
        self.log_text.clear()
        self.lbl_log_count.setText("0 líneas")

    def _exportar_logs(self):
        """Exporta los logs a un archivo."""
        ruta, _ = QFileDialog.getSaveFileName(
            self, "Exportar Logs",
            f"logs_{datetime.now().strftime('%Y%m%d_%H%M')}.txt",
            "Text Files (*.txt)"
        )
        if ruta:
            try:
                with open(ruta, 'w', encoding='utf-8') as f:
                    f.write(self.log_text.toPlainText())
                self._log(f"📤 Logs exportados a: {ruta}", "#28a745")
            except Exception as e:
                QMessageBox.critical(self, "Error", f"❌ Error al exportar logs: {str(e)}")

    # ============================================================
    # FILTRO DE AGENTES
    # ============================================================

    def _filtrar_agentes(self, texto: str):
        """Filtra los agentes mostrados según el texto de búsqueda."""
        texto = texto.strip().lower()
        for agente_id, widget in self.widgets.items():
            agente = self.scheduler.obtener_agente(agente_id)
            if not agente:
                continue
            visible = not texto or texto in agente.nombre.lower()
            widget.setVisible(visible)

    # ============================================================
    # ALTA DE AGENTES
    # ============================================================

    def _resolver_problema(self):
        from ui.problem_solver_dialog import ProblemSolverDialog
        dialog = ProblemSolverDialog(
            self,
            nombres_existentes=[a.nombre for a in self.scheduler.agentes.values()]
        )
        dialog.plan_accepted.connect(self._agentes_desde_plan)
        dialog.exec()

    def _agentes_desde_plan(self, agentes: List[Agente]):
        for agente in agentes:
            self._registrar_y_mostrar(agente)
        
        self.scheduler.resolver_dependencias()
        self._actualizar_stats()
        self._actualizar_grafo(recalcular_layout=True)
        
        self._log(
            f"🧠 Plan ejecutado: {len(agentes)} agentes creados automáticamente",
            "#6f42c1"
        )

    def _nombre_en_uso(self, nombre: str, excluir_id: Optional[str] = None) -> bool:
        """Verifica si un nombre ya está en uso."""
        return any(
            a.nombre == nombre
            for a in self.scheduler.agentes.values()
            if a.id != excluir_id
        )

    def _eliminar_agente(self, agente_id: str):
        """
        Elimina un agente de forma segura con confirmación del usuario.
        AHORA: Muestra claramente qué dependientes serán bloqueados.
        """
        agente = self.scheduler.obtener_agente(agente_id)
        if not agente:
            return

        # No permitir eliminar durante ejecución
        if self.scheduler.ejecutando or agente.estado == EstadoAgente.EJECUTANDO:
            QMessageBox.warning(
                self,
                "No se puede eliminar",
                "No puedes eliminar agentes mientras hay una ejecución activa.\n\n"
                "Detén la ejecución primero."
            )
            return

        # ──────────────────────────────────────────────────────────
        # ✅ NUEVO: Identificar dependientes para mostrar en confirmación
        # ──────────────────────────────────────────────────────────
        dependientes = []
        for otro in self.scheduler.agentes.values():
            if agente_id in (otro.dependencias_ids or []):
                if not EstadoAgente.es_terminal(otro.estado):
                    dependientes.append(otro.nombre)

        # ── Construir mensaje de confirmación ──
        mensaje = f"¿Eliminar el agente **'{agente.nombre}'**?\n\nEsta acción no se puede deshacer."
        
        if dependientes:
            mensaje += (
                f"\n\n🚫 **{len(dependientes)} agente(s) dependiente(s) serán BLOQUEADOS** "
                f"(no se ejecutarán):\n• " + "\n• ".join(dependientes)
            )
            mensaje += (
                f"\n\n💡 Para reactivarlos, deberás crear un nuevo agente que "
                f"reemplace a '{agente.nombre}' y actualizar las dependencias."
            )
        else:
            mensaje += "\n\nℹ️ Este agente no tiene dependientes activos."

        reply = QMessageBox.question(
            self,
            "🗑️ Confirmar eliminación",
            mensaje,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        # ──────────────────────────────────────────────────────────
        # Eliminar del scheduler (ahora bloquea dependientes automáticamente)
        # ──────────────────────────────────────────────────────────
        if not self.scheduler.eliminar_agente(agente_id):
            QMessageBox.warning(self, "Error", f"No se pudo eliminar el agente '{agente.nombre}'.")
            return

        # ── Eliminar widget de la UI ──
        widget = self.widgets.pop(agente_id, None)
        if widget:
            self.layout_agentes.removeWidget(widget)
            widget.deleteLater()

        # ── Actualizar todo ──
        self.scheduler.resolver_dependencias()
        self._actualizar_contador()
        self._actualizar_stats()
        self._actualizar_grafo(recalcular_layout=True)

        # Mostrar mensaje si no quedan agentes
        self.lbl_sin_agentes.setVisible(len(self.scheduler.agentes) == 0)

        # Limpiar registros auxiliares
        self._agentes_resultado_logueados.discard(agente_id)

    def _registrar_y_mostrar(self, agente: Agente):
        """Registra un agente y crea su widget."""
        self.scheduler.agregar_agente(agente)

        widget = AgentWidget(agente)
        widget.editar_solicitado.connect(self._editar_agente)
        widget.ejecutar_solicitado.connect(self._ejecutar_agente_individual)
        widget.eliminar_solicitado.connect(self._eliminar_agente)   # <-- NUEVO

        # Agregar badge visual para loops
        if agente.tipo == TipoAgente.LOOP:
            widget.setStyleSheet(widget.styleSheet() + """
                AgentWidget {
                    border-left: 4px solid #6f42c1;
                }
            """)

        # Insertar antes del stretch
        self.layout_agentes.insertWidget(self.layout_agentes.count() - 1, widget)
        self.widgets[agente.id] = widget

        # Ocultar mensaje de "sin agentes"
        self.lbl_sin_agentes.setVisible(False)

        # Actualizar contador
        self._actualizar_contador()

    def _actualizar_contador(self):
        """Actualiza el contador de agentes."""
        total = len(self.widgets)
        self.lbl_contador.setText(f"👥 {total} agente{'s' if total != 1 else ''}")

    def _crear_agentes_ejemplo(self):
        """Crea agentes de ejemplo."""
        agentes_data = [
            ("Analizar", 4, [], "Analiza documentos"),
            ("Procesar", 3, [], "Procesa datos"),
            ("Generar", 6, ["Analizar"], "Genera contenido"),
            ("Validar", 4, ["Procesar"], "Valida resultados"),
            ("Combinar", 5, ["Generar", "Validar"], "Combina resultados"),
            ("Reportar", 3, ["Combinar"], "Genera reporte"),
        ]

        for nombre, duracion, deps, desc in agentes_data:
            agente = Agente(
                nombre=nombre,
                duracion=duracion,
                dependencias_nombres=deps,
                descripcion=desc
            )
            self._registrar_y_mostrar(agente)

        self.scheduler.resolver_dependencias()
        self._actualizar_stats()
        self._actualizar_grafo(recalcular_layout=True)
        

        self._log(f"📦 Creados {len(agentes_data)} agentes de ejemplo", "#28a745")
        self._log("📊 Dependencias resueltas automáticamente", "#6c757d")

    # ============================================================
    # CREACIÓN RÁPIDA DE LOOP
    # ============================================================

    def _crear_loop_rapido(self):
        """Crea un agente Loop rápidamente con valores predeterminados."""
        # Diálogo para configurar el Loop
        nombre, ok = QInputDialog.getText(
            self, "🔄 Nuevo Loop",
            "Nombre del agente Loop:",
            QLineEdit.EchoMode.Normal,
            f"Loop_{len(self.scheduler.agentes) + 1}"
        )

        if not ok or not nombre:
            return

        # Verificar que el nombre no existe
        if self._nombre_en_uso(nombre):
            QMessageBox.warning(self, "Error", f"Ya existe un agente con el nombre '{nombre}'")
            return

        # Diálogo para la fuente de items
        fuente, ok = QInputDialog.getText(
            self, "🔄 Fuente de Items",
            "Ruta de la lista (ej: ObtenerLista.items):",
            QLineEdit.EchoMode.Normal,
            "Dependencia.items"
        )

        if not ok or not fuente:
            return

        # Diálogo para el código del Loop
        codigo_base = """# Procesar cada item del loop
# Variables disponibles: item, indice, total, contexto

import time

resultado = {
    'indice': indice,
    'item': item,
    'procesado': True,
    'timestamp': time.time()
}
"""

        codigo, ok = QInputDialog.getMultiLineText(
            self, "🔄 Código del Loop",
            "Código a ejecutar por cada item:",
            codigo_base
        )

        if not ok:
            return

        # Crear el agente Loop
        agente = Agente(
            nombre=nombre,
            tipo=TipoAgente.LOOP,
            descripcion="Agente Loop creado rápidamente",
            fuente_items=fuente,
            codigo_por_item=codigo,
            max_iteraciones=100,
            timeout_loop=300,
            timeout_python=30,
            continuar_en_error=False
        )

        self._registrar_y_mostrar(agente)
        self.scheduler.resolver_dependencias()
        self._actualizar_stats()
        self._actualizar_grafo(recalcular_layout=True)
        

        self._log(f"🔄 Loop creado: {nombre} (fuente: {fuente})", "#6f42c1")

    # ============================================================
    # EDICIÓN DE AGENTES
    # ============================================================

    def _agregar_agente_avanzado(self):
        """Abre el diálogo avanzado para crear un agente."""
        dialog = AgentConfigDialog(
            self,
            nombres_existentes=[a.nombre for a in self.scheduler.agentes.values()]
        )
        dialog.agent_updated.connect(self._registrar_agente)
        dialog.exec()

    def _editar_agente(self, agente_id: str):
        """Abre el diálogo para editar un agente."""
        agente = self.scheduler.obtener_agente(agente_id)
        if not agente:
            return

        nombres_existentes = [
            a.nombre for a in self.scheduler.agentes.values()
            if a.id != agente_id
        ]

        dialog = AgentConfigDialog(
            self,
            agente=agente,
            nombres_existentes=nombres_existentes
        )
        dialog.agent_updated.connect(self._actualizar_agente)
        dialog.exec()

    def _registrar_agente(self, agente: Agente):
        """Registra un agente nuevo desde el diálogo avanzado."""
        self._registrar_y_mostrar(agente)
        self.scheduler.resolver_dependencias()
        self._actualizar_stats()
        self._actualizar_grafo(recalcular_layout=True)
        

        if agente.tipo == TipoAgente.LOOP:
            self._log(f"🔄 Loop creado: {agente.nombre} (fuente: {agente.fuente_items})", "#6f42c1")
        else:
            self._log(f"➕ Agente creado: {agente.nombre} ({agente.tipo.value})", "#28a745")

    def _actualizar_agente(self, agente: Agente):
        """Actualiza un agente existente desde el diálogo avanzado."""
        if agente.id in self.widgets:
            self.widgets[agente.id].actualizar()
        self.scheduler.resolver_dependencias()
        self._actualizar_stats()
        self._actualizar_grafo(recalcular_layout=True)
        
        self._log(f"✏️ Agente actualizado: {agente.nombre}", "#17a2b8")

    def _ejecutar_agente_individual(self, agente_id: str):
        """Ejecuta únicamente el agente seleccionado."""
        if not self.scheduler.ejecutar_agente_individual(agente_id):
            return

        agente = self.scheduler.obtener_agente(agente_id)
        if agente:
            tipo = "🔄 Loop" if agente.tipo == TipoAgente.LOOP else "🧪"
            self._log(f"{tipo} Prueba individual iniciada: {agente.nombre}", "#007bff")

    # ============================================================
    # CONTROL DE EJECUCIÓN MEJORADO
    # ============================================================

    def _iniciar(self):
        """Inicia la ejecución de todos los agentes."""
        if not self.scheduler.agentes:
            QMessageBox.warning(self, "Aviso", "No hay agentes para ejecutar")
            return

        self.tiempo_inicio = time.time()

        # ✅ Limpiar registro de agentes ya logueados para esta ejecución
        self._agentes_resultado_logueados.clear()
        
        # ✅ Log mejorado al iniciar con desglose
        stats = self.scheduler.obtener_estadisticas()
        self._log_separador()
        self._log(f"▶️ Iniciando ejecución de {stats['total']} agentes", "#28a745")
        
        tipos_count = {}
        for agente in self.scheduler.agentes.values():
            tipo = agente.tipo.value
            tipos_count[tipo] = tipos_count.get(tipo, 0) + 1
        
        tipos_str = ", ".join(f"{k}: {v}" for k, v in tipos_count.items())
        self._log(f"📊 Tipos: {tipos_str}", "#6c757d")
        self._log_separador()
        
        self.scheduler.iniciar()
        self._actualizar_controles()
        self.notificador.notificar_inicio(stats['total'])
        
        if stats.get('loops_total', 0) > 0:
            self._log(f"🔄 {stats['loops_total']} agente(s) Loop en la ejecución", "#6f42c1")

    def _pausar(self):
        """Pausa o reanuda la ejecución."""
        pausado = self.scheduler.pausar()
        self.btn_pausar.setText("▶ Reanudar" if pausado else "⏸ Pausar")
        self.btn_pausar.setStyleSheet(f"""
            QPushButton {{
                background-color: {"#ffc107" if not pausado else "#28a745"};
                color: white;
                font-weight: bold;
                padding: 8px 20px;
                border-radius: 4px;
                min-width: 80px;
            }}
            QPushButton:hover {{
                background-color: {"#e0a800" if not pausado else "#218838"};
            }}
        """)

    def _detener(self):
        """Detiene la ejecución."""
        self.scheduler.detener()
        self._actualizar_controles()

    def _limpiar(self):
        """
        Reinicia el estado de ejecución de todos los agentes sin eliminarlos.
        ✅ CORREGIDO: No elimina widgets, solo resetea estado.
        ✅ CORREGIDO: Prevención de re-entrada.
        ✅ CORREGIDO: Sin processEvents() peligroso.
        """
        # Prevenir re-entrada
        if self._limpiando:
            return

        # Confirmar si hay agentes
        if self.scheduler.agentes:
            reply = QMessageBox.question(
                self, "⚠️ Confirmar",
                f"¿Reiniciar el estado de los {len(self.scheduler.agentes)} agentes?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self._limpiando = True
        try:
            # Resetear estado en el scheduler (NO elimina agentes)
            self.scheduler.limpiar()

            # ✅ Resetear registro de agentes logueados
            self._agentes_resultado_logueados.clear()

            # ✅ Actualizar todos los widgets existentes (sin eliminarlos)
            for widget in self.widgets.values():
                widget.actualizar()
                widget.setVisible(True)

            # ✅ El mensaje "No hay agentes" solo se muestra si realmente no hay
            if self.scheduler.agentes:
                self.lbl_sin_agentes.setVisible(False)
            else:
                self.lbl_sin_agentes.setVisible(True)

            self._actualizar_controles()
            self._actualizar_stats()
            self._actualizar_grafo(recalcular_layout=True)
            self._actualizar_contador()
            self.tiempo_inicio = None

            # Resetear dashboard de métricas
            if hasattr(self, 'metrics_dashboard'):
                self.metrics_dashboard.reset()

            # Resetear estado de loops
            self.loop_progress_bar.setVisible(False)
            self.loop_progress_bar.setRange(0, 100)
            self.btn_detener_loops.setEnabled(False)
            self.lbl_loop_status.setText("🔄 Loops: 0 activos")
            self.lbl_tiempo_ejecucion.setText("⏱ 0s")

            self._log("🗑 Estado de agentes reiniciado", "#6c757d")

        finally:
            self._limpiando = False

    def _actualizar_controles(self):
        """Actualiza el estado de los botones de control."""
        ejecutando = self.scheduler.ejecutando
        self.btn_iniciar.setEnabled(not ejecutando)
        self.btn_pausar.setEnabled(ejecutando)
        self.btn_detener.setEnabled(ejecutando)

        if not ejecutando:
            self.btn_pausar.setText("⏸ Pausar")
            self.btn_pausar.setStyleSheet("""
                QPushButton {
                    background-color: #ffc107;
                    color: white;
                    font-weight: bold;
                    padding: 8px 20px;
                    border-radius: 4px;
                    min-width: 80px;
                }
                QPushButton:hover {
                    background-color: #e0a800;
                }
                QPushButton:disabled {
                    background-color: #6c757d;
                    color: #adb5bd;
                }
            """)
            self.lbl_info.setText("✅ Detenido" if not self.scheduler.agentes else "✅ Listo")
            self.lbl_info.setStyleSheet("font-weight: bold; color: #28a745;")
            self._ejecucion_activa = False

        # Actualizar estado de loops
        loops_activos = len(self.scheduler.obtener_loops_activos())
        self.btn_detener_loops.setEnabled(loops_activos > 0)
        self.lbl_loop_status.setText(f"🔄 Loops: {loops_activos} activos")

        if loops_activos > 0:
            self.loop_progress_bar.setVisible(True)
            # ✅ CORREGIDO: Progreso REAL del loop
            loop_id = self.scheduler.obtener_loops_activos()[0]
            progress = self.scheduler.obtener_progreso_loop(loop_id)
            if progress:
                items = progress.get('items_procesados', 0)
                # ✅ Calcular porcentaje real si sabemos el total
                if hasattr(self, '_loop_total_items'):
                    total = self._loop_total_items.get(loop_id, 100)
                    porcentaje = int((items / total) * 100) if total > 0 else 0
                    self.loop_progress_bar.setValue(min(100, porcentaje))
                    self.loop_progress_bar.setFormat(f"Loop: {porcentaje}% ({items}/{total})")
                else:
                    self.loop_progress_bar.setFormat(f"Loop: {items} items")
                    self.loop_progress_bar.setValue(min(100, items * 2))
            else:
                # ✅ Barra indeterminada mientras no hay progreso
                self.loop_progress_bar.setRange(0, 0)
                self.loop_progress_bar.setFormat("Loop: iniciando...")
        else:
            self.loop_progress_bar.setVisible(False)
            self.loop_progress_bar.setRange(0, 100)  # Restaurar rango

    def _actualizar_stats(self):
        """Actualiza las estadísticas en la UI."""
        # ✅ Ahora con throttling, no se llama directamente en _on_agent_updated
        self._actualizar_stats_internal()

    def _actualizar_stats_internal(self):
        """Implementación interna de actualización de stats."""
        now = time.time()
        if (self._stats_cache is None or
            now - self._stats_cache_time > STATS_CACHE_TTL):
            self._stats_cache = self.scheduler.obtener_estadisticas()
            self._stats_cache_time = now

        stats = self._stats_cache
        
        # Construir string de estadísticas con BLOQUEADOS
        stats_parts = [
            f"📊 Total: {stats['total']}",
            f"✅ Completados: {stats['completados']}",
            f"⚡ Ejecutando: {stats['ejecutando']}",
            f"▶️ Listos: {stats['listos']}",
            f"⏳ Esperando: {stats['esperando']}",
            f"❌ Errores: {stats['errores']}",
            f"⛔ Cancelados: {stats['cancelados']}",
        ]
        
        # Añadir bloqueados si hay alguno
        bloqueados = stats.get('bloqueados', 0)
        if bloqueados > 0:
            stats_parts.append(f"🚫 Bloqueados: {bloqueados}")
        
        self.lbl_stats.setText(" | ".join(stats_parts))

        # Actualizar info de loops
        loops_total = stats.get('loops_total', 0)
        loops_activos = stats.get('loops_activos', 0)
        if loops_total > 0:
            self.lbl_loop_status.setText(f"🔄 Loops: {loops_activos}/{loops_total} activos")
        else:
            self.lbl_loop_status.setText("🔄 Loops: 0 activos")

    def _actualizar_stats_throttled(self):
        """Actualiza estadísticas con throttling."""
        self._stats_pending = False
        self._actualizar_stats_internal()

    def _actualizar_controles_ejecucion(self):
        """Actualiza el estado de ejecución en la UI."""
        if self.scheduler.ejecutando:
            self._ejecucion_activa = True
            self.lbl_info.setText("▶ Ejecutando...")
            self.lbl_info.setStyleSheet("font-weight: bold; color: #007bff;")
        elif self.scheduler.pausado:
            self.lbl_info.setText("⏸ Pausado")
            self.lbl_info.setStyleSheet("font-weight: bold; color: #ffc107;")

    # ============================================================
    # DETENER LOOPS ESPECÍFICOS
    # ============================================================

    def _detener_loops(self):
        """Detiene todos los loops activos."""
        loops_activos = self.scheduler.obtener_loops_activos()
        if not loops_activos:
            return

        respuesta = QMessageBox.question(
            self, "Detener Loops",
            f"¿Detener {len(loops_activos)} loop(s) activo(s)?\n"
            "Esto cancelará su ejecución.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if respuesta != QMessageBox.StandardButton.Yes:
            return

        # Marcar los loops para cancelación
        for loop_id in loops_activos:
            agente = self.scheduler.obtener_agente(loop_id)
            if agente:
                agente.estado = EstadoAgente.CANCELADO
                agente.mensaje = "Loop detenido por usuario"
                self._log(f"⛔ Loop detenido: {agente.nombre}", "#dc3545")
                self.scheduler.bridge.agente_actualizado.emit(loop_id)

        self._actualizar_controles()
        self._actualizar_stats()

    # ============================================================
    # ACTUALIZACIÓN DE UI (THROTTLED)
    # ============================================================

    def _actualizar_tiempos(self):
        if not self.scheduler.ejecutando and not self._ejecucion_activa:
            return

        if self.tiempo_inicio:
            elapsed = time.time() - self.tiempo_inicio
            self.lbl_tiempo_ejecucion.setText(f"⏱ {elapsed:.1f}s")

        # ✅ Actualizar TODOS los agentes, no solo los que están ejecutando
        for agente in self.scheduler.agentes.values():
            if agente.id in self.widgets:
                self.widgets[agente.id].actualizar()

    # ============================================================
    # LOG DE RESULTADOS DE AGENTES
    # ============================================================

    def _formatear_resultado(self, resultado: Optional[Any], max_len: int = MAX_RESULTADO_LOG) -> str:
        """
        Devuelve un resumen corto y legible del resultado de un agente.
        
        Args:
            resultado: El resultado a formatear
            max_len: Longitud máxima del resumen
            
        Returns:
            str: Resumen formateado
        """
        if resultado is None:
            return "(sin resultado)"

        try:
            if isinstance(resultado, dict):
                # ── Resúmenes inteligentes por tipo de resultado ──
                
                # LLM
                if 'respuesta' in resultado:
                    respuesta = str(resultado['respuesta'])
                    if len(respuesta) > max_len:
                        return f"LLM: {respuesta[:max_len]}..."
                    return f"LLM: {respuesta}"
                
                # HTTP
                if 'status_code' in resultado:
                    status = resultado.get('status_code', '?')
                    body = resultado.get('body', '')
                    if body:
                        body_preview = body[:max_len] + "..." if len(body) > max_len else body
                        return f"HTTP {status} | {body_preview}"
                    return f"HTTP {status}"
                
                # JSON
                if 'json' in resultado and resultado['json']:
                    json_data = resultado['json']
                    if isinstance(json_data, dict):
                        claves = list(json_data.keys())[:3]
                        return f"JSON: claves {claves}"
                    return f"JSON: {str(json_data)[:max_len]}..."
                
                # Shell
                if 'stdout' in resultado:
                    stdout = str(resultado['stdout'])
                    if len(stdout) > max_len:
                        return f"Shell: {stdout[:max_len]}..."
                    return f"Shell: {stdout}"
                
                # File
                if 'contenido' in resultado:
                    contenido = resultado['contenido']
                    if isinstance(contenido, str):
                        return f"File: {len(contenido)} caracteres"
                    return f"File: {str(contenido)[:max_len]}..."
                
                if 'archivo' in resultado:
                    archivo = resultado.get('archivo', '')
                    tamaño = resultado.get('tamaño', resultado.get('caracteres_escritos', 0))
                    return f"File: {archivo} ({tamaño} bytes)"
                
                # Loop
                if 'items' in resultado or 'total_items' in resultado:
                    total = resultado.get('total_items', len(resultado.get('items', [])))
                    exitos = resultado.get('exitos', '?')
                    errores = resultado.get('errores', '?')
                    return f"Loop: {total} items (✅{exitos} ❌{errores})"
                
                # Fallback: primeras claves
                claves = list(resultado.keys())[:4]
                preview = {k: resultado[k] for k in claves}
                texto = str(preview)
                return texto[:max_len] + "..." if len(texto) > max_len else texto

            # Si es un valor simple
            texto = str(resultado)
            return texto[:max_len] + "..." if len(texto) > max_len else texto

        except Exception as e:
            return f"(error al formatear: {str(e)[:50]})"

    def _loguear_resultado_agente(self, agente: Agente):
        """
        Escribe en el log el resultado de un agente que acaba de terminar.
        Solo se loguea una vez por agente y por ejecución.
        
        Args:
            agente: El agente que ha terminado
        """
        # Verificar si ya fue logueado
        if agente.id in self._agentes_resultado_logueados:
            return

        # Marcar como logueado
        self._agentes_resultado_logueados.add(agente.id)

        # Log según estado terminal
        if agente.estado == EstadoAgente.COMPLETADO:
            resumen = self._formatear_resultado(agente.resultado)
            
            # Si el scheduler ya logueó el resultado, no duplicar
            # (pero añadimos un icono diferente para distinguir)
            if agente.tipo == TipoAgente.LOOP:
                # El loop ya tiene su propio log en scheduler
                self._log(f"   📊 [{agente.nombre}] Detalle: {resumen}", "#6c757d")
            else:
                self._log(f"📦 [{agente.nombre}] Resultado → {resumen}", "#17a2b8")
                
        elif agente.estado == EstadoAgente.ERROR:
            error_msg = agente.error[:200] if agente.error else "Error desconocido"
            self._log(f"🔍 [{agente.nombre}] Error detalle → {error_msg}", "#ffc107")
            
        elif agente.estado == EstadoAgente.CANCELADO:
            self._log(f"⛔ [{agente.nombre}] Cancelado por usuario", "#6c757d")

    def _on_agent_updated(self, agente_id: str):
        """
        Maneja la actualización de un agente.
        AHORA: Log específico para agentes bloqueados.
        """
        agente = self.scheduler.obtener_agente(agente_id)
        if not agente:
            return

        # ── 1. Actualizar widget visual del agente ──
        if agente_id in self.widgets:
            self.widgets[agente_id].actualizar()

        # ── 2. Log específico para BLOQUEADO ──
        if agente.estado == EstadoAgente.BLOQUEADO:
            self._log(f"🚫 {agente.nombre}: {agente.mensaje}", "#8b0000")
            # Notificar al usuario si el notificador está disponible
            if hasattr(self, 'notificador'):
                self.notificador.mostrar_advertencia(
                    f"Agente Bloqueado: {agente.nombre}",
                    agente.mensaje
                )
            return  # ← No continuar con el resto para bloqueados

        # ── 3. Log de resultado cuando el agente termina ──
        if EstadoAgente.es_terminal(agente.estado):
            self._loguear_resultado_agente(agente)

        # ── 4. Throttle para estadísticas ──
        if not self._stats_pending:
            self._stats_pending = True
            self.signal_start_stats_timer.emit(STATS_THROTTLE_MS)

        # ── 5. Throttle para actualización del grafo DAG ──
        if not self._graph_update_pending:
            self._graph_update_pending = True
            self.signal_start_graph_timer.emit(GRAPH_THROTTLE_MS)
        # ── 6. Actualizar estado de botones de control ──
        self._actualizar_controles_ejecucion()

    def _actualizar_progreso_loop(self, agente_id: str):
        """Actualiza el progreso real de un loop."""
        progress = self.scheduler.obtener_progreso_loop(agente_id)
        if progress:
            items = progress.get('items_procesados', 0)
            # Intentar obtener el total desde el resultado del agente
            agente = self.scheduler.obtener_agente(agente_id)
            if agente and agente.resultado:
                total = agente.resultado.get('total_items', 0)
                if total > 0:
                    porcentaje = int((items / total) * 100)
                    self.loop_progress_bar.setValue(min(100, porcentaje))
                    self.loop_progress_bar.setFormat(f"Loop: {porcentaje}% ({items}/{total})")
            else:
                self.loop_progress_bar.setFormat(f"Loop: {items} items")
                self.loop_progress_bar.setValue(min(100, items * 2))

    def _actualizar_grafo_throttled(self):
        """Actualiza el grafo con throttling."""
        self._graph_update_pending = False
        self._actualizar_grafo(recalcular_layout=False)

    def _actualizar_grafo(self, recalcular_layout: bool = False):
        """Actualiza el grafo."""
        self.graph_view.actualizar_grafo(
            self.scheduler.agentes,
            recalcular_layout=recalcular_layout
        )

    def _refresh_ui(self):
        """Refresca toda la UI."""
        self._actualizar_stats_internal()
        self._actualizar_controles()
        self._actualizar_grafo(recalcular_layout=True)
        
        self._log("🔄 UI refrescada", "#6c757d")

    # ============================================================
    # EJECUCIÓN TERMINADA - RESUMEN DETALLADO
    # ============================================================

    def _on_ejecucion_terminada(self):
        """Maneja el fin de la ejecución con resumen detallado."""
        self._actualizar_controles()
        self._ejecucion_activa = False
        
        # Calcular duración total
        duracion_total = 0.0
        if self.tiempo_inicio:
            duracion_total = time.time() - self.tiempo_inicio
        
        stats = self.scheduler.obtener_estadisticas()
        
        # ✅ Log de finalización con formato destacado y resumen
        self._log_separador()
        self._log("🏁 ═══ EJECUCIÓN COMPLETADA ═══", "#28a745")
        self._log_separador()
        
        self._log(f"📊 Total: {stats['total']} agentes", "#495057")
        self._log(f"   ✅ Completados: {stats['completados']}", "#28a745")
        self._log(f"   ❌ Errores: {stats['errores']}", "#dc3545")
        self._log(f"   ⛔ Cancelados: {stats['cancelados']}", "#6c757d")
        
        if duracion_total > 0:
            self._log(f"⏱ Duración total: {duracion_total:.2f}s", "#6c757d")
            if stats['total'] > 0:
                velocidad = stats['total'] / duracion_total
                self._log(f"⚡ Velocidad: {velocidad:.2f} agentes/s", "#6c757d")
        
        total_finalizados = stats['completados'] + stats['errores']
        if total_finalizados > 0:
            tasa_exito = (stats['completados'] / total_finalizados) * 100
            color_tasa = "#28a745" if tasa_exito >= 80 else "#ffc107" if tasa_exito >= 50 else "#dc3545"
            self._log(f"📈 Tasa de éxito: {tasa_exito:.1f}%", color_tasa)
        
        if stats.get('loops_total', 0) > 0:
            self._log(f"🔄 Loops: {stats.get('loops_total', 0)} totales, {stats.get('completados', 0)} completados", "#6f42c1")
        
        # Detalle por agente
        self._log_separador("─")
        self._log("📋 Detalle por agente:", "#495057")
        
        agentes_ordenados = sorted(
            self.scheduler.agentes.values(),
            key=lambda a: a.tiempo_inicio or float('inf')
        )
        
        for agente in agentes_ordenados:
            emoji_estado = agente.obtener_emoji_estado()
            color_estado = agente.obtener_color_estado()
            
            duracion_agente = ""
            if agente.tiempo_inicio and agente.tiempo_fin:
                duracion_seg = agente.tiempo_fin - agente.tiempo_inicio
                duracion_agente = f" ({duracion_seg:.2f}s)"
            
            self._log(f"   {emoji_estado} {agente.nombre}{duracion_agente}", color_estado)
            
            # ✅ NUEVO: Mostrar resumen del resultado si existe
            if agente.resultado and agente.estado == EstadoAgente.COMPLETADO:
                resumen = self._formatear_resultado(agente.resultado, max_len=100)
                self._log(f"      ↳ Resultado: {resumen}", "#6c757d")
            elif agente.error and agente.estado == EstadoAgente.ERROR:
                error_preview = agente.error[:100] + "..." if len(agente.error) > 100 else agente.error
                self._log(f"      ↳ Error: {error_preview}", "#ffc107")
        
        self._log_separador()
        
        # Notificación y guardado
        self.notificador.notificar_completado(stats)
        
        if self.tiempo_inicio:
            agentes_data = [a.to_dict() for a in self.scheduler.agentes.values()]
            try:
                ejecucion_id = self.db.guardar_ejecucion(agentes_data, duracion_total)
                self._log(f"💾 Ejecución guardada (ID: {ejecucion_id})", "#6c757d")
                self._cargar_historial()
            except Exception as e:
                self._log(f"⚠️ Error al guardar ejecución: {e}", "#dc3545")
            
            self.tiempo_inicio = None
            self.lbl_tiempo_ejecucion.setText("⏱ 0s")

    # Métodos:
    def _crear_desde_texto(self):
        from ui.text_import_dialog import TextImportDialog
        dialog = TextImportDialog(
            self,
            nombres_existentes=[a.nombre for a in self.scheduler.agentes.values()]
        )
        dialog.agentes_creados.connect(self._registrar_agentes_desde_texto)
        dialog.exec()

    def _registrar_agentes_desde_texto(self, agentes: List[Agente]):
        for agente in agentes:
            self._registrar_y_mostrar(agente)
        self.scheduler.resolver_dependencias()
        self._actualizar_stats()
        self._actualizar_grafo(recalcular_layout=True)
        self._log(f"📝 {len(agentes)} agente(s) creado(s) desde texto", "#17a2b8")

    # ============================================================
    # HISTORIAL
    # ============================================================

    def _cargar_historial(self):
        """Carga el historial de ejecuciones."""
        try:
            historial = self.db.obtener_historial(limit=10)

            if not historial:
                self.history_text.setText("📭 No hay ejecuciones guardadas aún")
                return

            texto = "📜 HISTORIAL DE EJECUCIONES\n" + "=" * 60 + "\n\n"
            for ejec in historial:
                texto += f"🔹 ID: {ejec['id']} | {ejec['fecha']}\n"
                texto += f"   Duración: {ejec['duracion_total']:.2f}s | "
                texto += f"Agentes: {ejec['agentes_total']} | "
                texto += f"✅ {ejec['completados']} | ❌ {ejec['errores']}\n\n"

            self.history_text.setText(texto)
        except Exception as e:
            self.history_text.setText(f"❌ Error al cargar historial: {e}")

    # ============================================================
    # NOTIFICACIONES
    # ============================================================

    def _configurar_notificaciones_dialog(self):
        """Diálogo para configurar notificaciones."""
        dialog = QDialog(self)
        dialog.setWindowTitle("🔔 Configuración de Notificaciones")
        dialog.setModal(True)
        dialog.setMinimumWidth(350)

        layout = QVBoxLayout()
        layout.setSpacing(10)

        # Estado de plyer
        estado_plyer = "✅ Disponible" if PLYER_DISPONIBLE else "❌ No disponible"
        layout.addWidget(QLabel(f"📦 Plyer: {estado_plyer}"))

        # Configuraciones
        self.chk_notificar_inicio = QCheckBox("Notificar inicio de ejecución")
        self.chk_notificar_inicio.setChecked(
            self.settings.value("notify_start", True, type=bool)
        )
        layout.addWidget(self.chk_notificar_inicio)

        self.chk_notificar_completado = QCheckBox("Notificar finalización")
        self.chk_notificar_completado.setChecked(
            self.settings.value("notify_complete", True, type=bool)
        )
        layout.addWidget(self.chk_notificar_completado)

        self.chk_notificar_errores = QCheckBox("Notificar errores")
        self.chk_notificar_errores.setChecked(
            self.settings.value("notify_errors", True, type=bool)
        )
        layout.addWidget(self.chk_notificar_errores)

        self.chk_notificar_agentes = QCheckBox("Notificar cada agente")
        self.chk_notificar_agentes.setChecked(
            self.settings.value("notify_agents", False, type=bool)
        )
        layout.addWidget(self.chk_notificar_agentes)

        layout.addSpacing(10)

        # Botones
        btn_test = QPushButton("🔔 Probar Notificación")
        btn_test.clicked.connect(
            lambda: self.notificador.notificar(
                "🔔 Prueba", "¡Las notificaciones funcionan correctamente!"
            )
        )
        layout.addWidget(btn_test)

        btn_guardar = QPushButton("💾 Guardar")
        btn_guardar.clicked.connect(self._guardar_config_notificaciones)
        layout.addWidget(btn_guardar)

        btn_cerrar = QPushButton("Cerrar")
        btn_cerrar.clicked.connect(dialog.accept)
        layout.addWidget(btn_cerrar)

        dialog.setLayout(layout)
        dialog.exec()

    def _guardar_config_notificaciones(self):
        """Guarda la configuración de notificaciones."""
        self.settings.setValue("notify_start", self.chk_notificar_inicio.isChecked())
        self.settings.setValue("notify_complete", self.chk_notificar_completado.isChecked())
        self.settings.setValue("notify_errors", self.chk_notificar_errores.isChecked())
        self.settings.setValue("notify_agents", self.chk_notificar_agentes.isChecked())

        self._log("🔔 Configuración de notificaciones guardada", "#28a745")
        QMessageBox.information(self, "Éxito", "✅ Configuración guardada correctamente")

    def _on_notificacion_recibida(self, titulo: str, mensaje: str):
        """Maneja notificaciones recibidas."""
        mensaje_limpio = ' '.join(mensaje.split())
        self._log(f"🔔 {titulo}: {mensaje_limpio}", "#6c757d")

    # ============================================================
    # TEMAS
    # ============================================================

    def _alternar_tema(self):
        """Alterna entre tema claro y oscuro."""
        self.tema_actual = "dark" if self.tema_actual == "light" else "light"
        ThemeManager.aplicar_tema(self.tema_actual)
        self.settings.setValue("tema", self.tema_actual)

        emoji = "🌙" if self.tema_actual == "dark" else "☀️"
        self._log(f"{emoji} Tema cambiado a: {self.tema_actual}", "#6c757d")

        # Actualizar colores de widgets
        self._refresh_ui()

    # ============================================================
    # GUARDAR / CARGAR / EXPORTAR / IMPORTAR
    # ============================================================

    def _reconstruir_agente_desde_dict(self, data: Dict) -> Agente:
        """Reconstruye un Agente desde un diccionario."""
        kwargs = {k: v for k, v in data.items() if k in _CAMPOS_AGENTE}

        if 'tipo' in kwargs:
            try:
                kwargs['tipo'] = TipoAgente(kwargs['tipo'])
            except ValueError:
                kwargs['tipo'] = TipoAgente.PYTHON

        return Agente(**kwargs)

    def _guardar_configuracion(self):
        """Guarda la configuración actual."""
        if not self.scheduler.agentes:
            QMessageBox.warning(self, "Aviso", "No hay agentes para guardar")
            return

        nombre, ok = QInputDialog.getText(
            self, "💾 Guardar Configuración",
            "Nombre de la configuración:",
            QLineEdit.EchoMode.Normal,
            f"config_{datetime.now().strftime('%Y%m%d_%H%M')}"
        )

        if not ok or not nombre:
            return

        descripcion, ok = QInputDialog.getText(
            self, "💾 Guardar Configuración",
            "Descripción (opcional):"
        )

        if not ok:
            descripcion = ""

        try:
            ruta = self.config_manager.guardar(
                self.scheduler.agentes,
                nombre,
                descripcion
            )
            self._log(f"💾 Configuración guardada: {nombre} ({ruta})", "#28a745")

            loops = sum(1 for a in self.scheduler.agentes.values() if a.tipo == TipoAgente.LOOP)
            if loops > 0:
                self._log(f"🔄 Configuración contiene {loops} agente(s) Loop", "#6f42c1")

            QMessageBox.information(
                self, "Éxito",
                f"✅ Configuración guardada en:\n{ruta}"
            )
        except (ConfigError, ConfigSecurityError) as e:
            QMessageBox.critical(self, "Error", f"❌ Error al guardar: {str(e)}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"❌ Error inesperado: {str(e)}")

    def _cargar_configuracion(self):
        """
        Carga una configuración guardada desde el gestor de configuraciones.
        
        FLUJO:
        1. Obtiene la lista de configuraciones disponibles.
        2. Si hay más de 10, muestra un selector avanzado (QListWidget).
        3. Si hay 10 o menos, muestra un diálogo simple (QInputDialog).
        4. Confirma con el usuario antes de cargar.
        5. Elimina los agentes actuales y carga la nueva configuración.
        
        MEJORAS IMPLEMENTADAS:
        - ✅ Corrección del bug de QInputDialog.getItem() (devuelve str, no int)
        - ✅ Corrección del UnboundLocalError en la rama de >10 configuraciones
        - ✅ Validación robusta de todos los casos borde
        - ✅ Manejo de errores granular con logs
        - ✅ Feedback visual claro para el usuario
        """
        try:
            # ── 1. Obtener lista de configuraciones ──
            detalles = self.config_manager.listar_detalles()
            
            if not detalles:
                QMessageBox.information(
                    self, 
                    "📭 Sin configuraciones",
                    "No hay configuraciones guardadas para cargar.\n\n"
                    "Primero guarda una configuración con '💾 Guardar'."
                )
                self._log("📭 No hay configuraciones guardadas", "#6c757d")
                return

            self._log(f"📂 Obtenidas {len(detalles)} configuraciones", "#6c757d")

            # ── 2. Seleccionar configuración ──
            if len(detalles) > 10:
                # ── Más de 10: usar selector avanzado ──
                ruta_seleccionada = self._mostrar_selector_configuraciones(detalles)
                if not ruta_seleccionada:
                    self._log("⏹ Selección de configuración cancelada", "#6c757d")
                    return
                
                # Buscar el detalle por ruta
                detalle_seleccionado = next(
                    (d for d in detalles if d['ruta'] == ruta_seleccionada),
                    None
                )
                
                if not detalle_seleccionado:
                    QMessageBox.warning(
                        self,
                        "Error",
                        f"No se pudo encontrar la configuración seleccionada:\n{ruta_seleccionada}"
                    )
                    return
                
                nombre = detalle_seleccionado.get('nombre', Path(ruta_seleccionada).stem)
                ruta = ruta_seleccionada
                
            else:
                # ── 10 o menos: usar diálogo simple ──
                opciones = []
                for d in detalles:
                    # Construir etiqueta legible
                    label = f"{d['nombre']} - {d.get('fecha', 'sin fecha')}"
                    if d.get('descripcion'):
                        label += f" ({d['descripcion']})"
                    if d.get('tipos', {}).get('Loop', 0) > 0:
                        label += " 🔄"
                    if d.get('corrupto', False):
                        label += " ⚠️"
                    opciones.append(label)
                
                # ✅ CORRECCIÓN: QInputDialog.getItem() devuelve (str, bool)
                texto_seleccionado, ok = QInputDialog.getItem(
                    self,
                    "📂 Cargar Configuración",
                    "Selecciona una configuración:",
                    opciones,
                    0,
                    False
                )
                
                if not ok or not texto_seleccionado:
                    self._log("⏹ Selección de configuración cancelada", "#6c757d")
                    return
                
                # Obtener el índice del texto seleccionado
                try:
                    idx = opciones.index(texto_seleccionado)
                except ValueError:
                    QMessageBox.warning(
                        self,
                        "Error",
                        f"No se pudo encontrar la configuración seleccionada:\n{texto_seleccionado}"
                    )
                    return
                
                detalle_seleccionado = detalles[idx]
                nombre = detalle_seleccionado.get('nombre', Path(detalle_seleccionado['ruta']).stem)
                ruta = detalle_seleccionado['ruta']

            # ── 3. Validar que la ruta existe ──
            if not os.path.exists(ruta):
                QMessageBox.warning(
                    self,
                    "⚠️ Archivo no encontrado",
                    f"El archivo de configuración ya no existe:\n{ruta}\n\n"
                    "Puede haber sido movido o eliminado."
                )
                self._log(f"⚠️ Archivo de configuración no encontrado: {ruta}", "#dc3545")
                return

            # ── 4. Confirmar con el usuario ──
            confirmacion = QMessageBox.question(
                self,
                "⚠️ Confirmar carga",
                f"¿Cargar configuración **'{nombre}'**?\n\n"
                f"Esto **eliminará** todos los agentes actuales.\n"
                f"Contiene {detalle_seleccionado.get('total_agentes', '?')} agente(s).",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            
            if confirmacion != QMessageBox.StandardButton.Yes:
                self._log("⏹ Carga de configuración cancelada por el usuario", "#6c757d")
                return

            # ── 5. Cargar la configuración ──
            self._cargar_configuracion_desde_ruta(ruta, nombre)

        except Exception as e:
            logger.exception("Error en _cargar_configuracion")
            QMessageBox.critical(
                self,
                "❌ Error",
                f"Ocurrió un error al cargar la configuración:\n\n{str(e)}"
            )
            self._log(f"❌ Error cargando configuración: {e}", "#dc3545")

    def _mostrar_selector_configuraciones(self, detalles: List[Dict]) -> Optional[str]:
        """
        Muestra un selector de configuraciones con QListWidget.
        
        Args:
            detalles: Lista de diccionarios con información de configuraciones
            
        Returns:
            Optional[str]: Ruta de la configuración seleccionada, o None si se cancela
        """
        dialog = QDialog(self)
        dialog.setWindowTitle("📂 Cargar Configuración")
        dialog.setMinimumWidth(600)
        dialog.setMinimumHeight(450)
        dialog.setModal(True)

        layout = QVBoxLayout(dialog)
        layout.setSpacing(10)

        # ── Header ──
        header = QHBoxLayout()
        header.addWidget(QLabel("📋 Selecciona una configuración:"))
        
        # Filtro de búsqueda
        filtro_input = QLineEdit()
        filtro_input.setPlaceholderText("🔍 Filtrar por nombre...")
        filtro_input.setMaximumWidth(200)
        header.addWidget(filtro_input)
        
        layout.addLayout(header)

        # ── Lista de configuraciones ──
        list_widget = QListWidget()
        list_widget.setAlternatingRowColors(True)
        list_widget.setFont(QFont("Segoe UI", 10))
        
        # Índice para búsqueda rápida (texto -> ruta)
        item_map = {}
        
        for d in detalles:
            # Construir texto legible
            texto = f"{d['nombre']}"
            if d.get('fecha'):
                texto += f"  ({d['fecha']})"
            if d.get('descripcion'):
                texto += f"\n  📝 {d['descripcion']}"
            
            # Añadir indicadores visuales
            if d.get('tipos', {}).get('Loop', 0) > 0:
                texto += "  🔄"
            if d.get('corrupto', False):
                texto += "  ⚠️"
            
            item = QListWidgetItem(texto)
            item.setData(Qt.ItemDataRole.UserRole, d['ruta'])
            
            # Color según estado
            if d.get('corrupto', False):
                item.setForeground(QColor("#dc3545"))
            elif d.get('tipos', {}).get('Loop', 0) > 0:
                item.setForeground(QColor("#6f42c1"))
            
            list_widget.addItem(item)
            item_map[texto] = d['ruta']
        
        layout.addWidget(list_widget)

        # ── Información adicional ──
        info_label = QLabel(f"📊 {len(detalles)} configuraciones disponibles")
        info_label.setStyleSheet("color: #6c757d; font-size: 10px; padding: 4px;")
        layout.addWidget(info_label)

        # ── Filtro en tiempo real ──
        def filtrar_lista(texto: str):
            texto = texto.lower()
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                item.setHidden(texto not in item.text().lower())
        
        filtro_input.textChanged.connect(filtrar_lista)

        # ── Doble click para cargar ──
        def on_item_double_clicked(item: QListWidgetItem):
            dialog.accept()
        
        list_widget.itemDoubleClicked.connect(on_item_double_clicked)

        # ── Botones ──
        btn_layout = QHBoxLayout()
        
        btn_cargar = QPushButton("✅ Cargar")
        btn_cargar.setDefault(True)
        btn_cargar.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: white;
                font-weight: bold;
                padding: 8px 24px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #218838; }
        """)
        btn_cargar.clicked.connect(dialog.accept)
        btn_layout.addWidget(btn_cargar)

        btn_cancelar = QPushButton("❌ Cancelar")
        btn_cancelar.clicked.connect(dialog.reject)
        btn_cancelar.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #5a6268; }
        """)
        btn_layout.addWidget(btn_cancelar)
        
        layout.addLayout(btn_layout)

        # ── Ejecutar y obtener resultado ──
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        
        selected_item = list_widget.currentItem()
        if not selected_item:
            return None
        
        return selected_item.data(Qt.ItemDataRole.UserRole)

    def _cargar_configuracion_desde_ruta(self, ruta: str, nombre: str):
        """Carga una configuración desde una ruta."""
        try:
            self._limpiar()
            agentes_data = self.config_manager.cargar(ruta)

            loops_cargados = 0
            for data in agentes_data:
                agente = self._reconstruir_agente_desde_dict(data)
                self._registrar_y_mostrar(agente)
                if agente.tipo == TipoAgente.LOOP:
                    loops_cargados += 1

            self.scheduler.resolver_dependencias()
            self._actualizar_stats()
            self._actualizar_grafo(recalcular_layout=True)
            

            mensaje = f"✅ Configuración cargada correctamente\n{len(agentes_data)} agentes"
            if loops_cargados > 0:
                mensaje += f"\n🔄 {loops_cargados} agente(s) Loop"

            self._log(f"📂 Configuración cargada: {nombre} ({loops_cargados} loops)", "#007bff")
            QMessageBox.information(self, "Éxito", mensaje)

        except (ConfigError, ConfigSecurityError) as e:
            QMessageBox.critical(self, "Error", f"❌ Error al cargar: {str(e)}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"❌ Error inesperado: {str(e)}")

    def _importar_desde_drag(self, file_path: str):
        """Importa una configuración desde drag & drop."""
        try:
            # Verificar que es un archivo JSON válido
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            if 'agentes' not in data:
                QMessageBox.warning(self, "Error", "El archivo no contiene una configuración válida")
                return

            nombre = Path(file_path).stem
            respuesta = QMessageBox.question(
                self, "📥 Importar",
                f"¿Importar configuración desde '{nombre}'?\n"
                f"Esto eliminará los agentes actuales.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )

            if respuesta == QMessageBox.StandardButton.Yes:
                # Copiar el archivo a configs si no está ya allí
                if not file_path.startswith(self.config_manager.config_dir):
                    import shutil
                    dest = os.path.join(
                        self.config_manager.config_dir,
                        f"{nombre}_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
                    )
                    shutil.copy2(file_path, dest)
                    ruta = dest
                else:
                    ruta = file_path

                self._cargar_configuracion_desde_ruta(ruta, nombre)

        except json.JSONDecodeError as e:
            QMessageBox.critical(self, "Error", f"❌ JSON inválido: {str(e)}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"❌ Error al importar: {str(e)}")

    def _exportar_json(self):
        """Exporta agentes a JSON."""
        if not self.scheduler.agentes:
            QMessageBox.warning(self, "Aviso", "No hay agentes para exportar")
            return

        ruta, _ = QFileDialog.getSaveFileName(
            self, "Exportar Agentes",
            f"agentes_{datetime.now().strftime('%Y%m%d_%H%M')}.json",
            "JSON Files (*.json)"
        )

        if not ruta:
            return

        try:
            self.config_manager.exportar_a_json(self.scheduler.agentes, ruta)
            self._log(f"📤 Agentes exportados a: {ruta}", "#28a745")
            QMessageBox.information(self, "Éxito", f"✅ Agentes exportados a:\n{ruta}")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"❌ Error al exportar: {str(e)}")

    def _importar_json(self):
        """Importa agentes desde JSON."""
        ruta, _ = QFileDialog.getOpenFileName(
            self, "Importar Agentes",
            "",
            "JSON Files (*.json)"
        )

        if not ruta:
            return

        self._importar_desde_drag(ruta)

    # ============================================================
    # AYUDA
    # ============================================================

    def _show_help(self):
        """Muestra un diálogo de ayuda."""
        help_text = """
<h2>🚀 Ayuda de Agentes Visuales</h2>

<h3>📋 Atajos de Teclado</h3>
<ul>
    <li><b>Ctrl+N</b> - Nuevo agente</li>
    <li><b>Ctrl+S</b> - Guardar configuración</li>
    <li><b>Ctrl+O</b> - Cargar configuración</li>
    <li><b>Ctrl+E</b> - Exportar JSON</li>
    <li><b>Ctrl+I</b> - Importar JSON</li>
    <li><b>Ctrl+R</b> - Iniciar ejecución</li>
    <li><b>Ctrl+P</b> - Pausar/Reanudar</li>
    <li><b>Ctrl+Shift+R</b> - Detener ejecución</li>
    <li><b>Ctrl+Shift+C</b> - Limpiar todo</li>
    <li><b>Ctrl+T</b> - Alternar tema</li>
    <li><b>F5</b> - Refrescar UI</li>
</ul>

<h3>🔄 Agentes Loop</h3>
<ul>
    <li>Iteran sobre una lista de items</li>
    <li>Ejecutan código por cada item</li>
    <li>Pueden continuar en error (opcional)</li>
    <li>Tienen límites de seguridad (iteraciones, timeout)</li>
</ul>

<h3>📊 Tipos de Agentes</h3>
<ul>
    <li><b>Python</b> - Código Python en sandbox</li>
    <li><b>Shell</b> - Comandos de terminal</li>
    <li><b>HTTP</b> - Peticiones HTTP/HTTPS</li>
    <li><b>LLM</b> - Modelos de lenguaje (DeepSeek)</li>
    <li><b>File</b> - Operaciones con archivos</li>
    <li><b>Loop</b> - Iteración sobre listas</li>
</ul>

<h3>🔧 Consejos</h3>
<ul>
    <li>Arrastra archivos .json para importar</li>
    <li>Usa el editor visual para dependencias</li>
    <li>Los loops pueden probarse con items de ejemplo</li>
    <li>El dashboard muestra métricas en tiempo real</li>
</ul>
        """

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("❓ Ayuda")
        msg_box.setTextFormat(Qt.TextFormat.RichText)
        msg_box.setText(help_text)
        msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg_box.exec()

    # ============================================================
    # LIMPIEZA FINAL
    # ============================================================

    def __del__(self):
        """Limpieza final."""
        try:
            self.timer.stop()
            self._graph_update_timer.stop()
            self._stats_update_timer.stop()
        except:
            pass

    def _suscribir_eventos(self):
        """Suscribe los manejadores de eventos al bus."""
        
        # Eventos de agente
        self._bus.suscribir(EventType.AGENTE_ACTUALIZADO, self._on_agente_actualizado_bus)
        self._bus.suscribir(EventType.AGENTE_COMPLETADO, self._on_agente_completado_bus)
        self._bus.suscribir(EventType.AGENTE_ERROR, self._on_agente_error_bus)
        self._bus.suscribir(EventType.AGENTE_BLOQUEADO, self._on_agente_bloqueado_bus)
        
        # Eventos de log
        self._bus.suscribir(EventType.LOG_MENSAJE, self._on_log_mensaje_bus)
        
        # Eventos de ejecución
        self._bus.suscribir(EventType.EJECUCION_INICIADA, self._on_ejecucion_iniciada_bus)
        self._bus.suscribir(EventType.EJECUCION_TERMINADA, self._on_ejecucion_terminada_bus)
        self._bus.suscribir(EventType.ESTADO_CAMBIADO, self._on_estado_cambiado_bus)


    def _on_agente_actualizado_bus(self, evento: Event):
        """Maneja evento de agente actualizado desde el bus."""
        datos = evento.datos
        agente_id = datos.get("agente_id")
        if agente_id:
            self._on_agent_updated(agente_id)

    def _on_agente_completado_bus(self, evento: Event):
        """Maneja evento de agente completado desde el bus."""
        datos = evento.datos
        nombre = datos.get("nombre", "Desconocido")
        resultado = datos.get("resultado")
        self._log(f"📦 [{nombre}] Resultado → {self._formatear_resultado(resultado)}", "#17a2b8")


    def _on_agente_error_bus(self, evento: Event):
        """Maneja evento de agente en error desde el bus."""
        datos = evento.datos
        nombre = datos.get("nombre", "Desconocido")
        error = datos.get("error", "Error desconocido")
        self._log(f"🔍 [{nombre}] Error detalle → {error[:150]}", "#ffc107")


    def _on_agente_bloqueado_bus(self, evento: Event):
        """Maneja evento de agente bloqueado desde el bus."""
        datos = evento.datos
        nombre = datos.get("nombre", "Desconocido")
        razon = datos.get("razon", "Bloqueado")
        self._log(f"🚫 [{nombre}] Bloqueado: {razon}", "#8b0000")


    def _on_log_mensaje_bus(self, evento: Event):
        """Maneja evento de log desde el bus."""
        datos = evento.datos
        mensaje = datos.get("mensaje", "")
        color = datos.get("color", "#333")
        self._log(mensaje, color)


    def _on_ejecucion_iniciada_bus(self, evento: Event):
        """Maneja evento de ejecución iniciada desde el bus."""
        datos = evento.datos
        total = datos.get("total_agentes", 0)
        self._log(f"▶️ Ejecución iniciada ({total} agentes)", "#28a745")


    def _on_ejecucion_terminada_bus(self, evento: Event):
        """Maneja evento de ejecución terminada desde el bus."""
        datos = evento.datos
        stats = datos.get("stats", {})
        self._mostrar_resumen_ejecucion(stats)


    def _on_estado_cambiado_bus(self, evento: Event):
        """Maneja evento de cambio de estado desde el bus."""
        datos = evento.datos
        ejecutando = datos.get("ejecutando", False)
        if ejecutando:
            self.lbl_info.setText("▶ Ejecutando...")
            self.lbl_info.setStyleSheet("font-weight: bold; color: #007bff;")
        else:
            self.lbl_info.setText("✅ Detenido")
            self.lbl_info.setStyleSheet("font-weight: bold; color: #28a745;")
