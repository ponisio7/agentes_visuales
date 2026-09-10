# ui/text_import_dialog.py
"""
Diálogo para crear agentes desde texto estructurado (DSL) o desde lenguaje natural (con IA).
"""
import logging
import re
from typing import List, Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QLabel, QSplitter, QGroupBox, QMessageBox, QComboBox,
    QFrame, QWidget, QTabWidget, QProgressBar, QApplication
)
from PyQt6.QtCore import Qt, pyqtSignal, QTimer
from PyQt6.QtGui import QFont, QColor, QTextCharFormat, QSyntaxHighlighter

from core.agent import Agente
from core.text_parser import AgentTextParser
from core.llm_client import LLMClient

logger = logging.getLogger(__name__)


# ============================================================
# RESALTADOR DE SINTAXIS (DSL)
# ============================================================
class DSLHighlighter(QSyntaxHighlighter):
    """Resaltador básico para el DSL de agentes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._rules = []

        # @agente → morado bold
        self._rules.append((
            r'@agente\s+\w+',
            QColor('#6f42c1'), True
        ))

        # clave: → azul
        self._rules.append((
            r'^\w[\w_]*(?=\s*:)',
            QColor('#007bff'), False
        ))

        # comentarios → gris
        self._rules.append((
            r'#.*$',
            QColor('#6c757d'), False
        ))

        # tipo valores → verde
        self._rules.append((
            r':\s*(Python|Shell|HTTP|LLM|File|Loop)',
            QColor('#28a745'), True
        ))

    def highlightBlock(self, text):
        for pattern, color, bold in self._rules:
            for match in re.finditer(pattern, text):
                fmt = QTextCharFormat()
                fmt.setForeground(color)
                if bold:
                    fmt.setFontWeight(QFont.Weight.Bold)
                self.setFormat(match.start(), match.end() - match.start(), fmt)


# ============================================================
# PLANTILLAS DE TEXTO (DSL)
# ============================================================
PLANTILLAS_TEXTO = {
    "🐍 Agente Python básico": """@agente MiProcesador
tipo: Python
descripcion: Procesa datos de entrada
timeout: 30
codigo: |
    import json
    # Datos del contexto
    data = contexto.get('DependenciaAnterior', {})
    resultado = {
        'procesado': True,
        'items': len(data) if isinstance(data, dict) else 0
    }
""",

    "🌐 HTTP → Python → LLM": """@agente ObtenerDatos
tipo: HTTP
descripcion: Obtiene datos de la API
url: https://api.github.com/repos/python/cpython
metodo: GET
timeout: 15

@agente ProcesarDatos
tipo: Python
dependencias: ObtenerDatos
codigo: |
    data = contexto.get('ObtenerDatos', {})
    resultado = {
        'nombre': data.get('name', 'N/A'),
        'estrellas': data.get('stargazers_count', 0),
        'forks': data.get('forks_count', 0)
    }

@agente ResumirIA
tipo: LLM
dependencias: ProcesarDatos
modelo: deepseek-v4-pro
temperatura: 0.7
prompt: |
    Analiza estos datos del repositorio y genera un resumen:
    {contexto}
    Formato: 3 puntos clave con emojis.
""",

    "🔄 Loop sobre lista": """@agente GenerarLista
tipo: Python
codigo: |
    resultado = {
        'items': ['Python', 'JavaScript', 'Rust', 'Go', 'TypeScript']
    }

@agente ProcesarLoop
tipo: Loop
dependencias: GenerarLista
fuente: GenerarLista.items
timeout: 120
codigo: |
    # Procesar cada lenguaje
    resultado = {
        'indice': indice,
        'lenguaje': item,
        'longitud': len(item),
        'procesado': True
    }
""",

    "📁 Pipeline de archivos": """@agente LeerConfig
tipo: File
descripcion: Lee archivo de configuración
operacion: leer
archivo_origen: config.json

@agente TransformarConfig
tipo: Python
dependencias: LeerConfig
codigo: |
    import json
    config = contexto.get('LeerConfig', {})
    if isinstance(config, str):
        config = json.loads(config)
    resultado = {
        'transformado': True,
        'claves': list(config.keys()) if isinstance(config, dict) else []
    }

@agente GuardarResultado
tipo: File
dependencias: TransformarConfig
operacion: escribir
archivo_destino: output/resultado_{fecha}.json
""",

    "🖥️ Comandos Shell": """@agente InfoSistema
tipo: Shell
comando: uname -a && uptime && df -h

@agente ListarArchivos
tipo: Shell
dependencias: InfoSistema
comando: ls -la
timeout: 10
""",
}


# ============================================================
# DIÁLOGO PRINCIPAL
# ============================================================
class TextImportDialog(QDialog):
    """Diálogo para crear agentes desde texto (DSL o lenguaje natural con IA)."""

    agentes_creados = pyqtSignal(list)  # List[Agente]

    def __init__(self, parent=None, nombres_existentes: List[str] = None):
        super().__init__(parent)
        self.nombres_existentes = nombres_existentes or []
        self.parser = AgentTextParser()
        self._agentes_preview: List[Agente] = []
        self._debounce_timer = QTimer()
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._actualizar_preview)

        # Inicializar cliente LLM (si hay API key)
        self.llm_client = None
        try:
            self.llm_client = LLMClient()
            self._llm_disponible = self.llm_client.disponible
        except Exception as e:
            self._llm_disponible = False
            logger.warning(f"Cliente LLM no disponible: {e}")

        self.setWindowTitle("📝 Crear Agentes desde Texto")
        self.setMinimumSize(1000, 700)
        self.setModal(True)

        self._init_ui()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ── Toolbar superior (selector de plantillas) ──
        toolbar = self._crear_toolbar()
        layout.addWidget(toolbar)

        # ── Tabs ──
        self.tabs = QTabWidget()
        self.tabs.addTab(self._crear_panel_dsl(), "📝 DSL Estructurado")
        self.tabs.addTab(self._crear_panel_natural(), "🧠 Lenguaje Natural")
        layout.addWidget(self.tabs, stretch=1)

        # ── Splitter para vista previa (se muestra debajo de los tabs) ──
        preview_group = self._crear_panel_preview()
        layout.addWidget(preview_group, stretch=1)

        # ── Barra de estado ──
        self._crear_barra_estado(layout)

        # ── Botones inferiores ──
        self._crear_botones(layout)

    def _crear_toolbar(self) -> QWidget:
        toolbar = QWidget()
        layout = QHBoxLayout(toolbar)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(QLabel("📚 Plantilla:"))
        self.combo_plantillas = QComboBox()
        self.combo_plantillas.addItem("— Seleccionar plantilla —")
        self.combo_plantillas.addItems(list(PLANTILLAS_TEXTO.keys()))
        self.combo_plantillas.currentIndexChanged.connect(self._cargar_plantilla)
        self.combo_plantillas.setMinimumWidth(250)
        layout.addWidget(self.combo_plantillas)

        layout.addStretch()

        btn_formatear = QPushButton("📐 Formatear")
        btn_formatear.clicked.connect(self._formatear_texto)
        layout.addWidget(btn_formatear)

        btn_limpiar = QPushButton("🗑 Limpiar")
        btn_limpiar.clicked.connect(self._limpiar_editor)
        layout.addWidget(btn_limpiar)

        return toolbar

    # ============================================================
    # PESTAÑA DSL
    # ============================================================
    def _crear_panel_dsl(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        help_label = QLabel(
            "Escribe uno o más agentes usando el formato DSL.\n"
            "  <b>@agente Nombre</b>  |  tipo: Python | Shell | HTTP | LLM | File | Loop\n"
            "  dependencias: A, B  |  codigo: |  (bloque indentado)"
        )
        help_label.setWordWrap(True)
        help_label.setStyleSheet("color: #6c757d; font-size: 11px; padding: 4px;")
        layout.addWidget(help_label)

        self.text_edit = QTextEdit()
        self.text_edit.setFont(QFont("Consolas", 11))
        self.text_edit.setPlaceholderText(
            "@agente MiPrimerAgente\n"
            "tipo: Python\n"
            "descripcion: Un agente de ejemplo\n"
            "codigo: |\n"
            "    resultado = {'mensaje': 'Hola mundo'}\n"
        )
        self.text_edit.setTabStopDistance(40)
        self.text_edit.textChanged.connect(self._on_texto_cambiado)

        self.highlighter = DSLHighlighter(self.text_edit.document())

        layout.addWidget(self.text_edit, stretch=1)
        return widget

    # ============================================================
    # PESTAÑA LENGUAJE NATURAL
    # ============================================================
    def _crear_panel_natural(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        desc_label = QLabel(
            "Describe en lenguaje natural lo que quieres que hagan tus agentes.\n"
            "La IA generará el DSL automáticamente."
        )
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("color: #6c757d; padding: 4px;")
        layout.addWidget(desc_label)

        # Área de texto para la descripción
        self.natural_text = QTextEdit()
        self.natural_text.setPlaceholderText(
            "Ejemplo: Quiero un agente que consulte el clima en Madrid desde una API,\n"
            "luego otro agente que procese la respuesta y extraiga la temperatura,\n"
            "y finalmente un agente LLM que genere un resumen en español."
        )
        self.natural_text.setFont(QFont("Segoe UI", 11))
        layout.addWidget(self.natural_text, stretch=1)

        # Botones
        btn_layout = QHBoxLayout()
        btn_interpretar = QPushButton("🤖 Interpretar con IA")
        btn_interpretar.setStyleSheet("""
            QPushButton {
                background-color: #6f42c1;
                color: white;
                font-weight: bold;
                padding: 8px 20px;
                border-radius: 4px;
            }
            QPushButton:hover { background-color: #5a32a3; }
            QPushButton:disabled { background-color: #6c757d; color: #adb5bd; }
        """)
        btn_interpretar.clicked.connect(self._interpretar_con_ia)
        self.btn_interpretar = btn_interpretar
        btn_layout.addWidget(btn_interpretar)

        btn_limpiar_natural = QPushButton("🗑 Limpiar")
        btn_limpiar_natural.clicked.connect(lambda: self.natural_text.clear())
        btn_layout.addWidget(btn_limpiar_natural)

        btn_layout.addStretch()

        # Indicador de estado del LLM
        self.llm_status = QLabel("")
        self.llm_status.setStyleSheet("font-size: 10px; color: #6c757d;")
        if self._llm_disponible:
            self.llm_status.setText("✅ IA disponible")
        else:
            self.llm_status.setText("⚠️ IA no disponible (falta DEEPSEEK_API_KEY)")
        btn_layout.addWidget(self.llm_status)

        # Barra de progreso (oculta por defecto)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)  # Indeterminado
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumWidth(150)
        btn_layout.addWidget(self.progress_bar)

        layout.addLayout(btn_layout)
        return widget

    # ============================================================
    # VISTA PREVIA
    # ============================================================
    def _crear_panel_preview(self) -> QGroupBox:
        group = QGroupBox("👁️ Vista Previa de Agentes")
        layout = QVBoxLayout(group)

        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setFont(QFont("Consolas", 10))
        self.preview_text.setStyleSheet("""
            QTextEdit {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 4px;
                min-height: 150px;
                max-height: 250px;
            }
        """)
        layout.addWidget(self.preview_text, stretch=1)

        return group

    # ============================================================
    # BARRA DE ESTADO
    # ============================================================
    def _crear_barra_estado(self, parent_layout):
        self.status_frame = QFrame()
        self.status_frame.setStyleSheet("""
            QFrame {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 4px;
                padding: 6px 10px;
            }
        """)
        status_layout = QHBoxLayout(self.status_frame)

        self.status_icon = QLabel("⏳")
        status_layout.addWidget(self.status_icon)

        self.status_text = QLabel("Escribe para comenzar...")
        self.status_text.setStyleSheet("color: #6c757d;")
        status_layout.addWidget(self.status_text)

        status_layout.addStretch()

        self.status_count = QLabel("")
        self.status_count.setStyleSheet("color: #6c757d; font-size: 10px;")
        status_layout.addWidget(self.status_count)

        parent_layout.addWidget(self.status_frame)

    # ============================================================
    # BOTONES INFERIORES
    # ============================================================
    def _crear_botones(self, parent_layout):
        layout = QHBoxLayout()

        btn_ayuda = QPushButton("❓ Ayuda de Formato")
        btn_ayuda.clicked.connect(self._mostrar_ayuda)
        layout.addWidget(btn_ayuda)

        layout.addStretch()

        btn_cancelar = QPushButton("❌ Cancelar")
        btn_cancelar.clicked.connect(self.reject)
        layout.addWidget(btn_cancelar)

        self.btn_crear = QPushButton("✅ Crear Agentes")
        self.btn_crear.setEnabled(False)
        self.btn_crear.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: white;
                font-weight: bold;
                padding: 10px 30px;
                border-radius: 4px;
                min-width: 140px;
            }
            QPushButton:hover { background-color: #218838; }
            QPushButton:disabled {
                background-color: #6c757d;
                color: #adb5bd;
            }
        """)
        self.btn_crear.clicked.connect(self._crear_agentes)
        layout.addWidget(self.btn_crear)

        parent_layout.addLayout(layout)

    # ============================================================
    # LÓGICA DE DSL
    # ============================================================
    def _on_texto_cambiado(self):
        self._debounce_timer.start(300)

    def _actualizar_preview(self):
        texto = self.text_edit.toPlainText()
        if not texto.strip():
            self.preview_text.clear()
            self.status_icon.setText("⏳")
            self.status_text.setText("Escribe para comenzar...")
            self.status_count.setText("")
            self.btn_crear.setEnabled(False)
            self._agentes_preview = []
            return

        agentes, resultado = self.parser.parse_to_agents(texto)
        self._agentes_preview = agentes

        if resultado.exito:
            preview_lines = [f"✅ {len(agentes)} agente(s) detectados:\n"]
            for agente in agentes:
                icono = agente.obtener_icono()
                preview_lines.append(f"  {icono} <b>{agente.nombre}</b> ({agente.tipo.value})")
                if agente.descripcion:
                    preview_lines.append(f"     📝 {agente.descripcion}")
                if agente.dependencias_nombres:
                    deps = ', '.join(agente.dependencias_nombres)
                    preview_lines.append(f"     🔗 Depende de: {deps}")
                preview_lines.append("")
            if resultado.advertencias:
                preview_lines.append("⚠️ <i>Advertencias:</i>")
                for w in resultado.advertencias:
                    preview_lines.append(f"   • {w}")
            self.preview_text.setHtml('<br>'.join(preview_lines))
            self.status_icon.setText("✅")
            self.status_text.setText(f"{len(agentes)} agente(s) listo(s) para crear")
            self.status_text.setStyleSheet("color: #28a745; font-weight: bold;")
            self.btn_crear.setEnabled(True)
        else:
            error_lines = ["❌ <b>Errores:</b>\n"]
            for e in resultado.errores:
                error_lines.append(f"   • {e}")
            if resultado.advertencias:
                error_lines.append("\n⚠️ <i>Advertencias:</i>")
                for w in resultado.advertencias:
                    error_lines.append(f"   • {w}")
            self.preview_text.setHtml('<br>'.join(error_lines))
            self.status_icon.setText("❌")
            self.status_text.setText(resultado.errores[0] if resultado.errores else "Error")
            self.status_text.setStyleSheet("color: #dc3545; font-weight: bold;")
            self.btn_crear.setEnabled(False)

        self.status_count.setText(f"{len(texto)} caracteres")

    def _cargar_plantilla(self, index):
        if index <= 0:
            return
        nombre_plantilla = list(PLANTILLAS_TEXTO.keys())[index - 1]
        texto = PLANTILLAS_TEXTO[nombre_plantilla]
        self.text_edit.setPlainText(texto)
        self.combo_plantillas.setCurrentIndex(0)

    def _formatear_texto(self):
        texto = self.text_edit.toPlainText()
        texto = re.sub(r'(\w+):([^\s|])', r'\1: \2', texto)
        self.text_edit.setPlainText(texto)

    def _limpiar_editor(self):
        self.text_edit.clear()

    # ============================================================
    # LÓGICA DE IA (LENGUAJE NATURAL)
    # ============================================================
    def _interpretar_con_ia(self):
        """Envía el texto de lenguaje natural al LLM y coloca el DSL generado en la pestaña DSL."""
        if not self._llm_disponible or not self.llm_client:
            QMessageBox.warning(
                self, "IA no disponible",
                "No se encontró una API key de DeepSeek.\n"
                "Configura la variable de entorno DEEPSEEK_API_KEY."
            )
            return

        texto = self.natural_text.toPlainText().strip()
        if not texto:
            QMessageBox.information(self, "Texto vacío", "Escribe una descripción antes de interpretar.")
            return

        # Deshabilitar botón y mostrar progreso
        self.btn_interpretar.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.status_icon.setText("⏳")
        self.status_text.setText("Consultando IA...")
        QApplication.processEvents()

        try:
            system_prompt = (
                "Eres un asistente que convierte descripciones de agentes en un DSL específico.\n"
                "El DSL tiene el siguiente formato:\n"
                "@agente Nombre\n"
                "tipo: Python | Shell | HTTP | LLM | File | Loop\n"
                "descripcion: ...\n"
                "dependencias: A, B\n"
                "campo_especifico: valor\n"
                "bloque_multilinea: |\n"
                "    línea 1\n"
                "    línea 2\n"
                "Reglas:\n"
                "- Identifica el tipo de agente según la descripción.\n"
                "- Para HTTP, usa 'url', 'metodo' (GET por defecto).\n"
                "- Para LLM, usa 'prompt', 'modelo' (deepseek-v4-flash) y 'temperatura' (0.7).\n"
                "- Para Python, usa 'codigo'.\n"
                "- Para Shell, usa 'comando'.\n"
                "- Para File, usa 'operacion', 'archivo_origen', 'archivo_destino'.\n"
                "- Para Loop, usa 'fuente' y 'codigo'.\n"
                "- Si se describen varios agentes con dependencias, incluye todas las definiciones.\n"
                "- Devuelve ÚNICAMENTE el bloque DSL, sin texto adicional."
            )

            respuesta = self.llm_client.chat(
                prompt=texto,
                system_prompt=system_prompt,
                temperature=0.2,
                max_tokens=2500
            )

            # Limpiar posibles marcas de código (```dsl, ```, etc.)
            respuesta_limpia = re.sub(r'^```\w*\n?', '', respuesta)
            respuesta_limpia = re.sub(r'\n?```$', '', respuesta_limpia)

            # Colocar el resultado en la pestaña DSL
            self.text_edit.setPlainText(respuesta_limpia)
            self.tabs.setCurrentIndex(0)  # Cambiar a la pestaña DSL

            self.status_icon.setText("✅")
            self.status_text.setText("DSL generado por IA (revisa y edita si es necesario)")
            self.status_text.setStyleSheet("color: #6f42c1; font-weight: bold;")

        except Exception as e:
            QMessageBox.critical(self, "Error en IA", f"Ocurrió un error al interpretar el texto:\n{str(e)}")
            self.status_icon.setText("❌")
            self.status_text.setText("Error al generar DSL")
            self.status_text.setStyleSheet("color: #dc3545;")
        finally:
            self.btn_interpretar.setEnabled(True)
            self.progress_bar.setVisible(False)

    # ============================================================
    # CREACIÓN DE AGENTES
    # ============================================================
    def _crear_agentes(self):
        if not self._agentes_preview:
            return

        conflictos = []
        for agente in self._agentes_preview:
            if agente.nombre in self.nombres_existentes:
                conflictos.append(agente.nombre)

        if conflictos:
            reply = QMessageBox.question(
                self, "Nombres en conflicto",
                f"Los siguientes nombres ya existen:\n"
                f"{', '.join(conflictos)}\n\n"
                f"¿Deseas continuar (se crearán con nuevos IDs)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        self.agentes_creados.emit(self._agentes_preview)
        QMessageBox.information(
            self, "✅ Éxito",
            f"Se crearon {len(self._agentes_preview)} agente(s) correctamente."
        )
        self.accept()

    # ============================================================
    # AYUDA
    # ============================================================
    def _mostrar_ayuda(self):
        ayuda = """
<h2>📝 Formato DSL para Agentes</h2>

<h3>Estructura básica:</h3>
<pre style="background:#f4f4f4; padding:10px; border-radius:4px;">
@agente NombreDelAgente
tipo: Python | Shell | HTTP | LLM | File | Loop
descripcion: Texto descriptivo
dependencias: AgenteA, AgenteB
campo_específico: valor
bloque_multilinea: |
    línea 1
    línea 2
</pre>

<h3>Campos por tipo:</h3>
<table border="1" cellpadding="5" style="border-collapse:collapse;">
<tr><th>Tipo</th><th>Campos</th></tr>
<tr><td>🐍 Python</td><td><code>codigo: |</code>, <code>timeout</code></td></tr>
<tr><td>💻 Shell</td><td><code>comando:</code>, <code>timeout</code></td></tr>
<tr><td>🌐 HTTP</td><td><code>url:</code>, <code>metodo:</code>, <code>timeout</code></td></tr>
<tr><td>🧠 LLM</td><td><code>prompt: |</code>, <code>modelo:</code>, <code>temperatura:</code></td></tr>
<tr><td>📄 File</td><td><code>operacion:</code>, <code>archivo_origen:</code>, <code>archivo_destino:</code></td></tr>
<tr><td>🔄 Loop</td><td><code>fuente:</code>, <code>codigo: |</code>, <code>timeout</code></td></tr>
</table>

<h3>Bloques multilínea:</h3>
<p>Usa <code>|</code> al final de la línea e indenta con 4 espacios:</p>
<pre style="background:#f4f4f4; padding:10px;">
codigo: |
    import json
    data = contexto.get('Dep', {})
    resultado = {'ok': True}
</pre>

<h3>Comentarios:</h3>
<p>Líneas que empiezan con <code>#</code> se ignoran.</p>
"""
        msg = QMessageBox(self)
        msg.setWindowTitle("📝 Ayuda del Formato")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(ayuda)
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()