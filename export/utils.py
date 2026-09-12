# export/utils.py
"""
Utilidades compartidas por los exportadores:
- Aplanamiento de diccionarios anidados
- Constantes comunes
"""

from typing import Dict, List

# ============================================================
# CONSTANTES
# ============================================================

MAX_CSV_ROWS = 1000000
MAX_HTML_ROWS = 10000
MAX_EXCEL_ROWS = 1048576  # Límite de Excel
MAX_JSON_SIZE = 100 * 1024 * 1024  # 100 MB
DEFAULT_ENCODING = "utf-8-sig"
CHUNK_SIZE = 10000
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
# APLANAMIENTO DE DATOS
# ============================================================

def aplanar_diccionario(
    d: Dict,
    parent_key: str = "",
    separator: str = ".",
    max_depth: int = 10
) -> Dict:
    """
    Aplana un diccionario anidado.

    Args:
        d: Diccionario a aplanar
        parent_key: Clave padre
        separator: Separador para claves anidadas
        max_depth: Profundidad máxima

    Returns:
        Dict: Diccionario aplanado
    """
    items = []

    if not d:
        return {}

    # Limitar profundidad
    if max_depth <= 0:
        return {parent_key: str(d) if parent_key else "..."}

    for k, v in d.items():
        new_key = f"{parent_key}{separator}{k}" if parent_key else k

        if isinstance(v, dict):
            items.extend(
                aplanar_diccionario(v, new_key, separator, max_depth - 1).items()
            )
        elif isinstance(v, list):
            # Para listas, convertir a string con límite
            if len(v) > 10:
                v_str = f"[{len(v)} items: {str(v[:3])[1:-1]}...]"
            else:
                v_str = str(v)
            items.append((new_key, v_str))
        else:
            # Convertir a string para tipos no serializables
            if v is None:
                items.append((new_key, ""))
            elif isinstance(v, (int, float, bool)):
                items.append((new_key, v))
            else:
                items.append((new_key, str(v)))

    return dict(items)


def aplanar_lista(
    data: List[Dict],
    flatten: bool = True,
    separator: str = "."
) -> List[Dict]:
    """
    Aplana una lista de diccionarios.

    Args:
        data: Lista de diccionarios
        flatten: Si se debe aplanar
        separator: Separador para claves anidadas

    Returns:
        List[Dict]: Lista de diccionarios aplanados
    """
    if not data:
        return []

    if not flatten:
        return data

    result = []
    for item in data:
        if isinstance(item, dict):
            result.append(aplanar_diccionario(item, separator=separator))
        else:
            result.append({"_valor": str(item)})

    return result