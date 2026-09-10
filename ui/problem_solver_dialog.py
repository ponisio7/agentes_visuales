# ui/problem_solver_dialog.py
"""
Diálogo simplificado del Problem Solver.
El usuario describe un problema y el sistema genera automáticamente
la orquestación de agentes para resolverlo.

CARACTERÍSTICAS:
- Entrada de texto para el problema
- Ejemplos rápidos para empezar
- Barra de progreso durante la generación
- Vista previa del plan generado
- Creación directa de agentes con un clic
- Integración con el grafo y scheduler existentes
"""
import json
import logging
from typing import List, Optional, Dict, Any

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QLabel, QGroupBox, QMessageBox, QSplitter, QWidget,
    QScrollArea, QFrame, QProgressBar, QSpinBox, QComboBox,
    QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QTreeWidget, QTreeWidgetItem, QApplication, QSizePolicy
)
from PyQt6.QtCore import Qt, pyqtSignal, QThread, QObject, QTimer
from PyQt6.QtGui import QFont, QColor, QTextCursor, QPalette

from core.problem_solver import ProblemSolver, ExecutionPlan, StepPlan, PlanStatus
from core.agent import Agente
from core.llm_client import LLMClient

logger = logging.getLogger(__name__)


# ============================================================
# WORKER PARA EJECUCIÓN EN HILO SEPARADO
# ============================================================

class SolverWorker(QObject):
    """Worker para ejecutar el solver en hilo separado."""
    finished = pyqtSignal(object)  # ExecutionPlan
    error = pyqtSignal(str)
    started = pyqtSignal()
    progress = pyqtSignal(str, int)  # mensaje, porcentaje

    def __init__(self, solver: ProblemSolver, problema: str, max_pasos: int):
        super().__init__()
        self.solver = solver
        self.problema = problema
        self.max_pasos = max_pasos

    def run(self):
        try:
            self.started.emit()
            self.progress.emit("🧠 Analizando problema...", 20)
            
            plan = self.solver.resolver_problema(
                self.problema,
                max_pasos=self.max_pasos
            )
            
            self.progress.emit("✅ Plan generado", 100)
            self.finished.emit(plan)
            
        except Exception as e:
            logger.exception("Error en SolverWorker")
            self.error.emit(str(e))


# ============================================================
# EJEMPLOS DE PROBLEMAS (más variados y realistas)
# ============================================================

EJEMPLOS_PROBLEMAS = [
    {
        "titulo": "📊 Análisis de repositorios GitHub",
        "texto": "Obtener los repositorios más populares de Python en GitHub, analizar sus estrellas y generar un resumen ejecutivo en español"
    },
    {
        "titulo": "🌤️ Clima y reporte",
        "texto": "Consultar el clima actual en Madrid, procesar los datos (temperatura, viento, humedad) y guardar un reporte en archivo JSON con fecha"
    },
    {
        "titulo": "📄 Procesamiento de archivos",
        "texto": "Leer un archivo de configuración en JSON, transformar los datos (añadir timestamps y validar campos) y generar un reporte en Markdown"
    },
    {
        "titulo": "🔄 Procesamiento en lote",
        "texto": "Obtener una lista de lenguajes de programación, procesar cada uno con un LLM para obtener su descripción y características, y guardar todo en un archivo"
    },
    {
        "titulo": "💻 Monitoreo de sistema",
        "texto": "Ejecutar comandos del sistema para obtener información del disco, memoria y CPU, analizar los datos y generar un resumen con alertas"
    },
    {
        "titulo": "📈 Análisis de sentimiento",
        "texto": "Obtener comentarios de una API pública, clasificar el sentimiento de cada uno con IA, calcular estadísticas y generar un resumen ejecutivo"
    }
]


# ============================================================
# DIÁLOGO PRINCIPAL (VERSIÓN SIMPLIFICADA)
# ============================================================

class ProblemSolverDialog(QDialog):
    """
    Diálogo simplificado para resolver problemas con auto-orquestación.
    
    Flujo:
    1. Usuario escribe o selecciona un problema
    2. Click en "Generar Plan"
    3. IA genera los agentes
    4. Usuario ve el resumen
    5. Click en "Crear Agentes" → se añaden al scheduler
    """
    
    # Señal que emite la lista de agentes generados
    plan_accepted = pyqtSignal(list)  # List[Agente]
    
    def __init__(self, parent=None, nombres_existentes: Optional[List[str]] = None):
        super().__init__(parent)
        self.nombres_existentes = nombres_existentes or []
        self.plan_actual: Optional[ExecutionPlan] = None
        self._worker = None
        self._thread = None
        self._generando = False
        
        # Inicializar solver
        self.solver: Optional[ProblemSolver] = None
        try:
            llm_client = LLMClient()
            if llm_client.disponible:
                self.solver = ProblemSolver(llm_client)
                logger.info("ProblemSolver inicializado correctamente")
            else:
                logger.warning("LLMClient no disponible")
        except Exception as e:
            logger.warning(f"ProblemSolver no disponible: {e}")
        
        self.setWindowTitle("🧠 Resolver Problema con Agentes")
        self.setMinimumSize(900, 700)
        self.setModal(True)
        
        self._init_ui()
        self._verificar_disponibilidad()
        self._cargar_ejemplos()

    # ============================================================
    # CONSTRUCCIÓN DE UI
    # ============================================================
    
    def _init_ui(self):
        """Inicializa la interfaz de usuario."""
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        layout.setContentsMargins(15, 15, 15, 15)
        
        # ── Header ──
        self._crear_header(layout)
        
        # ── Splitter: entrada | resultado ──
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._crear_panel_entrada())
        splitter.addWidget(self._crear_panel_resultado())
        splitter.setSizes([350, 550])
        layout.addWidget(splitter, stretch=1)
        
        # ── Botones inferiores ──
        self._crear_botones(layout)
        
        # Aplicar estilo
        self._aplicar_estilo()
        
        # Configurar atajos
        self._setup_shortcuts()
    
    def _crear_header(self, layout: QVBoxLayout):
        """Crea el encabezado del diálogo."""
        header_widget = QWidget()
        header_layout = QHBoxLayout(header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)
        
        titulo = QLabel("🧠 Resolver Problema con Agentes")
        titulo.setStyleSheet("""
            QLabel {
                font-size: 18px;
                font-weight: bold;
                color: #6f42c1;
            }
        """)
        header_layout.addWidget(titulo)
        
        header_layout.addStretch()
        
        # Estado de disponibilidad
        self.lbl_estado = QLabel("")
        self.lbl_estado.setStyleSheet("font-size: 11px; padding: 4px 10px; border-radius: 4px;")
        header_layout.addWidget(self.lbl_estado)
        
        layout.addWidget(header_widget)
        
        # Separador
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        layout.addWidget(line)
    
    def _crear_panel_entrada(self) -> QWidget:
        """Crea el panel de entrada del problema."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        
        # ── Etiqueta ──
        label = QLabel("📝 Describe tu problema:")
        label.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(label)
        
        # ── Área de texto ──
        self.input_problema = QTextEdit()
        self.input_problema.setPlaceholderText(
            "Describe el problema que quieres resolver...\n\n"
            "Ejemplo:\n"
            "'Obtener los repositorios más populares de Python en GitHub, "
            "analizar sus estadísticas y generar un resumen ejecutivo'"
        )
        self.input_problema.setFont(QFont("Segoe UI", 11))
        self.input_problema.setMinimumHeight(150)
        layout.addWidget(self.input_problema, stretch=1)
        
        # ── Ejemplos rápidos ──
        ejemplos_label = QLabel("💡 Ejemplos rápidos:")
        ejemplos_label.setStyleSheet("font-weight: bold; font-size: 11px; margin-top: 8px;")
        layout.addWidget(ejemplos_label)
        
        ejemplos_container = QWidget()
        ejemplos_layout = QVBoxLayout(ejemplos_container)
        ejemplos_layout.setSpacing(4)
        ejemplos_layout.setContentsMargins(0, 0, 0, 0)
        
        # Mostrar primeros 4 ejemplos (los más comunes)
        for ejemplo in EJEMPLOS_PROBLEMAS[:4]:
            btn = QPushButton(ejemplo["titulo"])
            btn.setToolTip(ejemplo["texto"])
            btn.setStyleSheet("""
                QPushButton {
                    background-color: #f8f9fa;
                    border: 1px solid #dee2e6;
                    border-radius: 4px;
                    padding: 6px 10px;
                    text-align: left;
                    font-size: 10px;
                }
                QPushButton:hover {
                    background-color: #e9ecef;
                    border-color: #6f42c1;
                }
            """)
            btn.clicked.connect(lambda checked, e=ejemplo["texto"]: self.input_problema.setPlainText(e))
            ejemplos_layout.addWidget(btn)
        
        layout.addWidget(ejemplos_container)
        
        # ── Controles ──
        controles = QHBoxLayout()
        controles.setSpacing(8)
        
        controles.addWidget(QLabel("Máx. pasos:"))
        self.spin_max_pasos = QSpinBox()
        self.spin_max_pasos.setRange(2, 15)
        self.spin_max_pasos.setValue(6)
        self.spin_max_pasos.setToolTip("Número máximo de agentes a generar")
        controles.addWidget(self.spin_max_pasos)
        
        controles.addStretch()
        
        layout.addLayout(controles)
        
        # ── Botón generar ──
        self.btn_generar = QPushButton("🧠 Generar Plan")
        self.btn_generar.setStyleSheet("""
            QPushButton {
                background-color: #6f42c1;
                color: white;
                font-weight: bold;
                padding: 12px;
                border-radius: 6px;
                font-size: 13px;
            }
            QPushButton:hover { background-color: #5a32a3; }
            QPushButton:disabled { background-color: #6c757d; color: #adb5bd; }
        """)
        self.btn_generar.clicked.connect(self._generar_plan)
        layout.addWidget(self.btn_generar)
        
        # ── Barra de progreso ──
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
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
        layout.addWidget(self.progress_bar)
        
        # ── Mensaje de progreso ──
        self.lbl_progreso = QLabel("")
        self.lbl_progreso.setStyleSheet("color: #6c757d; font-size: 10px;")
        self.lbl_progreso.setVisible(False)
        layout.addWidget(self.lbl_progreso)
        
        return panel
    
    def _crear_panel_resultado(self) -> QWidget:
        """Crea el panel de resultado del plan."""
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        
        # ── Etiqueta ──
        label = QLabel("📋 Plan Generado:")
        label.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(label)
        
        # ── Área de resultado ──
        self.result_text = QTextEdit()
        self.result_text.setReadOnly(True)
        self.result_text.setFont(QFont("Consolas", 10))
        self.result_text.setPlaceholderText(
            "El plan generado aparecerá aquí...\n\n"
            "1. Escribe un problema en el panel izquierdo\n"
            "2. Haz clic en 'Generar Plan'\n"
            "3. Revisa el plan generado\n"
            "4. Haz clic en 'Crear Agentes' para añadirlos al sistema"
        )
        self.result_text.setStyleSheet("""
            QTextEdit {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 4px;
                padding: 8px;
            }
        """)
        layout.addWidget(self.result_text, stretch=1)
        
        # ── Resumen rápido ──
        self.resumen_widget = QWidget()
        resumen_layout = QHBoxLayout(self.resumen_widget)
        resumen_layout.setContentsMargins(4, 0, 4, 0)
        resumen_layout.setSpacing(15)
        
        self.lbl_pasos = QLabel("📊 Pasos: 0")
        self.lbl_pasos.setStyleSheet("color: #6c757d; font-size: 10px; font-weight: bold;")
        resumen_layout.addWidget(self.lbl_pasos)
        
        self.lbl_agentes = QLabel("🤖 Agentes: 0")
        self.lbl_agentes.setStyleSheet("color: #6c757d; font-size: 10px; font-weight: bold;")
        resumen_layout.addWidget(self.lbl_agentes)
        
        self.lbl_tiempo = QLabel("⏱ Estimado: 0s")
        self.lbl_tiempo.setStyleSheet("color: #6c757d; font-size: 10px; font-weight: bold;")
        resumen_layout.addWidget(self.lbl_tiempo)
        
        self.lbl_complejidad = QLabel("📈 Complejidad: -")
        self.lbl_complejidad.setStyleSheet("color: #6c757d; font-size: 10px; font-weight: bold;")
        resumen_layout.addWidget(self.lbl_complejidad)
        
        resumen_layout.addStretch()
        
        self.resumen_widget.setVisible(False)
        layout.addWidget(self.resumen_widget)
        
        return panel
    
    def _crear_botones(self, layout: QVBoxLayout):
        """Crea los botones inferiores."""
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(10)
        
        # Botón de ayuda
        btn_ayuda = QPushButton("❓ Ayuda")
        btn_ayuda.clicked.connect(self._mostrar_ayuda)
        btn_ayuda.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                border-radius: 4px;
                padding: 8px 16px;
            }
            QPushButton:hover { background-color: #5a6268; }
        """)
        btn_layout.addWidget(btn_ayuda)
        
        btn_layout.addStretch()
        
        # Botón cancelar
        self.btn_cancelar = QPushButton("❌ Cancelar")
        self.btn_cancelar.clicked.connect(self.reject)
        self.btn_cancelar.setStyleSheet("""
            QPushButton {
                background-color: #6c757d;
                color: white;
                border-radius: 4px;
                padding: 10px 20px;
            }
            QPushButton:hover { background-color: #5a6268; }
        """)
        btn_layout.addWidget(self.btn_cancelar)
        
        # Botón crear agentes
        self.btn_crear = QPushButton("✅ Crear Agentes")
        self.btn_crear.setEnabled(False)
        self.btn_crear.setStyleSheet("""
            QPushButton {
                background-color: #28a745;
                color: white;
                font-weight: bold;
                padding: 10px 30px;
                border-radius: 4px;
                font-size: 13px;
                min-width: 160px;
            }
            QPushButton:hover { background-color: #218838; }
            QPushButton:disabled { background-color: #6c757d; color: #adb5bd; }
        """)
        self.btn_crear.clicked.connect(self._crear_agentes)
        btn_layout.addWidget(self.btn_crear)
        
        layout.addLayout(btn_layout)
    
    def _aplicar_estilo(self):
        """Aplica el estilo al diálogo."""
        self.setStyleSheet("""
            QDialog {
                background-color: #ffffff;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #d1d5db;
                border-radius: 6px;
                margin-top: 12px;
                padding-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 10px;
                padding: 0 8px;
            }
            QTextEdit:focus {
                border-color: #6f42c1;
            }
            QSpinBox {
                padding: 4px 8px;
                border: 1px solid #d1d5db;
                border-radius: 4px;
                min-width: 60px;
            }
            QSpinBox:focus {
                border-color: #6f42c1;
            }
        """)
    
    def _setup_shortcuts(self):
        """Configura atajos de teclado."""
        # Ctrl+Enter para generar
        from PyQt6.QtGui import QShortcut, QKeySequence
        shortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        shortcut.activated.connect(self._generar_plan)
        
        # Escape para cerrar
        shortcut_escape = QShortcut(QKeySequence("Escape"), self)
        shortcut_escape.activated.connect(self.reject)

    # ============================================================
    # EJEMPLOS Y ESTADO
    # ============================================================
    
    def _cargar_ejemplos(self):
        """Carga los ejemplos en el combo de ejemplos."""
        # Ya están en los botones
        pass
    
    def _verificar_disponibilidad(self):
        """Verifica si el solver está disponible."""
        if self.solver:
            self.lbl_estado.setText("✅ IA disponible")
            self.lbl_estado.setStyleSheet("""
                QLabel {
                    background-color: #d4edda;
                    color: #155724;
                    font-size: 11px;
                    padding: 4px 10px;
                    border-radius: 4px;
                }
            """)
            self.btn_generar.setEnabled(True)
        else:
            self.lbl_estado.setText("❌ IA no disponible")
            self.lbl_estado.setStyleSheet("""
                QLabel {
                    background-color: #f8d7da;
                    color: #721c24;
                    font-size: 11px;
                    padding: 4px 10px;
                    border-radius: 4px;
                }
            """)
            self.btn_generar.setEnabled(False)

    # ============================================================
    # GENERACIÓN DEL PLAN
    # ============================================================
    
    def _generar_plan(self):
        """Genera el plan a partir del problema."""
        if self._generando:
            return
            
        problema = self.input_problema.toPlainText().strip()
        if not problema:
            QMessageBox.information(
                self, "Problema vacío",
                "Por favor, describe el problema que quieres resolver.\n\n"
                "Puedes usar los ejemplos rápidos para empezar."
            )
            return
        
        if not self.solver:
            QMessageBox.warning(
                self, "IA no disponible",
                "El solucionador no está disponible.\n"
                "Asegúrate de que DEEPSEEK_API_KEY esté configurada."
            )
            return
        
        # Limpiar resultado anterior
        self.result_text.clear()
        self.resumen_widget.setVisible(False)
        self.btn_crear.setEnabled(False)
        self._generando = True
        
        # Ejecutar en hilo separado
        self._thread = QThread()
        self._worker = SolverWorker(
            self.solver,
            problema,
            self.spin_max_pasos.value()
        )
        self._worker.moveToThread(self._thread)
        
        # Conectar señales
        self._thread.started.connect(self._worker.run)
        self._worker.started.connect(self._on_generacion_started)
        self._worker.progress.connect(self._on_generacion_progress)
        self._worker.finished.connect(self._on_generacion_finished)
        self._worker.error.connect(self._on_generacion_error)
        self._worker.finished.connect(self._thread.quit)
        self._worker.error.connect(self._thread.quit)
        
        # Limpieza
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._worker.deleteLater)
        
        self._thread.start()
    
    def _on_generacion_started(self):
        """Se llama cuando comienza la generación."""
        self.progress_bar.setVisible(True)
        self.progress_bar.setValue(0)
        self.lbl_progreso.setVisible(True)
        self.lbl_progreso.setText("🧠 Analizando problema...")
        self.btn_generar.setEnabled(False)
        self.btn_generar.setText("⏳ Generando...")
        self.btn_crear.setEnabled(False)
    
    def _on_generacion_progress(self, mensaje: str, porcentaje: int):
        """Actualiza el progreso."""
        self.progress_bar.setValue(min(100, max(0, porcentaje)))
        self.lbl_progreso.setText(mensaje)
        QApplication.processEvents()
    
    def _on_generacion_finished(self, plan: ExecutionPlan):
        """Se llama cuando el plan ha sido generado."""
        self.progress_bar.setVisible(False)
        self.lbl_progreso.setVisible(False)
        self.btn_generar.setEnabled(True)
        self.btn_generar.setText("🧠 Generar Plan")
        self._generando = False
        
        self.plan_actual = plan
        self._mostrar_plan(plan)
        self.btn_crear.setEnabled(True)
        
        logger.info(f"Plan generado: {len(plan.pasos)} pasos, {len(plan.agentes_generados)} agentes")
    
    def _on_generacion_error(self, error: str):
        """Se llama cuando hay un error en la generación."""
        self.progress_bar.setVisible(False)
        self.lbl_progreso.setVisible(False)
        self.btn_generar.setEnabled(True)
        self.btn_generar.setText("🧠 Generar Plan")
        self._generando = False
        
        self.result_text.setHtml(
            f"""
            <div style='background-color: #f8d7da; border: 1px solid #f5c6cb; 
                        border-radius: 4px; padding: 15px; color: #721c24;'>
                <b>❌ Error al generar el plan</b><br><br>
                {error}
            </div>
            """
        )
        
        QMessageBox.critical(
            self, "Error",
            f"No se pudo generar el plan:\n\n{error}"
        )

    # ============================================================
    # VISUALIZACIÓN DEL PLAN
    # ============================================================
    
    def _mostrar_plan(self, plan: ExecutionPlan):
        """Muestra el plan generado en el panel de resultado."""
        
        # ── Resumen ──
        self.lbl_pasos.setText(f"📊 Pasos: {len(plan.pasos)}")
        self.lbl_agentes.setText(f"🤖 Agentes: {len(plan.agentes_generados)}")
        
        # Estimar tiempo
        tiempo_estimado = self.solver.estimar_tiempo(plan) if self.solver else 0
        self.lbl_tiempo.setText(f"⏱ Estimado: {tiempo_estimado}s")
        
        complejidad = self.solver.estimar_complejidad(plan) if self.solver else "?"
        self.lbl_complejidad.setText(f"📈 Complejidad: {complejidad}")
        self.resumen_widget.setVisible(True)
        
        # ── Construir HTML para el resultado ──
        html = []
        html.append("<div style='font-family: Segoe UI, sans-serif;'>")
        
        # Título y análisis
        html.append(f"<h3 style='color: #6f42c1;'>{plan.titulo}</h3>")
        html.append(f"<p style='color: #495057;'><b>📝 Análisis:</b> {plan.analisis}</p>")
        
        # Advertencias
        if plan.advertencias:
            html.append("<div style='background-color: #fff3cd; border: 1px solid #ffc107; border-radius: 4px; padding: 10px; margin: 10px 0;'>")
            html.append("<b style='color: #856404;'>⚠️ Advertencias:</b><br>")
            for adv in plan.advertencias:
                html.append(f"<span style='color: #856404;'>• {adv}</span><br>")
            html.append("</div>")
        
        # Tabla de pasos
        html.append("<table style='width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 12px;'>")
        html.append("<tr style='background-color: #f8f9fa;'>")
        html.append("<th style='padding: 8px; border: 1px solid #dee2e6; text-align: left;'>#</th>")
        html.append("<th style='padding: 8px; border: 1px solid #dee2e6; text-align: left;'>Nombre</th>")
        html.append("<th style='padding: 8px; border: 1px solid #dee2e6; text-align: left;'>Tipo</th>")
        html.append("<th style='padding: 8px; border: 1px solid #dee2e6; text-align: left;'>Dependencias</th>")
        html.append("<th style='padding: 8px; border: 1px solid #dee2e6; text-align: left;'>Descripción</th>")
        html.append("</tr>")
        
        colores = {
            'HTTP': '#fd7e14',
            'Python': '#6f42c1',
            'LLM': '#17a2b8',
            'Shell': '#28a745',
            'File': '#20c997',
            'Loop': '#e83e8c'
        }
        
        for paso in plan.pasos:
            color = colores.get(paso.tipo_agente, '#6c757d')
            html.append("<tr>")
            html.append(f"<td style='padding: 6px 8px; border: 1px solid #dee2e6;'>{paso.orden}</td>")
            html.append(f"<td style='padding: 6px 8px; border: 1px solid #dee2e6; font-weight: bold;'>{paso.nombre}</td>")
            html.append(f"<td style='padding: 6px 8px; border: 1px solid #dee2e6; color: {color}; font-weight: bold;'>{paso.tipo_agente}</td>")
            deps = ', '.join(paso.dependencia_ids) if paso.dependencia_ids else '—'
            html.append(f"<td style='padding: 6px 8px; border: 1px solid #dee2e6;'>{deps}</td>")
            html.append(f"<td style='padding: 6px 8px; border: 1px solid #dee2e6;'>{paso.descripcion[:60]}{'...' if len(paso.descripcion) > 60 else ''}</td>")
            html.append("</tr>")
        
        html.append("</table>")
        
        # Justificaciones
        html.append("<details style='margin: 10px 0;'>")
        html.append("<summary style='cursor: pointer; color: #6c757d; font-size: 11px;'>📖 Ver justificaciones de cada paso</summary>")
        html.append("<div style='padding: 10px; background-color: #f8f9fa; border-radius: 4px; margin-top: 6px; font-size: 11px;'>")
        for paso in plan.pasos:
            if paso.justificacion:
                html.append(f"<p><b>{paso.nombre}:</b> {paso.justificacion}</p>")
        html.append("</div>")
        html.append("</details>")
        
        # DSL (colapsable)
        if self.solver:
            dsl = self.solver.generar_dsl(plan)
            html.append("<details style='margin: 10px 0;'>")
            html.append("<summary style='cursor: pointer; color: #6c757d; font-size: 11px;'>📄 Ver DSL generado</summary>")
            html.append(f"<pre style='background-color: #1e1e2e; color: #cdd6f4; padding: 12px; border-radius: 4px; font-family: Consolas; font-size: 10px; overflow-x: auto;'>{dsl}</pre>")
            html.append("</details>")
        
        html.append("</div>")
        
        self.result_text.setHtml('\n'.join(html))
        
        # Mover cursor al inicio
        cursor = self.result_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.result_text.setTextCursor(cursor)

    # ============================================================
    # CREACIÓN DE AGENTES
    # ============================================================
    
    def _crear_agentes(self):
        """Crea los agentes generados y los añade al scheduler."""
        if not self.plan_actual or not self.plan_actual.agentes_generados:
            QMessageBox.warning(self, "Error", "No hay agentes para crear.")
            return
        
        agentes = self.plan_actual.agentes_generados
        
        # Verificar conflictos de nombre
        conflictos = [a.nombre for a in agentes if a.nombre in self.nombres_existentes]
        if conflictos:
            reply = QMessageBox.question(
                self, "⚠️ Nombres en conflicto",
                f"Los siguientes nombres ya existen:\n\n"
                f"{', '.join(conflictos)}\n\n"
                f"¿Deseas continuar? (los agentes existentes se mantendrán)",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        
        # Emitir señal con los agentes
        self.plan_accepted.emit(agentes)
        
        # Mostrar mensaje de éxito
        nombres = ', '.join(a.nombre for a in agentes[:3])
        if len(agentes) > 3:
            nombres += f" y {len(agentes) - 3} más"
        
        QMessageBox.information(
            self, "✅ Agentes Creados",
            f"Se han creado {len(agentes)} agentes:\n\n"
            f"🤖 {nombres}\n\n"
            f"Los agentes se han añadido al sistema y están listos para ejecutar."
        )
        
        self.accept()

    # ============================================================
    # AYUDA
    # ============================================================
    
    def _mostrar_ayuda(self):
        """Muestra un diálogo de ayuda."""
        ayuda = """
<h2>🧠 Resolver Problemas con Agentes</h2>

<h3>¿Cómo funciona?</h3>
<p>Describe un problema en lenguaje natural y el sistema generará automáticamente 
una orquestación de agentes para resolverlo.</p>

<h3>Pasos:</h3>
<ol>
    <li><b>Describe tu problema</b> en el panel izquierdo</li>
    <li>Haz clic en <b>"Generar Plan"</b></li>
    <li>Revisa el plan generado (pasos, dependencias, configuraciones)</li>
    <li>Haz clic en <b>"Crear Agentes"</b> para añadirlos al sistema</li>
</ol>

<h3>Ejemplos de problemas:</h3>
<ul>
    <li><b>Análisis de datos:</b> "Obtener datos de una API, procesarlos y generar un resumen"</li>
    <li><b>Procesamiento de archivos:</b> "Leer un archivo, transformarlo y guardarlo"</li>
    <li><b>Monitoreo:</b> "Obtener métricas del sistema y generar un reporte"</li>
    <li><b>Análisis con IA:</b> "Clasificar textos y generar estadísticas"</li>
</ul>

<h3>Consejos:</h3>
<ul>
    <li>Usa ejemplos rápidos para empezar</li>
    <li>Sé específico en la descripción del problema</li>
    <li>Puedes ajustar el número máximo de pasos</li>
    <li>Revisa el plan antes de crear los agentes</li>
</ul>
"""
        msg = QMessageBox(self)
        msg.setWindowTitle("❓ Ayuda del Problem Solver")
        msg.setTextFormat(Qt.TextFormat.RichText)
        msg.setText(ayuda)
        msg.setStandardButtons(QMessageBox.StandardButton.Ok)
        msg.exec()

    # ============================================================
    # CIERRE SEGURO
    # ============================================================
    
    def reject(self):
        """Cierra el diálogo limpiando recursos."""
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(1000)
        super().reject()
    
    def closeEvent(self, event):
        """Maneja el cierre del diálogo."""
        if self._thread and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(1000)
        event.accept()