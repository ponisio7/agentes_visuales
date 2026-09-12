# export/txt_exporter.py
"""
Exportador TXT con formato legible.
"""

from datetime import datetime
from typing import List, Dict, Tuple

from .models import ExportConfig
from .utils import aplanar_lista


def exportar_txt(
    agentes: List[Dict],
    ruta: str,
    config: ExportConfig
) -> Tuple[int, List[str], List[str]]:
    """Exporta a TXT con formato legible."""
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