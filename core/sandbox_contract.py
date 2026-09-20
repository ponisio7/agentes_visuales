# core/sandbox_contract.py
"""
Contrato de runtime del sandbox de agentes Python.

El sandbox inyecta en el script generado una serie de nombres además de
``contexto`` (ver ``core/sandbox.py::_construir_script``). Este módulo es la
ÚNICA fuente de verdad de ese contrato:

- ``dependencia(contexto, nombre[, clave])``: resuelve el valor útil del
  resultado de un agente upstream por NOMBRE SEMÁNTICO (no por extensión de
  archivo, no por tipo de agente ni por ramas ``if`` ad-hoc). El orden de
  prioridad sale del contrato de salida documentado en
  ``core/problem_solver/prompt_builder.py`` (``CONTRATOS_SALIDA``).
- ``preparar_imagen(ruta)``: garantiza que un archivo contiene una imagen
  RASTER cuyos bytes acepta un consumidor de documentos (png/jpeg/gif/bmp/
  tiff). Detecta el formato REAL con Pillow (nunca por la extensión) y
  convierte lo que Pillow sabe leer pero el consumidor no acepta (p. ej.
  WEBP). Si los bytes no son una imagen raster legible (p. ej. SVG, un
  JSON o un archivo corrupto) lanza ``ValueError`` con un mensaje
  accionable.
- ``formato_imagen_real(ruta)``: formato real detectado (o ``None``).

``PRELUDE_CODIGO`` es el fragmento que se inserta en el script del sandbox.
Se genera a partir del código fuente de las funciones de este módulo para
que no existan dos implementaciones que puedan divergir.
"""
from __future__ import annotations

import inspect
import textwrap

# ============================================================
# VALOR PRINCIPAL DE UNA DEPENDENCIA
# ============================================================
# Orden de prioridad para "el valor útil" de un resultado de agente.
# Deriva de los contratos documentados:
#   LLM    -> json, respuesta_limpia, respuesta
#   HTTP   -> json, body
#   File   -> contenido
#   Shell  -> stdout
#   Loop   -> items
# El nombre de la clave es genérico (no hay ninguna atada a un caso del
# problema): un resultado sin ninguna de estas claves cae al fallback de
# "el string más largo", que es lo que un humano llamaría "la respuesta".
_CLAVES_VALOR_PRINCIPAL = (
    "json",
    "respuesta_limpia",
    "respuesta",
    "contenido",
    "texto",
    "html",
    "markdown",
    "body",
    "stdout",
    "items",
    "documento",
    "output",
    "resultado",
    "data",
)

_FORMATOS_NATIVOS_DOCUMENTO = ("PNG", "JPEG", "GIF", "BMP", "TIFF", "WMF", "EMF")


def _es_vacio(valor) -> bool:
    """True si el valor no aporta contenido (None, '', [], {})."""
    if valor is None:
        return True
    if isinstance(valor, (str, bytes, bytearray, list, tuple, dict, set)):
        return len(valor) == 0
    return False


def valor_principal(valor, _profundidad: int = 0):
    """Extrae el valor más útil de un resultado de agente.

    - Si es un escalar, se devuelve tal cual.
    - Si es un dict, se busca por orden de contrato (``json`` primero, para
      no perder el JSON ya parseado). Un ``json`` que sea dict se inspecciona
      un nivel más para devolver su valor textual principal.
    - Si nada del contrato aparece, se devuelve el string más largo del dict
      (lo que un consumidor llamaría "la respuesta").
    - Si no hay ningún string, se devuelve el propio valor sin deformarlo.
    """
    if _profundidad > 3:
        return valor
    if not isinstance(valor, dict):
        return valor

    for clave in _CLAVES_VALOR_PRINCIPAL:
        if clave not in valor:
            continue
        candidato = valor[clave]
        if _es_vacio(candidato):
            continue
        if isinstance(candidato, str):
            return candidato
        if isinstance(candidato, (int, float, bool)):
            return candidato
        if isinstance(candidato, list):
            return candidato
        if isinstance(candidato, dict):
            anidado = valor_principal(candidato, _profundidad + 1)
            # Si el anidado aporta un escalar lo usamos; si no, devolvemos
            # el dict tal cual para no perder estructura.
            return anidado if not isinstance(anidado, dict) else candidato

    mejor = None
    for candidato in valor.values():
        if isinstance(candidato, str) and candidato.strip():
            if mejor is None or len(candidato) > len(mejor):
                mejor = candidato
    if mejor is not None:
        return mejor
    return valor


def dependencia(contexto, nombre=None, clave=None):
    """Valor útil del resultado de la dependencia ``nombre``.

    - ``dependencia(contexto)``: el contexto completo.
    - ``dependencia(contexto, 'Agente')``: valor principal del resultado.
    - ``dependencia(contexto, 'Agente', 'json')``: JSON ya parseado.
    - ``dependencia(contexto, 'Agente', 'texto')``: texto crudo principal.
    - ``dependencia(contexto, 'Agente', 'otra_clave')``: esa clave, mirando
      también dentro de ``json`` si el resultado la anida.
    """
    if not isinstance(contexto, dict):
        return None
    if nombre is None:
        return contexto
    valor = contexto.get(nombre)
    if valor is None:
        return None

    if clave is None:
        return valor_principal(valor)
    if clave == "json":
        if isinstance(valor, dict) and not _es_vacio(valor.get("json")):
            return valor["json"]
        return None
    if clave == "texto":
        if isinstance(valor, str):
            return valor
        if isinstance(valor, dict):
            for nombre_clave in (
                "respuesta_limpia", "respuesta", "contenido", "texto",
                "body", "stdout", "html", "markdown",
            ):
                candidato = valor.get(nombre_clave)
                if isinstance(candidato, str) and candidato.strip():
                    return candidato
        return valor_principal(valor)

    if isinstance(valor, dict):
        if not _es_vacio(valor.get(clave)):
            return valor[clave]
        anidado = valor.get("json")
        if isinstance(anidado, dict):
            return anidado.get(clave)
    return None


# ============================================================
# CONTRATO DE IMAGEN PARA DOCUMENTOS
# ============================================================

def formato_imagen_real(ruta):
    """Formato REAL de los bytes de un archivo de imagen, no su extensión.

    Devuelve el nombre del formato en mayúsculas (``'PNG'``, ``'JPEG'``,
    ``'SVG'``...) o ``None`` si Pillow no está disponible o los bytes no
    son una imagen legible.
    """
    try:
        from PIL import Image
    except ImportError:
        Image = None

    if Image is not None:
        try:
            with Image.open(ruta) as im:
                im.verify()
            with Image.open(ruta) as im:
                formato = (im.format or "").upper()
            if formato:
                return formato
        except Exception:
            return None

    # Fallback sin Pillow: firma de bytes de los raster más comunes.
    try:
        with open(ruta, "rb") as manejador:
            cabecera = manejador.read(16)
    except OSError:
        return None
    if cabecera.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if cabecera.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if cabecera.startswith(b"GIF8"):
        return "GIF"
    if cabecera.startswith(b"BM"):
        return "BMP"
    if cabecera.startswith(b"II*\x00") or cabecera.startswith(b"MM\x00*"):
        return "TIFF"
    return None


def preparar_imagen(ruta, formatos_aceptados=None):
    """Devuelve una ruta a una imagen que un documento puede consumir.

    Garantiza que los BYTES (no el nombre) son de un formato raster que el
    consumidor acepta. Si el formato real no está en ``formatos_aceptados``
    pero Pillow puede leerlo, se convierte a PNG en un temporal y se
    devuelve esa ruta. Si los bytes no son una imagen raster legible, lanza
    ``ValueError`` con la cabecera detectada para que el error sea
    accionable (p. ej. un SVG o un placeholder no llegan a la librería del
    documento).
    """
    import os
    import tempfile

    aceptados = tuple(formatos_aceptados) if formatos_aceptados else _FORMATOS_NATIVOS_DOCUMENTO

    if not ruta or not os.path.exists(ruta):
        raise ValueError(f"preparar_imagen: no existe el archivo '{ruta}'")

    formato = formato_imagen_real(ruta)
    if formato is None:
        try:
            with open(ruta, "rb") as manejador:
                cabecera = manejador.read(16)
        except OSError as error:
            raise ValueError(
                f"preparar_imagen: no se pudo leer '{ruta}': {error}"
            ) from error
        raise ValueError(
            f"preparar_imagen: '{ruta}' no contiene una imagen raster "
            f"reconocible (cabecera={cabecera!r}). Genera una imagen cuyos "
            f"bytes sean de un formato aceptado: {', '.join(aceptados)}."
        )

    if formato in aceptados:
        return ruta

    try:
        from PIL import Image
    except ImportError as error:  # pragma: no cover - Pillow es dependencia
        raise ValueError(
            f"preparar_imagen: '{ruta}' es {formato}, que el documento no "
            f"acepta, y Pillow no está disponible para convertirlo."
        ) from error

    temporal = tempfile.NamedTemporaryFile(
        suffix=".png", prefix="av_img_conv_", delete=False
    )
    temporal.close()
    with Image.open(ruta) as imagen:
        if imagen.mode in ("RGBA", "LA", "P"):
            imagen.convert("RGBA").save(temporal.name, format="PNG")
        else:
            imagen.convert("RGB").save(temporal.name, format="PNG")
    return temporal.name


# ============================================================
# PRELUDE PARA EL SANDBOX
# ============================================================

def _fuente(funcion) -> str:
    """Código fuente de una función de este módulo, sin indentación."""
    return textwrap.dedent(inspect.getsource(funcion)).rstrip()


def _construir_prelude() -> str:
    """Genera el fragmento de script que inyecta el contrato en el sandbox."""
    partes = [
        "# ============================================================",
        "# CONTRATO DE RUNTIME INYECTADO (core/sandbox_contract.py)",
        "# ============================================================",
        f"_CLAVES_VALOR_PRINCIPAL = {_CLAVES_VALOR_PRINCIPAL!r}",
        f"_FORMATOS_NATIVOS_DOCUMENTO = {_FORMATOS_NATIVOS_DOCUMENTO!r}",
        "",
    ]
    for funcion in (
        _es_vacio,
        valor_principal,
        dependencia,
        formato_imagen_real,
        preparar_imagen,
    ):
        partes.append(_fuente(funcion))
        partes.append("")
    return "\n".join(partes)


PRELUDE_CODIGO = _construir_prelude()

# Nombres que el sandbox define y que, por tanto, un agente puede usar sin
# definirlos. Lo consume el validador AST para no marcar falsos positivos.
NOMBRES_INYECTADOS = frozenset({
    "contexto", "item", "indice", "total", "resultado",
    "dependencia", "preparar_imagen", "formato_imagen_real",
    "json", "sys", "traceback", "time", "datetime", "warnings",
})
