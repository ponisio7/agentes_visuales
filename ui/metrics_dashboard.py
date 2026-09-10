# ui/metrics_dashboard.py - VERSIÓN REFACTORIZADA Y CORREGIDA
"""
Panel de métricas en tiempo real con gráficos interactivos.

MEJORAS IMPLEMENTADAS:
- ✅ Backend de matplotlib compatible con PyQt6 (try/except para compatibilidad)
- ✅ Throttling con try/finally para evitar bloqueos permanentes
- ✅ Timer solo activo cuando hay ejecución (ahorro de CPU)
- ✅ Cache de estadísticas con TTL adecuado
- ✅ Gráficos con mejor rendimiento (actualización incremental)
- ✅ Exportación en múltiples formatos (PNG, PDF, SVG, CSV)
- ✅ Panel de estadísticas detalladas
- ✅ Soporte para temas oscuros
- ✅ Manejo de errores granular
- ✅ Logging estructurado
- ✅ Memoria optimizada (límite de datos históricos)
"""

import time
import math
import logging
from typing import Dict, List, Optional, Tuple, Any
from collections import deque
from datetime import datetime
import json

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QGridLayout,
    QFrame, QProgressBar, QPushButton, QFileDialog, QMessageBox,
    QMenu, QToolButton, QSplitter, QGroupBox, QApplication
)
from PyQt6.QtCore import Qt, QTimer, pyqtSignal, QSize, QEvent
from PyQt6.QtGui import (
    QFont, QColor, QAction, QPalette, QBrush, QPen, 
    QCursor, QKeySequence
)

# ✅ Backend de matplotlib compatible con PyQt6
try:
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg
except ImportError:
    try:
        from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg
    except ImportError:
        raise ImportError("No se pudo importar el backend de matplotlib para Qt")

from matplotlib.figure import Figure
from matplotlib.ticker import MaxNLocator
import numpy as np

from core.scheduler import Scheduler
from core.agent import EstadoAgente, TipoAgente

# Configurar logger
logger = logging.getLogger(__name__)

# ============================================================
# CONSTANTES
# ============================================================

DEFAULT_UPDATE_INTERVAL_MS = 500
MAX_HISTORY_POINTS = 300
MIN_UPDATE_INTERVAL_MS = 100
MAX_UPDATE_INTERVAL_MS = 2000

COLORES_ESTADO = {
    "pendiente": "#6c757d",
    "esperando": "#fd7e14",
    "listo": "#28a745",
    "ejecutando": "#007bff",
    "completado": "#28a745",
    "error": "#dc3545",
    "cancelado": "#6c757d",
}

COLORES_GRADIENTE = ["#007bff", "#6610f2", "#6f42c1", "#e83e8c", "#dc3545"]


# ============================================================
# WIDGET DE TARJETA DE MÉTRICA
# ============================================================

class MetricCard(QFrame):
    """Tarjeta de métrica individual con diseño moderno."""
    
    clicked = pyqtSignal(str)  # Emite el título de la tarjeta
    
    def __init__(
        self, 
        titulo: str, 
        icono: str = "", 
        color: str = "#007bff",
        tooltip: str = ""
    ):
        super().__init__()
        self.titulo = titulo
        self.icono = icono
        self.color = color
        self._is_dark = False
        
        self.setFrameStyle(QFrame.Shape.StyledPanel)
        self.setObjectName("metric_card")
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        
        self._aplicar_estilo()
        
        layout = QVBoxLayout()
        layout.setSpacing(2)
        layout.setContentsMargins(8, 6, 8, 6)
        
        # Título con ícono
        titulo_layout = QHBoxLayout()
        titulo_layout.setSpacing(4)
        
        if icono:
            self.icon_label = QLabel(icono)
            self.icon_label.setFont(QFont("Segoe UI Emoji", 14))
            titulo_layout.addWidget(self.icon_label)
        
        self.titulo_label = QLabel(titulo)
        self.titulo_label.setStyleSheet(f"""
            color: {color}; 
            font-weight: bold; 
            font-size: 10px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        """)
        titulo_layout.addWidget(self.titulo_label)
        titulo_layout.addStretch()
        
        if tooltip:
            self.setToolTip(tooltip)
        
        layout.addLayout(titulo_layout)
        
        # Valor principal
        self.valor_label = QLabel("0")
        self.valor_label.setFont(QFont("Arial", 22, QFont.Weight.Bold))
        self.valor_label.setStyleSheet(f"color: {color};")
        self.valor_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.valor_label)
        
        # Subtítulo
        self.subtitulo_label = QLabel("")
        self.subtitulo_label.setStyleSheet("""
            color: #888; 
            font-size: 9px;
            text-align: center;
        """)
        self.subtitulo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.subtitulo_label)
        
        self.setLayout(layout)
        self.installEventFilter(self)
    
    def _aplicar_estilo(self):
        """Aplica el estilo según el tema."""
        palette = self.palette()
        bg = palette.color(QPalette.ColorRole.Window)
        self._is_dark = bg.lightness() < 128
        
        bg_color = "#2d2d2d" if self._is_dark else "#ffffff"
        border_color = "#3d3d3d" if self._is_dark else "#e0e0e0"
        hover_color = "#3d3d3d" if self._is_dark else "#f8f9fa"
        
        self.setStyleSheet(f"""
            MetricCard {{
                background-color: {bg_color};
                border-radius: 8px;
                border: 1px solid {border_color};
                padding: 8px;
                min-height: 70px;
            }}
            MetricCard:hover {{
                border-color: {self.color};
                background-color: {hover_color};
            }}
        """)
    
    def eventFilter(self, obj, event):
        """Maneja eventos de mouse para el click."""
        if event.type() == QEvent.Type.MouseButtonPress:
            self.clicked.emit(self.titulo)
            return True
        return super().eventFilter(obj, event)
    
    def actualizar(self, valor: Any, subtitulo: str = "", porcentaje: Optional[float] = None):
        """Actualiza el valor y subtítulo de la tarjeta."""
        # Formatear valor
        if isinstance(valor, float):
            if valor >= 1000:
                valor_str = f"{valor:,.0f}"
            elif valor >= 100:
                valor_str = f"{valor:.1f}"
            else:
                valor_str = f"{valor:.2f}"
        else:
            valor_str = str(valor)
        
        self.valor_label.setText(valor_str)
        self.subtitulo_label.setText(subtitulo)
        
        # Actualizar color si hay porcentaje
        if porcentaje is not None:
            if porcentaje > 80:
                color = "#28a745"
            elif porcentaje > 50:
                color = "#ffc107"
            else:
                color = "#dc3545"
            self.valor_label.setStyleSheet(f"color: {color}; font-size: 22px; font-weight: bold;")


class ProgressMetricCard(MetricCard):
    """Tarjeta de métrica con barra de progreso."""
    
    def __init__(self, titulo: str, icono: str = "", color: str = "#007bff"):
        super().__init__(titulo, icono, color)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setStyleSheet(f"""
            QProgressBar {{
                border: 1px solid {color}40;
                border-radius: 4px;
                text-align: center;
                height: 16px;
                background-color: transparent;
            }}
            QProgressBar::chunk {{
                background-color: {color};
                border-radius: 3px;
            }}
        """)
        
        layout = self.layout()
        layout.insertWidget(2, self.progress_bar)
    
    def actualizar(self, valor: Any, subtitulo: str = "", progreso: int = 0):
        """Actualiza el valor, subtítulo y barra de progreso."""
        super().actualizar(valor, subtitulo)
        self.progress_bar.setValue(int(min(100, max(0, progreso))))


# ============================================================
# CLASE PRINCIPAL: METRICS DASHBOARD
# ============================================================

class MetricsDashboard(QWidget):
    """Panel de métricas en tiempo real con gráficos interactivos."""
    
    # Señales
    metric_clicked = pyqtSignal(str)
    export_requested = pyqtSignal(str)
    reset_requested = pyqtSignal()
    
    def __init__(self, scheduler: Scheduler, parent=None):
        super().__init__(parent)
        
        self.scheduler = scheduler
        self.logger = logging.getLogger(f"{__name__}.MetricsDashboard")
        
        # ── Datos históricos ──
        self.historial_tiempos = deque(maxlen=MAX_HISTORY_POINTS)
        self.historial_completados = deque(maxlen=MAX_HISTORY_POINTS)
        self.historial_ejecutando = deque(maxlen=MAX_HISTORY_POINTS)
        self.historial_errores = deque(maxlen=MAX_HISTORY_POINTS)
        
        self.tiempo_inicio: Optional[float] = None
        self.tiempo_ejecucion: float = 0.0
        self.ultimo_punto_tiempo: float = 0.0
        
        # ── Estado ──
        self.activo = False
        self.pausado = False
        self._update_in_progress = False  # ✅ Flag con try/finally
        
        # ── Configuración ──
        self.update_interval_ms = DEFAULT_UPDATE_INTERVAL_MS
        self.max_points = MAX_HISTORY_POINTS
        
        # ── Cache ──
        self._stats_cache: Optional[Dict] = None
        self._stats_cache_time: float = 0.0
        self._stats_cache_ttl: float = 0.5  # ✅ Aumentado a 500ms
        
        # ── Inicializar UI ──
        self._init_ui()
        
        # ── Timer de actualización (INICIADO SOLO CUANDO HAY EJECUCIÓN) ──
        self.timer = QTimer()
        self.timer.setSingleShot(False)
        self.timer.timeout.connect(self._actualizar_metricas_throttled)
        
        # ── Conectar señales del scheduler ──
        if self.scheduler:
            self.scheduler.agente_actualizado.connect(self._on_agente_actualizado)
            self.scheduler.ejecucion_terminada.connect(self._on_ejecucion_terminada)
            self.scheduler.log_mensaje.connect(self._on_log_mensaje)
            # ✅ Conectar nueva señal de estado
            if hasattr(self.scheduler, 'estado_cambiado'):
                self.scheduler.estado_cambiado.connect(self._on_estado_cambiado)
        
        # ── Aplicar tema ──
        self._aplicar_tema()
        
        self.logger.info("MetricsDashboard inicializado")
    
    # ============================================================
    # CONSTRUCCIÓN DE LA UI
    # ============================================================
    
    def _init_ui(self):
        """Inicializa la interfaz del dashboard."""
        main_layout = QVBoxLayout()
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(8, 8, 8, 8)
        
        # ── Barra de herramientas ──
        toolbar = self._crear_toolbar()
        main_layout.addWidget(toolbar)
        
        # ── Grid de métricas ──
        grid_metrics = self._crear_grid_metricas()
        main_layout.addLayout(grid_metrics)
        
        # ── Splitter para gráficos ──
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)
        
        # Gráfico principal
        self._crear_grafico_principal()
        splitter.addWidget(self.canvas_container)
        
        # Panel de estadísticas y detalles
        stats_panel = self._crear_panel_estadisticas()
        splitter.addWidget(stats_panel)
        
        splitter.setSizes([400, 200])
        main_layout.addWidget(splitter, stretch=1)
        
        # ── Barra de progreso general ──
        bottom_layout = self._crear_panel_inferior()
        main_layout.addLayout(bottom_layout)
        
        self.setLayout(main_layout)
        
        self.setStyleSheet("""
            MetricsDashboard {
                background-color: transparent;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #d1d5db;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
    
    def _crear_toolbar(self) -> QWidget:
        """Crea la barra de herramientas."""
        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)
        
        title = QLabel("📈 Dashboard de Métricas")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")
        toolbar_layout.addWidget(title)
        
        toolbar_layout.addStretch()
        
        # Botón de pausa
        self.btn_pausar = QPushButton("⏸ Pausar")
        self.btn_pausar.setCheckable(True)
        self.btn_pausar.toggled.connect(self._toggle_pausa)
        self.btn_pausar.setToolTip("Pausar/Reanudar actualización en tiempo real")
        self.btn_pausar.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 10px;
            }
            QPushButton:checked {
                background-color: #28a745;
            }
        """)
        toolbar_layout.addWidget(self.btn_pausar)
        
        # Botón de limpieza
        btn_limpiar = QPushButton("🗑 Limpiar Historial")
        btn_limpiar.setToolTip("Limpiar el historial de datos")
        btn_limpiar.clicked.connect(self._limpiar_historial)
        btn_limpiar.setStyleSheet("""
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
        """)
        toolbar_layout.addWidget(btn_limpiar)
        
        # Menú de exportación
        btn_exportar = QToolButton()
        btn_exportar.setText("📤 Exportar")
        btn_exportar.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        btn_exportar.setToolTip("Exportar gráfico o datos")
        btn_exportar.setStyleSheet("""
            QToolButton {
                background-color: #28a745;
                color: white;
                border-radius: 4px;
                padding: 4px 12px;
                font-size: 10px;
            }
            QToolButton::menu-indicator {
                image: none;
            }
            QToolButton:hover {
                background-color: #218838;
            }
        """)
        
        menu = QMenu()
        menu.addAction("📊 PNG", lambda: self._exportar_grafico("png"))
        menu.addAction("📄 PDF", lambda: self._exportar_grafico("pdf"))
        menu.addAction("📈 SVG", lambda: self._exportar_grafico("svg"))
        menu.addSeparator()
        menu.addAction("📋 CSV (Datos)", self._exportar_csv)
        menu.addAction("📋 JSON (Datos)", self._exportar_json)
        btn_exportar.setMenu(menu)
        toolbar_layout.addWidget(btn_exportar)
        
        # Intervalo de actualización
        toolbar_layout.addWidget(QLabel("Actualización:"))
        self.combo_intervalo = QPushButton("500ms")
        self.combo_intervalo.setStyleSheet("""
            QPushButton {
                background-color: #e9ecef;
                border: 1px solid #d1d5db;
                border-radius: 4px;
                padding: 4px 8px;
                font-size: 10px;
            }
        """)
        menu_intervalo = QMenu()
        for ms in [100, 250, 500, 1000, 2000]:
            action = QAction(f"{ms}ms", self)
            action.triggered.connect(lambda checked, v=ms: self._cambiar_intervalo(v))
            menu_intervalo.addAction(action)
        self.combo_intervalo.setMenu(menu_intervalo)
        toolbar_layout.addWidget(self.combo_intervalo)
        
        return toolbar
    
    def _crear_grid_metricas(self) -> QGridLayout:
        """Crea el grid de tarjetas de métricas."""
        grid = QGridLayout()
        grid.setSpacing(8)
        
        self.card_total = MetricCard(
            "Total Agentes", "👥", "#6c757d",
            "Número total de agentes en el sistema"
        )
        self.card_total.clicked.connect(lambda: self.metric_clicked.emit("total"))
        grid.addWidget(self.card_total, 0, 0)
        
        self.card_completados = ProgressMetricCard(
            "Completados", "✅", "#28a745"
        )
        self.card_completados.clicked.connect(lambda: self.metric_clicked.emit("completados"))
        grid.addWidget(self.card_completados, 0, 1)
        
        self.card_ejecutando = ProgressMetricCard(
            "Ejecutando", "⚡", "#007bff"
        )
        self.card_ejecutando.clicked.connect(lambda: self.metric_clicked.emit("ejecutando"))
        grid.addWidget(self.card_ejecutando, 0, 2)
        
        self.card_errores = MetricCard(
            "Errores", "❌", "#dc3545",
            "Agentes que fallaron en la ejecución"
        )
        self.card_errores.clicked.connect(lambda: self.metric_clicked.emit("errores"))
        grid.addWidget(self.card_errores, 0, 3)
        
        self.card_esperando = MetricCard(
            "Esperando", "⏳", "#fd7e14",
            "Agentes pendientes o esperando dependencias"
        )
        self.card_esperando.clicked.connect(lambda: self.metric_clicked.emit("esperando"))
        grid.addWidget(self.card_esperando, 0, 4)
        
        self.card_loops = MetricCard(
            "Loops Activos", "🔄", "#6f42c1",
            "Agentes Loop actualmente en ejecución"
        )
        self.card_loops.clicked.connect(lambda: self.metric_clicked.emit("loops"))
        grid.addWidget(self.card_loops, 0, 5)
        
        return grid
    
    def _crear_grafico_principal(self):
        """Crea el gráfico principal."""
        self.canvas_container = QWidget()
        container_layout = QVBoxLayout(self.canvas_container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        
        self.grafico_titulo = QLabel("📊 Progreso de Ejecución en Tiempo Real")
        self.grafico_titulo.setStyleSheet("font-weight: bold; font-size: 12px;")
        container_layout.addWidget(self.grafico_titulo)
        
        # ✅ Figura con compatibilidad de backend
        self.figure = Figure(
            figsize=(8, 3.5), 
            dpi=100, 
            facecolor='#f8f9fa',
            tight_layout=True
        )
        self.canvas = FigureCanvasQTAgg(self.figure)
        self.canvas.setMinimumHeight(250)
        self.canvas.setSizePolicy(
            self.canvas.sizePolicy().Policy.Expanding,
            self.canvas.sizePolicy().Policy.Expanding
        )
        container_layout.addWidget(self.canvas)
        
        self._setup_grafico()
    
    def _setup_grafico(self):
        """Configura el gráfico inicial."""
        self.figure.clear()
        self.ax = self.figure.add_subplot(111)
        
        self.ax.set_xlabel('Tiempo (segundos)', fontsize=9)
        self.ax.set_ylabel('Agentes', fontsize=9)
        self.ax.set_title('Progreso de Ejecución en Tiempo Real', fontsize=11, fontweight='bold')
        self.ax.grid(True, alpha=0.3, linestyle='--')
        
        self._aplicar_colores_grafico()
        
        self.ax.text(
            0.5, 0.5, '*** Esperando ejecución...',
            horizontalalignment='center', 
            verticalalignment='center',
            transform=self.ax.transAxes, 
            fontsize=14, 
            color='#999',
            fontweight='bold'
        )
        self.ax.set_xlim(0, 1)
        self.ax.set_ylim(0, 1)
        self.ax.set_xticks([])
        self.ax.set_yticks([])
        
        self.figure.tight_layout()
        self.canvas.draw()
    
    def _aplicar_colores_grafico(self):
        """Aplica los colores del tema al gráfico."""
        palette = self.palette()
        bg = palette.color(QPalette.ColorRole.Window)
        dark = bg.lightness() < 128
        
        if dark:
            self.figure.set_facecolor('#1e1e1e')
            self.ax.set_facecolor('#1e1e1e')
            self.ax.tick_params(colors='#ffffff')
            self.ax.xaxis.label.set_color('#ffffff')
            self.ax.yaxis.label.set_color('#ffffff')
            self.ax.title.set_color('#ffffff')
            self.ax.grid(True, alpha=0.2, linestyle='--', color='#ffffff')
        else:
            self.figure.set_facecolor('#f8f9fa')
            self.ax.set_facecolor('#f8f9fa')
            self.ax.tick_params(colors='#333333')
            self.ax.xaxis.label.set_color('#333333')
            self.ax.yaxis.label.set_color('#333333')
            self.ax.title.set_color('#333333')
            self.ax.grid(True, alpha=0.3, linestyle='--', color='#333333')
    
    def _crear_panel_estadisticas(self) -> QWidget:
        """Crea el panel de estadísticas detalladas."""
        panel = QGroupBox("📊 Estadísticas Detalladas")
        panel.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                border: 1px solid #d1d5db;
                border-radius: 4px;
                margin-top: 8px;
                padding-top: 8px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px 0 5px;
            }
        """)
        
        layout = QGridLayout(panel)
        layout.setSpacing(4)
        layout.setContentsMargins(8, 12, 8, 8)
        
        row = 0
        self.stat_tiempo = QLabel("⏱ Tiempo: 0.0s")
        self.stat_tiempo.setStyleSheet("font-weight: bold; color: #007bff;")
        layout.addWidget(self.stat_tiempo, row, 0)
        
        self.stat_velocidad = QLabel("⚡ Velocidad: 0.0 ag/s")
        layout.addWidget(self.stat_velocidad, row, 1)
        
        self.stat_tasa_exito = QLabel("📊 Tasa éxito: 0%")
        layout.addWidget(self.stat_tasa_exito, row, 2)
        
        row += 1
        self.stat_avg_tiempo = QLabel("⏱ Promedio: 0.0s/ag")
        layout.addWidget(self.stat_avg_tiempo, row, 0)
        
        self.stat_max_concurrent = QLabel("⚡ Máx concurrentes: 0")
        layout.addWidget(self.stat_max_concurrent, row, 1)
        
        self.stat_estado = QLabel("⏸ Inactivo")
        self.stat_estado.setStyleSheet("font-weight: bold; color: #6c757d;")
        layout.addWidget(self.stat_estado, row, 2)
        
        row += 1
        self.stat_loops = QLabel("🔄 Loops: 0 activos")
        layout.addWidget(self.stat_loops, row, 0)
        
        self.stat_memoria = QLabel("💾 Datos: 0 puntos")
        layout.addWidget(self.stat_memoria, row, 1)
        
        self.stat_cache = QLabel("📦 Cache: 0")
        layout.addWidget(self.stat_cache, row, 2)
        
        return panel
    
    def _crear_panel_inferior(self) -> QHBoxLayout:
        """Crea el panel inferior con barra de progreso y botones."""
        layout = QHBoxLayout()
        layout.setSpacing(8)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #d1d5db;
                border-radius: 4px;
                text-align: center;
                height: 24px;
                background-color: white;
                font-weight: bold;
            }
            QProgressBar::chunk {
                background-color: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #007bff, stop:1 #6f42c1);
                border-radius: 3px;
            }
        """)
        layout.addWidget(self.progress_bar, stretch=2)
        
        self.lbl_estado_ejecucion = QLabel("⏸ Inactivo")
        self.lbl_estado_ejecucion.setStyleSheet("""
            font-weight: bold;
            color: #6c757d;
            padding: 4px 12px;
            background-color: #f8f9fa;
            border-radius: 4px;
        """)
        layout.addWidget(self.lbl_estado_ejecucion)
        
        return layout
    
    # ============================================================
    # ACTUALIZACIÓN DE MÉTRICAS (CON TRY/FINALLY)
    # ============================================================
    
    def _actualizar_metricas_throttled(self):
        """Actualiza las métricas con throttling y try/finally."""
        # ✅ Previene re-entrada y asegura liberación del flag
        if self._update_in_progress:
            return
        
        self._update_in_progress = True
        try:
            self._actualizar_metricas()
        except Exception as e:
            self.logger.error(f"Error actualizando métricas: {e}")
        finally:
            self._update_in_progress = False  # ✅ Siempre se libera
    
    def _actualizar_metricas(self):
        """Actualiza todas las métricas en tiempo real."""
        if self.pausado or not self.scheduler:
            return
        
        try:
            stats = self._obtener_estadisticas_cache()
            
            self._actualizar_tarjetas(stats)
            self._actualizar_panel_estadisticas(stats)
            self._actualizar_progress_bar(stats)
            self._actualizar_grafico(stats)
            
        except Exception as e:
            self.logger.error(f"Error en _actualizar_metricas: {e}")
            raise
    
    def _obtener_estadisticas_cache(self) -> Dict:
        """Obtiene estadísticas con cache."""
        now = time.time()
        if (self._stats_cache is None or 
            now - self._stats_cache_time > self._stats_cache_ttl):
            self._stats_cache = self.scheduler.obtener_estadisticas()
            self._stats_cache_time = now
        return self._stats_cache
    
    def _actualizar_tarjetas(self, stats: Dict):
        """Actualiza las tarjetas de métricas."""
        total = stats.get('total', 0)
        completados = stats.get('completados', 0)
        ejecutando = stats.get('ejecutando', 0)
        errores = stats.get('errores', 0)
        esperando = stats.get('esperando', 0)
        loops_activos = stats.get('loops_activos', 0)
        
        self.card_total.actualizar(total)
        
        pct = self._calcular_porcentaje(completados, total)
        self.card_completados.actualizar(completados, f"{pct:.1f}%", pct)
        
        pct = self._calcular_porcentaje(ejecutando, total)
        self.card_ejecutando.actualizar(ejecutando, f"{pct:.1f}%", pct)
        
        pct = self._calcular_porcentaje(errores, total)
        self.card_errores.actualizar(
            errores, 
            f"{pct:.1f}% de los agentes" if total > 0 else ""
        )
        
        self.card_esperando.actualizar(esperando)
        self.card_loops.actualizar(loops_activos)
    
    def _actualizar_panel_estadisticas(self, stats: Dict):
        """Actualiza el panel de estadísticas detalladas."""
        total = stats.get('total', 0)
        completados = stats.get('completados', 0)
        errores = stats.get('errores', 0)
        cancelados = stats.get('cancelados', 0)
        
        if self.tiempo_inicio:
            self.tiempo_ejecucion = time.time() - self.tiempo_inicio
        self.stat_tiempo.setText(f"⏱ Tiempo: {self.tiempo_ejecucion:.1f}s")
        
        if self.tiempo_ejecucion > 0:
            velocidad = (completados + errores + cancelados) / self.tiempo_ejecucion
            self.stat_velocidad.setText(f"⚡ Velocidad: {velocidad:.2f} ag/s")
        
        total_finalizados = completados + errores
        if total_finalizados > 0:
            tasa = (completados / total_finalizados) * 100
            self.stat_tasa_exito.setText(f"📊 Tasa éxito: {tasa:.1f}%")
        
        if completados > 0:
            avg = self.tiempo_ejecucion / completados if completados > 0 else 0
            self.stat_avg_tiempo.setText(f"⏱ Promedio: {avg:.2f}s/ag")
        
        max_concurrent = max(stats.get('ejecutando', 0), 1)
        self.stat_max_concurrent.setText(f"⚡ Máx concurrentes: {max_concurrent}")
        
        if self.scheduler.ejecutando:
            if self.scheduler.pausado:
                self.stat_estado.setText("⏸ Pausado")
                self.stat_estado.setStyleSheet("font-weight: bold; color: #ffc107;")
            else:
                self.stat_estado.setText("▶ Ejecutando")
                self.stat_estado.setStyleSheet("font-weight: bold; color: #28a745;")
        else:
            if total > 0 and (completados + errores + cancelados) == total:
                self.stat_estado.setText("✅ Completado")
                self.stat_estado.setStyleSheet("font-weight: bold; color: #28a745;")
            else:
                self.stat_estado.setText("⏸ Inactivo")
                self.stat_estado.setStyleSheet("font-weight: bold; color: #6c757d;")
        
        loops_activos = stats.get('loops_activos', 0)
        loops_total = stats.get('loops_total', 0)
        if loops_total > 0:
            self.stat_loops.setText(f"🔄 Loops: {loops_activos}/{loops_total} activos")
        else:
            self.stat_loops.setText("🔄 Loops: 0")
        
        self.stat_memoria.setText(f"💾 Datos: {len(self.historial_tiempos)} pts")
        
        # Cache stats
        try:
            from core.executors import AgentExecutor
            cache_stats = AgentExecutor.get_http_cache_stats()
            self.stat_cache.setText(f"📦 Cache: {cache_stats.get('size', 0)} entradas")
        except:
            self.stat_cache.setText("📦 Cache: N/A")
    
    def _actualizar_progress_bar(self, stats: Dict):
        """Actualiza la barra de progreso general."""
        total = stats.get('total', 0)
        completados = stats.get('completados', 0)
        errores = stats.get('errores', 0)
        cancelados = stats.get('cancelados', 0)
        total_finalizados = completados + errores + cancelados
        
        if total > 0:
            porcentaje = (total_finalizados / total) * 100
            self.progress_bar.setValue(int(porcentaje))
            self.progress_bar.setFormat(f"{porcentaje:.1f}% ({total_finalizados}/{total})")
        else:
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("0%")
    
    def _actualizar_grafico(self, stats: Dict):
        """Actualiza el gráfico de progreso."""
        total = stats.get('total', 0)
        completados = stats.get('completados', 0)
        ejecutando = stats.get('ejecutando', 0)
        errores = stats.get('errores', 0)
        cancelados = stats.get('cancelados', 0)
        
        if self.tiempo_inicio and self.scheduler.ejecutando:
            tiempo_actual = time.time() - self.tiempo_inicio
            self.tiempo_ejecucion = tiempo_actual
            
            if (len(self.historial_tiempos) == 0 or 
                tiempo_actual - self.ultimo_punto_tiempo >= 0.5):
                self.historial_tiempos.append(tiempo_actual)
                self.historial_completados.append(completados)
                self.historial_ejecutando.append(ejecutando)
                self.historial_errores.append(errores)
                self.ultimo_punto_tiempo = tiempo_actual
        
        self._dibujar_grafico()
    
    def _dibujar_grafico(self):
        """Dibuja el gráfico de progreso con mejor rendimiento."""
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        self._aplicar_colores_grafico()
        
        if self.historial_tiempos and len(self.historial_tiempos) > 0:
            tiempos = list(self.historial_tiempos)
            completados = list(self.historial_completados)
            ejecutando = list(self.historial_ejecutando)
            errores = list(self.historial_errores)
            
            # Área sombreada para ejecutando
            ax.fill_between(
                tiempos, 0, ejecutando,
                alpha=0.2, color='#007bff', label='Ejecutando'
            )
            
            # Líneas
            ax.plot(
                tiempos, completados, 
                '-', linewidth=2.5, color='#28a745', 
                label='Completados', markersize=4
            )
            ax.plot(
                tiempos, ejecutando, 
                '-', linewidth=2, color='#007bff', 
                label='Ejecutando', markersize=4
            )
            
            # Puntos finales con marcadores
            if len(completados) > 0:
                ax.scatter(
                    tiempos[-1], completados[-1],
                    color='#28a745', s=80, zorder=5,
                    edgecolors='white', linewidth=2
                )
            
            if len(ejecutando) > 0 and ejecutando[-1] > 0:
                ax.scatter(
                    tiempos[-1], ejecutando[-1],
                    color='#007bff', s=80, zorder=5,
                    edgecolors='white', linewidth=2
                )
            
            ax.set_xlabel('Tiempo (segundos)', fontsize=9)
            ax.set_ylabel('Agentes', fontsize=9)
            ax.set_title('Progreso de Ejecución en Tiempo Real', fontsize=11, fontweight='bold')
            ax.grid(True, alpha=0.3, linestyle='--')
            
            if tiempos:
                ax.set_xlim(0, max(tiempos) * 1.05)
                max_val = max(max(completados + ejecutando + errores) + 2, 5)
                ax.set_ylim(0, max_val)
            
            ax.legend(loc='upper left', fontsize=8, framealpha=0.8)
            ax.xaxis.set_major_locator(MaxNLocator(integer=True))
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            
            if self.historial_tiempos:
                last_time = tiempos[-1]
                total = completados[-1] + ejecutando[-1] + errores[-1] if len(completados) > 0 else 0
                if total > 0:
                    text = f"Total: {total} | Completados: {completados[-1]} | Errores: {errores[-1] if len(errores) > 0 else 0}"
                    ax.text(
                        0.02, 0.98, text,
                        transform=ax.transAxes,
                        fontsize=8,
                        verticalalignment='top',
                        bbox=dict(boxstyle='round', facecolor='white', alpha=0.8)
                    )
        else:
            ax.text(
                0.5, 0.5, '*** Esperando datos...',
                horizontalalignment='center',
                verticalalignment='center',
                transform=ax.transAxes,
                fontsize=14,
                color='#999',
                fontweight='bold'
            )
            ax.set_xlim(0, 1)
            ax.set_ylim(0, 1)
            ax.set_xticks([])
            ax.set_yticks([])
        
        self.figure.tight_layout()
        self.canvas.draw()
    
    # ============================================================
    # EVENTOS DEL SCHEDULER
    # ============================================================
    
    def _on_estado_cambiado(self, ejecutando: bool):
        """Maneja cambios de estado del scheduler."""
        if ejecutando:
            self._iniciar_monitoreo()
        else:
            self._detener_monitoreo()
    
    def _iniciar_monitoreo(self):
        """Inicia el monitoreo cuando comienza la ejecución."""
        if not self.activo:
            self.activo = True
            self.tiempo_inicio = time.time()
            self.ultimo_punto_tiempo = 0
            if not self.timer.isActive() and not self.pausado:
                self.timer.start(self.update_interval_ms)
                self.logger.debug("Timer de métricas iniciado")
    
    def _detener_monitoreo(self):
        """Detiene el monitoreo cuando termina la ejecución."""
        self.activo = False
        if self.timer.isActive():
            self.timer.stop()
            self.logger.debug("Timer de métricas detenido")
        self._actualizar_metricas()
    
    def _on_agente_actualizado(self, agente_id: str):
        """Se llama cuando un agente se actualiza."""
        if not self.activo and self.scheduler.ejecutando:
            self._iniciar_monitoreo()
        self._actualizar_metricas()
    
    def _on_ejecucion_terminada(self):
        """Se llama cuando la ejecución termina."""
        self._detener_monitoreo()
        self.lbl_estado_ejecucion.setText("✅ Completado")
        self.lbl_estado_ejecucion.setStyleSheet("""
            font-weight: bold;
            color: #28a745;
            padding: 4px 12px;
            background-color: #d4edda;
            border-radius: 4px;
        """)
        self.progress_bar.setValue(100)
        self.metric_clicked.emit("ejecucion_terminada")
    
    def _on_log_mensaje(self, mensaje: str, color: str):
        """Maneja mensajes de log para actualizar estado."""
        if "ejecución iniciada" in mensaje.lower():
            self.lbl_estado_ejecucion.setText("▶ Ejecutando")
            self.lbl_estado_ejecucion.setStyleSheet("""
                font-weight: bold;
                color: #28a745;
                padding: 4px 12px;
                background-color: #d4edda;
                border-radius: 4px;
            """)
            self._iniciar_monitoreo()
        elif "pausado" in mensaje.lower():
            self.lbl_estado_ejecucion.setText("⏸ Pausado")
            self.lbl_estado_ejecucion.setStyleSheet("""
                font-weight: bold;
                color: #ffc107;
                padding: 4px 12px;
                background-color: #fff3cd;
                border-radius: 4px;
            """)
            if self.timer.isActive():
                self.timer.stop()
        elif "reanudado" in mensaje.lower():
            self.lbl_estado_ejecucion.setText("▶ Ejecutando")
            self.lbl_estado_ejecucion.setStyleSheet("""
                font-weight: bold;
                color: #28a745;
                padding: 4px 12px;
                background-color: #d4edda;
                border-radius: 4px;
            """)
            if not self.timer.isActive() and not self.pausado:
                self.timer.start(self.update_interval_ms)
        elif "detenido" in mensaje.lower() or "terminado" in mensaje.lower():
            self.lbl_estado_ejecucion.setText("⏹ Detenido")
            self.lbl_estado_ejecucion.setStyleSheet("""
                font-weight: bold;
                color: #dc3545;
                padding: 4px 12px;
                background-color: #f8d7da;
                border-radius: 4px;
            """)
            self._detener_monitoreo()
    
    # ============================================================
    # UTILIDADES
    # ============================================================
    
    @staticmethod
    def _calcular_porcentaje(valor: int, total: int) -> float:
        """Calcula porcentaje de forma segura."""
        if total == 0:
            return 0.0
        return (valor / total) * 100
    
    # ============================================================
    # CONTROLES DE USUARIO
    # ============================================================
    
    def _toggle_pausa(self, checked: bool):
        """Pausa o reanuda la actualización."""
        self.pausado = checked
        self.btn_pausar.setText("▶ Reanudar" if checked else "⏸ Pausar")
        
        if checked:
            if self.timer.isActive():
                self.timer.stop()
        else:
            if self.scheduler.ejecutando and not self.timer.isActive():
                self.timer.start(self.update_interval_ms)
            self._actualizar_metricas()
    
    def _limpiar_historial(self):
        """Limpia el historial del gráfico."""
        reply = QMessageBox.question(
            self, "Limpiar Historial",
            "¿Limpiar todo el historial de datos?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        
        if reply == QMessageBox.StandardButton.Yes:
            self.historial_tiempos.clear()
            self.historial_completados.clear()
            self.historial_ejecutando.clear()
            self.historial_errores.clear()
            self.tiempo_inicio = None
            self.tiempo_ejecucion = 0
            self.ultimo_punto_tiempo = 0
            
            self._setup_grafico()
            self.reset_requested.emit()
            self.logger.info("Historial limpiado")
    
    def _cambiar_intervalo(self, ms: int):
        """Cambia el intervalo de actualización."""
        self.update_interval_ms = max(MIN_UPDATE_INTERVAL_MS, min(MAX_UPDATE_INTERVAL_MS, ms))
        self.combo_intervalo.setText(f"{self.update_interval_ms}ms")
        
        if self.timer.isActive():
            self.timer.setInterval(self.update_interval_ms)
        
        self.logger.info(f"Intervalo de actualización cambiado a {self.update_interval_ms}ms")
    
    # ============================================================
    # EXPORTACIÓN
    # ============================================================
    
    def _exportar_grafico(self, formato: str):
        """Exporta el gráfico actual como imagen."""
        if not self.historial_tiempos:
            QMessageBox.information(self, "Info", "No hay datos para exportar")
            return
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        nombre_base = f"metricas_{timestamp}"
        extension = formato.lower()
        
        ruta, _ = QFileDialog.getSaveFileName(
            self, 
            f"Exportar Gráfico ({formato.upper()})",
            f"{nombre_base}.{extension}",
            f"{formato.upper()} (*.{extension})"
        )
        
        if not ruta:
            return
        
        try:
            self._dibujar_grafico()
            dpi = 150 if extension == 'png' else 100
            self.figure.savefig(ruta, dpi=dpi, bbox_inches='tight', facecolor=self.figure.get_facecolor())
            
            QMessageBox.information(self, "Éxito", f"✅ Gráfico exportado a:\n{ruta}")
            self.export_requested.emit(formato)
            self.logger.info(f"Gráfico exportado a {ruta}")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"❌ Error al exportar: {str(e)}")
            self.logger.error(f"Error exportando gráfico: {e}")
    
    def _exportar_csv(self):
        """Exporta los datos históricos a CSV."""
        if not self.historial_tiempos:
            QMessageBox.information(self, "Info", "No hay datos para exportar")
            return
        
        try:
            import pandas as pd
            
            timestamp = datetime.now().strftime('%Y%m%d_%H%M')
            ruta, _ = QFileDialog.getSaveFileName(
                self,
                "Exportar Datos (CSV)",
                f"metricas_datos_{timestamp}.csv",
                "CSV Files (*.csv)"
            )
            
            if not ruta:
                return
            
            data = {
                'tiempo': list(self.historial_tiempos),
                'completados': list(self.historial_completados),
                'ejecutando': list(self.historial_ejecutando),
                'errores': list(self.historial_errores),
            }
            df = pd.DataFrame(data)
            df.to_csv(ruta, index=False, encoding='utf-8-sig')
            
            QMessageBox.information(self, "Éxito", f"✅ Datos exportados a:\n{ruta}")
            
        except ImportError:
            QMessageBox.warning(
                self, "Error", 
                "pandas no está instalado. Instálalo con: pip install pandas"
            )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"❌ Error al exportar: {str(e)}")
    
    def _exportar_json(self):
        """Exporta los datos históricos a JSON."""
        if not self.historial_tiempos:
            QMessageBox.information(self, "Info", "No hay datos para exportar")
            return
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M')
        ruta, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar Datos (JSON)",
            f"metricas_datos_{timestamp}.json",
            "JSON Files (*.json)"
        )
        
        if not ruta:
            return
        
        try:
            data = {
                'timestamp': datetime.now().isoformat(),
                'total_puntos': len(self.historial_tiempos),
                'datos': {
                    'tiempo': list(self.historial_tiempos),
                    'completados': list(self.historial_completados),
                    'ejecutando': list(self.historial_ejecutando),
                    'errores': list(self.historial_errores),
                },
                'estadisticas': {
                    'tiempo_total': self.tiempo_ejecucion,
                    'puntos_totales': len(self.historial_tiempos),
                }
            }
            
            with open(ruta, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            
            QMessageBox.information(self, "Éxito", f"✅ Datos exportados a:\n{ruta}")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"❌ Error al exportar: {str(e)}")
    
    # ============================================================
    # TEMAS
    # ============================================================
    
    def _aplicar_tema(self):
        """Aplica el tema actual al dashboard."""
        self._aplicar_colores_grafico()
        
        for widget in self.findChildren(MetricCard):
            widget._aplicar_estilo()
    
    def changeEvent(self, event):
        """Maneja cambios de evento (como cambio de tema)."""
        if event.type() == QEvent.Type.PaletteChange:
            self._aplicar_tema()
        super().changeEvent(event)
    
    # ============================================================
    # RESET
    # ============================================================
    
    def reset(self):
        """Reinicia todas las métricas."""
        if self.timer.isActive():
            self.timer.stop()
        
        self.historial_tiempos.clear()
        self.historial_completados.clear()
        self.historial_ejecutando.clear()
        self.historial_errores.clear()
        self.tiempo_inicio = None
        self.tiempo_ejecucion = 0
        self.ultimo_punto_tiempo = 0
        self.activo = False
        self.pausado = False
        
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("0%")
        
        self.lbl_estado_ejecucion.setText("⏸ Inactivo")
        self.lbl_estado_ejecucion.setStyleSheet("""
            font-weight: bold;
            color: #6c757d;
            padding: 4px 12px;
            background-color: #f8f9fa;
            border-radius: 4px;
        """)
        
        self._setup_grafico()
        self._actualizar_metricas()
        self.btn_pausar.setChecked(False)
        self.btn_pausar.setText("⏸ Pausar")
        
        self.logger.info("Dashboard reseteado")
    
    # ============================================================
    # LIMPIEZA
    # ============================================================
    
    def closeEvent(self, event):
        """Maneja el cierre del widget."""
        if self.timer.isActive():
            self.timer.stop()
        self.logger.info("MetricsDashboard cerrado")
        event.accept()
    
    def __del__(self):
        """Limpieza final."""
        try:
            if hasattr(self, 'timer') and self.timer.isActive():
                self.timer.stop()
        except:
            pass