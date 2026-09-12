# export/markdown_exporter.py
"""
Exportador Markdown.
"""

from datetime import datetime
from typing import List, Dict, Tuple

from .models import ExportConfig
from .utils import aplanar_lista


def exportar_markdown(
    agentes: List[Dict],
    ruta: str,
    config: ExportConfig
) -> Tuple[int, List[str], List[str]]:
    """Exporta a Markdown."""
    errores: List[str] = []
    advertencias: List[str] = []

    try:
        # Aplanar datos
        data = aplanar_lista(agentes, config.flatten, config.separator)

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

        with open(ruta, 'w', encoding=config.encoding) as f:
            f.write('\n'.join(lines))

        return len(data), errores, advertencias

    except Exception as e:
        errores.append(f"Error exportando Markdown: {str(e)}")
        return 0, errores, advertencias