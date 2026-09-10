# ui/graph_view.py - VERSIÓN REFACTORIZADA Y COMPLETA
"""
Vista de gráfico para el visualizador de agentes con layout automático.

CARACTERÍSTICAS:
- Layout automático por niveles (orden topológico)
- Soporte para detección y visualización de ciclos
- Zoom y pan interactivos
- Tooltips con información detallada
- Colores por estado del agente
- ✅ Animaciones solo cuando hay nodos animados
- ✅ Corregido bug de closure en lambdas
- Exportación a imagen
- Soporte para temas oscuros/claros
- Layout circular opcional
- Filtrado por estado
- Navegación por teclado
"""

import math
import time
import logging
from typing import Dict, List, Tuple, Optional, Set, Any
from collections import deque
from enum import Enum

from PyQt6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem,
    QGraphicsTextItem, QGraphicsLineItem, QGraphicsPathItem,
    QGraphicsItem, QGraphicsRectItem, QGraphicsPolygonItem,
    QToolTip, QMenu, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QCheckBox, QFileDialog,
    QMessageBox, QSlider, QSpinBox
)
from PyQt6.QtCore import (
    Qt, QRectF, QPointF, QTimer, pyqtSignal, QEvent,
    QPropertyAnimation, QEasingCurve, QPoint
)
from PyQt6.QtGui import (
    QPainter, QPen, QBrush, QColor, QFont, QPainterPath,
    QAction, QKeySequence, QTransform, QWheelEvent,
    QMouseEvent, QContextMenuEvent, QPixmap, QImage, QPalette
)

from core.agent import Agente, EstadoAgente, TipoAgente

# Configurar logger
logger = logging.getLogger(__name__)

# ============================================================
# CONSTANTES
# ============================================================

SCENE_SIZE = 4000
GRID_STEP = 50
NODE_RADIUS = 30
MIN_NODE_RADIUS = 15
MAX_NODE_RADIUS = 50
X_SPACING = 180
Y_SPACING = 110
START_X = 150
START_Y = 300
ANIMATION_DURATION = 300  # ms

# Colores base por estado (se ajustan según tema)
#
# ui/graph_view.py - ACTUALIZAR COLORES_ESTADO

COLORES_ESTADO = {
    EstadoAgente.PENDIENTE:  QColor(108, 117, 125),
    EstadoAgente.EN_COLA:    QColor(108, 117, 125),
    EstadoAgente.ESPERANDO:  QColor(253, 126, 20),
    EstadoAgente.LISTO:      QColor(40, 167, 69),
    EstadoAgente.EJECUTANDO: QColor(0, 123, 255),
    EstadoAgente.REINTENTANDO: QColor(255, 193, 7),
    EstadoAgente.COMPLETADO: QColor(40, 167, 69),
    EstadoAgente.ERROR:      QColor(220, 53, 69),
    EstadoAgente.TIMEOUT:    QColor(220, 53, 69),
    EstadoAgente.CANCELADO:  QColor(108, 117, 125),
    EstadoAgente.SALTADO:    QColor(108, 117, 125),
    EstadoAgente.BLOQUEADO:  QColor(139, 0, 0),
}

COLOR_DEFECTO = QColor(150, 150, 150)

# Colores para tipos de agentes (badges)
COLORES_TIPO = {
    TipoAgente.PYTHON: "#6f42c1",
    TipoAgente.SHELL: "#28a745",
    TipoAgente.LLM: "#17a2b8",
    TipoAgente.HTTP: "#fd7e14",
    TipoAgente.FILE: "#20c997",
    TipoAgente.LOOP: "#6f42c1",
}


class LayoutMode(Enum):
    """Modos de layout para el grafo."""
    TOPOLOGICAL = "topological"
    CIRCULAR = "circular"
    GRID = "grid"
    SPRING = "spring"


# ============================================================
# ITEMS DEL GRAFO
# ============================================================

class GraphNodeItem(QGraphicsEllipseItem):
    """
    Nodo del grafo con soporte para hover, selección y animaciones.
    """
    
    def __init__(
        self,
        agente: Agente,
        x: float,
        y: float,
        radius: float = NODE_RADIUS,
        parent=None,
        on_double_click: Optional[callable] = None  # ✅ Callback para doble click
    ):
        super().__init__(-radius, -radius, radius * 2, radius * 2, parent)
        
        self.agente = agente
        self.radius = radius
        self._is_hover = False
        self._is_selected = False
        self._tooltip_visible = False
        self._animation = None
        self._on_double_click = on_double_click  # ✅ Almacenar callback
        self.agente_id = agente.id   # <--- Línea nueva
        
        # Configurar
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges, True)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(2)
        
        # Posición
        self.setPos(x, y)
        
        # Texto del nombre
        self.text_item = QGraphicsTextItem(agente.nombre, self)
        self.text_item.setDefaultTextColor(QColor(255, 255, 255))
        self.text_item.setFont(QFont("Arial", 9, QFont.Weight.Bold))
        self.text_item.setZValue(3)
        self._centrar_texto()
        
        # Badge para tipo Loop
        self.badge_item = None
        if agente.tipo == TipoAgente.LOOP:
            self.badge_item = QGraphicsTextItem("🔄", self)
            self.badge_item.setFont(QFont("Segoe UI Emoji", 11))
            self.badge_item.setZValue(4)
            self.badge_item.setPos(radius * 0.7, -radius * 0.9)
        
        # Estado inicial
        self.actualizar_estado()
    
    def _centrar_texto(self):
        """Centra el texto en el nodo."""
        if not hasattr(self, 'text_item') or self.text_item is None:
            return
        rect = self.text_item.boundingRect()
        rect = self.text_item.boundingRect()
        self.text_item.setPos(
            -rect.width() / 2,
            -rect.height() / 2
        )
    
    def actualizar_estado(self):
        """Actualiza el color del nodo según el estado."""
        color = COLORES_ESTADO.get(self.agente.estado, COLOR_DEFECTO)
        
        # Ajustar brillo para tema oscuro
        if self._is_selected:
            color = color.lighter(130)
        
        self.setBrush(QBrush(color))
        self.setPen(QPen(color.darker(120), 2))
        
        # Actualizar tooltip
        self.setToolTip(self.agente.obtener_info_tooltip())
        
        # Actualizar badge de estado (pequeño indicador)
        self._actualizar_badge_estado()
    
    def _actualizar_badge_estado(self):
        """Añade un badge de estado en la esquina superior derecha."""
        # Eliminar badge existente
        for child in self.childItems():
            if isinstance(child, QGraphicsEllipseItem) and child.pos().x() > 0:
                self.scene().removeItem(child)
        
        # Badge de estado (pequeño círculo)
        badge = QGraphicsEllipseItem(
            self.radius * 0.6, -self.radius * 0.6,
            self.radius * 0.4, self.radius * 0.4,
            self
        )
        badge.setBrush(QBrush(QColor(255, 255, 255)))
        badge.setPen(QPen(Qt.PenStyle.NoPen))
        badge.setZValue(5)
        
        # Color interior según estado
        color = COLORES_ESTADO.get(self.agente.estado, COLOR_DEFECTO)
        inner = QGraphicsEllipseItem(
            self.radius * 0.65, -self.radius * 0.55,
            self.radius * 0.3, self.radius * 0.3,
            badge
        )
        inner.setBrush(QBrush(color))
        inner.setPen(QPen(Qt.PenStyle.NoPen))
        inner.setZValue(6)
    
    def hoverEnterEvent(self, event):
        """Maneja entrada del mouse."""
        self._is_hover = True
        self.setScale(1.1)
        self._tooltip_visible = True
        QToolTip.showText(
            event.screenPos(),
            self.agente.obtener_info_tooltip()
        )
        super().hoverEnterEvent(event)
    
    def hoverLeaveEvent(self, event):
        """Maneja salida del mouse."""
        self._is_hover = False
        self.setScale(1.0)
        self._tooltip_visible = False
        QToolTip.hideText()
        super().hoverLeaveEvent(event)
    
    def mouseDoubleClickEvent(self, event):
        """Maneja doble click (zoom al nodo)."""
        # ✅ Usar callback almacenado en lugar de lambda con closure problemático
        if self._on_double_click:
            self._on_double_click(self.agente.id)
        elif self.scene():
            # Fallback: zoom al nodo
            view = self.scene().views()[0] if self.scene().views() else None
            if view:
                view.zoom_to_node(self)
        super().mouseDoubleClickEvent(event)
    
    def itemChange(self, change, value):
        """Maneja cambios en el item."""
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self._centrar_texto()
        return super().itemChange(change, value)


class GraphEdgeItem(QGraphicsPathItem):
    """
    Arista del grafo con flecha y soporte para hover.
    """
    
    def __init__(
        self,
        source: GraphNodeItem,
        target: GraphNodeItem,
        parent=None,
        on_double_click: Optional[callable] = None  # ✅ Callback para doble click
    ):
        super().__init__(parent)
        
        self.source = source
        self.target = target
        self._is_hover = False
        self._on_double_click = on_double_click  # ✅ Almacenar callback
        
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.setZValue(1)
        
        # Dibujar
        self._actualizar_geometria()
    
    def _actualizar_geometria(self):
        """Actualiza la geometría de la arista."""
        # Obtener posiciones de los nodos
        p1 = self.source.scenePos()
        p2 = self.target.scenePos()
        
        # Calcular vector y distancia
        dx = p2.x() - p1.x()
        dy = p2.y() - p1.y()
        length = math.sqrt(dx * dx + dy * dy)
        
        if length < 1:
            self.setPath(QPainterPath())
            return
        
        # Normalizar
        ux, uy = dx / length, dy / length
        
        # Puntos en el borde de los nodos
        radius = self.source.radius
        start_x = p1.x() + ux * radius
        start_y = p1.y() + uy * radius
        
        radius2 = self.target.radius
        end_x = p2.x() - ux * radius2
        end_y = p2.y() - uy * radius2
        
        # Crear path
        path = QPainterPath()
        path.moveTo(start_x, start_y)
        path.lineTo(end_x, end_y)
        
        # Flecha
        arrow_size = 12
        bx = end_x - ux * arrow_size
        by = end_y - uy * arrow_size
        px, py = -uy, ux
        
        path.moveTo(end_x, end_y)
        path.lineTo(bx + px * 5, by + py * 5)
        path.lineTo(bx - px * 5, by - py * 5)
        path.closeSubpath()
        
        self.setPath(path)
        
        # Estilo
        color = QColor(80, 80, 80)
        if self._is_hover:
            color = QColor(0, 123, 255)
            self.setPen(QPen(color, 3))
            self.setBrush(QBrush(color))
        else:
            self.setPen(QPen(color, 2))
            self.setBrush(QBrush(color))
    
    def hoverEnterEvent(self, event):
        """Maneja entrada del mouse."""
        self._is_hover = True
        self._actualizar_geometria()
        self.setToolTip(f"{self.source.agente.nombre} → {self.target.agente.nombre}")
        super().hoverEnterEvent(event)
    
    def hoverLeaveEvent(self, event):
        """Maneja salida del mouse."""
        self._is_hover = False
        self._actualizar_geometria()
        super().hoverLeaveEvent(event)
    
    def mouseDoubleClickEvent(self, event):
        """Maneja doble click (eliminar conexión)."""
        if self._on_double_click:
            self._on_double_click(self.source.agente.id, self.target.agente.id)
        super().mouseDoubleClickEvent(event)


# ============================================================
# CLASE PRINCIPAL: GRAPH VIEW
# ============================================================

class GraphView(QGraphicsView):
    """
    Vista de gráfico con layout automático y soporte para interacción.
    """
    
    # Señales
    node_clicked = pyqtSignal(str)  # agente_id
    node_double_clicked = pyqtSignal(str)  # agente_id
    selection_changed = pyqtSignal(list)  # lista de IDs seleccionados
    edge_double_clicked = pyqtSignal(str, str)  # origen, destino
    eliminar_agente_solicitado = pyqtSignal(str)   # <-- AÑADIR ESTA LÍNEA
    
    def __init__(self, scheduler=None, parent=None):
        super().__init__(parent)

        self.scheduler = scheduler   # <-- GUARDAR REFERENCIA
        
        self.logger = logging.getLogger(f"{__name__}.GraphView")
        
        # ── Escena ──
        self._scene = QGraphicsScene(self)
        self._scene.setSceneRect(0, 0, SCENE_SIZE, SCENE_SIZE)
        self.setScene(self._scene)
        
        # ── Configuración de vista ──
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        
        # ── Estado ──
        self._nodes: Dict[str, GraphNodeItem] = {}
        self._edges: List[GraphEdgeItem] = []
        self._node_positions: Dict[str, Tuple[float, float]] = {}
        
        self._is_dragging = False
        self._drag_start = QPointF()
        self._selected_nodes: Set[str] = set()
        
        # ── Layout ──
        self._layout_mode = LayoutMode.TOPOLOGICAL
        self._show_grid = True
        self._show_edge_labels = False
        self._zoom_level = 1.0
        self._min_zoom = 0.1
        self._max_zoom = 5.0
        
        # ── Tema ──
        self._dark_theme = False
        self._aplicar_tema()
        
        # ── Configurar fondo ──
        self._setup_background()
        
        # ── Menú contextual ──
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._mostrar_menu_contextual)
        
        # ── Timer para animaciones (INICIADO SOLO CUANDO HAY ANIMACIONES) ──
        self._animation_timer = QTimer()
        self._animation_timer.timeout.connect(self._actualizar_animaciones)
        # ✅ NO iniciar el timer automáticamente
        
        # ── Cache de nodos para animaciones ──
        self._animating_nodes: Dict[str, Tuple[QPointF, QPointF, float]] = {}
        self._animation_count = 0
        
        # ── Cache de agentes para layout ──
        self._agentes_cache: Dict[str, Agente] = {}
        
        self.logger.info("GraphView inicializado")
    
    # ============================================================
    # CONFIGURACIÓN DEL FONDO
    # ============================================================
    
    def _setup_background(self):
        """Configura el fondo y la cuadrícula."""
        # Fondo
        self.setBackgroundBrush(
            QBrush(QColor(255, 255, 255) if not self._dark_theme else QColor(30, 30, 30))
        )
        
        # Cuadrícula
        self._actualizar_grid()
    
    def _actualizar_grid(self):
        """Actualiza la cuadrícula."""
        # Limpiar grid anterior (los items con datos de usuario 'grid')
        for item in self._scene.items():
            if item.data(0) == "grid":
                self._scene.removeItem(item)
        
        if not self._show_grid:
            return
        
        # Color de la cuadrícula
        color = QColor(200, 200, 200, 80) if not self._dark_theme else QColor(60, 60, 60, 80)
        pen = QPen(color)
        pen.setStyle(Qt.PenStyle.DashLine)
        
        size = SCENE_SIZE
        step = GRID_STEP
        
        # Líneas verticales
        for x in range(0, size, step):
            line = self._scene.addLine(x, 0, x, size, pen)
            line.setData(0, "grid")
        
        # Líneas horizontales
        for y in range(0, size, step):
            line = self._scene.addLine(0, y, size, y, pen)
            line.setData(0, "grid")
        
        # Centro (más visible)
        center_pen = QPen(color.lighter(150) if not self._dark_theme else color.lighter(150), 1.5)
        center_pen.setStyle(Qt.PenStyle.SolidLine)
        cx, cy = size // 2, size // 2
        self._scene.addLine(cx - 50, cy, cx + 50, cy, center_pen).setData(0, "grid")
        self._scene.addLine(cx, cy - 50, cx, cy + 50, center_pen).setData(0, "grid")
    
    # ============================================================
    # TEMAS
    # ============================================================
    
    def _aplicar_tema(self):
        """Aplica el tema actual."""
        # Detectar tema del palette
        palette = self.palette()
        bg = palette.color(QPalette.ColorRole.Window)
        self._dark_theme = bg.lightness() < 128
        
        # Actualizar colores
        self._setup_background()
        
        # Actualizar nodos
        for node in self._nodes.values():
            node.actualizar_estado()
        
        # Actualizar aristas
        for edge in self._edges:
            edge._actualizar_geometria()
    
    def set_dark_theme(self, enabled: bool):
        """Establece el tema oscuro."""
        self._dark_theme = enabled
        self._aplicar_tema()
    
    # ============================================================
    # LAYOUT
    # ============================================================
    
    def _calcular_layout_topologico(self, agentes: Dict) -> Dict[str, Tuple[float, float]]:
        """
        Calcula layout por niveles topológicos (ordenamiento topológico).
        """
        if not agentes:
            return {}
        
        # Calcular niveles
        niveles = {aid: 0 for aid in agentes}
        indeg = {aid: 0 for aid in agentes}
        dependientes: Dict[str, List[str]] = {aid: [] for aid in agentes}
        
        for aid, agente in agentes.items():
            for dep in getattr(agente, "dependencias_ids", None) or []:
                if dep in agentes:
                    indeg[aid] += 1
                    dependientes[dep].append(aid)
        
        # BFS para orden topológico
        cola = deque(aid for aid, g in indeg.items() if g == 0)
        procesados = 0
        
        while cola:
            aid = cola.popleft()
            procesados += 1
            for hijo in dependientes[aid]:
                niveles[hijo] = max(niveles[hijo], niveles[aid] + 1)
                indeg[hijo] -= 1
                if indeg[hijo] == 0:
                    cola.append(hijo)
        
        # Si hay ciclos, agrupar en nivel 0
        if procesados < len(agentes):
            for aid, g in indeg.items():
                if g > 0:
                    niveles[aid] = 0
        
        # Distribuir nodos por nivel
        por_nivel: Dict[int, List[str]] = {}
        for aid, n in niveles.items():
            por_nivel.setdefault(n, []).append(aid)
        
        positions = {}
        total_width = max(por_nivel.keys()) * X_SPACING if por_nivel else 0
        
        for n, ids in sorted(por_nivel.items()):
            total = len(ids)
            x = START_X + n * X_SPACING
            
            # Centrar verticalmente
            for i, aid in enumerate(ids):
                y = START_Y + i * Y_SPACING - (total - 1) * Y_SPACING / 2
                positions[aid] = (x, y)
        
        return positions
    
    def _calcular_layout_circular(self, agentes: Dict) -> Dict[str, Tuple[float, float]]:
        """
        Calcula layout circular.
        """
        if not agentes:
            return {}
        
        n = len(agentes)
        radius = min(SCENE_SIZE // 3, 200 + n * 10)
        center_x = SCENE_SIZE // 2
        center_y = SCENE_SIZE // 2
        
        positions = {}
        for i, aid in enumerate(agentes.keys()):
            angle = 2 * math.pi * i / n - math.pi / 2
            x = center_x + radius * math.cos(angle)
            y = center_y + radius * math.sin(angle)
            positions[aid] = (x, y)
        
        return positions
    
    def _calcular_layout_grid(self, agentes: Dict) -> Dict[str, Tuple[float, float]]:
        """
        Calcula layout en cuadrícula.
        """
        if not agentes:
            return {}
        
        n = len(agentes)
        cols = int(math.ceil(math.sqrt(n)))
        rows = int(math.ceil(n / cols))
        
        cell_width = 250
        cell_height = 200
        
        start_x = (SCENE_SIZE - cols * cell_width) / 2
        start_y = (SCENE_SIZE - rows * cell_height) / 2
        
        positions = {}
        for i, aid in enumerate(agentes.keys()):
            row = i // cols
            col = i % cols
            x = start_x + col * cell_width + cell_width / 2
            y = start_y + row * cell_height + cell_height / 2
            positions[aid] = (x, y)
        
        return positions
    
    def _calcular_layout_spring(self, agentes: Dict) -> Dict[str, Tuple[float, float]]:
        """
        Calcula layout tipo resorte (force-directed) simplificado.
        """
        if not agentes:
            return {}
        
        # Iniciar con layout circular
        positions = self._calcular_layout_circular(agentes)
        
        if len(agentes) < 2:
            return positions
        
        # Iteraciones
        iterations = 50
        k = 150  # Constante de resorte
        repulsion = 200
        
        aids = list(agentes.keys())
        
        for _ in range(iterations):
            forces: Dict[str, Tuple[float, float]] = {aid: (0, 0) for aid in aids}
            
            # Fuerzas de repulsión entre todos los nodos
            for i in range(len(aids)):
                for j in range(i + 1, len(aids)):
                    a1, a2 = aids[i], aids[j]
                    x1, y1 = positions[a1]
                    x2, y2 = positions[a2]
                    
                    dx = x2 - x1
                    dy = y2 - y1
                    dist = math.sqrt(dx * dx + dy * dy) + 0.01
                    
                    # Fuerza de repulsión
                    force = repulsion / (dist * dist + 0.01)
                    fx = force * dx / dist
                    fy = force * dy / dist
                    
                    forces[a1] = (forces[a1][0] - fx, forces[a1][1] - fy)
                    forces[a2] = (forces[a2][0] + fx, forces[a2][1] + fy)
            
            # Fuerzas de atracción para dependencias
            for aid, agente in agentes.items():
                for dep_id in getattr(agente, "dependencias_ids", None) or []:
                    if dep_id not in positions:
                        continue
                    
                    x1, y1 = positions[aid]
                    x2, y2 = positions[dep_id]
                    
                    dx = x2 - x1
                    dy = y2 - y1
                    dist = math.sqrt(dx * dx + dy * dy) + 0.01
                    
                    # Fuerza de atracción
                    force = k * math.log(dist + 1) / (dist + 1)
                    fx = force * dx / dist
                    fy = force * dy / dist
                    
                    forces[aid] = (forces[aid][0] + fx, forces[aid][1] + fy)
                    forces[dep_id] = (forces[dep_id][0] - fx, forces[dep_id][1] - fy)
            
            # Aplicar fuerzas
            for aid in aids:
                fx, fy = forces[aid]
                x, y = positions[aid]
                # Limitar movimiento
                max_move = 20
                fx = max(-max_move, min(max_move, fx))
                fy = max(-max_move, min(max_move, fy))
                positions[aid] = (x + fx, y + fy)
            
            # Centrar
            cx = sum(p[0] for p in positions.values()) / len(positions)
            cy = sum(p[1] for p in positions.values()) / len(positions)
            for aid in aids:
                x, y = positions[aid]
                positions[aid] = (x - cx + SCENE_SIZE // 2, y - cy + SCENE_SIZE // 2)
        
        return positions
    
    # ============================================================
    # ACTUALIZACIÓN DEL GRAFO
    # ============================================================
    
    def actualizar_grafo(
        self,
        agentes: Dict[str, Agente],
        recalcular_layout: bool = False,
        layout_mode: Optional[LayoutMode] = None
    ):
        """
        Actualiza el grafo con los agentes actuales.
        
        Args:
            agentes: Diccionario de agentes por ID
            recalcular_layout: Si se debe recalcular el layout
            layout_mode: Modo de layout (opcional)
        """
        if layout_mode:
            self._layout_mode = layout_mode
        
        # Guardar cache de agentes para layout
        self._agentes_cache = agentes
        
        ids_actuales = set(agentes.keys())
        
        # ── Eliminar nodos que ya no existen ──
        for aid in list(self._nodes.keys()):
            if aid not in ids_actuales:
                node = self._nodes.pop(aid)
                self._scene.removeItem(node)
        
        # ── Eliminar aristas (se recrean) ──
        for edge in self._edges:
            self._scene.removeItem(edge)
        self._edges.clear()
        
        # ── Actualizar posiciones ──
        if recalcular_layout or not self._node_positions:
            self._node_positions = self._calcular_layout(agentes)
        else:
            # Añadir nuevos nodos
            nuevos = [aid for aid in agentes if aid not in self._node_positions]
            if nuevos:
                # Posicionar nuevos nodos alrededor del centro
                max_x = max((p[0] for p in self._node_positions.values()), default=100)
                max_y = max((p[1] for p in self._node_positions.values()), default=100)
                for i, aid in enumerate(nuevos):
                    self._node_positions[aid] = (
                        max_x + 250 * ((i + 1) % 5),
                        max_y + 200 * ((i + 1) // 5)
                    )
        
        # ── Crear/actualizar nodos ──
        for aid, agente in agentes.items():
            if aid not in self._node_positions:
                continue
            
            x, y = self._node_positions[aid]
            
            if aid in self._nodes:
                node = self._nodes[aid]
                node.setPos(x, y)
                node.agente = agente
                node.actualizar_estado()
            else:
                # ✅ CORREGIDO: Crear nodo con callback en lugar de lambda con closure
                node = GraphNodeItem(
                    agente, x, y,
                    on_double_click=self._on_node_double_clicked  # ✅ Usar método en lugar de lambda
                )
                self._scene.addItem(node)
                self._nodes[aid] = node
                
                # Conectar eventos
                node.mousePressEvent = lambda e, a=aid: self._on_node_clicked(a, e)
        
        # ── Crear aristas ──
        for aid, agente in agentes.items():
            if aid not in self._nodes:
                continue
            
            for dep_id in getattr(agente, "dependencias_ids", None) or []:
                if dep_id in self._nodes and dep_id != aid:
                    # Verificar que no haya ya una arista
                    edge_exists = any(
                        e.source == self._nodes[dep_id] and e.target == self._nodes[aid]
                        for e in self._edges
                    )
                    if not edge_exists:
                        edge = GraphEdgeItem(
                            self._nodes[dep_id], 
                            self._nodes[aid],
                            on_double_click=self._on_edge_double_clicked  # ✅ Usar método
                        )
                        self._scene.addItem(edge)
                        self._edges.append(edge)
        
        # ── Ajustar vista ──
        if recalcular_layout:
            self._ajustar_vista()
        
        # ✅ Detener animaciones si no hay nodos
        if not self._nodes and self._animation_timer.isActive():
            self._animation_timer.stop()
            self.logger.debug("Timer de animaciones detenido (sin nodos)")
        
        self.logger.debug(f"Grafo actualizado: {len(self._nodes)} nodos, {len(self._edges)} aristas")
    
    # ============================================================
    # MANEJADORES DE EVENTOS (CORREGIDOS)
    # ============================================================
    
    def _on_node_clicked(self, aid: str, event):
        """Maneja click en un nodo."""
        self.node_clicked.emit(aid)
    
    def _on_node_double_clicked(self, aid: str):
        """Maneja doble click en un nodo."""
        self.node_double_clicked.emit(aid)
        # Zoom al nodo
        if aid in self._nodes:
            self.zoom_to_node(self._nodes[aid])
    
    def _on_edge_double_clicked(self, origen: str, destino: str):
        """Maneja doble click en una arista."""
        self.edge_double_clicked.emit(origen, destino)
        self.logger.debug(f"Edge double-clicked: {origen} → {destino}")
    
    def _calcular_layout(self, agentes: Dict) -> Dict[str, Tuple[float, float]]:
        """Calcula el layout según el modo seleccionado."""
        if self._layout_mode == LayoutMode.CIRCULAR:
            return self._calcular_layout_circular(agentes)
        elif self._layout_mode == LayoutMode.GRID:
            return self._calcular_layout_grid(agentes)
        elif self._layout_mode == LayoutMode.SPRING:
            return self._calcular_layout_spring(agentes)
        else:  # TOPOLOGICAL
            return self._calcular_layout_topologico(agentes)
    
    def _ajustar_vista(self):
        """Ajusta la vista para mostrar todos los nodos."""
        if not self._nodes:
            return
        
        # Calcular bounding box de todos los nodos
        rect = QRectF()
        for node in self._nodes.values():
            pos = node.scenePos()
            r = node.radius
            rect = rect.united(QRectF(pos.x() - r, pos.y() - r, r * 2, r * 2))
        
        # Añadir margen
        margin = 100
        rect = rect.adjusted(-margin, -margin, margin, margin)
        
        # Ajustar vista
        self.fitInView(rect, Qt.AspectRatioMode.KeepAspectRatio)
        self._zoom_level = self.transform().m11()
    
    # ============================================================
    # EVENTOS DE MOUSE Y TECLADO
    # ============================================================
    
    def wheelEvent(self, event: QWheelEvent):
        """Maneja la rueda del mouse para zoom."""
        zoom_in_factor = 1.15
        zoom_out_factor = 1 / zoom_in_factor
        
        factor = zoom_in_factor if event.angleDelta().y() > 0 else zoom_out_factor
        
        new_zoom = self._zoom_level * factor
        if self._min_zoom <= new_zoom <= self._max_zoom:
            self.scale(factor, factor)
            self._zoom_level = new_zoom
    
    def mousePressEvent(self, event: QMouseEvent):
        """Maneja presión del mouse."""
        if event.button() == Qt.MouseButton.MiddleButton:
            self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
            self._is_dragging = True
            self._drag_start = event.position()
            super().mousePressEvent(event)
        elif event.button() == Qt.MouseButton.LeftButton:
            # Selección
            item = self.itemAt(event.pos())
            if isinstance(item, GraphNodeItem):
                if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                    # Toggle selección
                    if item.agente.id in self._selected_nodes:
                        self._selected_nodes.remove(item.agente.id)
                    else:
                        self._selected_nodes.add(item.agente.id)
                else:
                    # Selección única
                    self._selected_nodes.clear()
                    self._selected_nodes.add(item.agente.id)
                self.selection_changed.emit(list(self._selected_nodes))
                self._actualizar_seleccion()
            else:
                # Deseleccionar
                self._selected_nodes.clear()
                self.selection_changed.emit([])
                self._actualizar_seleccion()
            
            super().mousePressEvent(event)
        else:
            super().mousePressEvent(event)
    
    def mouseReleaseEvent(self, event: QMouseEvent):
        """Maneja liberación del mouse."""
        if self._is_dragging:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
            self._is_dragging = False
        super().mouseReleaseEvent(event)
    
    def keyPressEvent(self, event):
        """Maneja eventos de teclado."""
        if event.key() == Qt.Key.Key_Escape:
            self._selected_nodes.clear()
            self.selection_changed.emit([])
            self._actualizar_seleccion()
        elif event.key() == Qt.Key.Key_F:
            self._ajustar_vista()
        elif event.key() == Qt.Key.Key_Delete or event.key() == Qt.Key.Key_Backspace:
            # Eliminar nodos seleccionados (solo si hay callback)
            if self._selected_nodes:
                self.logger.info(f"Eliminar nodos seleccionados: {self._selected_nodes}")
        elif event.key() == Qt.Key.Key_Plus or event.key() == Qt.Key.Key_Equal:
            self.zoom_in()
        elif event.key() == Qt.Key.Key_Minus:
            self.zoom_out()
        elif event.key() == Qt.Key.Key_0:
            self.reset_zoom()
        else:
            super().keyPressEvent(event)
    
    def _actualizar_seleccion(self):
        """Actualiza la apariencia de los nodos seleccionados."""
        for aid, node in self._nodes.items():
            node._is_selected = aid in self._selected_nodes
            node.actualizar_estado()
    
    # ============================================================
    # ZOOM
    # ============================================================
    
    def zoom_in(self):
        """Aplica zoom in."""
        factor = 1.15
        new_zoom = self._zoom_level * factor
        if new_zoom <= self._max_zoom:
            self.scale(factor, factor)
            self._zoom_level = new_zoom
    
    def zoom_out(self):
        """Aplica zoom out."""
        factor = 1 / 1.15
        new_zoom = self._zoom_level * factor
        if new_zoom >= self._min_zoom:
            self.scale(factor, factor)
            self._zoom_level = new_zoom
    
    def reset_zoom(self):
        """Resetea el zoom a 1.0."""
        self.resetTransform()
        self._zoom_level = 1.0
    
    def zoom_to_node(self, node: GraphNodeItem):
        """Zoom a un nodo específico."""
        self.fitInView(node, Qt.AspectRatioMode.KeepAspectRatio)
        self._zoom_level = self.transform().m11()
    
    # ============================================================
    # ANIMACIONES (CON TIMER BAJO DEMANDA)
    # ============================================================
    
    def _actualizar_animaciones(self):
        """Actualiza las animaciones de nodos."""
        if not self._animating_nodes:
            # ✅ Detener timer si no hay animaciones
            if self._animation_timer.isActive():
                self._animation_timer.stop()
                self.logger.debug("Timer de animaciones detenido (sin animaciones activas)")
            return
        
        # Limpiar animaciones completadas
        to_remove = []
        for aid, (start, end, progress) in self._animating_nodes.items():
            progress += 0.05
            if progress >= 1.0:
                progress = 1.0
                to_remove.append(aid)
            
            if aid in self._nodes:
                x = start.x() + (end.x() - start.x()) * progress
                y = start.y() + (end.y() - start.y()) * progress
                self._nodes[aid].setPos(x, y)
                self._animating_nodes[aid] = (start, end, progress)
        
        for aid in to_remove:
            self._animating_nodes.pop(aid, None)
        
        # ✅ Detener timer si no quedan animaciones
        if not self._animating_nodes and self._animation_timer.isActive():
            self._animation_timer.stop()
            self.logger.debug("Timer de animaciones detenido (todas las animaciones completadas)")
    
    def animar_nodo(self, aid: str, posicion: QPointF):
        """Anima un nodo a una nueva posición."""
        if aid not in self._nodes:
            return
        
        node = self._nodes[aid]
        start = node.scenePos()
        self._animating_nodes[aid] = (start, posicion, 0.0)
        
        # ✅ Iniciar timer si no está activo
        if not self._animation_timer.isActive():
            self._animation_timer.start(16)  # ~60 FPS
            self.logger.debug("Timer de animaciones iniciado")
    
    def animar_nodos(self, posiciones: Dict[str, QPointF]):
        """Anima múltiples nodos a nuevas posiciones."""
        for aid, pos in posiciones.items():
            if aid in self._nodes:
                node = self._nodes[aid]
                start = node.scenePos()
                self._animating_nodes[aid] = (start, pos, 0.0)
        
        # ✅ Iniciar timer si no está activo
        if self._animating_nodes and not self._animation_timer.isActive():
            self._animation_timer.start(16)
            self.logger.debug(f"Timer de animaciones iniciado ({len(self._animating_nodes)} nodos)")
    
    # ============================================================
    # MENÚ CONTEXTUAL
    # ============================================================
    
    def _mostrar_menu_contextual(self, pos: QPoint):
        menu = QMenu()

        # Acciones de zoom...
        zoom_in_action = QAction("🔍 Zoom In", self)
        zoom_in_action.triggered.connect(self.zoom_in)
        menu.addAction(zoom_in_action)

        zoom_out_action = QAction("🔍 Zoom Out", self)
        zoom_out_action.triggered.connect(self.zoom_out)
        menu.addAction(zoom_out_action)

        reset_zoom_action = QAction("🔄 Reset Zoom", self)
        reset_zoom_action.triggered.connect(self.reset_zoom)
        menu.addAction(reset_zoom_action)

        fit_action = QAction("📐 Ajustar Vista", self)
        fit_action.triggered.connect(self._ajustar_vista)
        menu.addAction(fit_action)

        # Detectar nodo
        item = self.itemAt(pos)
        agente_id = None
        if isinstance(item, GraphNodeItem):
            agente_id = item.agente_id   # directo desde el atributo
        if item:
            agente_id = getattr(item, 'agente_id', None)
            if agente_id is None:
                agente_id = item.data(0)

        # Si hay agente y scheduler disponible, añadir opción eliminar
        if agente_id and self.scheduler:
            agente = self.scheduler.obtener_agente(agente_id)
            if agente:
                menu.addSeparator()
                
                # Mostrar estado del agente
                emoji = agente.obtener_emoji_estado()
                estado_text = f"📊 {emoji} {agente.estado.value}"
                estado_action = QAction(estado_text, self)
                estado_action.setEnabled(False)
                menu.addAction(estado_action)
                
                # Opción eliminar (deshabilitada si está en ejecución o bloqueado)
                eliminar_action = QAction("🗑 Eliminar Agente", self)
                if agente.estado in (EstadoAgente.EJECUTANDO, EstadoAgente.BLOQUEADO):
                    eliminar_action.setEnabled(False)
                    if agente.estado == EstadoAgente.BLOQUEADO:
                        eliminar_action.setToolTip("Agente bloqueado por dependencia eliminada")
                eliminar_action.triggered.connect(
                    lambda checked, aid=agente_id: self.eliminar_agente_solicitado.emit(aid)
                )
                menu.addAction(eliminar_action)

        # Submenú Layout
        layout_menu = menu.addMenu("📊 Layout")
        for mode in LayoutMode:
            action = QAction(mode.value.capitalize(), self)
            action.setCheckable(True)
            action.setChecked(self._layout_mode == mode)
            action.triggered.connect(lambda checked, m=mode: self._cambiar_layout(m))
            layout_menu.addAction(action)

        # Mostrar/ocultar cuadrícula
        grid_action = QAction("🧩 Mostrar Cuadrícula", self)
        grid_action.setCheckable(True)
        grid_action.setChecked(self._show_grid)
        grid_action.triggered.connect(self._toggle_grid)
        menu.addAction(grid_action)

        menu.addSeparator()
        export_action = QAction("📤 Exportar Imagen", self)
        export_action.triggered.connect(self._exportar_imagen)
        menu.addAction(export_action)

        menu.exec(self.mapToGlobal(pos))
    
    def _cambiar_layout(self, mode: LayoutMode):
        """Cambia el modo de layout."""
        self._layout_mode = mode
        if hasattr(self, '_agentes_cache') and self._agentes_cache:
            self.actualizar_grafo(self._agentes_cache, recalcular_layout=True)
    
    def _toggle_grid(self):
        """Alterna la visibilidad de la cuadrícula."""
        self._show_grid = not self._show_grid
        self._actualizar_grid()
    
    def _exportar_imagen(self):
        """Exporta el grafo a imagen."""
        ruta, _ = QFileDialog.getSaveFileName(
            self,
            "Exportar Grafo",
            f"grafo_{time.strftime('%Y%m%d_%H%M')}.png",
            "PNG Image (*.png);;JPG Image (*.jpg);;PDF (*.pdf);;SVG (*.svg)"
        )
        
        if not ruta:
            return
        
        try:
            # Crear imagen
            rect = self._scene.sceneRect()
            image = QImage(int(rect.width()), int(rect.height()), QImage.Format.Format_ARGB32)
            image.fill(Qt.GlobalColor.white)
            
            painter = QPainter(image)
            self._scene.render(painter)
            painter.end()
            
            image.save(ruta)
            
            QMessageBox.information(self, "Éxito", f"✅ Grafo exportado a:\n{ruta}")
            self.logger.info(f"Grafo exportado a {ruta}")
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"❌ Error al exportar: {str(e)}")
            self.logger.error(f"Error exportando grafo: {e}")
    
    # ============================================================
    # RESET
    # ============================================================
    
    def limpiar(self):
        """Limpia todos los nodos y aristas."""
        # ✅ Detener timer de animaciones
        if self._animation_timer.isActive():
            self._animation_timer.stop()
            self.logger.debug("Timer de animaciones detenido en limpieza")
        
        for node in self._nodes.values():
            self._scene.removeItem(node)
        self._nodes.clear()
        
        for edge in self._edges:
            self._scene.removeItem(edge)
        self._edges.clear()
        
        self._node_positions.clear()
        self._selected_nodes.clear()
        self._animating_nodes.clear()
        self._agentes_cache.clear()
        
        self._actualizar_grid()
        self.logger.debug("Grafo limpiado")
    
    # ============================================================
    # LIMPIEZA
    # ============================================================
    
    def closeEvent(self, event):
        """Maneja el cierre del widget."""
        # ✅ Detener timer al cerrar
        if self._animation_timer.isActive():
            self._animation_timer.stop()
            self.logger.debug("Timer de animaciones detenido en closeEvent")
        event.accept()
    
    def __del__(self):
        """Limpieza final."""
        try:
            if hasattr(self, '_animation_timer') and self._animation_timer.isActive():
                self._animation_timer.stop()
        except:
            pass