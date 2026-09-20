# ui/config_dialog.py
"""Diálogo ⚙ Configuración de la GUI (H1).

Permite configurar sin editar archivos: API key, modelo (Pro/Flash), base URL
y proxies. Persiste en ``~/.config/agentes_visuales/env`` (directorio 700,
archivo 600) a través de ``core.ia_config``.

Seguridad:

* La API key se introduce en un campo de contraseña y NUNCA se registra ni se
  imprime; solo se muestra enmascarada.
* Guardar aplica ``reset_llm_client_compartido()`` para que el cambio surta
  efecto sin reiniciar la aplicación.
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import QThread, pyqtSignal
from PyQt6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
)

from core.ia_config import (
    MODELO_FLASH,
    MODELO_PRO,
    borrar_api_key,
    guardar_configuracion,
    leer_configuracion,
    normalizar_modelo,
    probar_conexion,
)

logger = logging.getLogger(__name__)

GREEN = "#00ff88"
RED = "#ff5555"
AMBER = "#ffcc00"
MUTED = "#7a8f83"


class _TestConexion(QThread):
    """Prueba la conexión fuera del hilo de la GUI (no bloquea la ventana)."""

    resultado = pyqtSignal(bool, str)

    def __init__(self, api_key: str | None, base_url: str, parent=None):
        super().__init__(parent)
        self._api_key = api_key
        self._base_url = base_url

    def run(self):  # noqa: D102 (método de QThread)
        try:
            ok, mensaje = probar_conexion(self._api_key, self._base_url)
        except Exception as e:  # nunca debe tumbar la GUI
            ok, mensaje = False, f"Error inesperado: {e}"
        self.resultado.emit(ok, mensaje)


class ConfiguracionDialog(QDialog):
    """Formulario de configuración de la IA."""

    configuracion_guardada = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙ Configuración")
        self.setMinimumWidth(520)
        self._config = leer_configuracion()
        self._worker: _TestConexion | None = None
        self._borrar_clave = False
        self._construir_ui()

    # ------------------------------------------------------------
    # UI
    # ------------------------------------------------------------
    def _construir_ui(self):
        raiz = QVBoxLayout(self)

        # ── API key ──
        caja_key = QGroupBox("API key")
        form_key = QFormLayout(caja_key)
        self.campo_key = QLineEdit()
        self.campo_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.campo_key.setPlaceholderText(
            self._placeholder_key()
        )
        form_key.addRow("DEEPSEEK_API_KEY", self.campo_key)
        raiz.addWidget(caja_key)

        # ── Modelo ──
        caja_modelo = QGroupBox("Modelo")
        fila_modelo = QHBoxLayout(caja_modelo)
        self.radio_pro = QRadioButton("Pro (más capaz)")
        self.radio_flash = QRadioButton("Flash (más rápido)")
        self._grupo_modelo = QButtonGroup(self)
        self._grupo_modelo.addButton(self.radio_pro)
        self._grupo_modelo.addButton(self.radio_flash)
        if normalizar_modelo(self._config["modelo"]) == MODELO_FLASH:
            self.radio_flash.setChecked(True)
        else:
            self.radio_pro.setChecked(True)
        fila_modelo.addWidget(self.radio_pro)
        fila_modelo.addWidget(self.radio_flash)
        raiz.addWidget(caja_modelo)

        # ── Avanzado ──
        caja_avanzado = QGroupBox("Avanzado")
        form_av = QFormLayout(caja_avanzado)
        self.campo_base_url = QLineEdit(self._config["base_url"])
        self.campo_base_url.setPlaceholderText("https://api.deepseek.com")
        self.campo_http_proxy = QLineEdit(self._config["http_proxy"])
        self.campo_http_proxy.setPlaceholderText("http://proxy:8080")
        self.campo_https_proxy = QLineEdit(self._config["https_proxy"])
        self.campo_https_proxy.setPlaceholderText("http://proxy:8443")
        self.campo_no_proxy = QLineEdit(self._config["no_proxy"])
        self.campo_no_proxy.setPlaceholderText("localhost,127.0.0.1")
        form_av.addRow("Base URL", self.campo_base_url)
        form_av.addRow("HTTP proxy", self.campo_http_proxy)
        form_av.addRow("HTTPS proxy", self.campo_https_proxy)
        form_av.addRow("NO_PROXY", self.campo_no_proxy)
        raiz.addWidget(caja_avanzado)

        # ── Estado de la prueba de conexión ──
        self.lbl_resultado = QLabel("")
        self.lbl_resultado.setWordWrap(True)
        raiz.addWidget(self.lbl_resultado)

        # ── Botones ──
        fila = QHBoxLayout()
        self.btn_probar = QPushButton("Probar conexión")
        self.btn_probar.clicked.connect(self._on_probar)
        fila.addWidget(self.btn_probar)

        self.btn_borrar = QPushButton("Borrar clave")
        self.btn_borrar.clicked.connect(self._on_borrar)
        fila.addWidget(self.btn_borrar)

        fila.addStretch(1)

        self.btn_cancelar = QPushButton("Cancelar")
        self.btn_cancelar.clicked.connect(self.reject)
        fila.addWidget(self.btn_cancelar)

        self.btn_guardar = QPushButton("Guardar")
        self.btn_guardar.setDefault(True)
        self.btn_guardar.clicked.connect(self._on_guardar)
        fila.addWidget(self.btn_guardar)
        raiz.addLayout(fila)

    def _placeholder_key(self) -> str:
        if self._config["api_key_configurada"]:
            return f"configurada: {self._config['api_key_enmascarada']} (vacío = no cambiar)"
        return "sk-..."

    # ------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------
    def _modelo_seleccionado(self) -> str:
        return MODELO_FLASH if self.radio_flash.isChecked() else MODELO_PRO

    def _api_key_a_guardar(self) -> str | None:
        """None = conservar la actual; "" = borrar; texto = nueva."""
        texto = self.campo_key.text().strip()
        if texto:
            return texto
        if self._borrar_clave:
            return ""
        return None

    def _on_guardar(self):
        try:
            nueva = guardar_configuracion(
                api_key=self._api_key_a_guardar(),
                modelo=self._modelo_seleccionado(),
                base_url=self.campo_base_url.text().strip(),
                http_proxy=self.campo_http_proxy.text().strip(),
                https_proxy=self.campo_https_proxy.text().strip(),
                no_proxy=self.campo_no_proxy.text().strip(),
            )
        except Exception as e:
            logger.exception("No se pudo guardar la configuración de IA")
            QMessageBox.critical(self, "Configuración", f"No se pudo guardar:\n{e}")
            return

        # El cliente compartido debe recrearse con la configuración nueva.
        try:
            from core.llm_client import reset_llm_client_compartido

            reset_llm_client_compartido()
        except Exception as e:
            logger.warning(f"No se pudo reiniciar el cliente LLM: {e}")

        self._config = nueva
        self.campo_key.clear()
        self._borrar_clave = False
        self.campo_key.setPlaceholderText(self._placeholder_key())
        self.configuracion_guardada.emit(nueva)
        self.accept()

    def _on_borrar(self):
        respuesta = QMessageBox.question(
            self,
            "Borrar clave",
            "¿Borrar la API key guardada? Las demás opciones se conservan.",
        )
        if respuesta != QMessageBox.StandardButton.Yes:
            return
        try:
            nueva = borrar_api_key()
        except Exception as e:
            QMessageBox.critical(self, "Configuración", f"No se pudo borrar:\n{e}")
            return

        try:
            from core.llm_client import reset_llm_client_compartido

            reset_llm_client_compartido()
        except Exception:
            pass

        self._config = nueva
        self._borrar_clave = True
        self.campo_key.clear()
        self.campo_key.setPlaceholderText(self._placeholder_key())
        self._mostrar_resultado(False, "API key borrada.")
        self.configuracion_guardada.emit(nueva)

    def _on_probar(self):
        self.btn_probar.setEnabled(False)
        self._mostrar_resultado(None, "Probando conexión…")

        clave = self.campo_key.text().strip() or None
        self._worker = _TestConexion(
            clave, self.campo_base_url.text().strip() or "", self
        )
        self._worker.resultado.connect(self._on_resultado_prueba)
        self._worker.finished.connect(lambda: self.btn_probar.setEnabled(True))
        self._worker.start()

    def _on_resultado_prueba(self, ok: bool, mensaje: str):
        self._mostrar_resultado(ok, mensaje)

    def _mostrar_resultado(self, ok: bool | None, mensaje: str):
        if ok is None:
            color = AMBER
        else:
            color = GREEN if ok else RED
        self.lbl_resultado.setText(mensaje)
        self.lbl_resultado.setStyleSheet(f"color: {color};")
