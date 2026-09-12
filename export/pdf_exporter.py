# export/pdf_exporter.py
"""
Exportador PDF (requiere reportlab).
"""

from datetime import datetime
from typing import List, Dict, Tuple

from .models import ExportConfig
from .utils import aplanar_lista


def exportar_pdf(
    agentes: List[Dict],
    ruta: str,
    config: ExportConfig
) -> Tuple[int, List[str], List[str]]:
    """Exporta a PDF."""
    errores: List[str] = []
    advertencias: List[str] = []

    try:
        from reportlab.lib.pagesizes import letter
        from reportlab.platypus import (
            SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        )
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib import colors
    except ImportError:
        errores.append("reportlab no instalado. Ejecuta: pip install reportlab")
        return 0, errores, advertencias

    try:
        # Aplanar datos
        data = aplanar_lista(agentes, config.flatten, config.separator)

        if not data:
            errores.append("No hay datos para exportar")
            return 0, errores, advertencias

        # Aplicar límite
        if config.limit_rows:
            data = data[:min(config.limit_rows, 5000)]
            if len(data) < len(agentes):
                advertencias.append("Limitado a 5000 filas para PDF")

        # Crear documento
        doc = SimpleDocTemplate(ruta, pagesize=letter)
        styles = getSampleStyleSheet()

        title_style = ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=24,
            spaceAfter=30
        )

        story = []

        # Título
        story.append(Paragraph("📊 Reporte de Agentes", title_style))

        # Fecha
        story.append(Paragraph(
            f"<i>Generado: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</i>",
            styles['Normal']
        ))
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
        headers_display = [h.replace('_', ' ').title() for h in headers]

        table_data = [headers_display]
        for row in data:
            row_data = []
            for h in headers:
                value = row.get(h, '')
                if isinstance(value, (int, float)):
                    value = f"{value:.2f}" if isinstance(value, float) else str(value)
                row_data.append(str(value)[:50] if value else '')
            table_data.append(row_data)

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
        doc.build(story)

        return len(data), errores, advertencias

    except Exception as e:
        errores.append(f"Error exportando PDF: {str(e)}")
        return 0, errores, advertencias