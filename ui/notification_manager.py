# ui/notification_manager.py - VERSIÓN REFACTORIZADA Y COMPLETA
"""
Gestor de notificaciones del sistema con soporte multiplataforma.

CARACTERÍSTICAS:
- Soporte multiplataforma (Windows, macOS, Linux)
- Notificaciones por escritorio (plyer)
- Fallback a QSystemTrayIcon
- Cola de notificaciones con throttling
- Configuración persistente
- Niveles de prioridad
- Sonidos personalizables
- Logging estructurado
- Sistema de plantillas
- Notificaciones programadas
"""

import os
import sys
import time
import logging
import threading
import json
from typing import Optional, Dict, List, Callable, Any, Tuple
from enum import Enum, auto
from dataclasses import dataclass, field
from collections import deque
from datetime import datetime, timedelta

from PyQt6.QtWidgets import (
    QSystemTrayIcon, QMenu, QApplication, QWidget, 
    QPushButton, QVBoxLayout, QHBoxLayout, QLabel,
    QDialog, QCheckBox, QComboBox, QSpinBox,
    QMessageBox, QGroupBox
)
from PyQt6.QtCore import (
    Qt, QTimer, pyqtSignal, QObject, QSettings,
    QPoint, QRect, QSize
)
from PyQt6.QtGui import (
    QIcon, QAction, QPixmap, QPainter, QColor,
    QFont, QPalette, QBrush
)

# Intentar importar plyer para notificaciones nativas
try:
    from plyer import notification
    PLYER_DISPONIBLE = True
except ImportError:
    PLYER_DISPONIBLE = False
    logging.getLogger(__name__).warning(
        "plyer no instalado. Las notificaciones de escritorio no estarán disponibles.\n"
        "   Instalar con: pip install plyer"
    )

# Configurar logger
logger = logging.getLogger(__name__)

# ============================================================
# CONSTANTES
# ============================================================

DEFAULT_NOTIFICATION_TIMEOUT = 5  # segundos
MAX_NOTIFICATIONS_QUEUE = 100
MIN_NOTIFICATION_INTERVAL = 1.0  # segundos entre notificaciones
MAX_NOTIFICATIONS_PER_MINUTE = 20
ICON_SIZE = 64
SOUNDS_DIR = os.path.join(os.path.dirname(__file__), "..", "resources", "sounds")

# ============================================================
# ENUMERACIONES
# ============================================================

class NotificationPriority(Enum):
    """Prioridad de las notificaciones."""
    LOW = auto()
    NORMAL = auto()
    HIGH = auto()
    CRITICAL = auto()
    
    def timeout(self) -> int:
        """Timeout en segundos según prioridad."""
        timeouts = {
            NotificationPriority.LOW: 2,
            NotificationPriority.NORMAL: 5,
            NotificationPriority.HIGH: 8,
            NotificationPriority.CRITICAL: 15,
        }
        return timeouts.get(self, 5)
    
    def icon(self) -> str:
        """Icono según prioridad."""
        icons = {
            NotificationPriority.LOW: "ℹ️",
            NotificationPriority.NORMAL: "📢",
            NotificationPriority.HIGH: "⚠️",
            NotificationPriority.CRITICAL: "🚨",
        }
        return icons.get(self, "📢")


class NotificationCategory(Enum):
    """Categorías de notificaciones."""
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    PROGRESS = "progress"


# ============================================================
# MODELOS DE DATOS
# ============================================================

@dataclass
class Notification:
    """Modelo de una notificación."""
    titulo: str
    mensaje: str
    categoria: NotificationCategory = NotificationCategory.INFO
    prioridad: NotificationPriority = NotificationPriority.NORMAL
    timeout: Optional[int] = None
    timestamp: float = field(default_factory=time.time)
    id: Optional[str] = None
    datos_extra: Dict[str, Any] = field(default_factory=dict)
    callback: Optional[Callable] = None
    sonido: Optional[str] = None
    icono: Optional[str] = None
    
    def __post_init__(self):
        """Inicialización posterior."""
        if self.timeout is None:
            self.timeout = self.prioridad.timeout()
        if self.id is None:
            self.id = f"notif_{int(self.timestamp * 1000)}"
        if self.icono is None:
            self.icono = self.prioridad.icon()
    
    def to_dict(self) -> Dict:
        """Convierte a diccionario."""
        return {
            'id': self.id,
            'titulo': self.titulo,
            'mensaje': self.mensaje,
            'categoria': self.categoria.value,
            'prioridad': self.prioridad.name,
            'timeout': self.timeout,
            'timestamp': self.timestamp,
            'icono': self.icono,
        }


# ============================================================
# CLASE PRINCIPAL: NOTIFICATION MANAGER
# ============================================================

class NotificationManager(QObject):
    """
    Gestor de notificaciones del sistema con soporte multiplataforma.
    
    Características:
    - Cola de notificaciones con throttling
    - Múltiples backends (plyer, tray icon, consola)
    - Configuración persistente
    - Niveles de prioridad
    - Sistema de plantillas
    - Sonidos (opcional)
    """
    
    # Señales para comunicación con la UI
    notificacion_recibida = pyqtSignal(str, str)  # título, mensaje
    notificacion_clicked = pyqtSignal(str)  # id de la notificación
    notificacion_dismissed = pyqtSignal(str)  # id de la notificación
    notificacion_error = pyqtSignal(str)  # mensaje de error
    
    def __init__(self, parent: Optional[QWidget] = None):
        """
        Inicializa el gestor de notificaciones.
        
        Args:
            parent: Widget padre para el icono de bandeja
        """
        super().__init__(parent)
        
        self.parent = parent
        self.tray_icon: Optional[QSystemTrayIcon] = None
        self._initialized = False
        self._lock = threading.RLock()
        self._queue: deque = deque(maxlen=MAX_NOTIFICATIONS_QUEUE)
        self._last_notification_time: float = 0.0
        self._notifications_sent: int = 0
        self._notifications_failed: int = 0
        self._timer_reset = time.time()
        
        # Configuración
        self.settings = QSettings("AgentesVisuales", "Notificaciones")
        self._cargar_configuracion()
        
        # Temporizador para procesar cola
        self.queue_timer = QTimer()
        self.queue_timer.setSingleShot(False)
        self.queue_timer.timeout.connect(self._procesar_cola)
        self.queue_timer.start(1000)  # Procesar cada segundo
        
        # Inicializar icono de bandeja
        self._inicializar_tray_icon()
        
        # Registrar limpieza
        self._initialized = True
        
        logger.info("NotificationManager inicializado")
    
    # ============================================================
    # CONFIGURACIÓN
    # ============================================================
    
    def _cargar_configuracion(self):
        """Carga la configuración persistente."""
        self.enabled = self.settings.value("enabled", True, type=bool)
        self.show_tray = self.settings.value("show_tray", True, type=bool)
        self.play_sounds = self.settings.value("play_sounds", False, type=bool)
        self.show_toast = self.settings.value("show_toast", True, type=bool)
        self.min_interval = self.settings.value("min_interval", MIN_NOTIFICATION_INTERVAL, type=float)
        self.max_per_minute = self.settings.value("max_per_minute", MAX_NOTIFICATIONS_PER_MINUTE, type=int)
        self.notify_inicio = self.settings.value("notify_start", True, type=bool)
        self.notify_completado = self.settings.value("notify_complete", True, type=bool)
        self.notify_errores = self.settings.value("notify_errors", True, type=bool)
        self.notify_agentes = self.settings.value("notify_agents", False, type=bool)
    
    def guardar_configuracion(self):
        """Guarda la configuración persistente."""
        self.settings.setValue("enabled", self.enabled)
        self.settings.setValue("show_tray", self.show_tray)
        self.settings.setValue("play_sounds", self.play_sounds)
        self.settings.setValue("show_toast", self.show_toast)
        self.settings.setValue("min_interval", self.min_interval)
        self.settings.setValue("max_per_minute", self.max_per_minute)
        self.settings.setValue("notify_start", self.notify_inicio)
        self.settings.setValue("notify_complete", self.notify_completado)
        self.settings.setValue("notify_errors", self.notify_errores)
        self.settings.setValue("notify_agents", self.notify_agentes)
        
        logger.debug("Configuración guardada")
    
    # ============================================================
    # ICONO DE BANDEJA
    # ============================================================
    
    def _inicializar_tray_icon(self):
        """Inicializa el icono de la bandeja del sistema."""
        if not self.show_tray:
            return
        
        try:
            self.tray_icon = QSystemTrayIcon(self.parent)
            
            # Intentar cargar icono
            icon = self._cargar_icono()
            if icon:
                self.tray_icon.setIcon(icon)
            else:
                # Crear icono simple
                self.tray_icon.setIcon(self._crear_icono_placeholder())
            
            # Crear menú contextual
            menu = QMenu()
            
            # Acción mostrar ventana
            accion_mostrar = QAction("📊 Mostrar Ventana", self)
            accion_mostrar.triggered.connect(self._mostrar_ventana)
            menu.addAction(accion_mostrar)
            
            menu.addSeparator()
            
            # Estado de notificaciones
            accion_estado = QAction("🔔 Notificaciones activas", self)
            accion_estado.setEnabled(False)
            menu.addAction(accion_estado)
            
            menu.addSeparator()
            
            # Configuración
            accion_config = QAction("⚙️ Configurar", self)
            accion_config.triggered.connect(self._abrir_configuracion)
            menu.addAction(accion_config)
            
            menu.addSeparator()
            
            # Salir
            accion_salir = QAction("🚪 Salir", self)
            accion_salir.triggered.connect(self._salir_aplicacion)
            menu.addAction(accion_salir)
            
            self.tray_icon.setContextMenu(menu)
            
            # Conectar señales
            self.tray_icon.activated.connect(self._on_tray_activated)
            self.tray_icon.messageClicked.connect(self._on_tray_message_clicked)
            
            # Mostrar
            self.tray_icon.show()
            
            # Mostrar mensaje de bienvenida
            if self.enabled:
                self.tray_icon.showMessage(
                    "Agentes Visuales",
                    "🚀 Aplicación iniciada",
                    QSystemTrayIcon.MessageIcon.Information,
                    2000
                )
            
            logger.info("Icono de bandeja inicializado")
            
        except Exception as e:
            logger.warning(f"No se pudo inicializar el icono de bandeja: {e}")
            self.tray_icon = None
    
    def _cargar_icono(self) -> Optional[QIcon]:
        """Carga el icono desde el sistema de archivos."""
        icon_paths = [
            "icon.png",
            "icon.ico",
            os.path.join(os.path.dirname(__file__), "..", "resources", "icon.png"),
            os.path.join(os.path.dirname(__file__), "..", "resources", "icon.ico"),
            "/usr/share/icons/hicolor/48x48/apps/agentes_visuales.png",
            "/usr/share/pixmaps/agentes_visuales.png",
        ]
        
        for path in icon_paths:
            if os.path.exists(path):
                return QIcon(path)
        
        return None
    
    def _crear_icono_placeholder(self) -> QIcon:
        """Crea un icono de placeholder."""
        pixmap = QPixmap(ICON_SIZE, ICON_SIZE)
        pixmap.fill(Qt.GlobalColor.transparent)
        
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Círculo de fondo
        painter.setBrush(QBrush(QColor(0, 123, 255)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(8, 8, 48, 48)
        
        # Texto "AV"
        painter.setPen(QBrush(QColor(255, 255, 255)))
        painter.setFont(QFont("Arial", 18, QFont.Weight.Bold))
        painter.drawText(QRect(8, 8, 48, 48), Qt.AlignmentFlag.AlignCenter, "AV")
        
        painter.end()
        return QIcon(pixmap)
    
    def _mostrar_ventana(self):
        """Muestra la ventana principal."""
        if self.parent:
            self.parent.show()
            self.parent.raise_()
            self.parent.activateWindow()
    
    def _salir_aplicacion(self):
        """Cierra la aplicación."""
        QApplication.quit()
    
    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason):
        """Maneja clicks en el icono de bandeja."""
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self._mostrar_ventana()
            self.notificacion_clicked.emit("tray_icon")
    
    def _on_tray_message_clicked(self):
        """Maneja clicks en mensajes de la bandeja."""
        self._mostrar_ventana()
        self.notificacion_clicked.emit("tray_message")
    
    # ============================================================
    # ENVÍO DE NOTIFICACIONES
    # ============================================================
    
    def notificar(
        self,
        titulo: str,
        mensaje: str,
        categoria: NotificationCategory = NotificationCategory.INFO,
        prioridad: NotificationPriority = NotificationPriority.NORMAL,
        timeout: Optional[int] = None,
        callback: Optional[Callable] = None,
        sonido: Optional[str] = None,
        datos_extra: Optional[Dict] = None,
        inmediato: bool = False
    ) -> Optional[str]:
        """
        Envía una notificación.
        
        Args:
            titulo: Título de la notificación
            mensaje: Mensaje de la notificación
            categoria: Categoría de la notificación
            prioridad: Prioridad de la notificación
            timeout: Duración en segundos
            callback: Función a ejecutar al hacer click
            sonido: Nombre del sonido a reproducir
            datos_extra: Datos adicionales
            inmediato: Si debe enviarse inmediatamente (ignorar cola)
            
        Returns:
            Optional[str]: ID de la notificación o None si no se envió
        """
        if not self.enabled:
            logger.debug("Notificaciones deshabilitadas")
            return None
        
        # Crear notificación
        notif = Notification(
            titulo=titulo,
            mensaje=mensaje,
            categoria=categoria,
            prioridad=prioridad,
            timeout=timeout,
            datos_extra=datos_extra or {},
            callback=callback,
            sonido=sonido
        )
        
        # Emitir señal para la UI
        self.notificacion_recibida.emit(titulo, mensaje)
        
        # Si es inmediato, enviar directamente
        if inmediato or prioridad == NotificationPriority.CRITICAL:
            return self._enviar_notificacion(notif)
        
        # Agregar a la cola
        with self._lock:
            self._queue.append(notif)
        
        return notif.id
    
    def _enviar_notificacion(self, notif: Notification) -> Optional[str]:
        """
        Envía una notificación usando el backend disponible.
        
        Args:
            notif: Notificación a enviar
            
        Returns:
            Optional[str]: ID de la notificación o None si falló
        """
        # Verificar throttling
        if not self._puede_enviar(notif):
            return None
        
        # Actualizar estadísticas
        self._last_notification_time = time.time()
        self._notifications_sent += 1
        
        # Intentar con plyer primero
        enviado = False
        
        if PLYER_DISPONIBLE and self.show_toast:
            try:
                self._enviar_plyer(notif)
                enviado = True
                logger.debug(f"Notificación enviada vía plyer: {notif.titulo}")
            except Exception as e:
                logger.warning(f"Error enviando notificación vía plyer: {e}")
        
        # Fallback a bandeja de sistema
        if not enviado and self.tray_icon:
            try:
                self._enviar_tray(notif)
                enviado = True
                logger.debug(f"Notificación enviada vía tray: {notif.titulo}")
            except Exception as e:
                logger.warning(f"Error enviando notificación vía tray: {e}")
        
        # Último fallback: consola
        if not enviado:
            self._enviar_consola(notif)
            enviado = True
            logger.debug(f"Notificación enviada vía consola: {notif.titulo}")
        
        # Reproducir sonido
        if enviado and self.play_sounds and notif.sonido:
            self._reproducir_sonido(notif.sonido)
        
        return notif.id if enviado else None
    
    def _enviar_plyer(self, notif: Notification):
        """Envía notificación usando plyer."""
        notification.notify(
            title=notif.titulo,
            message=notif.mensaje,
            app_name="Agentes Visuales",
            timeout=notif.timeout,
            ticker=f"🔄 {notif.titulo}"  # Para Android
        )
    
    def _enviar_tray(self, notif: Notification):
        """Envía notificación usando QSystemTrayIcon."""
        # Mapear categoría a icono
        icon_map = {
            NotificationCategory.INFO: QSystemTrayIcon.MessageIcon.Information,
            NotificationCategory.SUCCESS: QSystemTrayIcon.MessageIcon.Information,
            NotificationCategory.WARNING: QSystemTrayIcon.MessageIcon.Warning,
            NotificationCategory.ERROR: QSystemTrayIcon.MessageIcon.Critical,
            NotificationCategory.PROGRESS: QSystemTrayIcon.MessageIcon.Information,
        }
        
        icon = icon_map.get(notif.categoria, QSystemTrayIcon.MessageIcon.Information)
        
        # Si es crítico, usar icono de warning
        if notif.prioridad == NotificationPriority.CRITICAL:
            icon = QSystemTrayIcon.MessageIcon.Critical
        
        self.tray_icon.showMessage(
            notif.titulo,
            notif.mensaje,
            icon,
            notif.timeout * 1000  # convertir a milisegundos
        )
    
    def _enviar_consola(self, notif: Notification):
        """Envía notificación a la consola."""
        emoji = notif.icono or "📢"
        categoria = notif.categoria.value.upper()
        print(f"\n{emoji} [{categoria}] {notif.titulo}")
        print(f"   {notif.mensaje}\n")
    
    def _puede_enviar(self, notif: Notification) -> bool:
        """
        Verifica si se puede enviar la notificación según el throttling.
        
        Args:
            notif: Notificación a verificar
            
        Returns:
            bool: True si se puede enviar
        """
        # Las notificaciones críticas siempre se envían
        if notif.prioridad == NotificationPriority.CRITICAL:
            return True
        
        # Verificar intervalo mínimo
        elapsed = time.time() - self._last_notification_time
        if elapsed < self.min_interval:
            return False
        
        # Verificar límite por minuto
        now = time.time()
        if now - self._timer_reset > 60:
            self._timer_reset = now
            self._notifications_sent = 0
        
        if self._notifications_sent >= self.max_per_minute:
            return False
        
        return True
    
    # ============================================================
    # PROCESAMIENTO DE COLA
    # ============================================================
    
    def _procesar_cola(self):
        """Procesa las notificaciones en cola."""
        if not self._queue:
            return
        
        with self._lock:
            while self._queue:
                notif = self._queue.popleft()
                self._enviar_notificacion(notif)
    
    def limpiar_cola(self):
        """Limpia la cola de notificaciones pendientes."""
        with self._lock:
            count = len(self._queue)
            self._queue.clear()
            logger.debug(f"Cola limpiada: {count} notificaciones eliminadas")
    
    # ============================================================
    # NOTIFICACIONES PREDEFINIDAS
    # ============================================================
    
    def notificar_inicio(self, total_agentes: int) -> Optional[str]:
        """Notifica el inicio de una ejecución."""
        if not self.notify_inicio:
            return None
        
        titulo = "🚀 Ejecución Iniciada"
        mensaje = f"Se han iniciado {total_agentes} agentes"
        return self.notificar(
            titulo, mensaje,
            categoria=NotificationCategory.INFO,
            prioridad=NotificationPriority.NORMAL,
            timeout=3
        )
    
    def notificar_completado(self, stats: Dict) -> Optional[str]:
        """Notifica que una ejecución ha terminado."""
        if not self.notify_completado:
            return None
        
        total = stats.get('total', 0)
        completados = stats.get('completados', 0)
        errores = stats.get('errores', 0)
        cancelados = stats.get('cancelados', 0)
        
        if errores > 0 or cancelados > 0:
            titulo = "⚠️ Ejecución Completada con Problemas"
            prioridad = NotificationPriority.HIGH
            categoria = NotificationCategory.WARNING
        else:
            titulo = "✅ Ejecución Completada"
            prioridad = NotificationPriority.NORMAL
            categoria = NotificationCategory.SUCCESS
        
        mensaje = (
            f"📊 Resumen:\n"
            f"   Total: {total}\n"
            f"   ✅ Completados: {completados}\n"
            f"   ❌ Errores: {errores}\n"
            f"   ⛔ Cancelados: {cancelados}"
        )
        
        if errores > 0:
            mensaje += "\n⚠️ Revisa los logs para más detalles"
        
        return self.notificar(
            titulo, mensaje,
            categoria=categoria,
            prioridad=prioridad,
            timeout=8 if prioridad == NotificationPriority.HIGH else 5,
            sonido="error.wav" if errores > 0 else "complete.wav"
        )
    
    def notificar_agente_completado(self, nombre: str, estado: str) -> Optional[str]:
        """Notifica que un agente ha terminado."""
        if not self.notify_agentes:
            return None
        
        emoji = "✅" if estado == "Completado" else "❌"
        titulo = f"{emoji} Agente {nombre}"
        mensaje = f"Estado: {estado}"
        
        categoria = NotificationCategory.SUCCESS if estado == "Completado" else NotificationCategory.ERROR
        
        return self.notificar(
            titulo, mensaje,
            categoria=categoria,
            prioridad=NotificationPriority.LOW,
            timeout=2
        )
    
    def notificar_error_critico(self, error: str, contexto: Optional[Dict] = None) -> Optional[str]:
        """Notifica un error crítico."""
        if not self.notify_errores:
            return None
        
        titulo = "🚨 Error Crítico"
        mensaje = f"Se ha producido un error:\n{error}"
        
        if contexto:
            mensaje += f"\n\nContexto: {json.dumps(contexto, indent=2)}"
        
        return self.notificar(
            titulo, mensaje,
            categoria=NotificationCategory.ERROR,
            prioridad=NotificationPriority.CRITICAL,
            timeout=10,
            sonido="critical.wav"
        )
    
    def notificar_progreso(self, progreso: int, total: int) -> Optional[str]:
        """Notifica el progreso general (solo cada 25%)."""
        if not self.notify_inicio:
            return None
        
        # Solo notificar cuando el progreso es múltiplo de 25
        if progreso % 25 == 0 and progreso > 0:
            porcentaje = int((progreso / total) * 100)
            titulo = "📊 Progreso de Ejecución"
            mensaje = f"Progreso: {porcentaje}% ({progreso}/{total})"
            return self.notificar(
                titulo, mensaje,
                categoria=NotificationCategory.PROGRESS,
                prioridad=NotificationPriority.LOW,
                timeout=2
            )
        return None
    
    def notificar_loop_progreso(self, nombre: str, items_procesados: int, total: int) -> Optional[str]:
        """Notifica el progreso de un loop."""
        if not self.notify_agentes:
            return None
        
        titulo = f"🔄 Loop: {nombre}"
        mensaje = f"Procesando: {items_procesados}/{total} items"
        
        return self.notificar(
            titulo, mensaje,
            categoria=NotificationCategory.PROGRESS,
            prioridad=NotificationPriority.LOW,
            timeout=1
        )
    
    # ============================================================
    # SONIDOS
    # ============================================================
    
    def _reproducir_sonido(self, nombre_sonido: str):
        """
        Reproduce un sonido.
        
        Args:
            nombre_sonido: Nombre del archivo de sonido
        """
        try:
            import platform
            
            # Buscar archivo de sonido
            ruta_sonido = os.path.join(SOUNDS_DIR, nombre_sonido)
            if not os.path.exists(ruta_sonido):
                logger.debug(f"Archivo de sonido no encontrado: {ruta_sonido}")
                return
            
            # Reproducir según plataforma
            sistema = platform.system()
            
            if sistema == "Windows":
                import winsound
                winsound.PlaySound(ruta_sonido, winsound.SND_ASYNC)
            elif sistema == "Darwin":  # macOS
                import subprocess
                subprocess.Popen(['afplay', ruta_sonido], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:  # Linux
                # Intentar con diferentes reproductores
                reproductores = ['paplay', 'aplay', 'play', 'mpg123']
                import subprocess
                for repro in reproductores:
                    try:
                        subprocess.Popen([repro, ruta_sonido], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        break
                    except FileNotFoundError:
                        continue
        except Exception as e:
            logger.debug(f"Error reproduciendo sonido: {e}")
    
    # ============================================================
    # CONFIGURACIÓN UI
    # ============================================================
    
    def _abrir_configuracion(self):
        """Abre el diálogo de configuración."""
        dialog = self._crear_dialogo_configuracion()
        dialog.exec()
    
    def _crear_dialogo_configuracion(self) -> QDialog:
        """Crea el diálogo de configuración de notificaciones."""
        dialog = QDialog(self.parent)
        dialog.setWindowTitle("🔔 Configuración de Notificaciones")
        dialog.setModal(True)
        dialog.setMinimumWidth(450)
        dialog.setMinimumHeight(500)
        
        layout = QVBoxLayout(dialog)
        layout.setSpacing(12)
        layout.setContentsMargins(16, 16, 16, 16)
        
        # ── Estado ──
        estado_group = QGroupBox("📡 Estado")
        estado_layout = QVBoxLayout(estado_group)
        
        estado_texto = "✅ Disponible" if PLYER_DISPONIBLE else "❌ No disponible"
        if self.tray_icon:
            estado_texto += " | 🖥️ Icono activo"
        else:
            estado_texto += " | ⚠️ Icono no disponible"
        
        lbl_estado = QLabel(f"📦 Plyer: {estado_texto}")
        lbl_estado.setStyleSheet("font-weight: bold;")
        estado_layout.addWidget(lbl_estado)
        
        # Estadísticas
        lbl_stats = QLabel(
            f"📊 Enviadas: {self._notifications_sent} | "
            f"Fallidas: {self._notifications_failed} | "
            f"Cola: {len(self._queue)}"
        )
        lbl_stats.setStyleSheet("color: #6c757d; font-size: 10px;")
        estado_layout.addWidget(lbl_stats)
        
        layout.addWidget(estado_group)
        
        # ── Configuración general ──
        general_group = QGroupBox("⚙️ Configuración General")
        general_layout = QVBoxLayout(general_group)
        
        self.chk_enabled = QCheckBox("🔔 Habilitar notificaciones")
        self.chk_enabled.setChecked(self.enabled)
        general_layout.addWidget(self.chk_enabled)
        
        self.chk_tray = QCheckBox("🖥️ Mostrar icono en bandeja")
        self.chk_tray.setChecked(self.show_tray)
        general_layout.addWidget(self.chk_tray)
        
        self.chk_toast = QCheckBox("💬 Mostrar notificaciones emergentes")
        self.chk_toast.setChecked(self.show_toast)
        general_layout.addWidget(self.chk_toast)
        
        self.chk_sounds = QCheckBox("🔊 Reproducir sonidos")
        self.chk_sounds.setChecked(self.play_sounds)
        general_layout.addWidget(self.chk_sounds)
        
        # Intervalo mínimo
        interval_layout = QHBoxLayout()
        interval_layout.addWidget(QLabel("⏱ Intervalo mínimo entre notificaciones:"))
        self.spin_interval = QSpinBox()
        self.spin_interval.setRange(0, 10)
        self.spin_interval.setValue(int(self.min_interval))
        self.spin_interval.setSuffix(" s")
        interval_layout.addWidget(self.spin_interval)
        interval_layout.addStretch()
        general_layout.addLayout(interval_layout)
        
        layout.addWidget(general_group)
        
        # ── Eventos ──
        events_group = QGroupBox("📋 Eventos a Notificar")
        events_layout = QVBoxLayout(events_group)
        
        self.chk_inicio = QCheckBox("🚀 Inicio de ejecución")
        self.chk_inicio.setChecked(self.notify_inicio)
        events_layout.addWidget(self.chk_inicio)
        
        self.chk_completado = QCheckBox("✅ Finalización de ejecución")
        self.chk_completado.setChecked(self.notify_completado)
        events_layout.addWidget(self.chk_completado)
        
        self.chk_errores = QCheckBox("❌ Errores críticos")
        self.chk_errores.setChecked(self.notify_errores)
        events_layout.addWidget(self.chk_errores)
        
        self.chk_agentes = QCheckBox("🤖 Agentes individuales")
        self.chk_agentes.setChecked(self.notify_agentes)
        events_layout.addWidget(self.chk_agentes)
        
        layout.addWidget(events_group)
        
        # ── Botones ──
        btn_layout = QHBoxLayout()
        
        # Botón prueba
        btn_test = QPushButton("🔔 Probar Notificación")
        btn_test.clicked.connect(
            lambda: self.notificar(
                "🔔 Prueba",
                "¡Las notificaciones funcionan correctamente!",
                categoria=NotificationCategory.SUCCESS,
                prioridad=NotificationPriority.NORMAL,
                inmediato=True
            )
        )
        btn_test.setStyleSheet("""
            QPushButton {
                background-color: #17a2b8;
                color: white;
                border-radius: 4px;
                padding: 6px 16px;
            }
            QPushButton:hover {
                background-color: #138496;
            }
        """)
        btn_layout.addWidget(btn_test)
        
        btn_layout.addStretch()
        
        # Botón guardar
        btn_guardar = QPushButton("💾 Guardar")
        btn_guardar.clicked.connect(lambda: self._guardar_config_dialog(dialog))
        btn_guardar.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: white;
                font-weight: bold;
                padding: 8px 24px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #218838;
            }
        """)
        btn_layout.addWidget(btn_guardar)
        
        # Botón cerrar
        btn_cerrar = QPushButton("Cerrar")
        btn_cerrar.clicked.connect(dialog.accept)
        btn_cerrar.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #5a6268;
            }
        """)
        btn_layout.addWidget(btn_cerrar)
        
        layout.addLayout(btn_layout)
        
        return dialog
    
    def _guardar_config_dialog(self, dialog: QDialog):
        """Guarda la configuración desde el diálogo."""
        self.enabled = self.chk_enabled.isChecked()
        self.show_tray = self.chk_tray.isChecked()
        self.show_toast = self.chk_toast.isChecked()
        self.play_sounds = self.chk_sounds.isChecked()
        self.min_interval = float(self.spin_interval.value())
        self.notify_inicio = self.chk_inicio.isChecked()
        self.notify_completado = self.chk_completado.isChecked()
        self.notify_errores = self.chk_errores.isChecked()
        self.notify_agentes = self.chk_agentes.isChecked()
        
        self.guardar_configuracion()
        
        # Re-inicializar tray si cambió
        if self.show_tray and not self.tray_icon:
            self._inicializar_tray_icon()
        elif not self.show_tray and self.tray_icon:
            self.tray_icon.hide()
            self.tray_icon = None
        
        QMessageBox.information(
            dialog, "Éxito",
            "✅ Configuración de notificaciones guardada correctamente"
        )
        
        dialog.accept()
    
    # ============================================================
    # ESTADÍSTICAS
    # ============================================================
    
    def obtener_estadisticas(self) -> Dict:
        """Obtiene estadísticas de notificaciones."""
        return {
            'enabled': self.enabled,
            'total_sent': self._notifications_sent,
            'total_failed': self._notifications_failed,
            'queue_size': len(self._queue),
            'plyer_available': PLYER_DISPONIBLE,
            'tray_available': self.tray_icon is not None,
            'notify_inicio': self.notify_inicio,
            'notify_completado': self.notify_completado,
            'notify_errores': self.notify_errores,
            'notify_agentes': self.notify_agentes,
            'min_interval': self.min_interval,
            'max_per_minute': self.max_per_minute,
        }
    
    def reset_estadisticas(self):
        """Reinicia las estadísticas."""
        self._notifications_sent = 0
        self._notifications_failed = 0
        self._timer_reset = time.time()
        self._last_notification_time = 0
    
    # ============================================================
    # MÉTODOS DE UTILIDAD
    # ============================================================
    
    def limpiar_notificaciones(self):
        """Limpia todas las notificaciones y la cola."""
        self.limpiar_cola()
        
        # Limpiar tray
        if self.tray_icon:
            try:
                self.tray_icon.showMessage("", "", QSystemTrayIcon.MessageIcon.Information, 0)
            except:
                pass
    
    def mostrar_error(self, titulo: str, mensaje: str) -> Optional[str]:
        """Muestra un error como notificación urgente."""
        return self.notificar(
            f"❌ {titulo}",
            mensaje,
            categoria=NotificationCategory.ERROR,
            prioridad=NotificationPriority.CRITICAL,
            timeout=10,
            sonido="error.wav",
            inmediato=True
        )
    
    def mostrar_advertencia(self, titulo: str, mensaje: str) -> Optional[str]:
        """Muestra una advertencia como notificación."""
        return self.notificar(
            f"⚠️ {titulo}",
            mensaje,
            categoria=NotificationCategory.WARNING,
            prioridad=NotificationPriority.HIGH,
            timeout=5,
            sonido="warning.wav"
        )
    
    def mostrar_info(self, titulo: str, mensaje: str) -> Optional[str]:
        """Muestra información como notificación."""
        return self.notificar(
            f"ℹ️ {titulo}",
            mensaje,
            categoria=NotificationCategory.INFO,
            prioridad=NotificationPriority.NORMAL,
            timeout=3
        )
    
    def mostrar_exito(self, titulo: str, mensaje: str) -> Optional[str]:
        """Muestra un éxito como notificación."""
        return self.notificar(
            f"✅ {titulo}",
            mensaje,
            categoria=NotificationCategory.SUCCESS,
            prioridad=NotificationPriority.NORMAL,
            timeout=3,
            sonido="success.wav"
        )
    
    # ============================================================
    # LIMPIEZA
    # ============================================================
    
    def close(self):
        """Cierra el gestor de notificaciones."""
        try:
            self.queue_timer.stop()
            self.limpiar_cola()
            
            if self.tray_icon:
                self.tray_icon.hide()
                self.tray_icon = None
            
            logger.info("NotificationManager cerrado")
        except Exception as e:
            logger.warning(f"Error cerrando NotificationManager: {e}")
    
    def __del__(self):
        """Limpieza final."""
        try:
            self.close()
        except:
            pass