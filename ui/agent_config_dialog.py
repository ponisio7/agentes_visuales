# ui/agent_config_dialog.py - VERSIÓN CON ASISTENTE IA REACTIVO COMPLETO
"""
Diálogo completo para crear/editar agentes con configuración por tipo.
Incluye ASISTENTE IA REACTIVO que analiza el formulario en tiempo real.
"""

import re
import json
import time
import logging
from typing import Dict, List, Optional, Any, Tuple, Set
from datetime import datetime
import dataclasses

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTabWidget, QWidget,
    QFormLayout, QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox,
    QTextEdit, QPushButton, QLabel, QGroupBox, QMessageBox,
    QScrollArea, QGridLayout, QInputDialog, QCheckBox, QFrame,
    QSplitter, QStackedWidget, QToolButton, QMenu, QApplication,
    QStyle, QFileDialog, QProgressBar
)
from PyQt6.QtCore import pyqtSignal, Qt, QTimer, QSettings, QThread, QObject
from PyQt6.QtGui import (
    QFont, QColor, QTextCursor, QKeySequence, QAction, QShortcut
)

from core.agent import Agente, TipoAgente, EstadoAgente

# Importar asistente IA (opcional)
try:
    from core.ai_assistant import AIAssistant
    from core.llm_client import LLMClient
    IA_DISPONIBLE = True
except ImportError:
    IA_DISPONIBLE = False

# Configurar logger
logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTES
# ============================================================
MODELOS_LLM_SUGERIDOS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro"
]

METODOS_HTTP_SOPORTADOS = [
    "GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"
]

OPERACIONES_FILE = [
    "leer", "escribir", "copiar", "mover", "eliminar"
]

TIPOS_AGENTES = [t.value for t in TipoAgente]

# ============================================================
# VALORES POR DEFECTO
# ============================================================
_DEFAULTS = {
    "nombre": "",
    "descripcion": "",
    "duracion": 5.0,
    "max_reintentos": 3,
    "dependencias_nombres": [],
}

_DEFAULTS_PYTHON = {
    "codigo_python": "",
    "timeout_python": 30,
}

_DEFAULTS_SHELL = {
    "comando_shell": "",
    "working_dir": "",
    "timeout_shell": 30,
}

_DEFAULTS_HTTP = {
    "url_http": "",
    "metodo_http": "GET",
    "headers_http": {},
    "body_http": "",
    "timeout_http": 30,
}

_DEFAULTS_LLM = {
    "prompt_llm": "",
    "modelo_llm": "deepseek-v4-flash",
    "temperatura_llm": 0.7,
    "max_tokens_llm": 4000,             # ← subido de 1000 a 4000
    "reasoning_effort_llm": "low",      # ← NUEVO: persistir reasoning
    "thinking_enabled_llm": False,      # ← NUEVO: default sin thinking
}

_DEFAULTS_FILE = {
    "operacion_file": "leer",
    "archivo_origen": "",
    "archivo_destino": "",
    "modo_salida_file": "auto",
}

_DEFAULTS_LOOP = {
    "fuente_items": "",
    "codigo_por_item": "",
    "max_iteraciones": 100,
    "timeout_loop": 300,
    "timeout_python": 30,
    "continuar_en_error": False,
}

# Mapa de campos por tipo para preservación
_TIPO_CAMPOS = {
    TipoAgente.PYTHON: set(_DEFAULTS_PYTHON.keys()),
    TipoAgente.SHELL: set(_DEFAULTS_SHELL.keys()),
    TipoAgente.HTTP: set(_DEFAULTS_HTTP.keys()),
    TipoAgente.LLM: set(_DEFAULTS_LLM.keys()),
    TipoAgente.FILE: set(_DEFAULTS_FILE.keys()),
    TipoAgente.LOOP: set(_DEFAULTS_LOOP.keys()),
}


# ============================================================
# WORKER PARA IA (HILO SEPARADO)
# ============================================================
class AIAnalysisWorker(QObject):
    """Worker para analizar el formulario con IA en hilo separado."""

    finished = pyqtSignal(dict)
    error = pyqtSignal(str)
    started = pyqtSignal()

    def __init__(self, assistant, contexto: dict):
        super().__init__()
        self.assistant = assistant
        self.contexto = contexto

    def run(self):
        """Ejecuta el análisis en hilo separado."""
        try:
            self.started.emit()
            resultado = self.assistant.analizar_formulario(self.contexto)
            # Asegurar que siempre emita un dict, incluso si es vacío
            self.finished.emit(resultado or {})
        except Exception as e:
            logger.exception("Error en análisis IA")
            self.error.emit(str(e))


# ============================================================
# VALIDADORES
# ============================================================
class ConfigValidator:
    """Valida configuraciones de agentes."""

    @staticmethod
    def validar_nombre(
        nombre: str,
        existentes: List[str] = None,
        agente_actual: Agente = None
    ) -> Tuple[bool, str]:
        """Valida el nombre del agente, permitiendo el nombre actual en edición."""
        if not nombre or not nombre.strip():
            return False, "El nombre es obligatorio"

        nombre = nombre.strip()

        if len(nombre) < 2:
            return False, "El nombre debe tener al menos 2 caracteres"
        if len(nombre) > 100:
            return False, "El nombre no puede tener más de 100 caracteres"
        if not re.match(r'^[A-Za-z0-9_\-\s]+$', nombre):
            return False, "El nombre solo puede contener letras, números, guiones y espacios"

        if existentes and agente_actual and nombre == agente_actual.nombre:
            return True, ""

        if existentes and nombre in existentes:
            return False, f"Ya existe un agente con el nombre '{nombre}'"

        return True, ""

    @staticmethod
    def validar_codigo_python(codigo: str) -> Tuple[bool, str]:
        """Valida código Python (sintaxis básica)."""
        if not codigo or not codigo.strip():
            return True, ""

        lines = codigo.split('\n')
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            if line.startswith(' '):
                spaces = len(line) - len(line.lstrip(' '))
                if spaces % 4 != 0:
                    return False, "La indentación debe ser múltiplo de 4 espacios"

        return True, ""

    @staticmethod
    def validar_url(url: str) -> Tuple[bool, str]:
        """Valida una URL."""
        if not url or not url.strip():
            return False, "La URL es obligatoria"

        url = url.strip()

        if not url.startswith(('http://', 'https://')):
            return False, "La URL debe comenzar con http:// o https://"
        if ' ' in url:
            return False, "La URL no puede contener espacios"

        return True, ""

    @staticmethod
    def validar_ruta_archivo(ruta: str) -> Tuple[bool, str]:
        """Valida una ruta de archivo."""
        if not ruta or not ruta.strip():
            return False, "La ruta es obligatoria"

        ruta = ruta.strip()

        if len(ruta) > 1000:
            return False, "La ruta es demasiado larga"
        if any(c in ruta for c in ['\n', '\r', '\t', '\0']):
            return False, "La ruta contiene caracteres no permitidos"

        return True, ""

    @staticmethod
    def validar_json(texto: str, campo_nombre: str = "JSON") -> Tuple[bool, str, Optional[Any]]:
        """Valida que un texto sea JSON válido."""
        if not texto or not texto.strip():
            return True, "", None

        try:
            parsed = json.loads(texto)
            return True, "", parsed
        except json.JSONDecodeError as e:
            return False, f"{campo_nombre} inválido: {str(e)[:80]}", None


# ============================================================
# CLASE PRINCIPAL: AGENT CONFIG DIALOG
# ============================================================
class AgentConfigDialog(QDialog):
    """
    Diálogo completo para crear/editar agentes con Asistente IA Reactivo.
    """

    agent_updated = pyqtSignal(object)  # Emite el agente modificado

    def __init__(
        self,
        parent=None,
        agente: Agente = None,
        nombres_existentes: List[str] = None,
        modo_lectura: bool = False
    ):
        super().__init__(parent)

        self.agente = agente
        self.nombres_existentes = nombres_existentes or []
        self.modo_lectura = modo_lectura
        self.resultado = None
        self._tipo_actual = None

        # ── Estado de preservación de datos ──
        self._datos_preservados: Dict[str, Dict] = {}

        # ── Preferencias ──
        self.settings = QSettings("AgentesVisuales", "AgentesVisuales")

        # ── Asistente IA ──
        self.ai_assistant = None
        if IA_DISPONIBLE:
            try:
                llm_client = LLMClient()
                if llm_client.disponible:
                    self.ai_assistant = AIAssistant(llm_client)
                    logger.info("Asistente IA inicializado correctamente")
            except Exception as e:
                logger.warning(f"No se pudo inicializar el asistente IA: {e}")

        # ── Estado de campos para IA reactiva ──
        self._origen_campos: Dict[str, str] = {}  # 'campo' -> 'usuario' | 'ia' | 'vacio'
        self._ultimo_analisis_ia = None
        self._analisis_pendiente = False

        # ── Timer de debounce para IA reactiva ──
        self.ai_debounce_timer = QTimer()
        self.ai_debounce_timer.setSingleShot(True)
        self.ai_debounce_timer.timeout.connect(self._analizar_formulario_con_ia)

        # ── Configurar título ──
        if agente:
            self.setWindowTitle(f"✏️ Editar Agente: {agente.nombre}")
        else:
            self.setWindowTitle("➕ Nuevo Agente")

        self.setMinimumSize(1050, 900)
        self.setModal(True)

        # ── Inicializar UI ──
        self._init_ui()
        self._cargar_datos()
        self._conectar_signals_ia()

        logger.info(f"AgentConfigDialog inicializado: {'edición' if agente else 'nuevo'}")

    # ============================================================
    # CONSTRUCCIÓN DE LA UI
    # ============================================================
    def _init_ui(self):
        """Inicializa la interfaz."""
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(10)
        main_layout.setContentsMargins(12, 12, 12, 12)

        # ── Panel del Asistente IA ──
        self.ai_panel = self._crear_panel_asistente()
        main_layout.addWidget(self.ai_panel)

        # ── Tabs ──
        self.tabs = QTabWidget()
        self.tabs.addTab(self._crear_tab_general(), "📋 General")
        self.tabs.addTab(self._crear_tab_contenido(), "📝 Contenido")
        self.tabs.addTab(self._crear_tab_avanzado(), "⚙️ Avanzado")
        self.tabs.addTab(self._crear_tab_loop(), "🔄 Loop")
        self.tabs.addTab(self._crear_tab_plantillas(), "📚 Plantillas")
        self.tabs.addTab(self._crear_tab_preview(), "👁️ Vista Previa")

        main_layout.addWidget(self.tabs)

        # ── Barra de estado de validación ──
        self._crear_barra_estado(main_layout)

        # ── Botones ──
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)

        # Botón de ayuda
        btn_ayuda = QPushButton("❓ Ayuda")
        btn_ayuda.clicked.connect(self._mostrar_ayuda)
        btn_ayuda.setToolTip("Mostrar ayuda sobre la configuración de agentes")
        btn_layout.addWidget(btn_ayuda)

        # ✨ Botón Asistente IA (diálogo completo)
        self.btn_ai_assistant = QPushButton("✨ Asistente IA")
        self.btn_ai_assistant.setToolTip(
            "Usa la IA para generar, mejorar o explicar la configuración del agente"
        )
        self.btn_ai_assistant.clicked.connect(self._abrir_asistente_ia)
        self.btn_ai_assistant.setStyleSheet("""
            QPushButton {
                background-color: #6f42c1;
                color: white;
                font-weight: bold;
                padding: 8px 16px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #5a32a3;
            }
            QPushButton:disabled {
                background-color: #6c757d;
                color: #adb5bd;
            }
        """)
        self.btn_ai_assistant.setEnabled(self.ai_assistant is not None)
        if self.ai_assistant is None:
            self.btn_ai_assistant.setToolTip(
                "Asistente IA no disponible.\n"
                "Configura la variable de entorno DEEPSEEK_API_KEY"
            )
        btn_layout.addWidget(self.btn_ai_assistant)

        btn_layout.addStretch()

        self.btn_guardar = QPushButton("💾 Guardar")
        self.btn_guardar.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: white;
                font-weight: bold;
                padding: 10px 30px;
                border-radius: 4px;
                min-width: 120px;
            }
            QPushButton:hover {
                background-color: #218838;
            }
            QPushButton:disabled {
                background-color: #6c757d;
                color: #adb5bd;
            }
        """)
        self.btn_guardar.clicked.connect(self._guardar)
        self.btn_guardar.setEnabled(not self.modo_lectura)
        btn_layout.addWidget(self.btn_guardar)

        self.btn_cancelar = QPushButton("❌ Cancelar")
        self.btn_cancelar.clicked.connect(self.reject)
        self.btn_cancelar.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                padding: 10px 20px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #5a6268;
            }
        """)
        btn_layout.addWidget(self.btn_cancelar)

        main_layout.addLayout(btn_layout)

        # Aplicar estilo
        self._aplicar_estilo()
        self._setup_shortcuts()

    # ============================================================
    # PANEL DEL ASISTENTE IA
    # ============================================================
    def _crear_panel_asistente(self) -> QWidget:
        """Crea el panel del asistente IA en la parte superior."""
        panel = QWidget()
        panel.setStyleSheet("""
            QWidget {
                background-color: #f8f9fa;
                border-radius: 6px;
                border: 1px solid #e9ecef;
                padding: 4px;
            }
        """)

        layout = QHBoxLayout(panel)
        layout.setContentsMargins(10, 4, 10, 4)

        # Icono y estado
        self.ai_icon = QLabel("🤖")
        layout.addWidget(self.ai_icon)

        self.ai_status = QLabel("Asistente IA listo")
        self.ai_status.setStyleSheet("color: #6c757d; font-size: 10px; font-weight: bold;")
        layout.addWidget(self.ai_status)

        layout.addStretch()

        # Barra de progreso (oculta por defecto)
        self.ai_progress = QProgressBar()
        self.ai_progress.setRange(0, 0)
        self.ai_progress.setMaximumWidth(120)
        self.ai_progress.setMaximumHeight(8)
        self.ai_progress.setVisible(False)
        self.ai_progress.setStyleSheet("""
            QProgressBar {
                border: none;
                background-color: #e9ecef;
                border-radius: 4px;
            }
            QProgressBar::chunk {
                background-color: #6f42c1;
                border-radius: 4px;
            }
        """)
        layout.addWidget(self.ai_progress)

        # Sugerencia actual
        self.ai_sugerencia = QLabel("")
        self.ai_sugerencia.setStyleSheet("color: #6f42c1; font-size: 10px; font-style: italic;")
        self.ai_sugerencia.setMaximumWidth(400)
        layout.addWidget(self.ai_sugerencia)

        return panel

    def _actualizar_panel_ia(self, estado: str, sugerencia: str = "", icono: str = "🤖"):
        """Actualiza el panel del asistente IA."""
        self.ai_icon.setText(icono)
        self.ai_status.setText(estado)

        if sugerencia:
            self.ai_sugerencia.setText(sugerencia)
            self.ai_sugerencia.setVisible(True)
        else:
            self.ai_sugerencia.setVisible(False)

    def _mostrar_progreso_ia(self, visible: bool):
        """Muestra u oculta la barra de progreso."""
        self.ai_progress.setVisible(visible)
        if visible:
            self.ai_icon.setText("⏳")
            self.ai_status.setText("Analizando con IA...")
        else:
            self.ai_icon.setText("🤖")
            self.ai_status.setText("Asistente IA listo")

    # ============================================================
    # GESTIÓN DE ORIGEN DE CAMPOS
    # ============================================================
    def _marcar_campo_como_usuario(self, campo: str):
        """Marca un campo como modificado por el usuario."""
        self._origen_campos[campo] = 'usuario'

    def _marcar_campo_como_ia(self, campo: str):
        """Marca un campo como completado por la IA."""
        if campo not in self._origen_campos or self._origen_campos[campo] != 'usuario':
            self._origen_campos[campo] = 'ia'

    def _campo_puede_ser_completado_por_ia(self, campo: str) -> bool:
        """Verifica si un campo puede ser modificado por la IA."""
        return self._origen_campos.get(campo) != 'usuario'

    def _contar_campos_vacios(self) -> int:
        """Cuenta cuántos campos importantes están vacíos."""
        vacios = 0
        if not self.input_nombre.text().strip():
            vacios += 1
        if not self.input_descripcion.text().strip():
            vacios += 1
        if not self._obtener_contenido_principal():
            vacios += 1
        return vacios

    # ============================================================
    # CONEXIÓN DE SEÑALES PARA IA REACTIVA
    # ============================================================
    def _conectar_signals_ia(self):
        """Conecta las señales de cambio de campos al debounce de IA."""
        if not self.ai_assistant:
            return

        # Campos que disparan análisis
        self.input_nombre.textChanged.connect(self._trigger_ai_debounce)
        self.input_tipo.currentTextChanged.connect(self._trigger_ai_debounce)
        self.input_descripcion.textChanged.connect(self._trigger_ai_debounce)
        self.input_deps.textChanged.connect(self._trigger_ai_debounce)
        self.text_edit.textChanged.connect(self._trigger_ai_debounce)
        self.text_codigo_loop.textChanged.connect(self._trigger_ai_debounce)
        self.input_fuente_items.textChanged.connect(self._trigger_ai_debounce)

        # ✅ Marcar campos como modificados por el usuario para protegerlos
        self.input_nombre.textChanged.connect(lambda: self._marcar_campo_como_usuario('nombre'))
        self.input_descripcion.textChanged.connect(lambda: self._marcar_campo_como_usuario('descripcion'))
        self.text_edit.textChanged.connect(lambda: self._marcar_campo_como_usuario('contenido'))
        self.text_codigo_loop.textChanged.connect(lambda: self._marcar_campo_como_usuario('codigo_por_item'))
        self.input_fuente_items.textChanged.connect(lambda: self._marcar_campo_como_usuario('fuente_items'))
        self.input_tipo.currentTextChanged.connect(lambda: self._marcar_campo_como_usuario('tipo'))
        self.input_deps.textChanged.connect(lambda: self._marcar_campo_como_usuario('dependencias'))

        # También al cambiar pestaña
        self.tabs.currentChanged.connect(self._trigger_ai_debounce)

    def _trigger_ai_debounce(self):
        """Dispara el análisis del formulario con debounce."""
        if not self.ai_assistant or not self.ai_assistant.disponible:
            return

        # No analizar si el usuario está editando un agente existente
        if self.agente and self.agente.nombre:
            return

        # No analizar si ya hay un análisis en progreso
        if self._analisis_pendiente:
            return

        self.ai_debounce_timer.start(700)

    # ============================================================
    # ANÁLISIS DEL FORMULARIO CON IA
    # ============================================================
    def _analizar_formulario_con_ia(self):
        """Analiza el formulario y sugiere campos faltantes."""
        if not self.ai_assistant or not self.ai_assistant.disponible:
            return

        # No analizar si el usuario está editando un agente existente
        if self.agente and self.agente.nombre:
            return

        # Construir contexto
        contexto = self._construir_contexto_ia()

        # Si no hay descripción con suficiente contenido, no analizar
        if not contexto.get('descripcion') or len(contexto['descripcion']) < 8:
            self._actualizar_panel_ia(
                "Esperando más detalles...",
                "Escribe una descripción más completa para que la IA te ayude",
                "⌨️"
            )
            return

        # Si el usuario ya ha escrito contenido y no hay campos vacíos, no analizar
        if contexto.get('contenido') and len(contexto['contenido']) > 100:
            campos_vacios = self._contar_campos_vacios()
            if campos_vacios == 0:
                self._actualizar_panel_ia(
                    "Todo completado",
                    "✅ Todos los campos están llenos",
                    "✅"
                )
                return

        # Ejecutar análisis en hilo separado
        self._ejecutar_analisis_ia(contexto)

    def _construir_contexto_ia(self) -> dict:
        """Construye el contexto para el análisis IA."""
        tipo = self.input_tipo.currentText()
        return {
            'nombre': self.input_nombre.text().strip(),
            'tipo': tipo,
            'descripcion': self.input_descripcion.text().strip(),
            'dependencias': [
                d.strip() for d in self.input_deps.text().split(',')
                if d.strip()
            ],
            'contenido': self._obtener_contenido_principal(),
            'campos_especificos': self._obtener_campos_especificos(tipo),
            'agentes_existentes': self.nombres_existentes,
            'modo': 'completar'
        }

    def _obtener_contenido_principal(self) -> str:
        """Obtiene el contenido principal según el tipo actual."""
        tipo = self.input_tipo.currentText()
        if tipo == "Loop":
            return self.text_codigo_loop.toPlainText().strip()
        return self.text_edit.toPlainText().strip()

    def _obtener_campos_especificos(self, tipo: str) -> dict:
        """Obtiene los campos específicos según el tipo."""
        if tipo == "Python":
            return {'timeout': self.timeout_python.value()}
        elif tipo == "HTTP":
            return {
                'metodo': self.metodo_http.currentText(),
                'timeout': self.timeout_http.value(),
                'headers': self.headers_http.toPlainText().strip(),
                'body': self.body_http.toPlainText().strip()
            }
        elif tipo == "LLM":
            return {
                'modelo': self.modelo_llm.currentText(),
                'temperatura': self.temp_llm.value(),
                'max_tokens': self.max_tokens_llm.value()
            }
        elif tipo == "Shell":
            return {
                'timeout': self.timeout_shell.value(),
                'working_dir': self.working_dir.text().strip()
            }
        elif tipo == "File":
            return {
                'operacion': self.operacion_file.currentText(),
                'archivo_origen': self.archivo_origen.text().strip(),
                'archivo_destino': self.archivo_destino.text().strip()
            }
        elif tipo == "Loop":
            return {
                'fuente_items': self.input_fuente_items.text().strip(),
                'max_iteraciones': self.input_max_iteraciones.value(),
                'timeout_loop': self.input_timeout_loop.value(),
                'continuar_en_error': self.check_continuar_en_error.isChecked()
            }
        return {}

    # ============================================================
    # EJECUCIÓN DE ANÁLISIS EN HILO SEPARADO
    # ============================================================
    def _ejecutar_analisis_ia(self, contexto: dict):
        """Ejecuta el análisis en hilo separado."""
        if self._analisis_pendiente:
            return

        self._analisis_pendiente = True
        self._mostrar_progreso_ia(True)
        self._actualizar_panel_ia(
            "Analizando formulario...",
            "🤔 La IA está procesando tu descripción",
            "⏳"
        )

        # Crear worker y thread
        self._ai_thread = QThread()
        self._ai_worker = AIAnalysisWorker(self.ai_assistant, contexto)
        self._ai_worker.moveToThread(self._ai_thread)

        # Conectar señales
        self._ai_thread.started.connect(self._ai_worker.run)
        self._ai_worker.finished.connect(self._on_analisis_completado)
        self._ai_worker.error.connect(self._on_analisis_error)
        self._ai_worker.finished.connect(self._ai_thread.quit)
        self._ai_worker.error.connect(self._ai_thread.quit)

        # Iniciar
        self._ai_thread.start()

    def _on_analisis_completado(self, sugerencias: dict):
        """Maneja la finalización del análisis IA."""
        self._analisis_pendiente = False
        self._mostrar_progreso_ia(False)

        if not sugerencias:
            self._actualizar_panel_ia(
                "No se generaron sugerencias",
                "ℹ️ La IA no encontró campos para completar",
                "ℹ️"
            )
            return

        cambios_aplicados = self._aplicar_sugerencias_ia(sugerencias)

        if cambios_aplicados:
            self._actualizar_panel_ia(
                "Sugerencias aplicadas",
                f"💡 {sugerencias.get('explicacion', 'Campos completados automáticamente')}",
                "🤖"
            )
            # Marcar campos como generados por IA
            for campo in cambios_aplicados:
                self._marcar_campo_como_ia(campo)
        else:
            self._actualizar_panel_ia(
                "Sin cambios aplicables",
                "ℹ️ Los campos sugeridos ya estaban completos",
                "ℹ️"
            )

    def _on_analisis_error(self, error: str):
        """Maneja errores en el análisis IA."""
        self._analisis_pendiente = False
        self._mostrar_progreso_ia(False)

        logger.warning(f"Error en análisis IA: {error}")
        self._actualizar_panel_ia(
            "Error en análisis",
            f"⚠️ {error[:80]}...",
            "❌"
        )

    # ============================================================
    # APLICAR SUGERENCIAS DE LA IA
    # ============================================================
    def _aplicar_sugerencias_ia(self, sugerencias: dict) -> List[str]:
        """
        Aplica las sugerencias de la IA respetando el origen de los campos.
        Retorna lista de campos que fueron modificados.
        """
        modificados: List[str] = []

        # ── Nombre ──
        if 'nombre' in sugerencias and self._campo_puede_ser_completado_por_ia('nombre'):
            if not self.input_nombre.text().strip():
                self.input_nombre.setText(sugerencias['nombre'])
                modificados.append('nombre')

        # ── Tipo ──
        if 'tipo' in sugerencias and self._campo_puede_ser_completado_por_ia('tipo'):
            if sugerencias['tipo'] in TIPOS_AGENTES:
                if self.input_tipo.currentIndex() == 0:  # Primer elemento (default)
                    self.input_tipo.setCurrentText(sugerencias['tipo'])
                    modificados.append('tipo')

        # ── Descripción ──
        if 'descripcion' in sugerencias and self._campo_puede_ser_completado_por_ia('descripcion'):
            if not self.input_descripcion.text().strip():
                self.input_descripcion.setText(sugerencias['descripcion'])
                modificados.append('descripcion')

        # ── Dependencias ──
        if 'dependencias' in sugerencias and self._campo_puede_ser_completado_por_ia('dependencias'):
            if not self.input_deps.text().strip():
                self.input_deps.setText(', '.join(sugerencias['dependencias']))
                modificados.append('dependencias')

        # ── Contenido según tipo ──
        tipo_actual = self.input_tipo.currentText()

        if tipo_actual == "Loop":
            # La IA puede devolver 'contenido' o 'codigo_por_item'
            codigo_sugerido = sugerencias.get('codigo_por_item') or sugerencias.get('contenido')
            if codigo_sugerido and self._campo_puede_ser_completado_por_ia('codigo_por_item'):
                if not self.text_codigo_loop.toPlainText().strip():
                    self.text_codigo_loop.setPlainText(codigo_sugerido)
                    modificados.append('codigo_por_item')
            
            if 'fuente_items' in sugerencias and self._campo_puede_ser_completado_por_ia('fuente_items'):
                if not self.input_fuente_items.text().strip():
                    self.input_fuente_items.setText(sugerencias['fuente_items'])
                    modificados.append('fuente_items')
        else:
            # Contenido principal (código, prompt, URL)
            if 'contenido' in sugerencias and self._campo_puede_ser_completado_por_ia('contenido'):
                if not self._obtener_contenido_principal():
                    self.text_edit.setPlainText(sugerencias['contenido'])
                    modificados.append('contenido')

        # ── Configuración avanzada específica ──
        self._aplicar_config_avanzada_ia(sugerencias, modificados)

        # ── Actualizar UI si hubo cambios ──
        if modificados:
            self._actualizar_contadores()
            self._actualizar_contadores_loop()
            self._validar_campos()
            self._actualizar_vista_previa()

        return modificados

    def _aplicar_config_avanzada_ia(self, sugerencias: dict, modificados: List[str]):
        """Aplica configuración avanzada específica del tipo."""
        tipo = self.input_tipo.currentText()

        if tipo == "Python":
            if 'timeout_python' in sugerencias and self._campo_puede_ser_completado_por_ia('timeout_python'):
                self.timeout_python.setValue(int(sugerencias['timeout_python']))
                modificados.append('timeout_python')

        elif tipo == "HTTP":
            if 'metodo_http' in sugerencias and sugerencias['metodo_http'] in METODOS_HTTP_SOPORTADOS:
                if self.metodo_http.currentIndex() == 0:
                    self.metodo_http.setCurrentText(sugerencias['metodo_http'])
                    modificados.append('metodo_http')
            if 'timeout_http' in sugerencias and self._campo_puede_ser_completado_por_ia('timeout_http'):
                self.timeout_http.setValue(int(sugerencias['timeout_http']))
                modificados.append('timeout_http')
            if 'headers_http' in sugerencias and self._campo_puede_ser_completado_por_ia('headers_http'):
                if not self.headers_http.toPlainText().strip():
                    headers = sugerencias['headers_http']
                    if isinstance(headers, dict):
                        self.headers_http.setPlainText(json.dumps(headers, indent=2, ensure_ascii=False))
                    else:
                        self.headers_http.setPlainText(str(headers))
                    modificados.append('headers_http')

        elif tipo == "LLM":
            if 'modelo_llm' in sugerencias and self._campo_puede_ser_completado_por_ia('modelo_llm'):
                self.modelo_llm.setCurrentText(sugerencias['modelo_llm'])
                modificados.append('modelo_llm')
            if 'temperatura_llm' in sugerencias and self._campo_puede_ser_completado_por_ia('temperatura_llm'):
                self.temp_llm.setValue(float(sugerencias['temperatura_llm']))
                modificados.append('temperatura_llm')
            if 'max_tokens_llm' in sugerencias and self._campo_puede_ser_completado_por_ia('max_tokens_llm'):
                self.max_tokens_llm.setValue(int(sugerencias['max_tokens_llm']))
                modificados.append('max_tokens_llm')

        elif tipo == "Shell":
            if 'timeout_shell' in sugerencias and self._campo_puede_ser_completado_por_ia('timeout_shell'):
                self.timeout_shell.setValue(int(sugerencias['timeout_shell']))
                modificados.append('timeout_shell')

        elif tipo == "File":
            if 'operacion_file' in sugerencias and sugerencias['operacion_file'] in OPERACIONES_FILE:
                if self.operacion_file.currentIndex() == 0:
                    self.operacion_file.setCurrentText(sugerencias['operacion_file'])
                    modificados.append('operacion_file')
            if 'archivo_origen' in sugerencias and self._campo_puede_ser_completado_por_ia('archivo_origen'):
                if not self.archivo_origen.text().strip():
                    self.archivo_origen.setText(sugerencias['archivo_origen'])
                    modificados.append('archivo_origen')
            if 'archivo_destino' in sugerencias and self._campo_puede_ser_completado_por_ia('archivo_destino'):
                if not self.archivo_destino.text().strip():
                    self.archivo_destino.setText(sugerencias['archivo_destino'])
                    modificados.append('archivo_destino')

        elif tipo == "Loop":
            if 'max_iteraciones' in sugerencias and self._campo_puede_ser_completado_por_ia('max_iteraciones'):
                self.input_max_iteraciones.setValue(int(sugerencias['max_iteraciones']))
                modificados.append('max_iteraciones')
            if 'timeout_loop' in sugerencias and self._campo_puede_ser_completado_por_ia('timeout_loop'):
                self.input_timeout_loop.setValue(int(sugerencias['timeout_loop']))
                modificados.append('timeout_loop')

    # ============================================================
    # BARRA DE ESTADO
    # ============================================================
    def _crear_barra_estado(self, parent_layout):
        """Crea la barra de estado con validación en tiempo real."""
        self.status_frame = QFrame()
        self.status_frame.setFrameShape(QFrame.Shape.StyledPanel)
        self.status_frame.setStyleSheet("""
            QFrame {
                background-color: #f8f9fa;
                border-radius: 4px;
                padding: 4px 8px;
            }
        """)

        status_layout = QHBoxLayout(self.status_frame)
        status_layout.setContentsMargins(8, 4, 8, 4)

        self.status_icon = QLabel("✅")
        status_layout.addWidget(self.status_icon)

        self.status_text = QLabel("Listo")
        self.status_text.setStyleSheet("color: #28a745;")
        status_layout.addWidget(self.status_text)

        status_layout.addStretch()

        self.status_details = QLabel("")
        self.status_details.setStyleSheet("color: #6c757d; font-size: 10px;")
        status_layout.addWidget(self.status_details)

        parent_layout.addWidget(self.status_frame)

    # ============================================================
    # ESTILO Y ATAJOS
    # ============================================================
    def _aplicar_estilo(self):
        """Aplica el estilo al diálogo."""
        self.setStyleSheet("""
            QTabWidget::pane {
                border: 1px solid #d1d5db;
                border-radius: 4px;
                background-color: #f8f9fa;
            }
            QTabBar::tab {
                padding: 8px 16px;
                background-color: #e9ecef;
                border: 1px solid #d1d5db;
                border-bottom: none;
                border-radius: 4px 4px 0 0;
                margin-right: 2px;
            }
            QTabBar::tab:selected {
                background-color: white;
                border-bottom: 1px solid white;
            }
            QTabBar::tab:hover:!selected {
                background-color: #dee2e6;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #d1d5db;
                border-radius: 4px;
                margin-top: 12px;
                padding-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 8px 0 8px;
                background-color: transparent;
            }
            QTextEdit {
                font-family: 'Consolas', 'Courier New', monospace;
                font-size: 11px;
                border: 1px solid #d1d5db;
                border-radius: 4px;
                background-color: #1e1e2e;
                color: #cdd6f4;
                selection-background-color: #45475a;
            }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
                padding: 6px 10px;
                border: 1px solid #d1d5db;
                border-radius: 4px;
                background-color: white;
            }
            QLineEdit:focus, QComboBox:focus, QTextEdit:focus {
                border-color: #007bff;
                outline: none;
            }
            QLineEdit[readOnly="true"] {
                background-color: #f0f0f0;
                color: #6c757d;
            }
            QCheckBox {
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
            QSpinBox::up-button, QDoubleSpinBox::up-button,
            QSpinBox::down-button, QDoubleSpinBox::down-button {
                width: 16px;
            }
            QScrollArea {
                border: none;
                background-color: transparent;
            }
            .validation-error {
                border-color: #dc3545 !important;
                background-color: #fff5f5 !important;
            }
            .validation-warning {
                border-color: #ffc107 !important;
                background-color: #fffbf0 !important;
            }
            .validation-success {
                border-color: #28a745 !important;
                background-color: #f0fff4 !important;
            }
        """)

    def _setup_shortcuts(self):
        """Configura atajos de teclado."""
        shortcut_save = QKeySequence("Ctrl+Return")
        QShortcut(shortcut_save, self, self._guardar)

        shortcut_close = QKeySequence("Ctrl+W")
        QShortcut(shortcut_close, self, self.reject)

        shortcut_help = QKeySequence("F1")
        QShortcut(shortcut_help, self, self._mostrar_ayuda)

    # ============================================================
    # CIERRE SEGURO DEL DIÁLOGO
    # ============================================================
    def reject(self):
        """Asegura que el hilo de IA se detenga limpiamente al cerrar."""
        if hasattr(self, '_ai_thread') and self._ai_thread.isRunning():
            self._ai_thread.quit()
            self._ai_thread.wait(1000)
        super().reject()

    # ============================================================
    # PESTAÑA GENERAL
    # ============================================================
    def _crear_tab_general(self) -> QWidget:
        """Crea la pestaña de configuración general."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(12)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        content_layout = QVBoxLayout(content)

        form = QFormLayout()
        form.setSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        # Nombre
        self.input_nombre = QLineEdit()
        self.input_nombre.setPlaceholderText("Nombre único del agente")
        self.input_nombre.textChanged.connect(self._validar_campos)
        self.input_nombre.setMaxLength(100)
        if self.modo_lectura:
            self.input_nombre.setReadOnly(True)
        form.addRow("🤖 Nombre *:", self.input_nombre)

        # Tipo
        self.input_tipo = QComboBox()
        self.input_tipo.addItems(TIPOS_AGENTES)
        self.input_tipo.currentTextChanged.connect(self._on_tipo_cambiado)
        form.addRow("📌 Tipo:", self.input_tipo)

        # Descripción
        self.input_descripcion = QLineEdit()
        self.input_descripcion.setPlaceholderText("Breve descripción del agente")
        self.input_descripcion.setMaxLength(200)
        if self.modo_lectura:
            self.input_descripcion.setReadOnly(True)
        form.addRow("📝 Descripción:", self.input_descripcion)

        # Duración
        self.input_duracion = QDoubleSpinBox()
        self.input_duracion.setRange(0.1, 3600)
        self.input_duracion.setSingleStep(0.5)
        self.input_duracion.setValue(5.0)
        self.input_duracion.setSuffix(" s")
        form.addRow("⏱ Duración estimada:", self.input_duracion)

        # Reintentos
        self.input_reintentos = QSpinBox()
        self.input_reintentos.setRange(0, 10)
        self.input_reintentos.setValue(3)
        self.input_reintentos.setSuffix(" veces")
        form.addRow("🔄 Máx. reintentos:", self.input_reintentos)

        # Dependencias
        self.input_deps = QLineEdit()
        self.input_deps.setPlaceholderText("nombre1, nombre2, nombre3")
        self.input_deps.setToolTip("Nombres de los agentes de los que depende")
        if self.modo_lectura:
            self.input_deps.setReadOnly(True)
        form.addRow("🔗 Dependencias:", self.input_deps)

        content_layout.addLayout(form)

        # Información adicional
        info_frame = QFrame()
        info_frame.setFrameShape(QFrame.Shape.StyledPanel)
        info_frame.setStyleSheet("""
            QFrame {
                background-color: #e9ecef;
                border-radius: 4px;
                padding: 8px;
            }
            QLabel {
                color: #495057;
                font-size: 10px;
            }
        """)

        info_layout = QVBoxLayout(info_frame)
        info_text = QLabel("ℹ️ Los agentes se ejecutan en orden según sus dependencias.")
        info_text.setWordWrap(True)
        info_layout.addWidget(info_text)

        if self.agente:
            info_text2 = QLabel(f"🆔 ID: {self.agente.id}")
            info_text2.setStyleSheet("font-family: monospace;")
            info_layout.addWidget(info_text2)

        content_layout.addWidget(info_frame)
        content_layout.addStretch()

        scroll.setWidget(content)
        layout.addWidget(scroll)

        return tab

    # ============================================================
    # PESTAÑA CONTENIDO
    # ============================================================
    def _crear_tab_contenido(self) -> QWidget:
        """Crea la pestaña de contenido específico."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(8)

        header = QHBoxLayout()

        self.label_contenido = QLabel("📝 Contenido específico del agente:")
        self.label_contenido.setStyleSheet("font-weight: bold; font-size: 12px;")
        header.addWidget(self.label_contenido)

        self.tipo_contenido = QComboBox()
        self.tipo_contenido.addItems(TIPOS_AGENTES)
        self.tipo_contenido.currentTextChanged.connect(self._on_tipo_contenido_cambiado)
        self.tipo_contenido.setMaximumWidth(200)
        header.addWidget(self.tipo_contenido)

        header.addStretch()
        layout.addLayout(header)

        self.text_edit = QTextEdit()
        self.text_edit.setPlaceholderText("""
Escribe aquí el contenido específico según el tipo de agente:

🔹 LLM: El prompt que guiará al modelo
🔹 Python: Código Python a ejecutar
🔹 Shell: Comando de terminal
🔹 HTTP: URL de la API
🔹 File: (se configura en la pestaña Avanzado)
🔹 Loop: (se configura en la pestaña Loop)

Variables disponibles:
  {contexto} - Resultados de dependencias
  {resultado} - Resultado del agente actual
  {nombre} - Nombre del agente
  {fecha} - Fecha actual
  {hora} - Hora actual
""")
        self.text_edit.textChanged.connect(self._on_texto_cambiado)
        self.text_edit.textChanged.connect(self._validar_campos)
        if self.modo_lectura:
            self.text_edit.setReadOnly(True)

        layout.addWidget(self.text_edit, stretch=1)

        # Barra de estado del texto
        status_layout = QHBoxLayout()
        status_layout.setSpacing(15)

        self.lbl_caracteres = QLabel("0 caracteres")
        self.lbl_caracteres.setStyleSheet("color: #6c7086; font-size: 10px;")
        status_layout.addWidget(self.lbl_caracteres)

        self.lbl_lineas = QLabel("| Líneas: 0")
        self.lbl_lineas.setStyleSheet("color: #6c7086; font-size: 10px;")
        status_layout.addWidget(self.lbl_lineas)

        self.lbl_palabras = QLabel("| Palabras: 0")
        self.lbl_palabras.setStyleSheet("color: #6c7086; font-size: 10px;")
        status_layout.addWidget(self.lbl_palabras)

        status_layout.addStretch()

        # Botones de ayuda
        self.btn_insertar_var = QPushButton("🔤 Insertar Variable")
        self.btn_insertar_var.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                border-radius: 3px;
                padding: 4px 12px;
                font-size: 10px;
            }
            QPushButton:hover {
                background-color: #5a6268;
            }
        """)
        self.btn_insertar_var.clicked.connect(self._insertar_variable)
        status_layout.addWidget(self.btn_insertar_var)

        self.btn_formatear = QPushButton("📐 Formatear")
        self.btn_formatear.setStyleSheet("""
            QPushButton {
                background-color: #17a2b8;
                color: white;
                border-radius: 3px;
                padding: 4px 12px;
                font-size: 10px;
            }
            QPushButton:hover {
                background-color: #138496;
            }
        """)
        self.btn_formatear.clicked.connect(self._formatear_texto)
        status_layout.addWidget(self.btn_formatear)

        layout.addLayout(status_layout)

        return tab

        # ============================================================
    # PESTAÑA AVANZADO
    # ============================================================
    def _crear_tab_avanzado(self) -> QWidget:
        """Crea la pestaña de configuración avanzada por tipo."""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        scroll_layout = QVBoxLayout(content)
        scroll_layout.setSpacing(12)

        # ══════════════════════════════════════════════════════════
        # 🐍 PYTHON
        # ══════════════════════════════════════════════════════════
        self.grupo_python = QGroupBox("🐍 Configuración Python")
        python_form = QFormLayout(self.grupo_python)

        self.timeout_python = QSpinBox()
        self.timeout_python.setRange(1, 600)
        self.timeout_python.setValue(30)
        self.timeout_python.setSuffix(" s")
        self.timeout_python.setToolTip("Tiempo máximo de ejecución del código Python")
        python_form.addRow("Timeout:", self.timeout_python)

        scroll_layout.addWidget(self.grupo_python)

        # ══════════════════════════════════════════════════════════
        # 🧠 LLM
        # ══════════════════════════════════════════════════════════
        self.grupo_llm = QGroupBox("🧠 Configuración LLM")
        llm_form = QFormLayout(self.grupo_llm)

        self.modelo_llm = QComboBox()
        self.modelo_llm.setEditable(True)
        self.modelo_llm.addItems(MODELOS_LLM_SUGERIDOS)
        self.modelo_llm.setToolTip("Modelo de DeepSeek a usar")
        llm_form.addRow("Modelo:", self.modelo_llm)

        self.temp_llm = QDoubleSpinBox()
        self.temp_llm.setRange(0.0, 2.0)
        self.temp_llm.setSingleStep(0.05)
        self.temp_llm.setValue(0.7)
        self.temp_llm.setToolTip(
            "Controla la creatividad del modelo:\n"
            "• 0.0-0.3: respuestas deterministas y precisas\n"
            "• 0.4-0.7: equilibrio (recomendado)\n"
            "• 0.8-2.0: más creatividad pero menos precisión"
        )
        llm_form.addRow("Temperatura:", self.temp_llm)

        self.max_tokens_llm = QSpinBox()
        self.max_tokens_llm.setRange(50, 16000)
        self.max_tokens_llm.setSingleStep(100)
        self.max_tokens_llm.setValue(4000)      # ← subido de 1000 a 4000
        self.max_tokens_llm.setSuffix(" tokens")
        self.max_tokens_llm.setToolTip(
            "Máximo de tokens en la respuesta.\n\n"
            "⚠️ Valores muy bajos (< 4000) pueden causar respuestas truncadas\n"
            "   cuando el modo 'thinking' está activo.\n"
            "   Recomendado: 4000 o más para tareas de generación estructurada."
        )
        llm_form.addRow("Máx. tokens:", self.max_tokens_llm)

        # ✅ NUEVO: selector de reasoning_effort
        self.reasoning_effort_llm = QComboBox()
        self.reasoning_effort_llm.addItems(["low", "medium", "high"])
        self.reasoning_effort_llm.setCurrentText("low")
        self.reasoning_effort_llm.setToolTip(
            "Nivel de razonamiento del modelo:\n"
            "• low: respuestas rápidas, menos profundidad (recomendado para tareas simples)\n"
            "• medium: equilibrio\n"
            "• high: análisis profundo, mayor latencia y consumo de tokens"
        )
        llm_form.addRow("Esfuerzo de razonamiento:", self.reasoning_effort_llm)

        # ✅ NUEVO: checkbox de thinking
        self.thinking_enabled_llm = QCheckBox(
            "Habilitar modo 'thinking' (consume más tokens)"
        )
        self.thinking_enabled_llm.setChecked(False)
        self.thinking_enabled_llm.setToolTip(
            "Cuando está activo, el modelo razona internamente antes de responder.\n\n"
            "⚠️ Consume parte del presupuesto de 'max_tokens'.\n"
            "Recomendado: DESACTIVADO para tareas de generación estructurada\n"
            "(HTML, JSON, código) y para prompts que requieren respuesta concisa."
        )
        llm_form.addRow("", self.thinking_enabled_llm)

        # ✅ Aviso informativo sobre tokens vs thinking
        aviso_llm = QLabel(
            "<span style='color: #6c757d; font-size: 10px;'>"
            "💡 Si el modo <b>thinking</b> está activado, el modelo necesita "
            "más tokens para razonar. Recomendado: <b>max_tokens ≥ 4000</b>."
            "</span>"
        )
        aviso_llm.setWordWrap(True)
        llm_form.addRow("", aviso_llm)

        scroll_layout.addWidget(self.grupo_llm)

        # ══════════════════════════════════════════════════════════
        # 🌐 HTTP
        # ══════════════════════════════════════════════════════════
        self.grupo_http = QGroupBox("🌐 Configuración HTTP")
        http_form = QFormLayout(self.grupo_http)

        self.metodo_http = QComboBox()
        self.metodo_http.addItems(METODOS_HTTP_SOPORTADOS)
        http_form.addRow("Método:", self.metodo_http)

        self.timeout_http = QSpinBox()
        self.timeout_http.setRange(1, 300)
        self.timeout_http.setValue(30)
        self.timeout_http.setSuffix(" s")
        http_form.addRow("Timeout:", self.timeout_http)

        self.headers_http = QTextEdit()
        self.headers_http.setMaximumHeight(80)
        self.headers_http.setPlaceholderText(
            '{\n  "Authorization": "Bearer token",\n  "Accept": "application/json"\n}'
        )
        self.headers_http.textChanged.connect(self._validar_campos)
        http_form.addRow("📋 Headers (JSON):", self.headers_http)

        self.body_http = QTextEdit()
        self.body_http.setMaximumHeight(100)
        self.body_http.setPlaceholderText('{"key": "value"} (solo POST/PUT/PATCH)')
        self.body_http.textChanged.connect(self._validar_campos)
        http_form.addRow("📦 Body (JSON):", self.body_http)

        scroll_layout.addWidget(self.grupo_http)

        # ══════════════════════════════════════════════════════════
        # 💻 SHELL
        # ══════════════════════════════════════════════════════════
        self.grupo_shell = QGroupBox("💻 Configuración Shell")
        shell_form = QFormLayout(self.grupo_shell)

        self.timeout_shell = QSpinBox()
        self.timeout_shell.setRange(1, 600)
        self.timeout_shell.setValue(30)
        self.timeout_shell.setSuffix(" s")
        shell_form.addRow("Timeout:", self.timeout_shell)

        self.working_dir = QLineEdit()
        self.working_dir.setPlaceholderText("Directorio de trabajo (opcional)")
        shell_form.addRow("Directorio:", self.working_dir)

        scroll_layout.addWidget(self.grupo_shell)

        # ══════════════════════════════════════════════════════════
        # 📄 FILE
        # ══════════════════════════════════════════════════════════
        self.grupo_file = QGroupBox("📄 Configuración File")
        file_form = QFormLayout(self.grupo_file)

        self.operacion_file = QComboBox()
        self.operacion_file.addItems(OPERACIONES_FILE)
        self.operacion_file.currentTextChanged.connect(self._on_operacion_file_cambiado)
        self.operacion_file.setToolTip(
            "Operación a realizar:\n"
            "• leer: lee un archivo\n"
            "• escribir: crea/sobrescribe un archivo\n"
            "• copiar: duplica un archivo\n"
            "• mover: mueve/renombra un archivo\n"
            "• eliminar: borra un archivo o directorio"
        )
        file_form.addRow("Operación:", self.operacion_file)

        self.archivo_origen = QLineEdit()
        self.archivo_origen.setPlaceholderText("Ruta del archivo origen")
        self.archivo_origen.textChanged.connect(self._validar_campos)
        file_form.addRow("Archivo origen:", self.archivo_origen)

        self.archivo_destino = QLineEdit()
        self.archivo_destino.setPlaceholderText("Ruta del archivo destino")
        self.archivo_destino.textChanged.connect(self._validar_campos)
        file_form.addRow("Archivo destino:", self.archivo_destino)

        # ✅ NUEVO: selector de modo de salida
        self.modo_salida_file = QComboBox()
        self.modo_salida_file.addItems(["auto", "contenido", "json", "texto"])
        self.modo_salida_file.setToolTip(
            "Cómo se propaga el contenido del archivo al siguiente agente:\n"
            "• auto: el sistema decide según el tipo de archivo y su contenido\n"
            "• contenido: siempre se propaga el texto crudo del archivo\n"
            "• json: solo se propaga el JSON parseado (si el archivo es JSON)\n"
            "• texto: igual que 'contenido' (para archivos de texto plano)"
        )
        file_form.addRow("🔀 Modo de salida:", self.modo_salida_file)

        aviso_salida = QLabel(
            "<span style='color: #6c757d; font-size: 10px;'>"
            "Determina qué recibe el siguiente agente que dependa de este."
            "</span>"
        )
        aviso_salida.setWordWrap(True)
        file_form.addRow("", aviso_salida)

        scroll_layout.addWidget(self.grupo_file)

        # ══════════════════════════════════════════════════════════
        # FINALIZACIÓN
        # ══════════════════════════════════════════════════════════
        scroll_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

        # Ajustar visibilidad inicial según el tipo actual
        self._actualizar_visibilidad_avanzado()

        return tab

    # ============================================================
    # PESTAÑA LOOP
    # ============================================================
    def _crear_tab_loop(self) -> QWidget:
        """Crea la pestaña de configuración específica para agentes LOOP."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        content = QWidget()
        scroll_layout = QVBoxLayout(content)
        scroll_layout.setSpacing(12)

        grupo = QGroupBox("🔄 Configuración del Loop")
        grupo.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                border: 2px solid #6f42c1;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 12px;
            }
            QGroupBox::title {
                color: #6f42c1;
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 8px 0 8px;
            }
        """)

        form_layout = QFormLayout(grupo)
        form_layout.setSpacing(15)
        form_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        ayuda_fuente = QLabel("""
            <span style='color: #6c757d; font-size: 10px;'>
            Ej: <b>ObtenerLista.items</b> o <b>Dependencia.data.results</b>
            </span>
        """)
        ayuda_fuente.setWordWrap(True)

        self.input_fuente_items = QLineEdit()
        self.input_fuente_items.setPlaceholderText("Dependencia.clave (ej: ObtenerLista.items)")
        self.input_fuente_items.textChanged.connect(self._validar_campos)
        form_layout.addRow("📋 Fuente de Items *:", self.input_fuente_items)
        form_layout.addRow("", ayuda_fuente)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        form_layout.addRow(line)

        self.label_codigo_loop = QLabel("💻 Código por Item:")
        self.label_codigo_loop.setStyleSheet("font-weight: bold;")
        form_layout.addRow(self.label_codigo_loop)

        self.text_codigo_loop = QTextEdit()
        self.text_codigo_loop.setMinimumHeight(250)
        self.text_codigo_loop.textChanged.connect(self._on_loop_texto_cambiado)
        self.text_codigo_loop.textChanged.connect(self._validar_campos)
        form_layout.addRow(self.text_codigo_loop)

        loop_status = QHBoxLayout()
        loop_status.setSpacing(15)

        self.lbl_loop_caracteres = QLabel("0 caracteres")
        self.lbl_loop_caracteres.setStyleSheet("color: #6c7086; font-size: 10px;")
        loop_status.addWidget(self.lbl_loop_caracteres)

        self.lbl_loop_lineas = QLabel("| Líneas: 0")
        self.lbl_loop_lineas.setStyleSheet("color: #6c7086; font-size: 10px;")
        loop_status.addWidget(self.lbl_loop_lineas)

        self.lbl_loop_palabras = QLabel("| Palabras: 0")
        self.lbl_loop_palabras.setStyleSheet("color: #6c7086; font-size: 10px;")
        loop_status.addWidget(self.lbl_loop_palabras)

        loop_status.addStretch()

        btn_insertar_var_loop = QPushButton("🔤 Insertar Variable")
        btn_insertar_var_loop.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                border-radius: 3px;
                padding: 4px 12px;
                font-size: 10px;
            }
            QPushButton:hover {
                background-color: #5a6268;
            }
        """)
        btn_insertar_var_loop.clicked.connect(self._insertar_variable_loop)
        loop_status.addWidget(btn_insertar_var_loop)

        form_layout.addRow(loop_status)

        line2 = QFrame()
        line2.setFrameShape(QFrame.Shape.HLine)
        line2.setFrameShadow(QFrame.Shadow.Sunken)
        form_layout.addRow(line2)

        seguridad_group = QGroupBox("🛡️ Límites de Seguridad")
        seguridad_layout = QFormLayout(seguridad_group)
        seguridad_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.input_max_iteraciones = QSpinBox()
        self.input_max_iteraciones.setRange(1, 10000)
        self.input_max_iteraciones.setValue(100)
        self.input_max_iteraciones.setSuffix(" items")
        seguridad_layout.addRow("Máx. iteraciones:", self.input_max_iteraciones)

        self.input_timeout_loop = QSpinBox()
        self.input_timeout_loop.setRange(1, 3600)
        self.input_timeout_loop.setValue(300)
        self.input_timeout_loop.setSuffix(" s")
        seguridad_layout.addRow("Timeout total:", self.input_timeout_loop)

        self.input_timeout_item = QSpinBox()
        self.input_timeout_item.setRange(1, 600)
        self.input_timeout_item.setValue(30)
        self.input_timeout_item.setSuffix(" s")
        seguridad_layout.addRow("Timeout por item:", self.input_timeout_item)

        self.check_continuar_en_error = QCheckBox("Continuar aunque algún item falle")
        seguridad_layout.addRow("", self.check_continuar_en_error)

        form_layout.addRow(seguridad_group)

        scroll_layout.addWidget(grupo)

        btn_test_loop = QPushButton("🧪 Probar Loop con Items de Ejemplo")
        btn_test_loop.setStyleSheet("""
            QPushButton {
                background-color: #6f42c1;
                color: white;
                font-weight: bold;
                padding: 10px;
                border-radius: 4px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #5a32a3;
            }
            QPushButton:disabled {
                background-color: #6c757d;
                color: #adb5bd;
            }
        """)
        btn_test_loop.setEnabled(not self.modo_lectura)
        btn_test_loop.clicked.connect(self._probar_loop)
        scroll_layout.addWidget(btn_test_loop)

        scroll_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll)

        self._actualizar_visibilidad_loop()

        return tab

    # ============================================================
    # PESTAÑA PLANTILLAS
    # ============================================================
    def _crear_tab_plantillas(self) -> QWidget:
        """Crea la pestaña de plantillas predefinidas."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setSpacing(10)

        header = QLabel("📚 Plantillas predefinidas (haz clic para cargar)")
        header.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(header)

        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Filtrar:"))

        self.filter_plantillas = QLineEdit()
        self.filter_plantillas.setPlaceholderText("Buscar plantilla...")
        self.filter_plantillas.textChanged.connect(self._filtrar_plantillas)
        filter_layout.addWidget(self.filter_plantillas)

        self.combo_categoria = QComboBox()
        self.combo_categoria.addItems(["Todas", "LLM", "Python", "Shell", "HTTP", "File", "Loop"])
        self.combo_categoria.currentTextChanged.connect(self._filtrar_plantillas)
        filter_layout.addWidget(self.combo_categoria)

        layout.addLayout(filter_layout)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)

        self.plantillas_container = QWidget()
        self.plantillas_layout = QGridLayout(self.plantillas_container)
        self.plantillas_layout.setSpacing(8)
        self.plantillas_layout.setContentsMargins(4, 4, 4, 4)

        self.plantillas = self._crear_plantillas()
        self._mostrar_plantillas()

        scroll.setWidget(self.plantillas_container)
        layout.addWidget(scroll, stretch=1)

        return tab

    def _crear_plantillas(self) -> Dict[str, Dict]:
        """Crea el diccionario de plantillas."""
        return {
            "Análisis de Datos": {
                "categoria": "LLM",
                "contenido": (
                    "Eres un analista de datos experto.\n\n"
                    "Analiza los siguientes datos y proporciona:\n"
                    "1. Resumen ejecutivo\n"
                    "2. Principales hallazgos\n"
                    "3. Recomendaciones\n"
                    "4. Posibles riesgos\n\n"
                    "Datos:\n{contexto}\n\n"
                    "Formato: Responde en secciones claras con emojis."
                ),
                "tipo": "LLM"
            },
            "Procesador JSON": {
                "categoria": "Python",
                "contenido": (
                    "# Procesador JSON\n"
                    "import json\n\n"
                    "data = contexto.get('input_data', {})\n\n"
                    "if data:\n"
                    "    resultado = {\n"
                    "        'datos_procesados': data,\n"
                    "        'total_items': len(data) if isinstance(data, (list, dict)) else 1,\n"
                    "        'estado': 'ok'\n"
                    "    }\n"
                    "else:\n"
                    "    resultado = {'error': 'No se recibieron datos', 'estado': 'error'}"
                ),
                "tipo": "Python"
            },
            "API REST (GET)": {
                "categoria": "HTTP",
                "contenido": "https://api.github.com/repos/python/cpython",
                "tipo": "HTTP",
                "meta": {
                    "metodo": "GET",
                    "headers": '{"Accept": "application/json", "User-Agent": "Agentes-Visuales/1.0"}'
                }
            },
            "Procesar Lista": {
                "categoria": "Loop",
                "contenido": (
                    "# Procesar cada item del loop\n"
                    "import json\n"
                    "import time\n\n"
                    "resultado_item = {\n"
                    "    'indice': indice,\n"
                    "    'item': item,\n"
                    "    'procesado': True,\n"
                    "    'timestamp': time.time()\n"
                    "}\n\n"
                    "if isinstance(item, dict):\n"
                    "    resultado_item.update({\n"
                    "        'claves': list(item.keys())\n"
                    "    })\n\n"
                    "resultado = resultado_item"
                ),
                "tipo": "Loop",
                "meta": {"fuente": "Dependencia.items"}
            }
        }

    def _mostrar_plantillas(self, filtro: str = "", categoria: str = "Todas"):
        """Muestra las plantillas en el grid."""
        for i in reversed(range(self.plantillas_layout.count())):
            widget = self.plantillas_layout.itemAt(i).widget()
            if widget:
                widget.deleteLater()

        row, col = 0, 0
        filtro = filtro.lower()

        for nombre, data in self.plantillas.items():
            if categoria != "Todas" and data.get("categoria") != categoria:
                continue
            if filtro and filtro not in nombre.lower():
                continue

            btn = QPushButton(nombre)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #e9ecef;
                    border: 1px solid #d1d5db;
                    border-radius: 4px;
                    padding: 10px 8px;
                    text-align: left;
                    min-height: 40px;
                }
                QPushButton:hover {
                    background-color: #dee2e6;
                    border-color: #007bff;
                }
            """)
            btn.clicked.connect(lambda checked, n=nombre: self._cargar_plantilla(n))

            categoria_label = QLabel(data.get("categoria", ""))
            categoria_label.setStyleSheet("""
                QLabel {
                    background-color: #6c757d;
                    color: white;
                    border-radius: 2px;
                    padding: 1px 6px;
                    font-size: 8px;
                }
            """)
            categoria_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

            btn_layout = QVBoxLayout(btn)
            btn_layout.setSpacing(2)
            btn_layout.setContentsMargins(4, 4, 4, 4)

            nombre_label = QLabel(nombre)
            nombre_label.setStyleSheet("font-weight: bold; font-size: 10px;")
            btn_layout.addWidget(nombre_label)
            btn_layout.addWidget(categoria_label)

            self.plantillas_layout.addWidget(btn, row, col)

            col += 1
            if col >= 3:
                col = 0
                row += 1

    def _filtrar_plantillas(self):
        filtro = self.filter_plantillas.text()
        categoria = self.combo_categoria.currentText()
        self._mostrar_plantillas(filtro, categoria)

    def _cargar_plantilla(self, nombre: str):
        data = self.plantillas.get(nombre)
        if not data:
            return

        reply = QMessageBox.question(
            self, "Cargar Plantilla",
            f"¿Deseas cargar la plantilla '{nombre}'?\nEsto reemplazará el contenido actual.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        tipo = data.get("tipo")
        if tipo and tipo in TIPOS_AGENTES:
            self.input_tipo.setCurrentText(tipo)
            self.tipo_contenido.setCurrentText(tipo)

        contenido = data.get("contenido", "")
        if contenido:
            self.text_edit.setPlainText(contenido)

        meta = data.get("meta", {})
        if meta.get("fuente"):
            self.input_fuente_items.setText(meta["fuente"])
        if meta.get("metodo"):
            self.metodo_http.setCurrentText(meta["metodo"])
        if meta.get("headers"):
            self.headers_http.setPlainText(meta["headers"])

        self._actualizar_contadores()
        self._validar_campos()
        self.tabs.setCurrentIndex(1)

    # ============================================================
    # PESTAÑA VISTA PREVIA
    # ============================================================
    def _crear_tab_preview(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)

        info_group = QGroupBox("📋 Información del Agente")
        info_layout = QFormLayout(info_group)

        self.preview_nombre = QLabel("")
        info_layout.addRow("Nombre:", self.preview_nombre)

        self.preview_tipo = QLabel("")
        info_layout.addRow("Tipo:", self.preview_tipo)

        self.preview_deps = QLabel("")
        info_layout.addRow("Dependencias:", self.preview_deps)

        self.preview_estado = QLabel("")
        info_layout.addRow("Estado:", self.preview_estado)

        layout.addWidget(info_group)

        config_group = QGroupBox("⚙️ Configuración")
        config_layout = QVBoxLayout(config_group)

        self.preview_config = QTextEdit()
        self.preview_config.setReadOnly(True)
        self.preview_config.setFont(QFont("Consolas", 9))
        self.preview_config.setMaximumHeight(250)
        config_layout.addWidget(self.preview_config)

        layout.addWidget(config_group)

        code_group = QGroupBox("📝 Código del Agente")
        code_layout = QVBoxLayout(code_group)

        self.preview_code = QTextEdit()
        self.preview_code.setReadOnly(True)
        self.preview_code.setFont(QFont("Consolas", 9))
        self.preview_code.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e2e;
                color: #cdd6f4;
                border: 1px solid #d1d5db;
                border-radius: 4px;
            }
        """)
        code_layout.addWidget(self.preview_code)

        layout.addWidget(code_group, stretch=1)

        btn_refresh = QPushButton("🔄 Actualizar Vista Previa")
        btn_refresh.clicked.connect(self._actualizar_vista_previa)
        layout.addWidget(btn_refresh)

        return tab

    def _actualizar_vista_previa(self):
        nombre = self.input_nombre.text().strip() or "(sin nombre)"
        tipo = self.input_tipo.currentText()
        deps = [d.strip() for d in self.input_deps.text().split(",") if d.strip()]

        self.preview_nombre.setText(nombre)
        self.preview_tipo.setText(tipo)
        self.preview_deps.setText(", ".join(deps) if deps else "Sin dependencias")
        self.preview_estado.setText("Nuevo" if not self.agente else "Editando")

        config_text = [f"Tipo: {tipo}"]
        if tipo == "Python":
            config_text.append(f"Líneas de código: {len(self.text_edit.toPlainText().splitlines())}")
            config_text.append(f"Timeout: {self.timeout_python.value()}s")
        elif tipo == "HTTP":
            config_text.append(f"Método: {self.metodo_http.currentText()}")
            config_text.append(f"URL: {self.text_edit.toPlainText()[:50]}...")
        elif tipo == "LLM":
            config_text.append(f"Modelo: {self.modelo_llm.currentText()}")
            config_text.append(f"Temperatura: {self.temp_llm.value()}")
        elif tipo == "Loop":
            config_text.append(f"Fuente: {self.input_fuente_items.text() or '(sin configurar)'}")
            config_text.append(f"Máx. iteraciones: {self.input_max_iteraciones.value()}")

        self.preview_config.setText("\n".join(config_text))

        code_lines = [f"# Agente: {nombre}", f"# Tipo: {tipo}"]
        if tipo == "Python":
            code_lines.append(self.text_edit.toPlainText() or "# (vacío)")
        elif tipo == "LLM":
            code_lines.append(self.text_edit.toPlainText() or "# (vacío)")
        elif tipo == "Loop":
            code_lines.append(self.text_codigo_loop.toPlainText() or "# (vacío)")
        self.preview_code.setText("\n".join(code_lines))

    # ============================================================
    # MANEJO DE TIPOS
    # ============================================================
    def _on_tipo_cambiado(self, texto: str):
        if self._tipo_actual:
            self._preservar_datos_actuales()

        self._tipo_actual = texto
        self.tipo_contenido.setCurrentText(texto)

        self._actualizar_placeholders(texto)
        self._actualizar_visibilidad_avanzado()
        self._actualizar_visibilidad_loop()
        self._cargar_datos_preservados(texto)

        self._validar_campos()
        self._actualizar_vista_previa()

    def _on_tipo_contenido_cambiado(self, texto: str):
        if self.input_tipo.currentText() != texto:
            self.input_tipo.setCurrentText(texto)

    def _preservar_datos_actuales(self):
        """
        Guarda el estado actual del formulario asociado al tipo activo,
        para poder restaurarlo si el usuario cambia temporalmente el tipo
        y luego vuelve al original.

        Se ejecuta automáticamente desde `_on_tipo_cambiado` antes de
        cambiar `self._tipo_actual`.
        """
        tipo_actual = self._tipo_actual
        if not tipo_actual:
            return

        self._datos_preservados[tipo_actual] = {
            # ── Contenido principal (código, prompt, comando, URL) ──
            'contenido': self.text_edit.toPlainText(),

            # ── Python ──
            'timeout_python': self.timeout_python.value(),

            # ── Shell ──
            'timeout_shell': self.timeout_shell.value(),
            'working_dir': self.working_dir.text(),

            # ── HTTP ──
            'metodo_http': self.metodo_http.currentText(),
            'timeout_http': self.timeout_http.value(),
            'headers_http': self.headers_http.toPlainText(),
            'body_http': self.body_http.toPlainText(),

            # ── File ──
            'operacion_file': self.operacion_file.currentText(),
            'archivo_origen': self.archivo_origen.text(),
            'archivo_destino': self.archivo_destino.text(),
            'modo_salida_file': self.modo_salida_file.currentText(),   # ✅ NUEVO

            # ── LLM ──
            'modelo_llm': self.modelo_llm.currentText(),
            'temp_llm': self.temp_llm.value(),
            'max_tokens_llm': self.max_tokens_llm.value(),
            'reasoning_effort_llm': self.reasoning_effort_llm.currentText(),  # ← NUEVO
            'thinking_enabled_llm': self.thinking_enabled_llm.isChecked(),    # ← NUEVO

            # ── Loop ──
            'fuente_items': self.input_fuente_items.text(),
            'codigo_loop': self.text_codigo_loop.toPlainText(),
            'max_iteraciones': self.input_max_iteraciones.value(),
            'timeout_loop': self.input_timeout_loop.value(),
            'timeout_item': self.input_timeout_item.value(),
            'continuar_en_error': self.check_continuar_en_error.isChecked(),
        }

    def _cargar_datos_preservados(self, tipo: str):
        datos = self._datos_preservados.get(tipo, {})

        if 'contenido' in datos:
            self.text_edit.setPlainText(datos['contenido'])
        if 'fuente_items' in datos:
            self.input_fuente_items.setText(datos['fuente_items'])
        if 'codigo_loop' in datos:
            self.text_codigo_loop.setPlainText(datos['codigo_loop'])
        if 'max_iteraciones' in datos:
            self.input_max_iteraciones.setValue(datos['max_iteraciones'])
        if 'timeout_python' in datos:
            self.timeout_python.setValue(datos['timeout_python'])
        if 'modelo_llm' in datos:
            self.modelo_llm.setCurrentText(datos['modelo_llm'])
        if 'temp_llm' in datos:
            self.temp_llm.setValue(datos['temp_llm'])
        if 'max_tokens_llm' in datos:
            self.max_tokens_llm.setValue(datos['max_tokens_llm'])

        # ✅ NUEVO: restaurar reasoning_effort y thinking_enabled
        if 'reasoning_effort_llm' in datos:
            idx = self.reasoning_effort_llm.findText(datos['reasoning_effort_llm'])
            if idx >= 0:
                self.reasoning_effort_llm.setCurrentIndex(idx)
        if 'thinking_enabled_llm' in datos:
            self.thinking_enabled_llm.setChecked(bool(datos['thinking_enabled_llm']))

        if 'metodo_http' in datos:
            self.metodo_http.setCurrentText(datos['metodo_http'])
        self._actualizar_contadores()

    def _actualizar_placeholders(self, tipo: str):
        placeholders = {
            "Python": (
                "Escribe aquí tu código Python:\n\n"
                "resultado = {'status': 'ok', 'data': 'procesado'}"
            ),
            "Shell": "ls -la\necho 'Hola mundo'",
            "HTTP": "https://api.example.com/data",
            "LLM": (
                "Analiza los siguientes datos y genera un resumen ejecutivo:\n\n"
                "{contexto}\n{resultado}"
            ),
            "File": "(la ruta y operación se configuran en la pestaña Avanzado)",
            "Loop": "(el código se configura en la pestaña Loop)"
        }
        self.text_edit.setPlaceholderText(placeholders.get(tipo, ""))

    def _actualizar_visibilidad_avanzado(self):
        tipo = self.input_tipo.currentText()
        visibilidad = {
            "Python": self.grupo_python,
            "Shell": self.grupo_shell,
            "HTTP": self.grupo_http,
            "LLM": self.grupo_llm,
            "File": self.grupo_file,
            "Loop": None,
        }
        grupo_activo = visibilidad.get(tipo)
        for grupo in (self.grupo_python, self.grupo_llm, self.grupo_http,
                      self.grupo_shell, self.grupo_file):
            grupo.setVisible(grupo is grupo_activo)

    def _actualizar_visibilidad_loop(self):
        tipo = self.input_tipo.currentText()
        visible = tipo == "Loop"
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == "🔄 Loop":
                self.tabs.setTabVisible(i, visible)
                break

    def _on_operacion_file_cambiado(self, texto: str):
        usa_origen = texto in ("leer", "copiar", "mover", "eliminar")
        usa_destino = texto in ("escribir", "copiar", "mover")
        self.archivo_origen.setEnabled(usa_origen)
        self.archivo_destino.setEnabled(usa_destino)

    # ============================================================
    # CONTADORES DE TEXTO
    # ============================================================
    def _on_texto_cambiado(self):
        self._actualizar_contadores()

    def _on_loop_texto_cambiado(self):
        self._actualizar_contadores_loop()

    def _actualizar_contadores(self):
        text = self.text_edit.toPlainText()
        self.lbl_caracteres.setText(f"{len(text)} caracteres")
        self.lbl_lineas.setText(f"| Líneas: {len(text.splitlines())}")
        self.lbl_palabras.setText(f"| Palabras: {len(text.split())}")

    def _actualizar_contadores_loop(self):
        text = self.text_codigo_loop.toPlainText()
        self.lbl_loop_caracteres.setText(f"{len(text)} caracteres")
        self.lbl_loop_lineas.setText(f"| Líneas: {len(text.splitlines())}")
        self.lbl_loop_palabras.setText(f"| Palabras: {len(text.split())}")

    def _formatear_texto(self, loop: bool = False):
        if self.modo_lectura:
            return

        editor = self.text_codigo_loop if loop else self.text_edit
        text = editor.toPlainText()
        if not text.strip():
            return

        lines = text.split('\n')
        formatted = []
        indent = 0

        for line in lines:
            stripped = line.strip()
            if not stripped:
                formatted.append("")
                continue

            if stripped.endswith(':'):
                formatted.append('    ' * indent + stripped)
                indent += 1
            elif stripped in ('}', ']', ')'):
                indent = max(0, indent - 1)
                formatted.append('    ' * indent + stripped)
            else:
                formatted.append('    ' * indent + stripped)

        editor.setPlainText('\n'.join(formatted))
        self._actualizar_contadores()

    def _insertar_variable(self):
        variables = ["{contexto}", "{resultado}", "{nombre}", "{fecha}", "{hora}"]
        variable, ok = QInputDialog.getItem(
            self, "Insertar Variable",
            "Selecciona una variable:",
            variables, 0, True
        )
        if ok and variable:
            cursor = self.text_edit.textCursor()
            cursor.insertText(variable)
            self.text_edit.setTextCursor(cursor)
            self.text_edit.setFocus()

    def _insertar_variable_loop(self):
        variables = ["{item}", "{indice}", "{total}", "{contexto}"]
        variable, ok = QInputDialog.getItem(
            self, "Insertar Variable para Loop",
            "Selecciona una variable:",
            variables, 0, True
        )
        if ok and variable:
            cursor = self.text_codigo_loop.textCursor()
            cursor.insertText(variable)
            self.text_codigo_loop.setTextCursor(cursor)
            self.text_codigo_loop.setFocus()

    # ============================================================
    # PRUEBA DE LOOP
    # ============================================================
    def _probar_loop(self):
        from core.executors import AgentExecutor

        tipo = self.input_tipo.currentText()
        if tipo != "Loop":
            QMessageBox.information(self, "Info", "Cambia el tipo a 'Loop' para probar.")
            return

        codigo = self.text_codigo_loop.toPlainText().strip()
        if not codigo:
            QMessageBox.warning(self, "Error", "El código del Loop está vacío")
            return

        fuente = self.input_fuente_items.text().strip()
        if not fuente:
            QMessageBox.warning(self, "Error", "La fuente de items está vacía")
            return

        items_str, ok = QInputDialog.getText(
            self, "🧪 Items de Prueba",
            "Items de ejemplo (separados por comas):",
            QLineEdit.EchoMode.Normal,
            "Python, JavaScript, Go, Rust, TypeScript"
        )

        if not ok or not items_str:
            return

        items_ejemplo = [item.strip() for item in items_str.split(",") if item.strip()]

        if not items_ejemplo:
            QMessageBox.warning(self, "Error", "No hay items para probar")
            return

        agente_test = Agente(
            nombre="TestLoop",
            tipo=TipoAgente.LOOP,
            fuente_items=fuente,
            codigo_por_item=codigo,
            max_iteraciones=self.input_max_iteraciones.value(),
            timeout_loop=self.input_timeout_loop.value(),
            timeout_python=self.input_timeout_item.value(),
            continuar_en_error=self.check_continuar_en_error.isChecked()
        )

        try:
            exito, mensaje, resultado = AgentExecutor.probar_loop(agente_test, items_ejemplo)

            if exito:
                QMessageBox.information(
                    self, "✅ Prueba Exitosa",
                    f"Loop completado correctamente!\n\n"
                    f"Items procesados: {resultado.get('total_items', 0)}\n"
                    f"Errores: {resultado.get('errores', 0)}"
                )
            else:
                QMessageBox.warning(
                    self, "❌ Prueba Fallida",
                    f"El Loop falló:\n{mensaje}"
                )
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error en prueba: {e}")

    # ============================================================
    # VALIDACIÓN
    # ============================================================
    def _nombre_existe_en_agentes(self, nombre: str) -> bool:
        if not self.nombres_existentes:
            return False
        if self.agente and nombre == self.agente.nombre:
            return False
        return nombre in self.nombres_existentes

    def _validar_campos(self):
        errores = []
        advertencias = []

        nombre = self.input_nombre.text().strip()
        if not nombre:
            errores.append("El nombre es obligatorio")
            self.input_nombre.setStyleSheet("border-color: #dc3545; background-color: #fff5f5;")
        elif len(nombre) < 2:
            errores.append("El nombre debe tener al menos 2 caracteres")
            self.input_nombre.setStyleSheet("border-color: #ffc107; background-color: #fffbf0;")
        elif self._nombre_existe_en_agentes(nombre):
            errores.append(f"Ya existe un agente con el nombre '{nombre}'")
            self.input_nombre.setStyleSheet("border-color: #dc3545; background-color: #fff5f5;")
        else:
            self.input_nombre.setStyleSheet("")

        tipo = self.input_tipo.currentText()
        contenido = self.text_edit.toPlainText().strip()

        if tipo == "HTTP":
            if not contenido:
                errores.append("La URL es obligatoria")
            elif not contenido.startswith(('http://', 'https://')):
                errores.append("La URL debe comenzar con http:// o https://")

            headers_text = self.headers_http.toPlainText().strip()
            if headers_text:
                es_valido, msg_error, _ = ConfigValidator.validar_json(headers_text, "Headers")
                if not es_valido:
                    errores.append(msg_error)

        elif tipo == "LLM":
            if not contenido:
                errores.append("El prompt es obligatorio")

        elif tipo == "Loop":
            if not self.input_fuente_items.text().strip():
                errores.append("La fuente de items es obligatoria")
            if not self.text_codigo_loop.toPlainText().strip():
                errores.append("El código del loop es obligatorio")

        if errores:
            self.status_icon.setText("❌")
            self.status_text.setText(f"Error: {errores[0]}")
            self.status_text.setStyleSheet("color: #dc3545;")
            self.status_details.setText(f"({len(errores)} error(es))")
            self.btn_guardar.setEnabled(False)
        elif advertencias:
            self.status_icon.setText("⚠️")
            self.status_text.setText(f"Advertencia: {advertencias[0]}")
            self.status_text.setStyleSheet("color: #ffc107;")
            self.status_details.setText(f"({len(advertencias)} advertencia(s))")
            self.btn_guardar.setEnabled(True)
        else:
            self.status_icon.setText("✅")
            self.status_text.setText("Configuración válida")
            self.status_text.setStyleSheet("color: #28a745;")
            self.status_details.setText("Listo para guardar")
            self.btn_guardar.setEnabled(not self.modo_lectura)

    # ============================================================
    # CARGA Y GUARDADO
    # ============================================================
    def _cargar_datos(self):
        if not self.agente:
            self._on_tipo_cambiado(self.input_tipo.currentText())
            self._validar_campos()
            return

        agente = self.agente

        self.input_nombre.setText(agente.nombre)
        self.input_tipo.setCurrentText(agente.tipo.value)
        self.input_descripcion.setText(agente.descripcion)
        self.input_duracion.setValue(agente.duracion)
        self.input_reintentos.setValue(agente.max_reintentos)

        if agente.dependencias_nombres:
            self.input_deps.setText(", ".join(agente.dependencias_nombres))

        self._tipo_actual = agente.tipo.value

        if agente.tipo == TipoAgente.LLM:
            self.text_edit.setPlainText(agente.prompt_llm)
            self.modelo_llm.setCurrentText(agente.modelo_llm)
            self.temp_llm.setValue(agente.temperatura_llm)
            self.max_tokens_llm.setValue(agente.max_tokens_llm)

            # ✅ NUEVO: cargar reasoning_effort y thinking_enabled
            reasoning = getattr(agente, 'reasoning_effort_llm', 'low')
            idx = self.reasoning_effort_llm.findText(reasoning)
            if idx >= 0:
                self.reasoning_effort_llm.setCurrentIndex(idx)
            else:
                self.reasoning_effort_llm.setCurrentText("low")

            self.thinking_enabled_llm.setChecked(
                bool(getattr(agente, 'thinking_enabled_llm', False))
            )

        elif agente.tipo == TipoAgente.SHELL:
            self.text_edit.setPlainText(agente.comando_shell)
            self.timeout_shell.setValue(agente.timeout_shell)
            self.working_dir.setText(agente.working_dir)

        elif agente.tipo == TipoAgente.HTTP:
            self.text_edit.setPlainText(agente.url_http)
            if agente.metodo_http in METODOS_HTTP_SOPORTADOS:
                self.metodo_http.setCurrentText(agente.metodo_http)
            self.timeout_http.setValue(agente.timeout_http)
            headers = getattr(agente, 'headers_http', {})
            if headers:
                try:
                    self.headers_http.setPlainText(json.dumps(headers, indent=2, ensure_ascii=False))
                except:
                    self.headers_http.setPlainText("")
            self.body_http.setPlainText(getattr(agente, 'body_http', ''))

        elif agente.tipo == TipoAgente.FILE:
            self.operacion_file.setCurrentText(agente.operacion_file)
            self.archivo_origen.setText(agente.archivo_origen)
            self.archivo_destino.setText(agente.archivo_destino)
            # ✅ NUEVO: cargar modo de salida
            modo = getattr(agente, 'modo_salida_file', 'auto')
            idx = self.modo_salida_file.findText(modo)
            if idx >= 0:
                self.modo_salida_file.setCurrentIndex(idx)

        elif agente.tipo == TipoAgente.LOOP:
            self.input_fuente_items.setText(getattr(agente, 'fuente_items', ''))
            self.text_codigo_loop.setPlainText(getattr(agente, 'codigo_por_item', ''))
            self.input_max_iteraciones.setValue(getattr(agente, 'max_iteraciones', 100))
            self.input_timeout_loop.setValue(getattr(agente, 'timeout_loop', 300))
            self.input_timeout_item.setValue(getattr(agente, 'timeout_python', 30))
            self.check_continuar_en_error.setChecked(getattr(agente, 'continuar_en_error', False))

        elif agente.tipo == TipoAgente.PYTHON:
            self.text_edit.setPlainText(agente.codigo_python or "")
            self.timeout_python.setValue(agente.timeout_python)

        self._actualizar_contadores()
        self._actualizar_contadores_loop()
        self._on_tipo_cambiado(agente.tipo.value)
        self._validar_campos()
        self._actualizar_vista_previa()

    def _guardar(self):
        nombre = self.input_nombre.text().strip()

        if not nombre:
            QMessageBox.warning(self, "Error", "El nombre es obligatorio")
            self.input_nombre.setFocus()
            return

        if self._nombre_existe_en_agentes(nombre):
            QMessageBox.warning(self, "Error", f"Ya existe un agente con el nombre '{nombre}'")
            self.input_nombre.setFocus()
            return

        try:
            if self.agente:
                agente = self.agente
                agente_id = agente.id
                self._resetear_campos_agente(agente)
                agente.id = agente_id
            else:
                agente = Agente()

            agente.nombre = nombre
            agente.tipo = TipoAgente(self.input_tipo.currentText())
            agente.descripcion = self.input_descripcion.text().strip()
            agente.duracion = self.input_duracion.value()
            agente.max_reintentos = self.input_reintentos.value()

            deps = [d.strip() for d in self.input_deps.text().split(",") if d.strip()]
            agente.dependencias_nombres = deps

            tipo = agente.tipo
            contenido = self.text_edit.toPlainText().strip()

            if tipo == TipoAgente.LLM:
                agente.prompt_llm = contenido
                agente.modelo_llm = self.modelo_llm.currentText().strip()
                agente.temperatura_llm = self.temp_llm.value()
                agente.max_tokens_llm = self.max_tokens_llm.value()

                # ✅ NUEVO: guardar reasoning_effort y thinking_enabled
                agente.reasoning_effort_llm = (
                    self.reasoning_effort_llm.currentText()
                )
                agente.thinking_enabled_llm = (
                    self.thinking_enabled_llm.isChecked()
                )

            elif tipo == TipoAgente.SHELL:
                agente.comando_shell = contenido
                agente.timeout_shell = self.timeout_shell.value()
                agente.working_dir = self.working_dir.text().strip()

            elif tipo == TipoAgente.HTTP:
                agente.url_http = contenido
                agente.metodo_http = self.metodo_http.currentText()
                agente.timeout_http = self.timeout_http.value()

                headers_text = self.headers_http.toPlainText().strip()
                if headers_text:
                    try:
                        headers_dict = json.loads(headers_text)
                        if isinstance(headers_dict, dict):
                            agente.headers_http = headers_dict
                        else:
                            agente.headers_http = {}
                    except json.JSONDecodeError:
                        agente.headers_http = {}
                else:
                    agente.headers_http = {}

                agente.body_http = self.body_http.toPlainText().strip()

            elif tipo == TipoAgente.FILE:
                agente.operacion_file = self.operacion_file.currentText()
                agente.archivo_origen = self.archivo_origen.text().strip()
                agente.archivo_destino = self.archivo_destino.text().strip()
                # ✅ NUEVO: guardar modo de salida
                agente.modo_salida_file = self.modo_salida_file.currentText()

            elif tipo == TipoAgente.LOOP:
                agente.fuente_items = self.input_fuente_items.text().strip()
                agente.codigo_por_item = self.text_codigo_loop.toPlainText().strip()
                agente.max_iteraciones = self.input_max_iteraciones.value()
                agente.timeout_loop = self.input_timeout_loop.value()
                agente.timeout_python = self.input_timeout_item.value()
                agente.continuar_en_error = self.check_continuar_en_error.isChecked()

            elif tipo == TipoAgente.PYTHON:
                agente.codigo_python = contenido
                agente.timeout_python = self.timeout_python.value()

            es_valido, mensaje = agente.validar_configuracion()
            if not es_valido:
                QMessageBox.warning(self, "Error de configuración", mensaje)
                return

            self.resultado = agente
            self.agent_updated.emit(agente)
            self.accept()

            logger.info(f"Agente guardado: {agente.nombre} ({agente.tipo.value})")

        except Exception as e:
            logger.exception("Error al guardar agente")
            QMessageBox.critical(self, "Error", f"Error al guardar el agente:\n{str(e)}")

    def _resetear_campos_agente(self, agente: Agente):
        for defaults in (_DEFAULTS_PYTHON, _DEFAULTS_SHELL, _DEFAULTS_HTTP,
                         _DEFAULTS_LLM, _DEFAULTS_FILE, _DEFAULTS_LOOP):
            for campo, valor in defaults.items():
                if hasattr(agente, campo):
                    setattr(agente, campo, valor)

    # ============================================================
    # ASISTENTE IA (DIÁLOGO COMPLETO)
    # ============================================================
    def _abrir_asistente_ia(self):
        """Abre el diálogo completo del asistente IA."""
        if not self.ai_assistant:
            QMessageBox.warning(
                self, "IA no disponible",
                "El asistente IA no está disponible.\n"
                "Configura la variable de entorno DEEPSEEK_API_KEY."
            )
            return

        try:
            from ui.ai_assistant_dialog import AIAssistantDialog

            tipo_actual = TipoAgente(self.input_tipo.currentText())
            contenido_actual = self._obtener_contenido_principal()
            agente_config = self._obtener_config_actual()

            dialog = AIAssistantDialog(
                parent=self,
                ai_assistant=self.ai_assistant,
                tipo_actual=tipo_actual,
                contenido_actual=contenido_actual,
                agente_config=agente_config
            )

            dialog.agente_generado.connect(self._aplicar_agente_de_ia)
            dialog.contenido_generado.connect(self._aplicar_contenido_de_ia)
            dialog.contenido_mejorado.connect(self._aplicar_contenido_mejorado)

            dialog.exec()

        except Exception as e:
            logger.exception("Error abriendo asistente IA")
            QMessageBox.critical(self, "Error", f"Error al abrir el asistente IA:\n{e}")

    def _obtener_config_actual(self) -> Dict:
        tipo = self.input_tipo.currentText()
        config = {
            'nombre': self.input_nombre.text().strip(),
            'tipo': tipo,
            'descripcion': self.input_descripcion.text().strip(),
            'dependencias': [d.strip() for d in self.input_deps.text().split(',') if d.strip()],
        }

        if tipo == "Python":
            config['codigo'] = self.text_edit.toPlainText()
            config['timeout'] = self.timeout_python.value()
        elif tipo == "HTTP":
            config['url'] = self.text_edit.toPlainText()
            config['metodo'] = self.metodo_http.currentText()
            config['headers'] = self.headers_http.toPlainText()
            config['body'] = self.body_http.toPlainText()
        elif tipo == "LLM":
            # ✅ NUEVO: exponer la config LLM al asistente IA
            config['prompt'] = self.text_edit.toPlainText()
            config['modelo'] = self.modelo_llm.currentText().strip()
            config['temperatura'] = self.temp_llm.value()
            config['max_tokens'] = self.max_tokens_llm.value()
            config['reasoning_effort'] = self.reasoning_effort_llm.currentText()
            config['thinking_enabled'] = self.thinking_enabled_llm.isChecked()
        elif tipo == "Shell":
            config['comando'] = self.text_edit.toPlainText()
            config['timeout'] = self.timeout_shell.value()
            config['working_dir'] = self.working_dir.text().strip()
        elif tipo == "File":
            config['operacion'] = self.operacion_file.currentText()
            config['archivo_origen'] = self.archivo_origen.text().strip()
            config['archivo_destino'] = self.archivo_destino.text().strip()
            config['modo_salida_file'] = self.modo_salida_file.currentText()
        elif tipo == "Loop":
            config['fuente_items'] = self.input_fuente_items.text()
            config['codigo_por_item'] = self.text_codigo_loop.toPlainText()

        return config

    def _aplicar_agente_de_ia(self, config: Dict):
        try:
            if config.get('nombre'):
                self.input_nombre.setText(config['nombre'])
            if config.get('tipo') and config['tipo'] in TIPOS_AGENTES:
                self.input_tipo.setCurrentText(config['tipo'])
            if config.get('descripcion'):
                self.input_descripcion.setText(config['descripcion'])
            if config.get('contenido'):
                self.text_edit.setPlainText(config['contenido'])

            config_avanzada = config.get('configuracion_avanzada', {})
            self._aplicar_config_avanzada_ia_dialog(config.get('tipo', ''), config_avanzada)

            self._actualizar_contadores()
            self._validar_campos()
            self._actualizar_vista_previa()
            self.tabs.setCurrentIndex(1)

            logger.info(f"Configuración de IA aplicada: {config.get('nombre')}")
        except Exception as e:
            logger.exception("Error aplicando configuración de IA")
            QMessageBox.warning(self, "Error", f"No se pudo aplicar la configuración:\n{e}")

    def _aplicar_config_avanzada_ia_dialog(self, tipo: str, config: Dict):
        """Aplica configuración avanzada desde el diálogo IA."""
        if not config:
            return

        try:
            if tipo == "HTTP":
                if 'metodo' in config and config['metodo'] in METODOS_HTTP_SOPORTADOS:
                    self.metodo_http.setCurrentText(config['metodo'])
                if 'headers' in config:
                    headers = config['headers']
                    if isinstance(headers, dict):
                        self.headers_http.setPlainText(
                            json.dumps(headers, indent=2, ensure_ascii=False)
                        )
                    elif isinstance(headers, str):
                        self.headers_http.setPlainText(headers)
                if 'body' in config:
                    body = config['body']
                    if isinstance(body, dict):
                        self.body_http.setPlainText(
                            json.dumps(body, indent=2, ensure_ascii=False)
                        )
                    elif isinstance(body, str):
                        self.body_http.setPlainText(body)

            # ✅ NUEVO: rama LLM
            elif tipo == "LLM":
                if 'modelo' in config:
                    self.modelo_llm.setCurrentText(str(config['modelo']))
                if 'temperatura' in config:
                    try:
                        self.temp_llm.setValue(float(config['temperatura']))
                    except (ValueError, TypeError):
                        pass
                if 'max_tokens' in config:
                    try:
                        self.max_tokens_llm.setValue(int(config['max_tokens']))
                    except (ValueError, TypeError):
                        pass
                if 'reasoning_effort' in config:
                    reasoning = str(config['reasoning_effort']).lower()
                    idx = self.reasoning_effort_llm.findText(reasoning)
                    if idx >= 0:
                        self.reasoning_effort_llm.setCurrentIndex(idx)
                if 'thinking_enabled' in config:
                    self.thinking_enabled_llm.setChecked(
                        bool(config['thinking_enabled'])
                    )

            elif tipo == "Loop":
                if 'fuente_items' in config:
                    self.input_fuente_items.setText(config['fuente_items'])
                if 'max_iteraciones' in config:
                    self.input_max_iteraciones.setValue(int(config['max_iteraciones']))

        except Exception as e:
            logger.warning(f"Error aplicando config avanzada: {e}")

    def _aplicar_contenido_de_ia(self, contenido: str):
        tipo = self.input_tipo.currentText()
        if tipo == "Loop":
            self.text_codigo_loop.setPlainText(contenido)
            self._actualizar_contadores_loop()
        else:
            self.text_edit.setPlainText(contenido)
            self._actualizar_contadores()
        self._validar_campos()

    def _aplicar_contenido_mejorado(self, contenido: str):
        reply = QMessageBox.question(
            self, "Aplicar mejoras",
            "¿Reemplazar el contenido actual con la versión mejorada?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._aplicar_contenido_de_ia(contenido)

    # ============================================================
    # AYUDA
    # ============================================================
    def _mostrar_ayuda(self):
        help_text = """
<h2>❓ Ayuda - Configuración de Agentes</h2>

<h3>🤖 Asistente IA</h3>
<p>El Asistente IA analiza el formulario en tiempo real y sugiere valores.</p>
<ul>
  <li>Escribe una descripción clara de lo que quieres que haga el agente</li>
  <li>La IA completará automáticamente los campos vacíos</li>
  <li>Puedes usar el botón "✨ Asistente IA" para más opciones</li>
</ul>

<h3>📋 Campos Generales</h3>
<ul>
  <li><b>Nombre</b>: Identificador único del agente</li>
  <li><b>Tipo</b>: Python, Shell, HTTP, LLM, File, Loop</li>
  <li><b>Descripción</b>: Qué hace el agente</li>
  <li><b>Dependencias</b>: Nombres de agentes de los que depende</li>
</ul>

<h3>⌨️ Atajos</h3>
<ul>
  <li><b>Ctrl+Enter</b> - Guardar</li>
  <li><b>Ctrl+W</b> - Cerrar</li>
  <li><b>F1</b> - Ayuda</li>
</ul>
"""
        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("❓ Ayuda")
        msg_box.setTextFormat(Qt.TextFormat.RichText)
        msg_box.setText(help_text)
        msg_box.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg_box.exec()

    # ============================================================
    # LOGGING
    # ============================================================
    def _log(self, mensaje: str, color: str = "#333"):
        logger.debug(f"[AgentConfigDialog] {mensaje}")

    # ============================================================
    # OBTENER AGENTE
    # ============================================================
    def obtener_agente(self) -> Optional[Agente]:
        return self.resultado