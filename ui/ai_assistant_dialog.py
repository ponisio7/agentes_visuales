# ui/ai_assistant_dialog.py
"""
Diálogo del Asistente IA para creación de agentes.
Permite al usuario interactuar con la IA para generar, mejorar y explicar código.
"""

import json
import logging
from typing import Optional, Dict, Any, List

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QLabel, QComboBox, QGroupBox, QMessageBox, QSplitter,
    QWidget, QFrame, QProgressBar, QApplication, QScrollArea,
    QRadioButton, QButtonGroup, QSizePolicy, QCheckBox
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QObject, QTimer
from PyQt6.QtGui import QFont, QColor, QTextCursor

from core.ai_assistant import AIAssistant
from core.agent import TipoAgente

logger = logging.getLogger(__name__)


# ============================================================
# WORKER PARA EJECUCIÓN EN HILO SEPARADO
# ============================================================
class AIWorker(QObject):
    """Worker para ejecutar tareas de IA en un hilo separado."""

    finished = pyqtSignal(object)
    error = pyqtSignal(str)
    started = pyqtSignal()
    progress = pyqtSignal(str)

    def __init__(self, assistant: AIAssistant, task: str, **kwargs):
        super().__init__()
        self.assistant = assistant
        self.task = task
        self.kwargs = kwargs

    def run(self):
        """Ejecuta la tarea de IA."""
        try:
            self.started.emit()

            if self.task == "generar_agente":
                resultado = self.assistant.generar_agente_completo(
                    self.kwargs.get('descripcion', '')
                )
            elif self.task == "generar_contenido":
                resultado = self.assistant.generar_contenido(
                    self.kwargs.get('tipo'),
                    self.kwargs.get('descripcion', ''),
                    self.kwargs.get('contexto')
                )
            elif self.task == "mejorar_contenido":
                resultado = self.assistant.mejorar_contenido(
                    self.kwargs.get('tipo'),
                    self.kwargs.get('contenido', ''),
                    self.kwargs.get('instruccion', '')
                )
            elif self.task == "explicar_contenido":
                resultado = self.assistant.explicar_contenido(
                    self.kwargs.get('tipo'),
                    self.kwargs.get('contenido', '')
                )
            elif self.task == "sugerir_mejoras":
                resultado = self.assistant.sugerir_mejoras(
                    self.kwargs.get('config', {})
                )
            # ✅ NUEVO: mejorar configuración completa del agente
            elif self.task == "mejorar_configuracion":
                resultado = self._mejorar_configuracion_agente(
                    self.kwargs.get('config', {}),
                    self.kwargs.get('instruccion', ''),
                    self.kwargs.get('tipo', None)
                )
            else:
                raise ValueError(f"Tarea desconocida: {self.task}")

            self.finished.emit(resultado)

        except Exception as e:
            logger.exception(f"Error en AIWorker: {e}")
            self.error.emit(str(e))

    def _mejorar_configuracion_agente(
        self,
        config: Dict,
        instruccion: str,
        tipo
    ) -> Dict:
        """
        Mejora la configuración completa de un agente usando IA.

        Devuelve un dict con los campos modificados (parcial), que el
        diálogo aplicará campo por campo respetando la elección del usuario.
        """
        import json as _json

        tipo_str = tipo.value if hasattr(tipo, 'value') else str(tipo or '')

        if not instruccion or not instruccion.strip():
            instruccion = (
                "Mejora la configuración de este agente: "
                "optimiza los parámetros, ajusta temperatura, max_tokens, "
                "reasoning_effort y thinking_enabled según el tipo de tarea. "
                "Explica brevemente los cambios propuestos."
            )

        system_prompt = (
            "Eres un experto en configuración de agentes LLM y automatizaciones.\n"
            "Recibirás la configuración actual de un agente y una instrucción.\n"
            "Devuelve SOLO un objeto JSON con los campos que deben cambiarse.\n\n"
            "Campos válidos según el tipo de agente:\n"
            "- LLM: prompt, modelo, temperatura, max_tokens, "
            "reasoning_effort ('low'|'medium'|'high'), thinking_enabled (bool)\n"
            "- HTTP: url, metodo, headers, body, timeout\n"
            "- Python: codigo, timeout\n"
            "- Shell: comando, timeout, working_dir\n"
            "- File: operacion, archivo_origen, archivo_destino, modo_salida_file\n"
            "- Loop: fuente_items, codigo_por_item, max_iteraciones, "
            "timeout_loop, timeout_python, continuar_en_error\n\n"
            "Reglas:\n"
            "- Devuelve SOLO los campos que cambies, no todos.\n"
            "- Incluye una clave '_explicacion' con un resumen breve en español.\n"
            "- No uses markdown (```json).\n"
            "- Sé conservador: no rompas la funcionalidad actual."
        )

        user_prompt = (
            f"TIPO DE AGENTE: {tipo_str}\n\n"
            f"CONFIGURACIÓN ACTUAL:\n"
            f"{_json.dumps(config, indent=2, ensure_ascii=False, default=str)}\n\n"
            f"INSTRUCCIÓN:\n{instruccion}"
        )

        respuesta = self.assistant.client.chat(
            prompt=user_prompt,
            system_prompt=system_prompt,
            temperature=0.3,
            max_tokens=1500
        )

        from core.utils import extraer_json_de_llm
        data = extraer_json_de_llm(respuesta) or {}
        return data


# ============================================================
# DIÁLOGO PRINCIPAL DEL ASISTENTE IA
# ============================================================
class AIAssistantDialog(QDialog):
    """
    Diálogo del Asistente IA para creación y mejora de agentes.
    """

    # Señales
    agente_generado = pyqtSignal(dict)      # Emite dict con configuración completa
    contenido_generado = pyqtSignal(str)    # Emite contenido generado
    contenido_mejorado = pyqtSignal(str)    # Emite contenido mejorado

    def __init__(
        self,
        parent=None,
        ai_assistant: AIAssistant = None,
        tipo_actual: TipoAgente = None,
        contenido_actual: str = "",
        agente_config: Dict = None
    ):
        super().__init__(parent)

        self.ai_assistant = ai_assistant
        self.tipo_actual = tipo_actual or TipoAgente.PYTHON
        self.contenido_actual = contenido_actual
        self.agente_config = agente_config or {}

        self._worker = None
        self._thread = None
        self._ultimo_resultado = None
        self._ultimo_modo = -1

        self.setWindowTitle("✨ Asistente IA")
        self.setMinimumSize(850, 750)
        self.setModal(True)

        self._init_ui()
        self._verificar_disponibilidad()
        self._on_modo_cambiado()

    # ============================================================
    # CONSTRUCCIÓN DE LA UI
    # ============================================================
    def _init_ui(self):
        """Inicializa la interfaz."""
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(15, 15, 15, 15)

        # ── Header ──
        self._crear_header(layout)

        # ── Selector de Modo ──
        self._crear_modos(layout)

        # ── Área de entrada ──
        self._crear_input(layout)

        # ── Área de resultado ──
        self._crear_resultado(layout)

        # ── Botones ──
        self._crear_botones(layout)

        # Aplicar estilo
        self._aplicar_estilo()

    def _crear_header(self, layout: QVBoxLayout):
        """Crea el header del diálogo."""
        header = QLabel("✨ Asistente IA para Agentes")
        header.setStyleSheet("""
            QLabel {
                font-size: 20px;
                font-weight: bold;
                color: #6f42c1;
                padding: 5px;
            }
        """)
        layout.addWidget(header)

        # Estado de disponibilidad
        self.lbl_estado = QLabel("")
        self.lbl_estado.setStyleSheet("font-size: 11px; color: #6c757d;")
        layout.addWidget(self.lbl_estado)

        # Separador
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)

    def _crear_modos(self, layout: QVBoxLayout):
        """Crea el selector de modos."""
        modo_group = QGroupBox("🎯 ¿Qué quieres hacer?")
        modo_group.setStyleSheet("""
            QGroupBox {
                font-weight: bold;
                border: 2px solid #6f42c1;
                border-radius: 6px;
                margin-top: 10px;
                padding-top: 15px;
            }
            QGroupBox::title {
                color: #6f42c1;
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 5px;
            }
        """)
        modo_layout = QVBoxLayout(modo_group)

        self.modo_group = QButtonGroup(self)

        # Modo 0: Generar agente completo
        self.radio_agente_completo = QRadioButton(
            "🚀 Generar agente COMPLETO desde descripción"
        )
        self.radio_agente_completo.setToolTip(
            "Describe lo que quieres que haga el agente y la IA generará\n"
            "todos los campos: nombre, tipo, código/prompt, configuración, etc."
        )
        self.radio_agente_completo.setChecked(True)
        self.modo_group.addButton(self.radio_agente_completo, 0)
        modo_layout.addWidget(self.radio_agente_completo)

        # Modo 1: Generar contenido
        self.radio_contenido = QRadioButton(
            "📝 Generar contenido (código/prompt/comando) para el agente actual"
        )
        self.radio_contenido.setToolTip(
            "Genera solo el contenido principal (código, prompt, URL, etc.)\n"
            "basándose en el tipo de agente seleccionado."
        )
        self.modo_group.addButton(self.radio_contenido, 1)
        modo_layout.addWidget(self.radio_contenido)

        # Modo 2: Mejorar contenido
        self.radio_mejorar = QRadioButton(
            "🔧 Mejorar el contenido actual del agente"
        )
        self.radio_mejorar.setToolTip(
            "Toma el código/prompt actual y lo mejora: optimiza, añade\n"
            "manejo de errores, mejora legibilidad, etc."
        )
        self.modo_group.addButton(self.radio_mejorar, 2)
        modo_layout.addWidget(self.radio_mejorar)

        # Modo 3: Explicar contenido
        self.radio_explicar = QRadioButton(
            "💡 Explicar qué hace el contenido actual"
        )
        self.radio_explicar.setToolTip(
            "La IA analiza el código/prompt actual y explica qué hace."
        )
        self.modo_group.addButton(self.radio_explicar, 3)
        modo_layout.addWidget(self.radio_explicar)

        # Modo 4: Sugerir mejoras
        self.radio_sugerencias = QRadioButton(
            "🎯 Sugerir mejoras para la configuración del agente"
        )
        self.radio_sugerencias.setToolTip(
            "La IA analiza toda la configuración del agente y sugiere\n"
            "mejoras concretas y accionables."
        )
        self.modo_group.addButton(self.radio_sugerencias, 4)
        modo_layout.addWidget(self.radio_sugerencias)

        # ✅ NUEVO: Modo 5 - Mejorar configuración (aplica cambios)
        self.radio_mejorar_config = QRadioButton(
            "⚙️ Mejorar configuración completa y aplicarla (temperatura, "
            "max_tokens, reasoning, thinking, etc.)"
        )
        self.radio_mejorar_config.setToolTip(
            "La IA propone cambios en los parámetros del agente (temperatura,\n"
            "max_tokens, reasoning_effort, thinking_enabled, etc.) y los\n"
            "aplica automáticamente. Ideal para afinar agentes LLM."
        )
        self.modo_group.addButton(self.radio_mejorar_config, 5)
        modo_layout.addWidget(self.radio_mejorar_config)

        self.modo_group.buttonClicked.connect(self._on_modo_cambiado)

        layout.addWidget(modo_group)

    def _crear_input(self, layout: QVBoxLayout):
        """Crea el área de entrada."""
        input_group = QGroupBox("💬 Descripción / Instrucción")
        input_layout = QVBoxLayout(input_group)

        self.input_text = QTextEdit()
        self.input_text.setPlaceholderText(
            "Describe lo que quieres que haga el agente...\n\n"
            "Ejemplos:\n"
            "• 'Un agente que consulte el clima en Madrid y lo guarde en un archivo'\n"
            "• 'Procesar una lista de URLs y extraer el título de cada página'\n"
            "• 'Analizar un texto y clasificar su sentimiento'\n"
            "• 'Mejorar este código añadiendo manejo de errores'"
        )
        self.input_text.setFont(QFont("Segoe UI", 11))
        self.input_text.setMinimumHeight(100)
        self.input_text.setMaximumHeight(150)
        input_layout.addWidget(self.input_text)

        # Instrucciones rápidas (para modo mejorar)
        self.instrucciones_rapidas = QWidget()
        instrucciones_layout = QHBoxLayout(self.instrucciones_rapidas)
        instrucciones_layout.setContentsMargins(0, 0, 0, 0)

        for texto, instruccion in [
            ("🔧 Optimizar", "Optimiza este código para mejor rendimiento"),
            ("🛡️ Errores", "Añade manejo de errores robusto"),
            ("📖 Documentar", "Añade comentarios claros y documentación"),
            ("✨ Mejorar", "Mejora la legibilidad y calidad general"),
            ("⚡ Performance", "Optimiza el rendimiento y reduce el tiempo de ejecución"),
        ]:
            btn = QPushButton(texto)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #e9ecef;
                    border: 1px solid #d1d5db;
                    border-radius: 3px;
                    padding: 4px 10px;
                    font-size: 10px;
                }
                QPushButton:hover {
                    background-color: #dee2e6;
                    border-color: #6f42c1;
                }
            """)
            btn.clicked.connect(lambda checked, i=instruccion: self._insertar_instruccion(i))
            instrucciones_layout.addWidget(btn)

        instrucciones_layout.addStretch()
        input_layout.addWidget(self.instrucciones_rapidas)
        self.instrucciones_rapidas.setVisible(False)

        layout.addWidget(input_group)

    def _crear_resultado(self, layout: QVBoxLayout):
        """Crea el área de resultado."""
        result_group = QGroupBox("📤 Resultado")
        result_layout = QVBoxLayout(result_group)

        # Barra de herramientas del resultado
        toolbar = QHBoxLayout()

        self.btn_copiar = QPushButton("📋 Copiar")
        self.btn_copiar.setEnabled(False)
        self.btn_copiar.setStyleSheet("""
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
            QPushButton:disabled {
                background-color: #6c757d;
                color: #adb5bd;
            }
        """)
        self.btn_copiar.clicked.connect(self._copiar_resultado)
        toolbar.addWidget(self.btn_copiar)

        self.btn_limpiar_resultado = QPushButton("🗑 Limpiar")
        self.btn_limpiar_resultado.setStyleSheet("""
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
        self.btn_limpiar_resultado.clicked.connect(lambda: self.result_text.clear())
        toolbar.addWidget(self.btn_limpiar_resultado)

        toolbar.addStretch()

        self.lbl_tokens = QLabel("")
        self.lbl_tokens.setStyleSheet("color: #6c757d; font-size: 9px;")
        toolbar.addWidget(self.lbl_tokens)

        result_layout.addLayout(toolbar)

        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setFont(QFont("Consolas", 10))
        self.result_text.setStyleSheet("""
            QTextEdit {
                background-color: #f8f9fa;
                border: 1px solid #d1d5db;
                border-radius: 4px;
                padding: 8px;
                min-height: 200px;
            }
        """)
        self.result_text.setPlaceholderText("El resultado de la IA aparecerá aquí...")
        result_layout.addWidget(self.result_text, stretch=1)

        # Barra de progreso
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # Indeterminado
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumHeight(8)
        self.progress_bar.setStyleSheet("""
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
        result_layout.addWidget(self.progress_bar)

        layout.addWidget(result_group, stretch=1)

    def _crear_botones(self, layout: QVBoxLayout):
        """Crea los botones inferiores."""
        btn_layout = QHBoxLayout()

        self.btn_ejecutar = QPushButton("✨ Ejecutar")
        self.btn_ejecutar.setStyleSheet("""
            QPushButton {
                background-color: #6f42c1;
                color: white;
                font-weight: bold;
                padding: 10px 30px;
                border-radius: 4px;
                font-size: 13px;
                min-width: 140px;
            }
            QPushButton:hover {
                background-color: #5a32a3;
            }
            QPushButton:disabled {
                background-color: #6c757d;
                color: #adb5bd;
            }
        """)
        self.btn_ejecutar.clicked.connect(self._ejecutar)
        btn_layout.addWidget(self.btn_ejecutar)

        self.btn_aplicar = QPushButton("✅ Aplicar")
        self.btn_aplicar.setEnabled(False)
        self.btn_aplicar.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: white;
                font-weight: bold;
                padding: 10px 30px;
                border-radius: 4px;
                font-size: 13px;
                min-width: 140px;
            }
            QPushButton:hover {
                background-color: #218838;
            }
            QPushButton:disabled {
                background-color: #6c757d;
                color: #adb5bd;
            }
        """)
        self.btn_aplicar.clicked.connect(self._aplicar_resultado)
        btn_layout.addWidget(self.btn_aplicar)

        btn_layout.addStretch()

        # ✅ NUEVO: botón "Restablecer" para deshacer cambios visuales
        self.btn_restablecer = QPushButton("🔄 Restablecer")
        self.btn_restablecer.setToolTip(
            "Restablecer el resultado actual (no afecta al agente)"
        )
        self.btn_restablecer.setStyleSheet("""
            QPushButton {
                background-color: #fd7e14;
                color: white;
                padding: 10px 20px;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: #e06b0a;
            }
        """)
        self.btn_restablecer.clicked.connect(self._restablecer_resultado)
        btn_layout.addWidget(self.btn_restablecer)

        btn_cerrar = QPushButton("❌ Cerrar")
        btn_cerrar.clicked.connect(self.reject)
        btn_cerrar.setStyleSheet("""
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
        btn_layout.addWidget(btn_cerrar)

        layout.addLayout(btn_layout)

    def _restablecer_resultado(self):
        """Limpia el resultado actual sin afectar al agente."""
        self.result_text.clear()
        self._ultimo_resultado = None
        self._ultimo_modo = -1
        self.btn_aplicar.setEnabled(False)
        self.btn_copiar.setEnabled(False)

    # ============================================================
    # ESTILO
    # ============================================================
    def _aplicar_estilo(self):
        """Aplica el estilo al diálogo."""
        self.setStyleSheet("""
            QDialog {
                background-color: #ffffff;
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
                border: 1px solid #d1d5db;
                border-radius: 4px;
                padding: 4px;
            }
            QTextEdit:focus {
                border-color: #6f42c1;
            }
            QRadioButton {
                spacing: 8px;
                padding: 4px;
            }
            QRadioButton:hover {
                background-color: #f8f9fa;
                border-radius: 4px;
            }
            QRadioButton:checked {
                color: #6f42c1;
                font-weight: bold;
            }
        """)

    # ============================================================
    # VERIFICACIÓN DE DISPONIBILIDAD
    # ============================================================
    def _verificar_disponibilidad(self):
        """Verifica si el asistente IA está disponible."""
        if self.ai_assistant and self.ai_assistant.disponible:
            self.lbl_estado.setText("✅ IA disponible (DeepSeek)")
            self.lbl_estado.setStyleSheet("color: #28a745; font-size: 11px;")
            self.btn_ejecutar.setEnabled(True)
        else:
            self.lbl_estado.setText(
                "❌ IA no disponible. Configura la variable de entorno DEEPSEEK_API_KEY"
            )
            self.lbl_estado.setStyleSheet("color: #dc3545; font-size: 11px;")
            self.btn_ejecutar.setEnabled(False)

    # ============================================================
    # MANEJO DE MODOS
    # ============================================================
    def _on_modo_cambiado(self):
        """Actualiza la UI según el modo seleccionado."""
        modo = self.modo_group.checkedId()

        # Actualizar placeholder del input
        placeholders = {
            0: (
                "Describe lo que quieres que haga el agente completo...\n\n"
                "Ejemplo:\n"
                "'Un agente que consulte el clima en Madrid usando una API "
                "pública y lo guarde en un archivo JSON'"
            ),
            1: (
                f"Describe qué debe hacer el agente {self.tipo_actual.value}...\n\n"
                "Ejemplo:\n"
                "'Procesar datos JSON y extraer los campos nombre y email'"
            ),
            2: (
                "Describe qué mejoras quieres (o deja vacío para mejoras generales)...\n\n"
                "Ejemplos:\n"
                "• 'Añade manejo de errores'\n"
                "• 'Optimiza el rendimiento'\n"
                "• 'Añade comentarios'"
            ),
            3: "(Puedes dejar esto vacío, la IA analizará el contenido actual)",
            4: "(Puedes dejar esto vacío, la IA analizará toda la configuración)",
            5: (
                "Describe qué quieres mejorar en la configuración del agente...\n\n"
                "Ejemplos:\n"
                "• 'Optimiza para respuestas más largas y detalladas'\n"
                "• 'Ajusta para que sea más rápido y conciso'\n"
                "• 'Desactiva thinking y baja la temperatura para outputs deterministas'"
            ),
        }

        self.input_text.setPlaceholderText(placeholders.get(modo, ""))

        # Mostrar/ocultar instrucciones rápidas
        self.instrucciones_rapidas.setVisible(modo in (2, 5))

        # Actualizar las instrucciones rápidas según el modo
        self._actualizar_instrucciones_rapidas(modo)

        # Habilitar/deshabilitar según contenido actual
        if modo in (2, 3) and not self.contenido_actual.strip():
            self.result_text.setHtml(
                "<i style='color: #dc3545;'>⚠️ No hay contenido actual para analizar/mejorar.<br>"
                "Primero escribe algo en el editor del agente.</i>"
            )
            self.btn_ejecutar.setEnabled(False)
        elif modo == 4 and not self.agente_config:
            self.result_text.setHtml(
                "<i style='color: #dc3545;'>⚠️ No hay configuración del agente para analizar.</i>"
            )
            self.btn_ejecutar.setEnabled(False)
        elif modo == 5 and not self.agente_config:
            self.result_text.setHtml(
                "<i style='color: #dc3545;'>⚠️ No hay configuración del agente para mejorar.</i>"
            )
            self.btn_ejecutar.setEnabled(False)
        else:
            self.result_text.clear()
            self._verificar_disponibilidad()

        # Limpiar resultado anterior
        self._ultimo_resultado = None
        self.btn_aplicar.setEnabled(False)
        self.btn_copiar.setEnabled(False)

    def _actualizar_instrucciones_rapidas(self, modo: int):
        """
        Actualiza los botones de instrucciones rápidas según el modo.
        Sustituye el contenido del layout existente.
        """
        layout = self.instrucciones_rapidas.layout()
        if layout is None:
            return

        # Vaciar layout
        while layout.count():
            item = layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        # Definir instrucciones según modo
        if modo == 2:
            pares = [
                ("🔧 Optimizar", "Optimiza este código para mejor rendimiento"),
                ("🛡️ Errores", "Añade manejo de errores robusto"),
                ("📖 Documentar", "Añade comentarios claros y documentación"),
                ("✨ Mejorar", "Mejora la legibilidad y calidad general"),
                ("⚡ Performance", "Optimiza el rendimiento y reduce el tiempo de ejecución"),
            ]
        elif modo == 5:
            pares = [
                ("⚡ Más rápido", "Ajusta max_tokens, reasoning_effort y thinking para minimizar latencia"),
                ("🎯 Más preciso", "Sube reasoning_effort a 'medium' o 'high' y ajusta temperatura baja"),
                ("🚫 Sin thinking", "Desactiva thinking_enabled y baja max_tokens a un valor mínimo seguro"),
                ("🧠 Con thinking", "Activa thinking_enabled y sube max_tokens a 6000 o más"),
                ("💬 Más creativo", "Sube temperatura a 0.9-1.2 y desactiva thinking"),
                ("📏 Respuestas largas", "Sube max_tokens a 6000-8000 y baja temperatura a 0.5"),
            ]
        else:
            pares = []

        for texto, instruccion in pares:
            btn = QPushButton(texto)
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #e9ecef;
                    border: 1px solid #d1d5db;
                    border-radius: 3px;
                    padding: 4px 10px;
                    font-size: 10px;
                }
                QPushButton:hover {
                    background-color: #dee2e6;
                    border-color: #6f42c1;
                }
            """)
            btn.clicked.connect(
                lambda checked, i=instruccion: self._insertar_instruccion(i)
            )
            layout.addWidget(btn)

        layout.addStretch()

    def _insertar_instruccion(self, instruccion: str):
        """Inserta una instrucción rápida en el input."""
        current = self.input_text.toPlainText()
        if current:
            self.input_text.setPlainText(f"{current}\n{instruccion}")
        else:
            self.input_text.setPlainText(instruccion)

    # ============================================================
    # EJECUCIÓN DE TAREAS
    # ============================================================
    def _ejecutar(self):
        """Ejecuta la tarea de IA seleccionada."""
        if not self.ai_assistant or not self.ai_assistant.disponible:
            QMessageBox.warning(
                self, "IA no disponible",
                "El asistente IA no está disponible.\n"
                "Configura la variable de entorno DEEPSEEK_API_KEY."
            )
            return

        modo = self.modo_group.checkedId()
        descripcion = self.input_text.toPlainText().strip()

        # Validar entrada según modo
        if modo in (0, 1) and not descripcion:
            QMessageBox.information(
                self, "Descripción requerida",
                "Por favor, describe lo que quieres que haga la IA."
            )
            return

        # El modo 2 permite instrucción vacía (mejora general)
        # El modo 5 permite instrucción vacía (mejora general)
        # Los modos 3 y 4 no requieren descripción

        # Preparar kwargs según modo
        kwargs = {}
        task = ""

        if modo == 0:  # Generar agente completo
            task = "generar_agente"
            kwargs['descripcion'] = descripcion

        elif modo == 1:  # Generar contenido
            task = "generar_contenido"
            kwargs['tipo'] = self.tipo_actual
            kwargs['descripcion'] = descripcion
            kwargs['contexto'] = {
                'dependencias': self.agente_config.get('dependencias', [])
            }

        elif modo == 2:  # Mejorar contenido
            task = "mejorar_contenido"
            kwargs['tipo'] = self.tipo_actual
            kwargs['contenido'] = self.contenido_actual
            kwargs['instruccion'] = descripcion or ""

        elif modo == 3:  # Explicar contenido
            task = "explicar_contenido"
            kwargs['tipo'] = self.tipo_actual
            kwargs['contenido'] = self.contenido_actual

        elif modo == 4:  # Sugerir mejoras
            task = "sugerir_mejoras"
            kwargs['config'] = self.agente_config

        elif modo == 5:  # ✅ NUEVO: Mejorar configuración completa
            task = "mejorar_configuracion"
            kwargs['config'] = self.agente_config
            kwargs['instruccion'] = descripcion or ""
            kwargs['tipo'] = self.tipo_actual

        # Ejecutar en hilo separado
        self._ejecutar_en_hilo(task, **kwargs)

    def _ejecutar_en_hilo(self, task: str, **kwargs):
        """Ejecuta una tarea de IA en un hilo separado."""
        # Limpiar worker anterior si existe
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(1000)

        # Crear nuevo worker y thread
        self._thread = QThread()
        self._worker = AIWorker(self.ai_assistant, task, **kwargs)
        self._worker.moveToThread(self._thread)

        # Conectar señales
        self._thread.started.connect(self._worker.run)
        self._worker.started.connect(self._on_task_started)
        self._worker.finished.connect(self._on_task_finished)
        self._worker.error.connect(self._on_task_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)

        # Iniciar
        self._thread.start()

    # ============================================================
    # MANEJO DE RESULTADOS
    # ============================================================
    def _on_task_started(self):
        """Se llama cuando inicia la tarea."""
        self.progress_bar.setVisible(True)
        self.btn_ejecutar.setEnabled(False)
        self.btn_ejecutar.setText("⏳ Consultando IA...")
        self.result_text.setHtml(
            "<i style='color: #6f42c1;'>✨ Consultando a la IA... esto puede tomar unos segundos.</i>"
        )
        QApplication.processEvents()

    def _on_task_finished(self, resultado):
        """Se llama cuando la tarea termina exitosamente."""
        self.progress_bar.setVisible(False)
        self.btn_ejecutar.setEnabled(True)
        self.btn_ejecutar.setText("✨ Ejecutar")
        self.btn_aplicar.setEnabled(True)
        self.btn_copiar.setEnabled(True)

        modo = self.modo_group.checkedId()
        self._ultimo_modo = modo
        self._ultimo_resultado = resultado

        # Mostrar resultado según modo
        if modo == 0:  # Agente completo
            self._mostrar_resultado_agente(resultado)
        elif modo == 1:  # Contenido generado
            self.result_text.setPlainText(resultado)
            self.result_text.setFont(QFont("Consolas", 10))
        elif modo == 2:  # Contenido mejorado
            self._mostrar_diff(self.contenido_actual, resultado)
        elif modo == 3:  # Explicación
            self.result_text.setPlainText(resultado)
        elif modo == 4:  # Sugerencias
            self._mostrar_sugerencias(resultado)
        elif modo == 5:  # ✅ NUEVO: Configuración mejorada
            self._mostrar_config_mejorada(resultado)

    def _on_task_error(self, error_msg: str):
        """Se llama cuando hay un error en la tarea."""
        self.progress_bar.setVisible(False)
        self.btn_ejecutar.setEnabled(True)
        self.btn_ejecutar.setText("✨ Ejecutar")
        self.btn_aplicar.setEnabled(False)
        self.btn_copiar.setEnabled(False)

        self.result_text.setHtml(
            f"<div style='color: #dc3545; padding: 10px; background-color: #fff5f5; "
            f"border-left: 3px solid #dc3545; border-radius: 4px;'>"
            f"<b>❌ Error:</b><br>{error_msg}"
            f"</div>"
        )

    # ============================================================
    # VISUALIZACIÓN DE RESULTADOS
    # ============================================================
    def _mostrar_resultado_agente(self, resultado: Dict):
        """Muestra el resultado de generación de agente completo."""
        html = "<div style='font-family: Segoe UI;'>"
        html += f"<h3 style='color: #6f42c1;'>🚀 Agente Generado: {resultado.get('nombre', 'Sin nombre')}</h3>"
        html += f"<p><b>Tipo:</b> {resultado.get('tipo', '?')}</p>"
        html += f"<p><b>Descripción:</b> {resultado.get('descripcion', '')}</p>"

        if resultado.get('dependencias_sugeridas'):
            deps = ', '.join(resultado['dependencias_sugeridas'])
            html += f"<p><b>Dependencias sugeridas:</b> {deps}</p>"

        html += "<hr>"
        html += "<h4>📝 Contenido:</h4>"
        html += f"<pre style='background-color: #1e1e2e; color: #cdd6f4; padding: 10px; "
        html += f"border-radius: 4px; font-family: Consolas; font-size: 11px; overflow-x: auto;'>"
        html += resultado.get('contenido', '(vacío)')
        html += "</pre>"

        if resultado.get('configuracion_avanzada'):
            html += "<h4>⚙️ Configuración avanzada:</h4>"
            html += "<pre style='background-color: #f8f9fa; padding: 10px; border-radius: 4px; "
            html += "font-family: Consolas; font-size: 11px;'>"
            config_str = json.dumps(
                resultado['configuracion_avanzada'],
                indent=2,
                ensure_ascii=False,
                default=str
            )
            html += config_str
            html += "</pre>"

        if resultado.get('explicacion'):
            html += "<hr>"
            html += f"<p><b>💡 Explicación:</b> {resultado['explicacion']}</p>"

        html += "</div>"
        self.result_text.setHtml(html)

    def _mostrar_diff(self, original: str, mejorado: str):
        """Muestra una comparación entre el contenido original y el mejorado."""
        html = "<div style='font-family: Segoe UI;'>"
        html += "<h4 style='color: #28a745;'>✅ Contenido Mejorado</h4>"
        html += "<p style='color: #6c757d; font-size: 11px;'>"
        html += "Puedes copiar el contenido mejorado o aplicarlo directamente con el botón 'Aplicar'."
        html += "</p>"

        # Mostrar diff simple
        if original:
            html += "<details>"
            html += "<summary style='cursor: pointer; color: #6c757d; font-size: 11px;'>"
            html += "📊 Ver comparación con el original"
            html += "</summary>"
            html += "<div style='display: flex; gap: 10px; margin-top: 10px;'>"

            # Original
            html += "<div style='flex: 1;'>"
            html += "<p style='font-weight: bold; color: #dc3545;'>Original:</p>"
            html += f"<pre style='background-color: #fff5f5; padding: 10px; border-radius: 4px; "
            html += f"font-family: Consolas; font-size: 11px; overflow-x: auto; max-height: 200px;'>"
            html += original.replace('<', '&lt;').replace('>', '&gt;')
            html += "</pre>"
            html += "</div>"

            # Mejorado
            html += "<div style='flex: 1;'>"
            html += "<p style='font-weight: bold; color: #28a745;'>Mejorado:</p>"
            html += f"<pre style='background-color: #f0fff4; padding: 10px; border-radius: 4px; "
            html += f"font-family: Consolas; font-size: 11px; overflow-x: auto; max-height: 200px;'>"
            html += mejorado.replace('<', '&lt;').replace('>', '&gt;')
            html += "</pre>"
            html += "</div>"

            html += "</div>"
            html += "</details>"
            html += "<br>"

        # Mostrar solo el mejorado
        html += "<pre style='background-color: #1e1e2e; color: #cdd6f4; padding: 10px; "
        html += "border-radius: 4px; font-family: Consolas; font-size: 11px; overflow-x: auto;'>"
        html += mejorado.replace('<', '&lt;').replace('>', '&gt;')
        html += "</pre>"
        html += "</div>"
        self.result_text.setHtml(html)

    def _mostrar_sugerencias(self, sugerencias: list):
        """Muestra las sugerencias de mejora."""
        html = "<div style='font-family: Segoe UI;'>"
        html += "<h4 style='color: #6f42c1;'>🎯 Sugerencias de Mejora</h4>"

        if not sugerencias:
            html += "<p style='color: #6c757d;'>No se encontraron sugerencias.</p>"
        else:
            html += "<ul style='line-height: 1.8; font-size: 13px;'>"
            for sugerencia in sugerencias:
                html += f"<li>{sugerencia}</li>"
            html += "</ul>"

        html += "</div>"
        self.result_text.setHtml(html)

    # ============================================================
    # ACCIONES DE RESULTADO
    # ============================================================
    def _copiar_resultado(self):
        """Copia el resultado al portapapeles."""
        texto = self.result_text.toPlainText()
        if texto:
            clipboard = QApplication.clipboard()
            clipboard.setText(texto)
            QMessageBox.information(self, "Copiado", "✅ Resultado copiado al portapapeles")

    def _aplicar_resultado(self):
        """Aplica el resultado al agente."""
        if self._ultimo_resultado is None:
            return

        modo = self._ultimo_modo
        resultado = self._ultimo_resultado

        if modo == 0:  # Agente completo
            self.agente_generado.emit(resultado)
            QMessageBox.information(
                self, "✅ Aplicado",
                "La configuración del agente ha sido aplicada.\n"
                "Revisa los campos y ajusta si es necesario."
            )
            self.accept()

        elif modo == 1:  # Contenido generado
            self.contenido_generado.emit(resultado)
            QMessageBox.information(
                self, "✅ Aplicado",
                "El contenido ha sido aplicado al editor."
            )
            self.accept()

        elif modo == 2:  # Contenido mejorado
            self.contenido_mejorado.emit(resultado)
            QMessageBox.information(
                self, "✅ Aplicado",
                "El contenido mejorado ha sido aplicado al editor."
            )
            self.accept()

        elif modo == 5:  # ✅ NUEVO: Configuración mejorada
            self._aplicar_mejoras_config(resultado)

        elif modo in (3, 4):  # Explicación o sugerencias
            QMessageBox.information(
                self, "ℹ️ Info",
                "Las explicaciones y sugerencias son informativas.\n"
                "No se aplican automáticamente al agente."
            )

    def _aplicar_mejoras_config(self, resultado: Dict):
        """
        Aplica los cambios propuestos en la configuración al diálogo padre.

        Emite señales específicas que el AgentConfigDialog manejará.
        """
        if not isinstance(resultado, dict) or not resultado:
            QMessageBox.warning(
                self, "Sin cambios",
                "No hay cambios válidos para aplicar."
            )
            return

        # Filtrar _explicacion
        cambios = {
            k: v for k, v in resultado.items()
            if not k.startswith('_')
        }

        if not cambios:
            QMessageBox.information(
                self, "Sin cambios",
                "La IA no propuso cambios concretos que se puedan aplicar."
            )
            return

        # Confirmación visual
        campos_str = ", ".join(cambios.keys())
        reply = QMessageBox.question(
            self, "Aplicar cambios",
            f"Se aplicarán {len(cambios)} cambio(s) en la configuración:\n\n"
            f"Campos: {campos_str}\n\n"
            f"¿Continuar?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )

        if reply != QMessageBox.StandardButton.Yes:
            return

        # Emitir señal con los cambios
        self.agente_generado.emit(cambios)

        QMessageBox.information(
            self, "✅ Aplicado",
            f"Se han aplicado {len(cambios)} cambio(s) a la configuración del agente."
        )
        self.accept()

    # ============================================================
    # CIERRE SEGURO
    # ============================================================
    def reject(self):
        """Limpia el hilo al cerrar."""
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(1000)
        super().reject()

    def closeEvent(self, event):
        """Limpia el hilo al cerrar."""
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(1000)
        event.accept()