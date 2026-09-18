# ui/admin_panel.py
"""
Panel de administración del sistema de aprendizaje.

Vista de solo lectura sobre el estado del A/B testing:
  · KPIs globales (feedback, reescrituras, usos, promociones).
  · Tabla con todas las reescrituras y su score medio.
  · Botón Refrescar para recargar datos.

No modifica la BD. Las acciones (desactivar, promover, descartar)
vendrán en una fase posterior.
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import closing        # ← añadir
from datetime import datetime
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QColor, QTextCursor
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QFrame, QTextEdit, QSizePolicy,
)

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────────
# Paleta (coherente con simple_main_window)
# ────────────────────────────────────────────────────────────────
BG = "#04070a"
GREEN = "#39ff88"
GREEN_DIM = "#1fae5c"
GREEN_FAINT = "#0d3a24"
AMBER_WARN = "#ffb454"
RED_ERR = "#ff5c5c"
CYAN_INFO = "#7fdcff"
TXT_MUTED = "#4c8c6a"

# Tabla: fondo un poco más claro para mejor legibilidad
TABLE_BG = "#0a1410"
TABLE_ALT_BG = "#0d1a14"
TABLE_HEADER_BG = "#152a1e"

MONO_FAMILIES = ["Cascadia Code", "Consolas", "Courier New", "monospace"]


def _mono_font(size: int = 11, bold: bool = False) -> QFont:
    f = QFont(MONO_FAMILIES[0], size)
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setFamilies(MONO_FAMILIES)
    f.setBold(bold)
    return f


# ────────────────────────────────────────────────────────────────
# Widget KPI (una caja con número grande y etiqueta)
# ────────────────────────────────────────────────────────────────
class KpiBox(QFrame):
    """Caja con un número grande y una etiqueta debajo."""

    def __init__(self, titulo: str, color: str = GREEN, parent=None):
        super().__init__(parent)
        self._color = color
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setMinimumHeight(80)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)

        self.valor_lbl = QLabel("—")
        self.valor_lbl.setFont(_mono_font(24, bold=True))
        self.valor_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.valor_lbl.setStyleSheet(f"color: {color};")
        layout.addWidget(self.valor_lbl)

        titulo_lbl = QLabel(titulo)
        titulo_lbl.setFont(_mono_font(9))
        titulo_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        titulo_lbl.setStyleSheet(f"color: {TXT_MUTED};")
        layout.addWidget(titulo_lbl)

    def set_valor(self, valor):
        self.valor_lbl.setText(str(valor))


# ────────────────────────────────────────────────────────────────
# Ventana principal del panel
# ────────────────────────────────────────────────────────────────
class AdminPanelWindow(QMainWindow):
    """
    Ventana de administración del sistema de aprendizaje.

    Uso:
        panel = AdminPanelWindow(db_path="agent_history.db")
        panel.show()
    """

    REFRESCO_SEGUNDOS = 0  # sin auto-refresh; solo manual

    def __init__(self, db_path: str, parent=None):
        super().__init__(parent)
        self.db_path = str(db_path)
        self.setWindowTitle("ADMIN://learning")
        self.setMinimumSize(1000, 640)

        self._init_ui()
        self._cargar_todo()

    # ────────────────────────────────────────────────────────────
    # UI
    # ────────────────────────────────────────────────────────────
    def _init_ui(self):
        self.setStyleSheet(f"""
            QMainWindow {{ background: {BG}; }}
            QWidget {{ background: {BG}; color: {GREEN}; }}
            QLabel {{ color: {GREEN_DIM}; }}
            QFrame {{ background: {BG}; }}
            QPushButton {{
                background: transparent; color: {GREEN};
                border: 1px solid {GREEN_DIM}; border-radius: 4px;
                padding: 8px 16px; font-weight: 700;
            }}
            QPushButton:hover {{ background: {GREEN_FAINT}; border: 1px solid {GREEN}; }}
            QPushButton:pressed {{ background: {GREEN_DIM}; color: {BG}; }}
            QTableWidget {{
                background: {TABLE_BG}; color: {GREEN};
                gridline-color: {GREEN_FAINT};
                border: 1px solid {GREEN_FAINT};
                selection-background-color: {GREEN_DIM};
                selection-color: {BG};
            }}
            QHeaderView::section {{
                background: {TABLE_HEADER_BG};
                color: {GREEN_DIM};
                border: 1px solid {GREEN_FAINT};
                padding: 6px;
                font-weight: 700;
            }}
            QTextEdit {{
                background: #050a08; color: {GREEN};
                border: 1px solid {GREEN_FAINT}; border-radius: 4px;
                padding: 6px;
            }}
        """)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(12)

        # ── Cabecera ──
        cabecera = QHBoxLayout()
        titulo = QLabel("📊 ADMIN://learning")
        titulo.setFont(_mono_font(14, bold=True))
        titulo.setStyleSheet(f"color: {CYAN_INFO};")
        cabecera.addWidget(titulo)
        cabecera.addStretch()

        self.lbl_ultima_carga = QLabel("—")
        self.lbl_ultima_carga.setFont(_mono_font(9))
        self.lbl_ultima_carga.setStyleSheet(f"color: {TXT_MUTED};")
        cabecera.addWidget(self.lbl_ultima_carga)

        self.btn_refrescar = QPushButton("↻ Refrescar")
        self.btn_refrescar.setFont(_mono_font(10, bold=True))
        self.btn_refrescar.clicked.connect(self._cargar_todo)
        cabecera.addWidget(self.btn_refrescar)

        layout.addLayout(cabecera)

        # ── KPIs (dos filas de 3) ──
        kpis_layout = QGridLayout()
        kpis_layout.setSpacing(10)

        self.kpi_feedback      = KpiBox("Feedback total",     CYAN_INFO)
        self.kpi_reescrituras  = KpiBox("Reescrituras",       GREEN)
        self.kpi_activos       = KpiBox("A/B activos",        GREEN)
        self.kpi_usos          = KpiBox("Usos de A/B",        CYAN_INFO)
        self.kpi_promociones   = KpiBox("Promociones",        GREEN_DIM)
        self.kpi_descartes     = KpiBox("Descartes",          AMBER_WARN)

        kpis_layout.addWidget(self.kpi_feedback,     0, 0)
        kpis_layout.addWidget(self.kpi_reescrituras, 0, 1)
        kpis_layout.addWidget(self.kpi_activos,      0, 2)
        kpis_layout.addWidget(self.kpi_usos,         1, 0)
        kpis_layout.addWidget(self.kpi_promociones,  1, 1)
        kpis_layout.addWidget(self.kpi_descartes,    1, 2)

        layout.addLayout(kpis_layout)

        # ── Separador ──
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"background: {GREEN_FAINT};")
        sep.setFixedHeight(1)
        layout.addWidget(sep)

        # ── Tabla de reescrituras ──
        tabla_lbl = QLabel("Reescrituras de prompt")
        tabla_lbl.setFont(_mono_font(11, bold=True))
        tabla_lbl.setStyleSheet(f"color: {CYAN_INFO};")
        layout.addWidget(tabla_lbl)

        self.tabla = QTableWidget()
        self.tabla.setFont(_mono_font(10))
        self.tabla.setColumnCount(7)
        self.tabla.setHorizontalHeaderLabels([
            "ID", "Firma", "Estado", "Usos", "Score medio",
            "Fecha", "Prompt original (preview)"
        ])
        self.tabla.verticalHeader().setVisible(False)
        self.tabla.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.tabla.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tabla.setAlternatingRowColors(True)
        self.tabla.setStyleSheet(f"""
            QTableWidget {{
                background: {TABLE_BG};
                alternate-background-color: {TABLE_ALT_BG};
            }}
        """)

        header = self.tabla.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)

        layout.addWidget(self.tabla, stretch=1)

        # ── Log de operaciones ──
        log_lbl = QLabel("Estado")
        log_lbl.setFont(_mono_font(9))
        log_lbl.setStyleSheet(f"color: {TXT_MUTED};")
        layout.addWidget(log_lbl)

        self.log = QTextEdit()
        self.log.setReadOnly(True)
        self.log.setFont(_mono_font(9))
        self.log.setFixedHeight(70)
        layout.addWidget(self.log)

    # ────────────────────────────────────────────────────────────
    # Carga de datos
    # ────────────────────────────────────────────────────────────
    def _cargar_todo(self):
        """Recarga KPIs y tabla. Se llama al abrir y al pulsar Refrescar."""
        self._log("Cargando datos...")
        try:
            kpis = self._leer_kpis()
            self._actualizar_kpis(kpis)
            filas = self._leer_reescrituras()
            self._actualizar_tabla(filas)
            ahora = datetime.now().strftime("%H:%M:%S")
            self.lbl_ultima_carga.setText(f"actualizado {ahora}")
            self._log(
                f"OK · {kpis['feedback']} feedback, "
                f"{kpis['total_reescrituras']} reescrituras, "
                f"{kpis['usos_ab']} usos A/B"
            )
        except Exception as e:
            logger.exception("Error cargando panel admin")
            self._log(f"❌ Error: {e}")

    def _leer_kpis(self) -> Dict:
        """Consulta los KPIs principales."""
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")

            feedback = conn.execute(
                "SELECT COUNT(*) AS n FROM feedback_usuario"
            ).fetchone()["n"]

            por_estado = {}
            for r in conn.execute(
                "SELECT estado, COUNT(*) AS n FROM prompts_reescritos GROUP BY estado"
            ):
                por_estado[r["estado"]] = r["n"]

            usos = conn.execute(
                "SELECT COUNT(*) AS n FROM prompt_reescrito_usos"
            ).fetchone()["n"]

            usos_con_score = conn.execute(
                "SELECT COUNT(*) AS n FROM prompt_reescrito_usos WHERE score IS NOT NULL"
            ).fetchone()["n"]

            return {
                "feedback": feedback,
                "total_reescrituras": sum(por_estado.values()),
                "activos": por_estado.get("activo", 0),
                "candidatos": por_estado.get("candidato", 0),
                "descartados": por_estado.get("descartado", 0),
                "usos_ab": usos,
                "usos_con_score": usos_con_score,
            }

    def _leer_reescrituras(self) -> List[Dict]:
        """Lee todas las reescrituras con su score medio."""
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")

            cursor = conn.execute("""
                SELECT
                    pr.id,
                    pr.firma,
                    pr.estado,
                    pr.n_usos,
                    pr.fecha,
                    pr.prompt_original,
                    ROUND(AVG(pru.score), 3) AS score_medio,
                    COUNT(pru.score)         AS usos_con_score
                FROM prompts_reescritos pr
                LEFT JOIN prompt_reescrito_usos pru
                    ON pru.prompt_reescrito_id = pr.id AND pru.score IS NOT NULL
                GROUP BY pr.id
                ORDER BY
                    CASE pr.estado
                        WHEN 'activo'     THEN 0
                        WHEN 'candidato'  THEN 1
                        WHEN 'descartado' THEN 2
                        ELSE 3
                    END,
                    pr.id DESC
                LIMIT 100
            """)
            return [dict(r) for r in cursor.fetchall()]

    # ────────────────────────────────────────────────────────────
    # Render
    # ────────────────────────────────────────────────────────────
    def _actualizar_kpis(self, kpis: Dict):
        self.kpi_feedback.set_valor(kpis["feedback"])
        self.kpi_reescrituras.set_valor(kpis["total_reescrituras"])
        self.kpi_activos.set_valor(kpis["activos"])
        self.kpi_usos.set_valor(kpis["usos_ab"])
        # No tenemos un contador explícito de promociones/descartes
        # históricos. Los aproximamos con el estado actual.
        self.kpi_promociones.set_valor(kpis["activos"])
        self.kpi_descartes.set_valor(kpis["descartados"])

    def _actualizar_tabla(self, filas: List[Dict]):
        self.tabla.setRowCount(len(filas))
        for i, r in enumerate(filas):
            firma_corta = (r["firma"] or "")[:8]
            score = r["score_medio"]
            score_str = f"{score:.3f}" if score is not None else "—"
            fecha = (r["fecha"] or "")[:16].replace("T", " ")
            orig = (r["prompt_original"] or "").replace("\n", " ")[:80]

            color_estado = {
                "activo": GREEN,
                "candidato": CYAN_INFO,
                "descartado": TXT_MUTED,
            }.get(r["estado"], AMBER_WARN)

            items = [
                self._item(str(r["id"])),
                self._item(firma_corta),
                self._item(r["estado"], color=color_estado, bold=True),
                self._item(str(r["n_usos"])),
                self._item(score_str),
                self._item(fecha),
                self._item(orig),
            ]
            for j, item in enumerate(items):
                self.tabla.setItem(i, j, item)

    @staticmethod
    def _item(texto: str, color: Optional[str] = None, bold: bool = False) -> QTableWidgetItem:
        it = QTableWidgetItem(texto)
        if color:
            from PyQt6.QtGui import QColor
            it.setForeground(QColor(color))
        if bold:
            f = it.font()
            f.setBold(True)
            it.setFont(f)
        return it

    def _log(self, mensaje: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {mensaje}")
        cursor = self.log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        self.log.setTextCursor(cursor)