# export/base.py
"""
Fachada principal del exportador.

`ResultExporter` actúa como orquestador: valida, delega en el exportador
concreto de cada formato, comprime si procede y devuelve un `ExportResult`.
"""

import os
import time
import logging
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Iterator

from .models import ExportConfig, ExportResult

# Exportadores concretos
from .csv_exporter import exportar_csv, exportar_csv_streaming
from .json_exporter import exportar_json
from .html_exporter import exportar_html
from .excel_exporter import exportar_excel
from .markdown_exporter import exportar_markdown
from .pdf_exporter import exportar_pdf
from .txt_exporter import exportar_txt

logger = logging.getLogger(__name__)


class ResultExporter:
    """
    Exporta resultados en diferentes formatos con soporte para datos anidados.
    """

    def __init__(self, config: Optional[ExportConfig] = None):
        """
        Inicializa el exportador.

        Args:
            config: Configuración de exportación (opcional)
        """
        self.config = config or ExportConfig()
        self.logger = logging.getLogger(f"{__name__}.ResultExporter")

        # Registro de formatos soportados
        self.formatos_soportados = {
            "csv": exportar_csv,
            "json": exportar_json,
            "html": exportar_html,
            "excel": exportar_excel,
            "markdown": exportar_markdown,
            "pdf": exportar_pdf,
            "txt": exportar_txt,
        }

    # ============================================================
    # MÉTODO PRINCIPAL
    # ============================================================

    def exportar(
        self,
        agentes: List[Dict],
        ruta: Optional[str] = None,
        formato: Optional[str] = None,
        **kwargs
    ) -> ExportResult:
        """
        Exporta agentes al formato especificado.

        Args:
            agentes: Lista de agentes a exportar
            ruta: Ruta de destino (opcional)
            formato: Formato de exportación (csv, json, html, excel, markdown, pdf, txt)
            **kwargs: Configuración adicional

        Returns:
            ExportResult: Resultado de la exportación
        """
        start_time = time.time()

        # Validar entrada
        if not agentes:
            return ExportResult(
                exito=False,
                ruta="",
                formato=formato or "json",
                filas_exportadas=0,
                tamaño_bytes=0,
                tiempo_ejecucion=0,
                errores=["No hay datos para exportar"]
            )

        formato = formato or self.config.formato
        formato = formato.lower()

        # Validar formato
        if formato not in self.formatos_soportados:
            return ExportResult(
                exito=False,
                ruta="",
                formato=formato,
                filas_exportadas=0,
                tamaño_bytes=0,
                tiempo_ejecucion=0,
                errores=[f"Formato no soportado: {formato}"]
            )

        # Actualizar configuración
        config = self._actualizar_config(kwargs)

        # Generar ruta si no se proporcionó
        if not ruta:
            ruta = self._generar_ruta(formato, config)

        try:
            # Crear directorio si no existe
            os.makedirs(os.path.dirname(os.path.abspath(ruta)) or ".", exist_ok=True)

            # Exportar según formato
            filas, errores, advertencias = self.formatos_soportados[formato](
                agentes, ruta, config
            )

            # Calcular tamaño
            tamaño = os.path.getsize(ruta) if os.path.exists(ruta) else 0

            # Comprimir si está configurado
            if config.comprimir and formato not in ("zip", "gz"):
                ruta = self._comprimir_archivo(ruta)
                tamaño = os.path.getsize(ruta) if os.path.exists(ruta) else 0

            elapsed = time.time() - start_time

            self.logger.info(f"Exportación completada: {filas} filas en {elapsed:.2f}s")

            return ExportResult(
                exito=True,
                ruta=ruta,
                formato=formato,
                filas_exportadas=filas,
                tamaño_bytes=tamaño,
                tiempo_ejecucion=elapsed,
                errores=errores,
                advertencias=advertencias
            )

        except Exception as e:
            self.logger.exception(f"Error en exportación: {e}")
            return ExportResult(
                exito=False,
                ruta=ruta,
                formato=formato,
                filas_exportadas=0,
                tamaño_bytes=0,
                tiempo_ejecucion=time.time() - start_time,
                errores=[str(e)]
            )

    # ============================================================
    # CONFIGURACIÓN
    # ============================================================

    def _actualizar_config(self, kwargs: Dict) -> ExportConfig:
        """Actualiza la configuración con los kwargs proporcionados."""
        return ExportConfig(
            formato=kwargs.get('formato', self.config.formato),
            encoding=kwargs.get('encoding', self.config.encoding),
            comprimir=kwargs.get('comprimir', self.config.comprimir),
            incluir_graficos=kwargs.get('incluir_graficos', self.config.incluir_graficos),
            incluir_timestamp=kwargs.get('incluir_timestamp', self.config.incluir_timestamp),
            limit_rows=kwargs.get('limit_rows', self.config.limit_rows),
            flatten=kwargs.get('flatten', self.config.flatten),
            separator=kwargs.get('separator', self.config.separator),
            estilo=kwargs.get('estilo', self.config.estilo),
            plantilla=kwargs.get('plantilla', self.config.plantilla),
            chunk_size=kwargs.get('chunk_size', self.config.chunk_size),
        )

    def _generar_ruta(self, formato: str, config: ExportConfig) -> str:
        """Genera una ruta de archivo automática."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        nombre = f"export_{timestamp}"

        extensiones = {
            "csv": "csv",
            "json": "json",
            "html": "html",
            "excel": "xlsx",
            "markdown": "md",
            "pdf": "pdf",
            "txt": "txt",
        }

        extension = extensiones.get(formato, "json")
        nombre_archivo = f"{nombre}.{extension}"

        if config.comprimir:
            nombre_archivo = f"{nombre_archivo}.zip"

        return nombre_archivo

    # ============================================================
    # COMPRESIÓN
    # ============================================================

    def _comprimir_archivo(self, ruta: str) -> str:
        """
        Comprime un archivo en formato ZIP.

        Args:
            ruta: Ruta del archivo a comprimir

        Returns:
            str: Ruta del archivo comprimido
        """
        import zipfile
        try:
            ruta_zip = f"{ruta}.zip"

            with zipfile.ZipFile(ruta_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
                zf.write(ruta, os.path.basename(ruta))

            # Eliminar archivo original
            os.remove(ruta)

            self.logger.info(f"Archivo comprimido: {ruta_zip}")
            return ruta_zip

        except Exception as e:
            self.logger.warning(f"Error comprimiendo archivo: {e}")
            return ruta

    # ============================================================
    # EXPORTACIÓN POR CHUNKS (para grandes volúmenes)
    # ============================================================

    def exportar_streaming(
        self,
        generador: Iterator[Dict],
        ruta: str,
        formato: str = "csv",
        **kwargs
    ) -> ExportResult:
        """
        Exporta datos desde un generador (para grandes volúmenes).

        Args:
            generador: Generador de filas de datos
            ruta: Ruta de destino
            formato: Formato de exportación
            **kwargs: Configuración adicional

        Returns:
            ExportResult: Resultado de la exportación
        """
        start_time = time.time()
        errores = []
        advertencias = []
        filas_exportadas = 0

        try:
            config = self._actualizar_config(kwargs)

            if formato == "csv":
                filas_exportadas, errores, advertencias = exportar_csv_streaming(
                    generador, ruta, config
                )
            else:
                errores.append(f"Streaming no soportado para formato: {formato}")
                return ExportResult(
                    exito=False,
                    ruta=ruta,
                    formato=formato,
                    filas_exportadas=0,
                    tamaño_bytes=0,
                    tiempo_ejecucion=time.time() - start_time,
                    errores=errores
                )

            tamaño = os.path.getsize(ruta) if os.path.exists(ruta) else 0

            exito = len(errores) == 0 and filas_exportadas > 0

            return ExportResult(
                exito=exito,
                ruta=ruta,
                formato=formato,
                filas_exportadas=filas_exportadas,
                tamaño_bytes=tamaño,
                tiempo_ejecucion=time.time() - start_time,
                errores=errores,
                advertencias=advertencias
            )

        except Exception as e:
            errores.append(str(e))
            return ExportResult(
                exito=False,
                ruta=ruta,
                formato=formato,
                filas_exportadas=filas_exportadas,
                tamaño_bytes=0,
                tiempo_ejecucion=time.time() - start_time,
                errores=errores
            )

    # ============================================================
    # UTILIDADES
    # ============================================================

    @staticmethod
    def obtener_formatos_soportados() -> List[str]:
        """
        Obtiene la lista de formatos soportados.

        Returns:
            List[str]: Lista de formatos
        """
        return ["csv", "json", "html", "excel", "markdown", "pdf", "txt"]

    @staticmethod
    def obtener_extension(formato: str) -> str:
        """
        Obtiene la extensión de archivo para un formato.

        Args:
            formato: Nombre del formato

        Returns:
            str: Extensión del archivo
        """
        extensiones = {
            "csv": "csv",
            "json": "json",
            "html": "html",
            "excel": "xlsx",
            "markdown": "md",
            "pdf": "pdf",
            "txt": "txt",
        }
        return extensiones.get(formato.lower(), "json")