# export/csv_exporter.py
"""
Exportador CSV (incluye versión streaming).
"""

import csv
from typing import List, Dict, Tuple, Iterator

from .models import ExportConfig
from .utils import aplanar_lista, aplanar_diccionario


def exportar_csv(
    agentes: List[Dict],
    ruta: str,
    config: ExportConfig
) -> Tuple[int, List[str], List[str]]:
    """
    Exporta a CSV con aplanamiento automático.

    Returns:
        (filas_exportadas, errores, advertencias)
    """
    errores: List[str] = []
    advertencias: List[str] = []

    try:
        # Aplanar datos
        data = aplanar_lista(agentes, config.flatten, config.separator)

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
                clean_row = {k: v if v is not None else '' for k, v in row.items()}
                writer.writerow(clean_row)

        return len(data), errores, advertencias

    except Exception as e:
        errores.append(f"Error exportando CSV: {str(e)}")
        return 0, errores, advertencias


def exportar_csv_streaming(
    generador: Iterator[Dict],
    ruta: str,
    config: ExportConfig
) -> Tuple[int, List[str], List[str]]:
    """
    Exporta CSV desde un generador.

    Nota: materializa todas las filas para calcular la unión de claves
    (necesario para escribir un header correcto en CSV).
    """
    errores: List[str] = []
    advertencias: List[str] = []
    filas = 0

    try:
        todas = []
        for row in generador:
            if config.flatten:
                row = aplanar_diccionario(row, separator=config.separator)
            todas.append(row)
            if config.limit_rows and len(todas) >= config.limit_rows:
                advertencias.append(f"Limitado a {config.limit_rows} filas")
                break

        if not todas:
            errores.append("No hay datos para exportar")
            return 0, errores, advertencias

        # Unión de todas las claves (evita ValueError si filas posteriores
        # tienen campos que la primera fila no tenía)
        fieldnames = sorted({k for row in todas for k in row.keys()})

        with open(ruta, 'w', newline='', encoding=config.encoding) as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, restval='')
            writer.writeheader()

            for row in todas:
                clean_row = {k: v if v is not None else '' for k, v in row.items()}
                writer.writerow(clean_row)
                filas += 1

        return filas, errores, advertencias

    except Exception as e:
        errores.append(f"Error exportando CSV streaming: {str(e)}")
        return filas, errores, advertencias