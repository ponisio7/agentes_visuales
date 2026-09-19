# core/utils/json_extra.py
"""
Utilidades genéricas de texto/JSON movidas desde el antiguo core/utils.py.

Contiene el escáner de JSON balanceado (respeta llaves dentro de cadenas)
y el limpiador de fences markdown para bloques de código. Se movieron al
paquete core/utils/ para eliminar el ensombrecimiento que impedía
importarlas como `core.utils`.
"""

import re


def extraer_json_balanceado(texto: str) -> str | None:
    """Extrae el primer objeto/array JSON balanceado de un texto.

    A diferencia de "primer '{' .. último '}'", respeta las llaves dentro
    de cadenas y se detiene en el cierre que equilibra la apertura, por lo
    que funciona aunque haya texto adicional o varios JSON concatenados.
    """
    if not texto:
        return None

    inicio = None
    apertura = None
    for i, c in enumerate(texto):
        if c in "[{":
            inicio = i
            apertura = c
            break
    if inicio is None:
        return None

    cierre = "]" if apertura == "[" else "}"
    profundidad = 0
    en_cadena = False
    escape = False
    for i in range(inicio, len(texto)):
        c = texto[i]
        if en_cadena:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                en_cadena = False
            continue
        if c == '"':
            en_cadena = True
        elif c == apertura:
            profundidad += 1
        elif c == cierre:
            profundidad -= 1
            if profundidad == 0:
                return texto[inicio:i + 1]
    return None


def limpiar_codigo(codigo: str) -> str:
    """
    Limpia código de posibles marcas de markdown.

    Args:
        codigo: Código con posibles marcas de markdown

    Returns:
        str: Código limpio
    """
    codigo = re.sub(r'^```\w*\s*\n', '', codigo, flags=re.MULTILINE)
    codigo = re.sub(r'\n```\s*$', '', codigo, flags=re.MULTILINE)
    return codigo.strip()


__all__ = ["extraer_json_balanceado", "limpiar_codigo"]
