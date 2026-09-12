# export/json_exporter.py
"""
Exportador JSON.
"""

import json
from datetime import datetime
from typing import List, Dict, Tuple

from .models import ExportConfig
from .utils import INDENT


def exportar_json(
    agentes: List[Dict],
    ruta: str,
    config: ExportConfig
) -> Tuple[int, List[str], List[str]]:
    """Exporta a JSON con metadatos."""
    errores: List[str] = []
    advertencias: List[str] = []

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

        # Usar utf-8 para JSON (sin BOM)
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