# export/exporters.py - VERSIÓN REFACTORIZADA Y COMPLETA
"""
Exportadores de resultados en múltiples formatos.

CARACTERÍSTICAS:
- Múltiples formatos: CSV, JSON, HTML, Excel, Markdown, PDF
- Aplanamiento automático de estructuras anidadas
- Compresión opcional (ZIP)
- Exportación con gráficos embebidos
- Estilos CSS para HTML
- Soporte para grandes volúmenes de datos (streaming)
- Validación de datos de entrada
- Logging estructurado
- Configuración de exportación
- Plantillas personalizables
"""

import csv
import json
import os
import time
import logging
import zipfile
import io
import base64
from typing import List, Dict, Any, Optional, Union, Iterator, Tuple
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass, field
from collections import defaultdict
import re

from .utils import aplanar_diccionario, aplanar_lista

# Configurar logger
logger = logging.getLogger(__name__)

# ============================================================
# CONSTANTES
# ============================================================

MAX_CSV_ROWS = 1000000
MAX_HTML_ROWS = 10000
MAX_EXCEL_ROWS = 1048576  # Límite de Excel
MAX_JSON_SIZE = 100 * 1024 * 1024  # 100 MB
CHUNK_SIZE = 10000
STREAM_SNIFF_ROWS = 1000  # filas iniciales para calcular la cabecera CSV
INDENT = 2

# Colores para HTML
COLORES_ESTADO = {
    "Completado": "#28a745",
    "Error": "#dc3545",
    "Cancelado": "#6c757d",
    "Pendiente": "#6c757d",
    "Esperando dependencias": "#fd7e14",
    "Listo para ejecutar": "#28a745",
    "Ejecutando": "#007bff",
}


# ============================================================
# MODELOS DE DATOS
# ============================================================

@dataclass
class ExportConfig:
    """Configuración de exportación."""
    formato: str = "json"
    encoding: str = "utf-8"  # Cambiar a utf-8 como default
    comprimir: bool = False
    incluir_graficos: bool = True
    incluir_timestamp: bool = True
    limit_rows: Optional[int] = None
    flatten: bool = True
    separator: str = "."
    estilo: str = "moderno"
    plantilla: Optional[str] = None
    chunk_size: int = CHUNK_SIZE


@dataclass
class ExportResult:
    """Resultado de una exportación."""
    exito: bool
    ruta: str
    formato: str
    filas_exportadas: int
    tamaño_bytes: int
    tiempo_ejecucion: float
    errores: List[str] = field(default_factory=list)
    advertencias: List[str] = field(default_factory=list)


# ============================================================
# CLASE PRINCIPAL: RESULT EXPORTER
# ============================================================

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
            "csv": self._exportar_csv,
            "json": self._exportar_json,
            "html": self._exportar_html,
            "excel": self._exportar_excel,
            "markdown": self._exportar_markdown,
            "pdf": self._exportar_pdf,
            "txt": self._exportar_txt,
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
            filas, errores, advertencias = self.formatos_soportados[formato](agentes, ruta, config)
            
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
        config = ExportConfig(
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
        return config
    
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
    # APLANAMIENTO DE DATOS
    # ============================================================
    
    def _aplanar_diccionario(
        self,
        d: Dict,
        parent_key: str = "",
        separator: str = ".",
        max_depth: int = 10
    ) -> Dict:
        """Delegación en :func:`export.utils.aplanar_diccionario`."""
        return aplanar_diccionario(d, parent_key, separator, max_depth)

    def _aplanar_lista(
        self,
        data: List[Dict],
        flatten: bool = True,
        separator: str = "."
    ) -> List[Dict]:
        """Delegación en :func:`export.utils.aplanar_lista`."""
        return aplanar_lista(data, flatten, separator)

    # ============================================================
    # EXPORTADOR CSV
    # ============================================================
    
    def _exportar_csv(
        self,
        agentes: List[Dict],
        ruta: str,
        config: ExportConfig
    ) -> Tuple[int, List[str], List[str]]:
        """
        Exporta a CSV con aplanamiento automático.
        """
        errores = []
        advertencias = []
        
        try:
            # Aplanar datos
            data = self._aplanar_lista(agentes, config.flatten, config.separator)
            
            if not data:
                errores.append("No hay datos para exportar")
                return 0, errores, advertencias
            
            # Aplicar límite de filas
            if config.limit_rows:
                data = data[:config.limit_rows]
                if len(data) < len(agentes):
                    advertencias.append(f"Limitado a {config.limit_rows} filas")
            
            # Obtener todas las claves
            fieldnames = set()
            for row in data:
                fieldnames.update(row.keys())
            fieldnames = sorted(fieldnames)
            
            # Escribir CSV
            with open(ruta, 'w', newline='', encoding=config.encoding) as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, restval='')
                writer.writeheader()
                
                for row in data:
                    # Limpiar fila
                    clean_row = {k: v if v is not None else '' for k, v in row.items()}
                    writer.writerow(clean_row)
            
            return len(data), errores, advertencias
            
        except Exception as e:
            errores.append(f"Error exportando CSV: {str(e)}")
            return 0, errores, advertencias
    
    # ============================================================
    # EXPORTADOR JSON
    # ============================================================
    
    def _exportar_json(
        self,
        agentes: List[Dict],
        ruta: str,
        config: ExportConfig
    ) -> Tuple[int, List[str], List[str]]:
        errores = []
        advertencias = []

        try:
            # Aplicar límite
            data = agentes
            if config.limit_rows:
                data = data[:config.limit_rows]
                if len(data) < len(agentes):
                    advertencias.append(f"Limitado a {config.limit_rows} filas")

            export_data = {
                "metadata": {
                    "exportado": datetime.now().isoformat(),
                    "total_agentes": len(agentes),
                    "exportados": len(data),
                    "formato": "json",
                    "version": "2.0"
                },
                "agentes": data
            }

            # ✅ CORREGIDO: Usar utf-8 para JSON (sin BOM)
            with open(ruta, 'w', encoding='utf-8') as f:
                json.dump(
                    export_data,
                    f,
                    indent=INDENT,
                    ensure_ascii=False,
                    default=str
                )

            return len(data), errores, advertencias

        except Exception as e:
            errores.append(f"Error exportando JSON: {str(e)}")
            return 0, errores, advertencias 
    
    # ============================================================
    # EXPORTADOR HTML
    # ============================================================
    
    def _exportar_html(
        self,
        agentes: List[Dict],
        ruta: str,
        config: ExportConfig
    ) -> Tuple[int, List[str], List[str]]:
        """
        Exporta a HTML con estilos y gráficos.
        """
        errores = []
        advertencias = []
        
        try:
            # Aplicar límite
            data = agentes
            if config.limit_rows:
                data = data[:min(config.limit_rows, MAX_HTML_ROWS)]
                if len(data) < len(agentes):
                    advertencias.append(f"Limitado a {MAX_HTML_ROWS} filas para HTML")
            
            # Estadísticas
            total = len(agentes)
            completados = sum(1 for a in agentes if a.get('estado') == "Completado")
            errores_count = sum(1 for a in agentes if a.get('estado') == "Error")
            
            # Construir HTML
            html = self._generar_html(data, total, completados, errores_count, config)
            
            with open(ruta, 'w', encoding=config.encoding) as f:
                f.write(html)
            
            return len(data), errores, advertencias
            
        except Exception as e:
            errores.append(f"Error exportando HTML: {str(e)}")
            return 0, errores, advertencias
    
    def _generar_html(
        self,
        data: List[Dict],
        total: int,
        completados: int,
        errores: int,
        config: ExportConfig
    ) -> str:
        """Genera el contenido HTML."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # Estilos según configuración
        estilos = self._obtener_estilos_html(config.estilo)
        
        # Tabla de datos
        if data:
            headers = sorted(set().union(*[d.keys() for d in data if isinstance(d, dict)]))
            headers = [h for h in headers if h not in ['resultado', 'salida', 'error']]
            
            rows_html = ""
            for row in data:
                estado = row.get('estado', 'Desconocido')
                color = COLORES_ESTADO.get(estado, '#6c757d')
                
                rows_html += f'<tr style="border-bottom: 1px solid #dee2e6;">'
                for header in headers:
                    value = row.get(header, '')
                    if header == 'estado':
                        value = f'<span style="background-color: {color}; color: white; padding: 2px 8px; border-radius: 4px; font-size: 12px;">{value}</span>'
                    elif header == 'duracion':
                        value = f"{value:.2f}s" if isinstance(value, (int, float)) else value
                    rows_html += f'<td style="padding: 8px;">{value}</td>'
                rows_html += '</tr>'
        else:
            rows_html = '<tr><td colspan="100%" style="text-align: center; padding: 40px; color: #999;">No hay datos para mostrar</td></tr>'
        
        # Gráfico de resumen (simple)
        grafico_html = self._generar_grafico_html(completados, errores, total) if config.incluir_graficos else ""
        
        html = f"""
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reporte de Agentes - {timestamp}</title>
    <style>
        {estilos}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 Reporte de Ejecución de Agentes</h1>
            <div class="timestamp">Generado: {timestamp}</div>
        </div>
        
        <div class="stats">
            <div class="stat-box">
                <div class="stat-number">{total}</div>
                <div class="stat-label">Total Agentes</div>
            </div>
            <div class="stat-box" style="border-color: #28a745;">
                <div class="stat-number" style="color: #28a745;">{completados}</div>
                <div class="stat-label">Completados</div>
            </div>
            <div class="stat-box" style="border-color: #dc3545;">
                <div class="stat-number" style="color: #dc3545;">{errores}</div>
                <div class="stat-label">Errores</div>
            </div>
            <div class="stat-box" style="border-color: #6c757d;">
                <div class="stat-number" style="color: #6c757d;">{total - completados - errores}</div>
                <div class="stat-label">Pendientes/Cancelados</div>
            </div>
        </div>
        
        {grafico_html}
        
        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        {"".join(f'<th>{h.replace("_", " ").title()}</th>' for h in headers)}
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>
        
        <div class="footer">
            <p>Reporte generado por Agentes Visuales</p>
        </div>
    </div>
</body>
</html>
"""
        return html
    
    def _obtener_estilos_html(self, estilo: str) -> str:
        """Obtiene los estilos CSS para HTML."""
        estilos_base = """
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f5f7fa; color: #333; padding: 20px; }
            .container { max-width: 1200px; margin: 0 auto; background: white; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); padding: 30px; }
            .header { border-bottom: 2px solid #007bff; padding-bottom: 15px; margin-bottom: 25px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
            .header h1 { font-size: 28px; color: #2c3e50; }
            .timestamp { color: #6c757d; font-size: 14px; }
            .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; margin-bottom: 25px; }
            .stat-box { background: #f8f9fa; border-radius: 8px; padding: 15px; text-align: center; border-left: 4px solid #007bff; }
            .stat-number { font-size: 32px; font-weight: bold; color: #2c3e50; }
            .stat-label { font-size: 14px; color: #6c757d; margin-top: 4px; }
            .table-container { overflow-x: auto; margin-top: 20px; }
            table { width: 100%; border-collapse: collapse; font-size: 14px; }
            th { background: #f8f9fa; padding: 12px; text-align: left; font-weight: 600; color: #495057; border-bottom: 2px solid #dee2e6; position: sticky; top: 0; }
            td { padding: 10px 12px; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
            tr:hover { background-color: #f8f9fa; }
            .footer { margin-top: 30px; padding-top: 15px; border-top: 1px solid #dee2e6; text-align: center; color: #6c757d; font-size: 12px; }
            .chart-container { background: #f8f9fa; border-radius: 8px; padding: 20px; margin-bottom: 20px; text-align: center; }
            .chart-bar { display: flex; justify-content: center; align-items: flex-end; gap: 30px; height: 100px; }
            .chart-item { display: flex; flex-direction: column; align-items: center; }
            .chart-bar-item { width: 60px; border-radius: 4px 4px 0 0; transition: height 0.5s; min-height: 10px; }
            .chart-label { margin-top: 8px; font-size: 12px; color: #6c757d; }
            .chart-value { font-weight: bold; font-size: 14px; }
            @media (max-width: 600px) { .container { padding: 15px; } .stats { grid-template-columns: 1fr 1fr; } }
        """
        
        estilos_moderno = estilos_base
        
        estilos_clasico = """
            body { font-family: Arial, sans-serif; background: white; }
            .container { max-width: 100%; padding: 10px; box-shadow: none; }
            .stats { display: flex; gap: 10px; flex-wrap: wrap; }
            .stat-box { min-width: 100px; }
        """
        
        return estilos_moderno if estilo == "moderno" else estilos_clasico
    
    def _generar_grafico_html(self, completados: int, errores: int, total: int) -> str:
        """Genera un gráfico de barras simple en HTML."""
        pendientes = total - completados - errores
        
        max_val = max(completados, errores, pendientes, 1)
        max_height = 80
        
        completados_height = (completados / max_val) * max_height if max_val > 0 else 10
        errores_height = (errores / max_val) * max_height if max_val > 0 else 10
        pendientes_height = (pendientes / max_val) * max_height if max_val > 0 else 10
        
        return f"""
        <div class="chart-container">
            <h3 style="margin-bottom: 15px; color: #2c3e50;">📊 Resumen de Estados</h3>
            <div class="chart-bar">
                <div class="chart-item">
                    <div class="chart-bar-item" style="height: {completados_height}px; background: #28a745;"></div>
                    <div class="chart-label">✅ Completados</div>
                    <div class="chart-value" style="color: #28a745;">{completados}</div>
                </div>
                <div class="chart-item">
                    <div class="chart-bar-item" style="height: {errores_height}px; background: #dc3545;"></div>
                    <div class="chart-label">❌ Errores</div>
                    <div class="chart-value" style="color: #dc3545;">{errores}</div>
                </div>
                <div class="chart-item">
                    <div class="chart-bar-item" style="height: {pendientes_height}px; background: #6c757d;"></div>
                    <div class="chart-label">⏳ Pendientes</div>
                    <div class="chart-value" style="color: #6c757d;">{pendientes}</div>
                </div>
            </div>
        </div>
        """
    
    # ============================================================
    # EXPORTADOR EXCEL
    # ============================================================
    
    def _exportar_excel(
        self,
        agentes: List[Dict],
        ruta: str,
        config: ExportConfig
    ) -> Tuple[int, List[str], List[str]]:
        """
        Exporta a Excel con múltiples hojas.
        """
        errores = []
        advertencias = []
        
        try:
            import pandas as pd
        except ImportError:
            errores.append("pandas no instalado. Ejecuta: pip install pandas openpyxl")
            return 0, errores, advertencias
        
        try:
            # Aplanar datos
            data = self._aplanar_lista(agentes, config.flatten, config.separator)
            
            if not data:
                errores.append("No hay datos para exportar")
                return 0, errores, advertencias
            
            # Aplicar límite
            if config.limit_rows:
                data = data[:min(config.limit_rows, MAX_EXCEL_ROWS)]
                if len(data) < len(agentes):
                    advertencias.append(f"Limitado a {MAX_EXCEL_ROWS} filas para Excel")
            
            # Crear DataFrame
            df = pd.DataFrame(data)
            
            # Crear archivo Excel con múltiples hojas
            with pd.ExcelWriter(ruta, engine='openpyxl') as writer:
                # Hoja principal
                df.to_excel(writer, sheet_name='Agentes', index=False)
                
                # Hoja de resumen
                resumen = pd.DataFrame({
                    'Métrica': ['Total Agentes', 'Completados', 'Errores', 'Pendientes'],
                    'Valor': [
                        len(agentes),
                        sum(1 for a in agentes if a.get('estado') == 'Completado'),
                        sum(1 for a in agentes if a.get('estado') == 'Error'),
                        sum(1 for a in agentes if a.get('estado') not in ['Completado', 'Error'])
                    ]
                })
                resumen.to_excel(writer, sheet_name='Resumen', index=False)
                
                # Hoja de estadísticas por tipo
                tipos = defaultdict(int)
                for a in agentes:
                    tipos[a.get('tipo', 'Desconocido')] += 1
                tipos_df = pd.DataFrame({
                    'Tipo': list(tipos.keys()),
                    'Cantidad': list(tipos.values())
                })
                tipos_df.to_excel(writer, sheet_name='Tipos', index=False)
            
            return len(data), errores, advertencias
            
        except Exception as e:
            errores.append(f"Error exportando Excel: {str(e)}")
            return 0, errores, advertencias
    
    # ============================================================
    # EXPORTADOR MARKDOWN
    # ============================================================
    
    def _exportar_markdown(
        self,
        agentes: List[Dict],
        ruta: str,
        config: ExportConfig
    ) -> Tuple[int, List[str], List[str]]:
        """
        Exporta a Markdown.
        """
        errores = []
        advertencias = []
        
        try:
            # Aplanar datos
            data = self._aplanar_lista(agentes, config.flatten, config.separator)
            
            if not data:
                errores.append("No hay datos para exportar")
                return 0, errores, advertencias
            
            # Aplicar límite
            if config.limit_rows:
                data = data[:config.limit_rows]
                if len(data) < len(agentes):
                    advertencias.append(f"Limitado a {config.limit_rows} filas")
            
            # Obtener headers
            headers = sorted(set().union(*[d.keys() for d in data if isinstance(d, dict)]))
            
            # Generar Markdown
            lines = []
            lines.append("# 📊 Reporte de Agentes")
            lines.append("")
            lines.append(f"*Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*")
            lines.append("")
            lines.append(f"**Total Agentes:** {len(agentes)}")
            lines.append("")
            
            # Tabla
            lines.append("| " + " | ".join(h.replace('_', ' ').title() for h in headers) + " |")
            lines.append("|" + "|".join(" --- " for _ in headers) + "|")
            
            for row in data:
                row_data = []
                for h in headers:
                    value = row.get(h, '')
                    if isinstance(value, (int, float)):
                        value = f"{value:.2f}" if isinstance(value, float) else str(value)
                    row_data.append(str(value)[:50] if value else '')
                lines.append("| " + " | ".join(row_data) + " |")
            
            # Pie
            lines.append("")
            lines.append("---")
            lines.append("*Reporte generado por Agentes Visuales*")
            
            # Escribir
            with open(ruta, 'w', encoding=config.encoding) as f:
                f.write('\n'.join(lines))
            
            return len(data), errores, advertencias
            
        except Exception as e:
            errores.append(f"Error exportando Markdown: {str(e)}")
            return 0, errores, advertencias
    
    # ============================================================
    # EXPORTADOR PDF (requiere reportlab)
    # ============================================================
    
    def _exportar_pdf(
        self,
        agentes: List[Dict],
        ruta: str,
        config: ExportConfig
    ) -> Tuple[int, List[str], List[str]]:
        """
        Exporta a PDF.
        """
        errores = []
        advertencias = []
        
        try:
            from reportlab.lib.pagesizes import letter, landscape
            from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
            from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
            from reportlab.lib import colors
            from reportlab.lib.units import inch
        except ImportError:
            errores.append("reportlab no instalado. Ejecuta: pip install reportlab")
            return 0, errores, advertencias
        
        try:
            # Aplanar datos
            data = self._aplanar_lista(agentes, config.flatten, config.separator)
            
            if not data:
                errores.append("No hay datos para exportar")
                return 0, errores, advertencias
            
            # Aplicar límite
            if config.limit_rows:
                data = data[:min(config.limit_rows, 5000)]
                if len(data) < len(agentes):
                    advertencias.append(f"Limitado a 5000 filas para PDF")
            
            # Crear documento
            doc = SimpleDocTemplate(ruta, pagesize=letter)
            styles = getSampleStyleSheet()
            
            # Estilo personalizado
            title_style = ParagraphStyle(
                'CustomTitle',
                parent=styles['Heading1'],
                fontSize=24,
                spaceAfter=30
            )
            
            # Elementos
            story = []
            
            # Título
            story.append(Paragraph("📊 Reporte de Agentes", title_style))
            
            # Fecha
            story.append(Paragraph(f"<i>Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>", styles['Normal']))
            story.append(Spacer(1, 20))
            
            # Resumen
            completados = sum(1 for a in agentes if a.get('estado') == 'Completado')
            errores_count = sum(1 for a in agentes if a.get('estado') == 'Error')
            
            resumen_text = f"<b>Total Agentes:</b> {len(agentes)} | "
            resumen_text += f"<b>Completados:</b> {completados} | "
            resumen_text += f"<b>Errores:</b> {errores_count}"
            story.append(Paragraph(resumen_text, styles['Normal']))
            story.append(Spacer(1, 20))
            
            # Tabla
            headers = sorted(set().union(*[d.keys() for d in data if isinstance(d, dict)]))
            headers = [h.replace('_', ' ').title() for h in headers]
            
            # Datos de tabla
            table_data = [headers]
            for row in data:
                row_data = []
                for h in headers:
                    key = h.lower().replace(' ', '_')
                    value = row.get(key, '')
                    if isinstance(value, (int, float)):
                        value = f"{value:.2f}" if isinstance(value, float) else str(value)
                    row_data.append(str(value)[:50] if value else '')
                table_data.append(row_data)
            
            # Crear tabla
            table = Table(table_data, repeatRows=1)
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('FONTSIZE', (0, 0), (-1, 0), 10),
                ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
                ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
                ('GRID', (0, 0), (-1, -1), 1, colors.black),
                ('FONTSIZE', (0, 1), (-1, -1), 8),
            ]))
            
            story.append(table)
            
            # Construir PDF
            doc.build(story)
            
            return len(data), errores, advertencias
            
        except Exception as e:
            errores.append(f"Error exportando PDF: {str(e)}")
            return 0, errores, advertencias
    
    # ============================================================
    # EXPORTADOR TXT
    # ============================================================
    
    def _exportar_txt(
        self,
        agentes: List[Dict],
        ruta: str,
        config: ExportConfig
    ) -> Tuple[int, List[str], List[str]]:
        """
        Exporta a TXT con formato legible.
        """
        errores = []
        advertencias = []
        
        try:
            # Aplanar datos
            data = self._aplanar_lista(agentes, config.flatten, config.separator)
            
            if not data:
                errores.append("No hay datos para exportar")
                return 0, errores, advertencias
            
            # Aplicar límite
            if config.limit_rows:
                data = data[:config.limit_rows]
                if len(data) < len(agentes):
                    advertencias.append(f"Limitado a {config.limit_rows} filas")
            
            lines = []
            lines.append("=" * 70)
            lines.append("📊 REPORTE DE AGENTES")
            lines.append("=" * 70)
            lines.append(f"Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"Total Agentes: {len(agentes)}")
            lines.append("=" * 70)
            lines.append("")
            
            for i, row in enumerate(data, 1):
                lines.append(f"📌 Agente {i}")
                lines.append("-" * 40)
                for key, value in sorted(row.items()):
                    if value is not None and value != '':
                        lines.append(f"  {key.replace('_', ' ').title()}: {value}")
                lines.append("")
            
            lines.append("=" * 70)
            lines.append("*Reporte generado por Agentes Visuales*")
            
            with open(ruta, 'w', encoding=config.encoding) as f:
                f.write('\n'.join(lines))
            
            return len(data), errores, advertencias
            
        except Exception as e:
            errores.append(f"Error exportando TXT: {str(e)}")
            return 0, errores, advertencias
    
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
                filas_exportadas, errores, advertencias = self._exportar_csv_streaming(
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
            
            return ExportResult(
                exito=not errores and filas_exportadas > 0,
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
    
    def _exportar_csv_streaming(
        self,
        generador: Iterator[Dict],
        ruta: str,
        config: ExportConfig
    ) -> Tuple[int, List[str], List[str]]:
        """
        Exporta CSV desde un generador.

        Para conocer todas las columnas sin materializar el generador
        completo se inspecciona una ventana inicial de filas. Si más
        adelante aparece una clave nueva, se omite y se registra una
        advertencia (comportamiento ``extrasaction="ignore"``).
        """
        errores = []
        advertencias = []
        filas = 0

        try:
            # Ventana inicial para calcular la cabecera completa
            ventana = []
            for _ in range(STREAM_SNIFF_ROWS):
                fila = next(generador, None)
                if fila is None:
                    break
                ventana.append(fila)

            if not ventana:
                errores.append("No hay datos para exportar")
                return 0, errores, advertencias

            def preparar(fila: Dict) -> Dict:
                if config.flatten:
                    fila = self._aplanar_diccionario(fila, separator=config.separator)
                return {k: (v if v is not None else '') for k, v in fila.items()}

            filas_preparadas = [preparar(f) for f in ventana]
            fieldnames = sorted({k for fila in filas_preparadas for k in fila})

            with open(ruta, 'w', newline='', encoding=config.encoding) as f:
                writer = csv.DictWriter(
                    f, fieldnames=fieldnames, restval='', extrasaction='ignore'
                )
                writer.writeheader()

                for clean_row in filas_preparadas:
                    writer.writerow(clean_row)
                    filas += 1
                    if config.limit_rows and filas >= config.limit_rows:
                        advertencias.append(f"Limitado a {config.limit_rows} filas")
                        return filas, errores, advertencias

                claves_conocidas = set(fieldnames)
                avisado = False
                for row in generador:
                    clean_row = preparar(row)
                    nuevas = set(clean_row) - claves_conocidas
                    if nuevas and not avisado:
                        advertencias.append(
                            "Columnas nuevas ignoradas tras la ventana inicial: "
                            + ", ".join(sorted(nuevas))
                        )
                        avisado = True
                    writer.writerow(clean_row)
                    filas += 1
                    if config.limit_rows and filas >= config.limit_rows:
                        advertencias.append(f"Limitado a {config.limit_rows} filas")
                        break

            return filas, errores, advertencias

        except StopIteration:
            errores.append("No hay datos para exportar")
            return 0, errores, advertencias
        except Exception as e:
            errores.append(f"Error exportando CSV streaming: {str(e)}")
            return filas, errores, advertencias

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


# ============================================================
# FUNCIÓN DE AYUDA PARA USO RÁPIDO
# ============================================================

def exportar_resultados(
    agentes: List[Dict],
    ruta: Optional[str] = None,
    formato: str = "json",
    **kwargs
) -> ExportResult:
    """
    Función rápida para exportar resultados.
    
    Args:
        agentes: Lista de agentes a exportar
        ruta: Ruta de destino (opcional)
        formato: Formato de exportación
        **kwargs: Configuración adicional
        
    Returns:
        ExportResult: Resultado de la exportación
    """
    exporter = ResultExporter()
    return exporter.exportar(agentes, ruta, formato, **kwargs)
