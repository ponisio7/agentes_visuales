# export/excel_exporter.py
"""
Exportador Excel con múltiples hojas.
"""

from collections import defaultdict
from typing import List, Dict, Tuple

from .models import ExportConfig
from .utils import aplanar_lista, MAX_EXCEL_ROWS


def exportar_excel(
    agentes: List[Dict],
    ruta: str,
    config: ExportConfig
) -> Tuple[int, List[str], List[str]]:
    """Exporta a Excel con múltiples hojas."""
    errores: List[str] = []
    advertencias: List[str] = []

    try:
        import pandas as pd
    except ImportError:
        errores.append("pandas no instalado. Ejecuta: pip install pandas openpyxl")
        return 0, errores, advertencias

    try:
        # Aplanar datos
        data = aplanar_lista(agentes, config.flatten, config.separator)

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