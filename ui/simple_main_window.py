# ui/simple_main_window.py
"""
UI única de Agentes Visuales.

Flujo:
  1. Usuario describe un problema en el textbox.
  2. ProblemSolver genera un plan (LLM) en un hilo separado.
  3. Scheduler ejecuta los agentes (asíncrono, con reintentos y Plan B).
  4. Al terminar, se persiste la ejecución y se alimenta al LearningEngine.

Estética: terminal retro (fósforo verde).
"""
import os
import sys

# Si se ejecuta como script suelto (python ui/simple_main_window.py),
# añadir la raíz del proyecto al sys.path para que resuelvan
# los imports storage.*, core.*, learning.*
if __package__ in (None, ""):
    _raiz = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _raiz not in sys.path:
        sys.path.insert(0, _raiz)
import logging
import random
import string
import time
from datetime import datetime
from typing import Optional

from PyQt6.QtCore import Qt, QThread, QObject, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QTextCursor, QFont, QPainter, QColor, QPen
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QTextEdit, QPushButton, QLabel, QFrame, QMessageBox,
    QGraphicsDropShadowEffect,
    QDialog, QDialogButtonBox,  # ✅ FASE 2c
)
from typing import Dict, List, Optional, Tuple
from storage.database import Database
from core.llm_client import obtener_llm_client_compartido
from core.scheduler import Scheduler
from core.problem_solver import ProblemSolver, ExecutionPlan
from core.execution_recorder import registrar_ejecucion_en_aprendizaje

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────
# Paleta y utilidades
# ─────────────────────────────────────────────────────────────────
BG = "#04070a"
GREEN = "#39ff88"
GREEN_DIM = "#1fae5c"
GREEN_FAINT = "#0d3a24"
AMBER_WARN = "#ffb454"
RED_ERR = "#ff5c5c"
CYAN_INFO = "#7fdcff"
TXT_MUTED = "#4c8c6a"

MONO_FAMILIES = ["Cascadia Code", "Consolas", "Courier New", "monospace"]

BANNER = r"""
 ██████  ██████  ██      ██    ██ ███████ ██████
██      ██    ██ ██      ██    ██ ██      ██   ██
 █████   ██    ██ ██      ██    ██ █████   ██████
     ██  ██    ██ ██       ██  ██  ██      ██   ██
██████    ██████  ███████   ████   ███████ ██   ██
""".strip("\n")

BOOT_LINES = [
    "BIOS SOLVER-TERM v2.1 ................ OK",
    "Inicializando subsistema de agentes ... OK",
    "Cargando ProblemSolver ................ OK",
    "Verificando enlace LLM ................ OK",
    "Montando scheduler concurrente ........ OK",
    "Inicializando LearningEngine .......... OK",
    "Calibrando barra de progreso ASCII .... OK",
]


def _mono_font(size: int = 12, bold: bool = False) -> QFont:
    f = QFont(MONO_FAMILIES[0], size)
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setFamilies(MONO_FAMILIES)
    f.setBold(bold)
    return f


def _glow(widget, color=GREEN, radius=14, strength=200):
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(radius)
    effect.setOffset(0, 0)
    c = QColor(color)
    c.setAlpha(strength)
    effect.setColor(c)
    widget.setGraphicsEffect(effect)


# ─────────────────────────────────────────────────────────────────
# Widgets decorativos (SIN lógica de negocio, SIN Database)
# ─────────────────────────────────────────────────────────────────
class _MatrixRainHeader(QWidget):
    """Fondo animado. El banner ASCII se monta ENCIMA con un layout."""

    CHARSET = string.ascii_uppercase + string.digits + "░▒▓█<>/\\|+*#"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(96)
        self._cols = []
        self._font = _mono_font(11)

        self.overlay_label = QLabel(self)
        self.overlay_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.overlay_label.setStyleSheet("background: transparent;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.addWidget(self.overlay_label)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(90)
        

    def resizeEvent(self, event):
        step = 14
        n = max(1, self.width() // step)
        self._cols = [
            {"x": i * step, "y": random.randint(-200, 0),
             "speed": random.randint(1, 3),
             "ch": random.choice(self.CHARSET)}
            for i in range(n)
        ]
        super().resizeEvent(event)

    def _tick(self):
        h = self.height()
        for col in self._cols:
            col["y"] += col["speed"] * 4
            if col["y"] > h:
                col["y"] = random.randint(-60, 0)
                col["speed"] = random.randint(1, 3)
            if random.random() < 0.3:
                col["ch"] = random.choice(self.CHARSET)
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(BG))
        p.setFont(self._font)
        for col in self._cols:
            p.setPen(QColor(GREEN_FAINT))
            p.drawText(col["x"], col["y"], col["ch"])
        p.end()


class _ScanlineOverlay(QWidget):
    """Overlay de scanlines CRT."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)

    def paintEvent(self, event):
        p = QPainter(self)
        pen = QPen(QColor(0, 0, 0, 26))
        pen.setWidth(1)
        p.setPen(pen)
        for y in range(0, self.height(), 3):
            p.drawLine(0, y, self.width(), y)
        p.end()


class AsciiProgressBar(QWidget):
    """Barra de progreso ASCII."""

    def __init__(self, chars: int = 32, parent=None):
        super().__init__(parent)
        self._value = 0
        self._chars = chars
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setFont(_mono_font(13, bold=True))
        self.label.setStyleSheet(f"color: {GREEN};")
        _glow(self.label, GREEN, radius=10, strength=160)
        layout.addWidget(self.label)
        self._render()

    def setFormat(self, *_args, **_kwargs):
        pass

    def setValue(self, v: int):
        self._value = max(0, min(100, int(v)))
        self._render()

    def value(self) -> int:
        return self._value

    def _render(self):
        filled = int(self._chars * self._value / 100)
        bar = "█" * filled + "░" * (self._chars - filled)
        self.label.setText(f"[{bar}] {self._value:3d}%")

# ─────────────────────────────────────────────────────────────────
# FASE 2c: Diálogo de feedback del usuario
# ─────────────────────────────────────────────────────────────────
class FeedbackDialog(QDialog):
    """
    Diálogo modal que pide al usuario una valoración tras una ejecución.

    Decisión de diseño (confirmada):
      · Excelente → score = +1.0, se guarda, NO dispara reescritura.
      · OK        → score =  0.0, se guarda. Con comentario, la LLM
                    decide si merece reescritura.
      · Mal       → score = -1.0, se guarda. Con comentario, dispara
                    reescritura (FeedbackProcessor).

    Si el usuario cierra sin elegir, devuelve None y no se guarda nada.
    """

    SCORE_MAP = {
        "excelente": 1.0,
        "ok": 0.0,
        "mal": -1.0,
    }

    def __init__(self, parent=None, resumen: str = ""):
        super().__init__(parent)
        self.setWindowTitle("¿Cómo fue esta ejecución?")
        self.setMinimumWidth(520)
        self._score: Optional[float] = None
        self._comentario: str = ""

        # Estilo consistente con la ventana principal (terminal verde).
        self.setStyleSheet(f"""
            QDialog {{ background: {BG}; color: {GREEN}; }}
            QLabel {{ color: {GREEN_DIM}; }}
            QTextEdit {{
                background: #050a08; color: {GREEN};
                border: 1px solid {GREEN_FAINT}; border-radius: 4px;
                padding: 8px; selection-background-color: {GREEN_DIM};
            }}
            QTextEdit:focus {{ border: 1px solid {GREEN}; }}
            QPushButton {{
                background: transparent; color: {GREEN};
                border: 1px solid {GREEN_DIM}; border-radius: 4px;
                padding: 10px 16px; font-weight: 700;
            }}
            QPushButton:hover {{ background: {GREEN_FAINT}; border: 1px solid {GREEN}; }}
            QPushButton:pressed {{ background: {GREEN_DIM}; color: {BG}; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        titulo = QLabel("La ejecución ha terminado. ¿Cómo fue?")
        titulo.setFont(_mono_font(11, bold=True))
        titulo.setStyleSheet(f"color: {CYAN_INFO};")
        layout.addWidget(titulo)

        if resumen:
            resumen_lbl = QLabel(resumen[:300])
            resumen_lbl.setFont(_mono_font(9))
            resumen_lbl.setWordWrap(True)
            resumen_lbl.setStyleSheet(f"color: {TXT_MUTED};")
            layout.addWidget(resumen_lbl)

        # Botones de valoración
        botones = QHBoxLayout()
        self.btn_excelente = QPushButton("Excelente")
        self.btn_ok = QPushButton("OK")
        self.btn_mal = QPushButton("Mal")
        for b, key in (
            (self.btn_excelente, "excelente"),
            (self.btn_ok, "ok"),
            (self.btn_mal, "mal"),
        ):
            b.setFont(_mono_font(10, bold=True))
            b.clicked.connect(lambda _checked=False, k=key: self._elegir(k))
            botones.addWidget(b)
        layout.addLayout(botones)

        # Comentario libre
        lbl_com = QLabel("Comentario (opcional, pero ayuda a mejorar):")
        lbl_com.setFont(_mono_font(9))
        lbl_com.setStyleSheet(f"color: {TXT_MUTED};")
        layout.addWidget(lbl_com)

        self.comentario = QTextEdit()
        self.comentario.setFont(_mono_font(10))
        self.comentario.setPlaceholderText(
            "> _ qué estuvo bien, qué falló, qué esperabas..."
        )
        self.comentario.setFixedHeight(90)
        layout.addWidget(self.comentario)

        # Botones de acción
        caja = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        caja.button(QDialogButtonBox.StandardButton.Ok).setText("Enviar")
        caja.button(QDialogButtonBox.StandardButton.Cancel).setText("Cancelar")
        caja.accepted.connect(self._on_aceptar)
        caja.rejected.connect(self.reject)
        layout.addWidget(caja)

        self._valoracion_elegida: Optional[str] = None

    def _elegir(self, key: str):
        """Marca visualmente el botón elegido."""
        self._valoracion_elegida = key
        for b, k in (
            (self.btn_excelente, "excelente"),
            (self.btn_ok, "ok"),
            (self.btn_mal, "mal"),
        ):
            if k == key:
                b.setStyleSheet(
                    f"background: {GREEN_DIM}; color: {BG}; "
                    f"border: 1px solid {GREEN}; border-radius: 4px; "
                    f"padding: 10px 16px; font-weight: 700;"
                )
            else:
                b.setStyleSheet("")

    def _on_aceptar(self):
        """Valida que se haya elegido una valoración."""
        if self._valoracion_elegida is None:
            QMessageBox.warning(
                self,
                "Falta valoración",
                "Elige Excelente, OK o Mal antes de enviar. "
                "Si no quieres opinar, pulsa Cancelar.",
            )
            return
        self._score = self.SCORE_MAP[self._valoracion_elegida]
        self._comentario = self.comentario.toPlainText().strip()
        self.accept()

    def obtener_resultado(self) -> Optional[Dict]:
        """
        Devuelve {'score': float, 'comentario': str} o None si el usuario
        canceló. La distinción score=None vs score=0.0 es importante:
        None = no hay señal, 0.0 = OK.
        """
        if self._score is None:
            return None
        return {"score": self._score, "comentario": self._comentario}

# ─────────────────────────────────────────────────────────────────
# Worker: genera el plan en un hilo aparte
# ─────────────────────────────────────────────────────────────────
class _PlanGenerator(QObject):
    plan_listo = pyqtSignal(object)
    error = pyqtSignal(str)
    progreso = pyqtSignal(str, int)

    def __init__(self, solver: ProblemSolver, problema: str, max_pasos: int = 6):
        super().__init__()
        self.solver = solver
        self.problema = problema
        self.max_pasos = max_pasos
        self._cancelado = False

    def cancelar(self):
        self._cancelado = True

    @pyqtSlot()
    def run(self):
        try:
            if self._cancelado:
                self.error.emit("generación de plan cancelada")
                return
            self.progreso.emit("consultando al LLM...", 10)

            plan = self.solver.resolver_problema(
                self.problema, max_pasos=self.max_pasos
            )

            if self._cancelado:
                self.error.emit("generación de plan cancelada")
                return

            self.progreso.emit(
                f"plan generado ({len(plan.agentes_generados)} agentes)", 40
            )
            self.plan_listo.emit(plan)

        except Exception as e:
            logger.exception("Error generando plan")
            if not self._cancelado:
                self.error.emit(str(e))


# ─────────────────────────────────────────────────────────────────
# Ventana principal
# ─────────────────────────────────────────────────────────────────
class SimpleMainWindow(QMainWindow):

    # ✅ Signal para log desde hilos worker.
    _log_desde_worker = pyqtSignal(str, str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SOLVER://terminal")
        self.setMinimumSize(760, 660)

        # ══════════════════════════════════════════════════════════
        # ORDEN CRÍTICO:
        #  1. Database
        #  2. LearningEngine singleton (con la ruta correcta)
        #  3. LLMClient compartido
        #  4. ProblemSolver
        #  5. Scheduler
        # ══════════════════════════════════════════════════════════

        # 1. Database
        self.db = Database()

        # 2. LearningEngine (reentrena al arrancar)
        self._inicializar_learning_engine()

        # 3. LLM compartido
        self.solver: Optional[ProblemSolver] = None
        try:
            llm = obtener_llm_client_compartido()
            if llm and llm.disponible:
                self.solver = ProblemSolver(llm)
            else:
                logger.warning("LLM no disponible (falta DEEPSEEK_API_KEY)")
        except Exception as e:
            logger.warning(f"ProblemSolver no disponible: {e}")

        # 4. Scheduler
        self.scheduler = Scheduler(max_concurrent=4)

        # 5. Estado interno
        self._hilo_plan: Optional[QThread] = None
        self._worker_plan: Optional[_PlanGenerator] = None
        self._ejecutando = False
        self._ultimo_problema: str = ""
        self._ultimo_plan: Optional[ExecutionPlan] = None
        self._tiempo_inicio_ejecucion: Optional[float] = None
        self._cursor_on = True
        self._status_base = "listo"
        self._ultima_ejecucion_id: int = 0

        # UI
        self._init_ui()
        self._log_desde_worker.connect(self._log, Qt.ConnectionType.QueuedConnection)
        self._conectar_scheduler()
        self._iniciar_cursor_parpadeante()
        self._iniciar_boot_sequence()

    # ── Learning ────────────────────────────────────────────────
    def _inicializar_learning_engine(self):
        """Crea el singleton de learning con la ruta correcta y reentrena."""
        try:
            from learning import obtener_learning_engine
            engine = obtener_learning_engine(
                db_path=self.db.db_path,
                llm_client=obtener_llm_client_compartido(),
            )
            if engine is not None:
                resumen = engine.reentrenar_desde_historial()
                logger.info(
                    f"🧠 LearningEngine listo: "
                    f"{resumen.get('agentes_minados', 0)} muestras minadas, "
                    f"entrenado={bool(resumen.get('entrenado', 0))}"
                )
        except Exception as e:
            logger.debug(f"LearningEngine no inicializado: {e}")

    # ── UI ──────────────────────────────────────────────────────
    def _init_ui(self):
        self.setStyleSheet(f"""
            QMainWindow {{ background: {BG}; }}
            QWidget {{ background: {BG}; }}
            QTextEdit {{
                background: #050a08; color: {GREEN};
                border: 1px solid {GREEN_FAINT}; border-radius: 4px;
                padding: 12px; selection-background-color: {GREEN_DIM};
            }}
            QTextEdit:focus {{ border: 1px solid {GREEN}; }}
            QPushButton {{
                background: transparent; color: {GREEN};
                border: 1px solid {GREEN_DIM}; border-radius: 4px;
                padding: 12px; font-weight: 700; letter-spacing: 1px;
            }}
            QPushButton:hover {{ background: {GREEN_FAINT}; border: 1px solid {GREEN}; }}
            QPushButton:pressed {{ background: {GREEN_DIM}; color: {BG}; }}
            QPushButton:disabled {{ color: {TXT_MUTED}; border: 1px solid #12241a; }}
            QLabel {{ color: {TXT_MUTED}; }}
            QScrollBar:vertical {{ background: #05100a; width: 10px; }}
            QScrollBar::handle:vertical {{ background: {GREEN_DIM}; border-radius: 4px; min-height: 24px; }}
            QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        """)

        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Cabecera con matrix rain
        self._rain = _MatrixRainHeader()
        self.banner_label = self._rain.overlay_label
        self.banner_label.setText(BANNER)
        self.banner_label.setFont(_mono_font(9, bold=True))
        self.banner_label.setStyleSheet(f"color: {GREEN}; background: transparent;")
        _glow(self.banner_label, GREEN, radius=18, strength=180)
        outer.addWidget(self._rain)

        sub = QLabel("── resolución de problemas mediante agentes autónomos ──")
        sub.setFont(_mono_font(9))
        sub.setStyleSheet(f"color: {TXT_MUTED};")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(sub)

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(22, 14, 22, 18)
        layout.setSpacing(12)
        outer.addWidget(body, stretch=1)

        prompt_lbl = QLabel("root@solver:~$ describe el problema a resolver")
        prompt_lbl.setFont(_mono_font(10))
        prompt_lbl.setStyleSheet(f"color: {CYAN_INFO};")
        layout.addWidget(prompt_lbl)

        self.textbox = QTextEdit()
        self.textbox.setFont(_mono_font(12))
        self.textbox.setPlaceholderText(
            "> _ pega aquí el enunciado, datos, o lo que quieres resolver..."
        )
        layout.addWidget(self.textbox, stretch=2)

        fila = QHBoxLayout()
        self.btn_ejecutar = QPushButton("[ ▶ EJECUTAR ]")
        self.btn_ejecutar.setFont(_mono_font(11, bold=True))
        self.btn_ejecutar.clicked.connect(self._on_ejecutar)
        fila.addWidget(self.btn_ejecutar, stretch=3)

        self.btn_detener = QPushButton("[ ■ ABORTAR ]")
        self.btn_detener.setFont(_mono_font(11, bold=True))
        self.btn_detener.setEnabled(False)
        self.btn_detener.clicked.connect(self._on_detener)
        fila.addWidget(self.btn_detener, stretch=1)
        layout.addLayout(fila)

        self.progress = AsciiProgressBar()
        layout.addWidget(self.progress)

        self.status_label = QLabel("")
        self.status_label.setFont(_mono_font(11))
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setStyleSheet(f"color: {GREEN_DIM};")
        layout.addWidget(self.status_label)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {GREEN_FAINT}; background: {GREEN_FAINT};")
        layout.addWidget(sep)

        log_lbl = QLabel("── stdout ──")
        log_lbl.setFont(_mono_font(9))
        log_lbl.setStyleSheet(f"color: {TXT_MUTED};")
        layout.addWidget(log_lbl)

        self.log = QTextEdit()
        self.log.setObjectName("logArea")
        self.log.setReadOnly(True)
        self.log.setFont(_mono_font(11))
        layout.addWidget(self.log, stretch=1)

        self.lbl_disp = QLabel("")
        self.lbl_disp.setFont(_mono_font(10, bold=True))
        self.lbl_disp.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.lbl_disp)

        if self.solver:
            self.lbl_disp.setText("● IA DISPONIBLE")
            self.lbl_disp.setStyleSheet(f"color: {GREEN};")
        else:
            self.lbl_disp.setText("● IA NO DISPONIBLE — falta DEEPSEEK_API_KEY")
            self.lbl_disp.setStyleSheet(f"color: {RED_ERR};")
            self.btn_ejecutar.setEnabled(False)

        # Scanlines por encima
        self._scanlines = _ScanlineOverlay(central)
        self._scanlines.setGeometry(central.rect())
        self._scanlines.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "_scanlines"):
            self._scanlines.setGeometry(self.centralWidget().rect())

    # ── Cursor parpadeante ──────────────────────────────────────
    def _iniciar_cursor_parpadeante(self):
        self._cursor_timer = QTimer(self)
        self._cursor_timer.timeout.connect(self._toggle_cursor)
        self._cursor_timer.start(500)

    def _toggle_cursor(self):
        self._cursor_on = not self._cursor_on
        cursor_char = "█" if self._cursor_on else " "
        self.status_label.setText(f"{self._status_base} {cursor_char}")

    # ── Boot sequence ───────────────────────────────────────────
    def _iniciar_boot_sequence(self):
        self.btn_ejecutar.setEnabled(False)
        self._boot_index = 0
        self._boot_timer = QTimer(self)
        self._boot_timer.timeout.connect(self._boot_step)
        self._boot_timer.start(180)

    def _boot_step(self):
        if self._boot_index < len(BOOT_LINES):
            self._log(BOOT_LINES[self._boot_index], TXT_MUTED)
            self._boot_index += 1
        else:
            self._boot_timer.stop()
            self._log("SISTEMA LISTO ── esperando comandos", GREEN)
            self._set_status("listo")
            if self.solver:
                self.btn_ejecutar.setEnabled(True)

    # ── Scheduler ───────────────────────────────────────────────
    def _conectar_scheduler(self):
        self.scheduler.agente_actualizado.connect(
            self._on_agente_actualizado, Qt.ConnectionType.QueuedConnection
        )
        self.scheduler.log_mensaje.connect(
            self._on_log_scheduler, Qt.ConnectionType.QueuedConnection
        )
        self.scheduler.ejecucion_terminada.connect(
            self._on_ejecucion_terminada, Qt.ConnectionType.QueuedConnection
        )

    def _log(self, mensaje: str, color: str = GREEN):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append(
            f'<span style="color:{TXT_MUTED}">[{ts}]</span> '
            f'<span style="color:{color}">{mensaje}</span>'
        )
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log.setTextCursor(cursor)

    def _set_status(self, texto: str, color: str = GREEN_DIM):
        self._status_base = texto
        self.status_label.setStyleSheet(f"color: {color};")

    # ── Ejecución ───────────────────────────────────────────────
    def _on_ejecutar(self):
        logger.info(
        f"[EJECUTAR] llamado | _ejecutando={self._ejecutando} | "
        f"solver={'OK' if self.solver else 'None'} | "
        f"boton_enabled={self.btn_ejecutar.isEnabled()}"
    )
        if self._ejecutando:
            return

        problema = self.textbox.toPlainText().strip()
        if not problema:
            self._set_status("⚠ escribe algo primero", AMBER_WARN)
            return
        if not self.solver:
            QMessageBox.warning(self, "IA no disponible", "Configura DEEPSEEK_API_KEY.")
            return

        # ── 1. Desconectar y detener scheduler ANTERIOR ──
        self._desconectar_scheduler()
        if getattr(self, 'scheduler', None):
            try:
                self.scheduler.detener()
                self.scheduler.limpiar()
            except Exception:
                pass

        # ── 2. Cancelar worker de plan anterior si quedó colgado ──
        if self._worker_plan is not None:
            try:
                self._worker_plan.cancelar()
            except RuntimeError:
                pass

        if self._hilo_plan is not None:
            try:
                if self._hilo_plan.isRunning():
                    self._hilo_plan.quit()
                    self._hilo_plan.wait(1000)
            except RuntimeError:
                # El objeto C++ ya fue destruido; limpiamos la referencia Python
                self._hilo_plan = None

        # ── 3. Limpiar EventBus (singleton, acumula eventos) ──
        try:
            from core.event_bus import obtener_bus
            obtener_bus().limpiar_historial()
        except Exception:
            pass

        # ── 4. Estado limpio para la NUEVA ejecución ──
        self._ejecutando = True
        self._ultimo_problema = problema
        self._ultimo_plan = None
        self._tiempo_inicio_ejecucion = time.time()

        self.btn_ejecutar.setEnabled(False)
        self.btn_detener.setEnabled(True)
        self.progress.setValue(0)
        self.log.clear()

        self._set_status("generando plan...", CYAN_INFO)
        resumen = problema[:80] + ("..." if len(problema) > 80 else "")
        self._log(f"> {resumen}", CYAN_INFO)

        # ── 5. Scheduler NUEVO (instancia aislada) ──
        self.scheduler = Scheduler(max_concurrent=4)
        self._conectar_scheduler()

        # ── 6. Lanzar generación de plan ──
        self._hilo_plan = QThread()
        self._worker_plan = _PlanGenerator(self.solver, problema, max_pasos=6)
        self._worker_plan.moveToThread(self._hilo_plan)

        self._hilo_plan.started.connect(self._worker_plan.run)
        self._worker_plan.progreso.connect(self._on_progreso_plan)
        self._worker_plan.plan_listo.connect(self._on_plan_listo)
        self._worker_plan.error.connect(self._on_error_plan)
        self._worker_plan.plan_listo.connect(self._hilo_plan.quit)
        self._worker_plan.error.connect(self._hilo_plan.quit)
        self._hilo_plan.finished.connect(self._on_hilo_plan_terminado)

        self._hilo_plan.start()

    def _on_detener(self):
        if self._worker_plan is not None:
            try:
                self._worker_plan.cancelar()
            except RuntimeError:
                pass
        try:
            self.scheduler.detener()
        except Exception:
            pass
        self._log("señal de aborto enviada", RED_ERR)
        self._set_status("deteniendo...", RED_ERR)

        # Reset forzado por si el scheduler no emite señal a tiempo
        QTimer.singleShot(1500, self._forzar_reset_si_sigue_ejecutando)


    def _forzar_reset_si_sigue_ejecutando(self):
        if self._ejecutando:
            self._log("ejecución abortada (timeout de cierre)", RED_ERR)
            self._reset_estado_ejecucion()

    @pyqtSlot(str, int)
    def _on_progreso_plan(self, mensaje: str, pct: int):
        self.progress.setValue(pct)
        self._set_status(mensaje, CYAN_INFO)

    @pyqtSlot(object)
    def _on_plan_listo(self, plan: ExecutionPlan):
        self._ultimo_plan = plan

        for adv in plan.advertencias:
            self._log(f"⚠ {adv}", AMBER_WARN)

        if not plan.agentes_generados:
            self._on_error_plan("el plan no generó agentes.")
            return

        self._log(
            f"plan «{plan.titulo}» listo ({len(plan.agentes_generados)} agentes)",
            GREEN,
        )
        self._set_status("cargando agentes...", CYAN_INFO)

        self._inyectar_contexto_plan_b()

        self.scheduler.agregar_agentes(plan.agentes_generados)
        self.scheduler.resolver_dependencias()
        self._log("iniciando ejecución de agentes...", GREEN)
        self.scheduler.iniciar()

    @pyqtSlot(str)
    def _on_error_plan(self, error: str):
        self._ejecutando = False
        self.btn_ejecutar.setEnabled(bool(self.solver))
        self.btn_detener.setEnabled(False)
        self._set_status("✗ error", RED_ERR)
        self._log(f"✗ {error}", RED_ERR)

    def _inyectar_contexto_plan_b(self):
        if not self.solver or not self._ultimo_plan:
            return
        try:
            from core.plan_recovery import PlanRecovery
            recovery = PlanRecovery(
                llm_client=self.solver.llm_client,
                problem_solver=self.solver,
                db_path=self.db.db_path,
            )
            self.scheduler.set_contexto_plan_b(
                recovery=recovery,
                problema_original=self._ultimo_problema,
                plan_original=self._ultimo_plan,
            )
            self._log("plan B activado para esta ejecución", TXT_MUTED)
        except Exception as e:
            logger.debug(f"Plan B no disponible: {e}")

    @pyqtSlot(str)
    def _on_agente_actualizado(self, agente_id: str):
        agente = self.scheduler.obtener_agente(agente_id)
        if not agente:
            return

        stats = self.scheduler.obtener_estadisticas()
        total = stats.get("total", 0) or 1
        hechos = (
            stats.get("completados", 0)
            + stats.get("errores", 0)
            + stats.get("cancelados", 0)
            + stats.get("bloqueados", 0)
        )
        pct = 40 + int((hechos / total) * 60)
        self.progress.setValue(min(100, pct))
        self._set_status(f"ejecutando {hechos}/{total} agentes...", CYAN_INFO)

    @pyqtSlot(str, str)
    def _on_log_scheduler(self, mensaje: str, color: str):
        self._log(mensaje, color or GREEN)

    @pyqtSlot()
    def _on_hilo_plan_terminado(self):
        """Limpia la referencia al QThread cuando termina."""
        # El QThread ya terminó; soltamos la referencia Python.
        # No usamos deleteLater porque queremos controlar cuándo se destruye.
        self._worker_plan = None
        if self._hilo_plan is not None:
            # Programamos el borrado del C++ pero NO guardamos más la referencia
            hilo = self._hilo_plan
            self._hilo_plan = None
            hilo.deleteLater()

    @pyqtSlot()
    def _on_ejecucion_terminada(self):
        self._ejecutando = False
        self.btn_ejecutar.setEnabled(True)
        self.btn_detener.setEnabled(False)
        self.progress.setValue(100)

        stats = self.scheduler.obtener_estadisticas()
        completados = stats.get("completados", 0)
        errores = stats.get("errores", 0)
        bloqueados = stats.get("bloqueados", 0)

        if errores == 0 and bloqueados == 0:
            self._set_status("✓ completado", GREEN)
        else:
            self._set_status(
                f"⚠ terminado ({completados} ok, {errores} err, {bloqueados} bloq)",
                AMBER_WARN,
            )
        self._log("── ejecución finalizada ──", TXT_MUTED)

        # Persistir + alimentar learning
        if self._tiempo_inicio_ejecucion:
            duracion_total = time.time() - self._tiempo_inicio_ejecucion
            ejecucion_id = registrar_ejecucion_en_aprendizaje(
                scheduler=self.scheduler,
                db=self.db,
                plan=self._ultimo_plan,
                problema=self._ultimo_problema,
                duracion_total=duracion_total,
            )
            self._ultima_ejecucion_id = ejecucion_id or 0
            if ejecucion_id:
                self._log(f"💾 ejecución guardada (ID: {ejecucion_id})", TXT_MUTED)
                self._log("🧠 aprendizaje lanzado en background", TXT_MUTED)
            self._tiempo_inicio_ejecucion = None

        # ✅ FASE 2c: feedback (fuera del if, para que se dispare siempre
        # que haya una ejecución completa con id asignado).
        if self._ultima_ejecucion_id:
            self._quizas_pedir_feedback()

        # ── FASE 2c: feedback del usuario ────────────────────────────
    def _quizas_pedir_feedback(self):
        """
        Decide si pedir feedback (1 de cada 3 ejecuciones) y, si toca,
        abre el diálogo. El feedback se guarda en `feedback_usuario` y,
        si procede, se procesa en un hilo de background para reescribir
        el prompt del agente LLM relevante.
        """
        import random
        if random.random() >= 1 / 3:
            return

        if not self._ultimo_plan:
            return

        # Resumen para el diálogo
        stats = self.scheduler.obtener_estadisticas() if self.scheduler else {}
        completados = stats.get("completados", 0)
        errores = stats.get("errores", 0)
        total = stats.get("total", 0)
        resumen = (
            f"Plan: {self._ultimo_plan.titulo} | "
            f"{total} agentes: ✅{completados} ❌{errores}"
        )

        dialog = FeedbackDialog(self, resumen=resumen)
        resultado = dialog.exec()

        if resultado != QDialog.DialogCode.Accepted:
            self._log("feedback omitido", TXT_MUTED)
            return

        datos = dialog.obtener_resultado()
        if not datos:
            return

        self._guardar_y_procesar_feedback(datos)

    def _guardar_y_procesar_feedback(self, datos: Dict):
        """
        Guarda el feedback en `feedback_usuario` y lanza el procesamiento
        en un hilo de background (no bloquea la UI).

        Si el feedback es positivo (Excelente), no se procesa: el prompt
        funciona, no se toca.
        """
        score = datos["score"]
        comentario = datos["comentario"]

        try:
            import sqlite3
            with sqlite3.connect(self.db.db_path, timeout=10) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                cur = conn.execute(
                    """INSERT INTO feedback_usuario
                       (ejecucion_id, agente_ejecucion_id, alcance,
                        score, comentario, fecha)
                       VALUES (?, NULL, 'plan', ?, ?, ?)""",
                    (
                        self._ultima_ejecucion_id or 0,
                        score,
                        comentario,
                        datetime.now().isoformat(),
                    ),
                )
                feedback_id = cur.lastrowid
                conn.commit()
        except Exception as e:
            self._log(f"⚠ no se pudo guardar el feedback: {e}", AMBER_WARN)
            return

        self._log(
            f"💬 feedback guardado (id={feedback_id}, score={score:+.1f})",
            TXT_MUTED,
        )

        # Solo procesamos si hay señal negativa o tibia con comentario.
        if score > 0:
            return
        if not comentario:
            return

        self._log("🧠 procesando feedback para reescribir prompt...", CYAN_INFO)
        self._lanzar_procesamiento_feedback(feedback_id)

    def _lanzar_procesamiento_feedback(self, feedback_id: int):
        """Lanza el FeedbackProcessor en un hilo daemon y reporta por signal."""
        import threading

        def worker():
            try:
                from learning.feedback_processor import FeedbackProcessor
                from core.llm_client import obtener_llm_client_compartido
                proc = FeedbackProcessor(
                    self.db.db_path, obtener_llm_client_compartido()
                )
                res = proc.procesar_feedback(feedback_id)
                msg = (
                    f"✅ prompt reescrito (id={res.get('prompt_id')})"
                    if res.get("procesado")
                    else f"ℹ no se reescribió: {res.get('razon', '?')}"
                )
                self._log_desde_worker.emit(msg, TXT_MUTED)
            except Exception as e:
                self._log_desde_worker.emit(f"⚠ feedback falló: {e}", AMBER_WARN)

        threading.Thread(
            target=worker, name=f"feedback-{feedback_id}", daemon=True
        ).start()

    # ── Cierre ──────────────────────────────────────────────────
    def closeEvent(self, event):
        try:
            if self._worker_plan is not None:
                try:
                    self._worker_plan.cancelar()
                except RuntimeError:
                    pass

            if self._hilo_plan is not None:
                try:
                    if self._hilo_plan.isRunning():
                        self._hilo_plan.quit()
                        self._hilo_plan.wait(2000)
                except RuntimeError:
                    pass

            self._desconectar_scheduler()
            if getattr(self, 'scheduler', None):
                try:
                    self.scheduler.detener()
                except Exception:
                    pass

            self.db.close()
        except Exception:
            logger.exception("Error en closeEvent")
        event.accept()

    def _reset_estado_ejecucion(self):
        """Deja la UI lista para una nueva ejecución."""
        self._ejecutando = False
        self._tiempo_inicio_ejecucion = None
        self.btn_ejecutar.setEnabled(bool(self.solver))
        self.btn_detener.setEnabled(False)
        self.progress.setValue(0)
        self._set_status("listo", GREEN_DIM)


    def _desconectar_scheduler(self):
        """Desconecta las señales del scheduler actual (si existe)."""
        if not getattr(self, 'scheduler', None):
            return
        for signal, slot in (
            (self.scheduler.agente_actualizado, self._on_agente_actualizado),
            (self.scheduler.log_mensaje, self._on_log_scheduler),
            (self.scheduler.ejecucion_terminada, self._on_ejecucion_terminada),
        ):
            try:
                signal.disconnect(slot)
            except TypeError:
                # Ya estaba desconectada
                pass


if __name__ == "__main__":
    
    logging.basicConfig(level=logging.INFO)
    app = QApplication(sys.argv)
    w = SimpleMainWindow()
    w.show()
    sys.exit(app.exec())