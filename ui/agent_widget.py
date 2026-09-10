# ui/agent_widget.py
import time
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar, QPushButton
from PyQt6.QtGui import QFont
from PyQt6.QtCore import pyqtSignal
from core.agent import Agente, EstadoAgente


class AgentWidget(QWidget):
    """Widget visual para representar un agente"""
    editar_solicitado = pyqtSignal(str)
    ejecutar_solicitado = pyqtSignal(str)  
    eliminar_solicitado = pyqtSignal(str)   # <-- NUEVA SEÑAL

    def __init__(self, agente: Agente, parent=None):
        super().__init__(parent)
        self.agente = agente
        self._init_ui()
        self.actualizar()

    def _init_ui(self):
        layout = QVBoxLayout()
        layout.setSpacing(5)
        layout.setContentsMargins(10, 8, 10, 8)

        # Cabecera con nombre y estado
        header = QHBoxLayout()

        # Botón Editar
        self.btn_editar = QPushButton("✏️")
        self.btn_editar.setFixedWidth(28)
        self.btn_editar.setToolTip("Editar configuración / prompt")
        self.btn_editar.clicked.connect(lambda: self.editar_solicitado.emit(self.agente.id))
        self.btn_editar.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #e9ecef;
                border-radius: 4px;
            }
        """)
        header.addWidget(self.btn_editar)

        # Botón Ejecutar Individual (NUEVO)
        self.btn_ejecutar = QPushButton("▶")
        self.btn_ejecutar.setFixedWidth(28)
        self.btn_ejecutar.setToolTip("Ejecutar este agente individualmente")
        self.btn_ejecutar.clicked.connect(lambda: self.ejecutar_solicitado.emit(self.agente.id))
        self.btn_ejecutar.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                font-size: 12px;
                color: #28a745;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #e9ecef;
                border-radius: 4px;
            }
            QPushButton:disabled {
                color: #adb5bd;
            }
        """)
        header.addWidget(self.btn_ejecutar)

        # Botón Eliminar
        self.btn_eliminar = QPushButton("🗑")
        self.btn_eliminar.setFixedWidth(28)
        self.btn_eliminar.setToolTip("Eliminar este agente")
        self.btn_eliminar.clicked.connect(
            lambda: self.eliminar_solicitado.emit(self.agente.id)
        )
        self.btn_eliminar.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                font-size: 12px;
                color: #dc3545;
            }
            QPushButton:hover {
                background-color: #f8d7da;
                border-radius: 4px;
            }
            QPushButton:disabled {
                color: #adb5bd;
            }
        """)
        header.addWidget(self.btn_eliminar) 

        # Nombre del agente
        self.lbl_nombre = QLabel(f"🤖 {self.agente.nombre}")
        self.lbl_nombre.setFont(QFont("", 11, QFont.Weight.Bold))
        header.addWidget(self.lbl_nombre)

        # Estado
        self.lbl_estado = QLabel("")
        self.lbl_estado.setFont(QFont("", 10))
        header.addWidget(self.lbl_estado)

        header.addStretch()

        # Tiempo
        self.lbl_tiempo = QLabel("")
        self.lbl_tiempo.setFont(QFont("", 9))
        self.lbl_tiempo.setStyleSheet("color: #888;")
        header.addWidget(self.lbl_tiempo)

        layout.addLayout(header)

        # Barra de progreso
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(True)
        layout.addWidget(self.progress)

        # Mensaje y dependencias
        self.lbl_mensaje = QLabel("")
        self.lbl_mensaje.setFont(QFont("", 9))
        self.lbl_mensaje.setStyleSheet("color: #666;")
        layout.addWidget(self.lbl_mensaje)

        deps_layout = QHBoxLayout()
        self.lbl_deps = QLabel("")
        self.lbl_deps.setFont(QFont("", 9))
        self.lbl_deps.setStyleSheet("color: #999;")
        deps_layout.addWidget(self.lbl_deps)
        deps_layout.addStretch()
        layout.addLayout(deps_layout)

        self.setLayout(layout)
        self.setStyleSheet("""
            AgentWidget {
                background-color: #f8f9fa;
                border: 1px solid #dee2e6;
                border-radius: 6px;
                margin: 4px;
            }
        """)

    def actualizar(self):
        """Actualiza la UI según el estado del agente"""
        a = self.agente

        # ---- Progreso ----
        self.progress.setValue(a.obtener_progreso_real())

        # ---- Estado y colores ----
        # ui/agent_widget.py - ACTUALIZAR estados

        estados = {
            EstadoAgente.PENDIENTE:   ("⏳ Pendiente", "#6c757d"),
            EstadoAgente.EN_COLA:     ("📋 En cola", "#6c757d"),
            EstadoAgente.ESPERANDO:   ("🔄 Esperando", "#fd7e14"),
            EstadoAgente.LISTO:       ("✅ Listo", "#28a745"),
            EstadoAgente.EJECUTANDO:  ("⚡ Ejecutando", "#007bff"),
            EstadoAgente.REINTENTANDO: ("🔄 Reintentando", "#ffc107"),
            EstadoAgente.COMPLETADO:  ("✅ Completado", "#28a745"),
            EstadoAgente.ERROR:       ("❌ Error", "#dc3545"),
            EstadoAgente.TIMEOUT:     ("⏱️ Timeout", "#dc3545"),
            EstadoAgente.CANCELADO:   ("⛔ Cancelado", "#6c757d"),
            EstadoAgente.SALTADO:     ("⏭️ Saltado", "#6c757d"),
            EstadoAgente.BLOQUEADO:   ("🚫 Bloqueado", "#8b0000"),
        }

        # Estilos de barra
        estilos = {
            EstadoAgente.COMPLETADO: "QProgressBar::chunk { background-color: #28a745; }",
            EstadoAgente.ERROR:      "QProgressBar::chunk { background-color: #dc3545; }",
            EstadoAgente.TIMEOUT:    "QProgressBar::chunk { background-color: #dc3545; }",
            EstadoAgente.EJECUTANDO: "QProgressBar::chunk { background-color: #007bff; }",
            EstadoAgente.REINTENTANDO: "QProgressBar::chunk { background-color: #ffc107; }",
            EstadoAgente.ESPERANDO:  "QProgressBar::chunk { background-color: #fd7e14; }",
            EstadoAgente.BLOQUEADO:  "QProgressBar::chunk { background-color: #8b0000; }",
        }
        self.progress.setStyleSheet(estilos.get(a.estado, ""))

        # ---- Mensaje ----
        self.lbl_mensaje.setText(a.mensaje if a.mensaje else " ")

        # ---- Dependencias ----
        if a.dependencias_ids:
            deps_text = f"Depende de: {', '.join(d[:8] for d in a.dependencias_ids)}"
        else:
            deps_text = "Sin dependencias"
        self.lbl_deps.setText(deps_text)

        # ---- Tiempo ----
        if a.tiempo_inicio and a.tiempo_fin:
            tiempo = a.tiempo_fin - a.tiempo_inicio
            self.lbl_tiempo.setText(f"⌛ {tiempo:.1f}s")
        elif a.tiempo_inicio:
            self.lbl_tiempo.setText(f"⌛ {time.time() - a.tiempo_inicio:.1f}s")
        else:
            self.lbl_tiempo.setText("")

        # ---- Habilitar/deshabilitar botones durante ejecución ----
        ejecutando = (a.estado == EstadoAgente.EJECUTANDO)
        self.btn_editar.setEnabled(not ejecutando)
        self.btn_ejecutar.setEnabled(not ejecutando)
        self.btn_eliminar.setEnabled(not ejecutando)