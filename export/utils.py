"""Utilidades compartidas por los exportadores.

Contiene el aplanamiento de estructuras anidadas como funciones libres,
de modo que puedan reutilizarse (y probarse) con independencia de
:class:`export.exporters.ResultExporter`.
"""
from typing import Any, Dict, List


def aplanar_diccionario(
    d: Dict,
    parent_key: str = "",
    separator: str = ".",
    max_depth: int = 10,
) -> Dict:
    """Aplana un diccionario anidado usando ``separator`` entre claves.

    Las listas se resumen como texto y los valores ``None`` se convierten
    en cadena vacía. La recursión se limita con ``max_depth``.

    Args:
        d: Diccionario a aplanar.
        parent_key: Prefijo de las claves (uso interno recursivo).
        separator: Separador para claves anidadas.
        max_depth: Profundidad máxima de anidamiento.

    Returns:
        Diccionario plano.
    """
    if not d:
        return {}

    if max_depth <= 0:
        return {parent_key: str(d) if parent_key else "..."}

    items: List[tuple] = []
    for k, v in d.items():
        new_key = f"{parent_key}{separator}{k}" if parent_key else k

        if isinstance(v, dict):
            items.extend(
                aplanar_diccionario(v, new_key, separator, max_depth - 1).items()
            )
        elif isinstance(v, list):
            if len(v) > 10:
                v_str = f"[{len(v)} items: {str(v[:3])[1:-1]}...]"
            else:
                v_str = str(v)
            items.append((new_key, v_str))
        elif v is None:
            items.append((new_key, ""))
        elif isinstance(v, (int, float, bool)):
            items.append((new_key, v))
        else:
            items.append((new_key, str(v)))

    return dict(items)


def aplanar_lista(
    data: List[Dict],
    flatten: bool = True,
    separator: str = ".",
) -> List[Dict]:
    """Aplana una lista de diccionarios.

    Args:
        data: Lista de diccionarios.
        flatten: Si es False devuelve la lista sin tocar.
        separator: Separador para claves anidadas.

    Returns:
        Lista de diccionarios planos.
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
