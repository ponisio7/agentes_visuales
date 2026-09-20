# core/verification.py
"""Verificador determinista de la salida de un paso (H6).

Convierte «éxito» en una propiedad **del artefacto**, no del estado del
agente: comprueba en disco y en bytes lo que el contrato de aceptación
declara (existe, tamaño > 0, formato real de imagen, JSON parseable, claves
presentes, nº de items/errores...), nunca lo que el LLM dice haber generado.

Reutiliza utilidades que ya existían:

* ``es_resultado_sospechoso`` (``core/executors/helpers``) — Nivel 1:
  un resultado vacío de un paso crítico deja de pasar en silencio.
* ``formato_imagen_real`` (``core/sandbox_contract``) — formato REAL de los
  bytes, no la extensión.
* ``validar_ruta_archivo`` (``core/executors/security``) — las rutas que
  verifica el contrato pasan por el mismo filtro que el FileExecutor.

Guardrails (dudas_sobre_el_programa.md §4):

* Se verifica el artefacto real, no la afirmación del LLM.
* Se distingue «no verificable» de «verificado»: una comprobación que no se
  pudo evaluar NO se da por buena.
* El verificador no sustituye al validador estático ni al sandbox.
* El verificador solo lee: no escribe, no borra y no ejecuta nada.
"""
from __future__ import annotations

import json
import logging
import os
import zipfile
from dataclasses import dataclass, field
from typing import Any

from core.executors.helpers import es_resultado_sospechoso
from core.executors.security import validar_ruta_archivo
from core.sandbox_contract import formato_imagen_real

logger = logging.getLogger(__name__)


# ============================================================
# FORMATOS DE DOCUMENTO CON IMÁGENES INCRUSTADAS
# ============================================================

# Contenedores ZIP: extensión -> prefijos internos donde viven los medios.
_MEDIOS_ZIP = {
    ".docx": ("word/media/",),
    ".docm": ("word/media/",),
    ".dotx": ("word/media/",),
    ".pptx": ("ppt/media/",),
    ".ppsx": ("ppt/media/",),
    ".xlsx": ("xl/media/",),
    ".odt": ("Pictures/",),
    ".ods": ("Pictures/",),
    ".odp": ("Pictures/",),
}

# Claves donde los ejecutores suelen dejar el texto útil de un resultado.
_CLAVES_TEXTO = (
    "texto", "contenido", "respuesta", "respuesta_limpia", "cuento",
    "documento", "html", "markdown", "body", "stdout", "mensaje",
)

_CLAVES_LISTA = ("items", "resultados", "resultados_por_url", "datos", "lista")


@dataclass
class Comprobacion:
    """Resultado de una comprobación concreta del contrato."""
    nombre: str
    ok: bool
    detalle: str = ""
    no_verificable: bool = False

    def to_dict(self) -> dict:
        return {
            "nombre": self.nombre,
            "ok": self.ok,
            "detalle": self.detalle,
            "no_verificable": self.no_verificable,
        }


@dataclass
class ResultadoVerificacion:
    """Veredicto del verificador sobre la salida de un paso."""
    aceptado: bool
    verificado: bool = False
    comprobaciones: list[Comprobacion] = field(default_factory=list)
    motivos: list[str] = field(default_factory=list)

    def motivo(self) -> str:
        """Motivo legible (uno o varios) del rechazo."""
        if not self.motivos:
            return ""
        return "; ".join(self.motivos)

    def to_dict(self) -> dict:
        return {
            "aceptado": self.aceptado,
            "verificado": self.verificado,
            "motivos": list(self.motivos),
            "comprobaciones": [c.to_dict() for c in self.comprobaciones],
        }


# ============================================================
# NORMALIZACIÓN DEL CONTRATO
# ============================================================

def _normalizar_contrato(contrato: Any) -> dict | None:
    """Acepta ``ContratoAceptacion``, dict o None y devuelve un dict plano."""
    if contrato is None:
        return None
    if hasattr(contrato, "es_vacio") and hasattr(contrato, "to_dict"):
        if contrato.es_vacio():
            return None
        return contrato.to_dict()
    if isinstance(contrato, dict):
        from core.problem_solver.models import ContratoAceptacion

        normalizado = ContratoAceptacion.from_dict(contrato)
        return normalizado.to_dict() if normalizado is not None else None
    return None


def _resolver_ruta(ruta: str, cwd: str | None) -> str:
    if os.path.isabs(ruta):
        return ruta
    return os.path.join(cwd or os.getcwd(), ruta)


def _textos_del_resultado(resultado: Any) -> str:
    """Concatena los textos útiles del resultado para medir longitud."""
    if resultado is None:
        return ""
    if isinstance(resultado, str):
        return resultado
    if isinstance(resultado, dict):
        partes: list[str] = []
        for clave in _CLAVES_TEXTO:
            valor = resultado.get(clave)
            if isinstance(valor, str):
                partes.append(valor)
        if not partes:
            # Sin claves conocidas: el string más largo del dict (1 nivel).
            candidatos = [v for v in resultado.values() if isinstance(v, str)]
            if candidatos:
                partes.append(max(candidatos, key=len))
        return "\n".join(partes)
    return ""


def _claves_del_resultado(resultado: Any) -> set[str]:
    if isinstance(resultado, dict):
        return set(resultado.keys())
    return set()


def _items_del_resultado(resultado: Any) -> int:
    """Número de items de un resultado de Loop/lista (0 si no aplica)."""
    if isinstance(resultado, list):
        return len(resultado)
    if isinstance(resultado, dict):
        total = resultado.get("total_items")
        if isinstance(total, (int, float)):
            return int(total)
        for clave in _CLAVES_LISTA:
            valor = resultado.get(clave)
            if isinstance(valor, list):
                return len(valor)
    return 0


def _errores_del_resultado(resultado: Any) -> int:
    """Número de errores reportados por el resultado (0 si no aplica)."""
    if isinstance(resultado, dict):
        errores = resultado.get("errores")
        if isinstance(errores, list):
            return len(errores)
        if isinstance(errores, (int, float)):
            return int(errores)
        if errores is None and "exitos" in resultado and "total_items" in resultado:
            try:
                return max(0, int(resultado["total_items"]) - int(resultado["exitos"]))
            except (TypeError, ValueError):
                return 0
    return 0


# ============================================================
# IMÁGENES INCRUSTADAS EN DOCUMENTOS
# ============================================================

def _imagenes_en_documento(ruta: str) -> tuple[int | None, list[str]]:
    """Cuenta imágenes RSTER incrustadas en un documento ofimático.

    Devuelve ``(n, detalles)``. ``n is None`` significa «formato no
    verificable» (no se puede inspeccionar sin una librería): el contrato
    fallará con motivo explícito en vez de darse por bueno.
    """
    extension = os.path.splitext(ruta)[1].lower()

    if extension not in _MEDIOS_ZIP:
        if extension == ".pdf":
            return _imagenes_en_pdf(ruta)
        return None, [f"formato '{extension or '?'}' no verificable"]

    detalles: list[str] = []
    imagenes = 0
    try:
        with zipfile.ZipFile(ruta) as paquete:
            prefijos = _MEDIOS_ZIP[extension]
            medios = [
                nombre for nombre in paquete.namelist()
                if any(nombre.startswith(p) for p in prefijos)
                and not nombre.endswith("/")
            ]
            if not medios:
                return 0, ["el documento no contiene medios"]
            for nombre in medios:
                try:
                    datos = paquete.read(nombre)
                except Exception as e:  # zip corrupto
                    detalles.append(f"{nombre}: no se pudo leer ({e})")
                    continue
                formato = _formato_bytes(datos, os.path.splitext(nombre)[1])
                if formato is None:
                    detalles.append(f"{nombre}: no es una imagen raster legible")
                else:
                    imagenes += 1
                    detalles.append(f"{nombre}: {formato}")
    except zipfile.BadZipFile:
        return 0, ["el documento no es un ZIP válido (posible corrupción)"]
    except OSError as e:
        return 0, [f"no se pudo abrir el documento: {e}"]
    return imagenes, detalles


def _imagenes_en_pdf(ruta: str) -> tuple[int | None, list[str]]:
    """Cuenta imágenes de un PDF con pypdf/PyPDF2 si está disponible."""
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        try:
            from PyPDF2 import PdfReader  # type: ignore
        except ImportError:
            return None, ["pypdf/PyPDF2 no disponible: PDF no verificable"]

    try:
        lector = PdfReader(ruta)
        imagenes = 0
        for pagina in lector.pages:
            try:
                imagenes += len(list(pagina.images))
            except Exception:
                continue
        return imagenes, [f"páginas: {len(lector.pages)}, imágenes: {imagenes}"]
    except Exception as e:
        return 0, [f"no se pudo leer el PDF: {e}"]


def _formato_bytes(datos: bytes, extension: str) -> str | None:
    """Formato real de unos bytes de imagen, sin depender de la extensión."""
    try:
        import io

        from PIL import Image

        with Image.open(io.BytesIO(datos)) as im:
            im.verify()
        with Image.open(io.BytesIO(datos)) as im:
            return (im.format or "").upper() or None
    except ImportError:
        pass
    except Exception:
        return None

    # Fallback sin Pillow: firma de bytes (mismas cabeceras que
    # ``formato_imagen_real``).
    if datos.startswith(b"\x89PNG\r\n\x1a\n"):
        return "PNG"
    if datos.startswith(b"\xff\xd8\xff"):
        return "JPEG"
    if datos.startswith(b"GIF8"):
        return "GIF"
    if datos.startswith(b"BM"):
        return "BMP"
    if datos.startswith(b"II*\x00") or datos.startswith(b"MM\x00*"):
        return "TIFF"
    return None


# ============================================================
# COMPROBACIONES DEL CONTRATO
# ============================================================

class _Acumulador:
    """Acumula comprobaciones y motivos de fallo."""

    def __init__(self):
        self.comprobaciones: list[Comprobacion] = []
        self.motivos: list[str] = []

    def anota(self, nombre: str, ok: bool, detalle: str = "", no_verificable: bool = False):
        self.comprobaciones.append(
            Comprobacion(nombre=nombre, ok=ok, detalle=detalle, no_verificable=no_verificable)
        )
        if not ok:
            etiqueta = "no verificable" if no_verificable else "falló"
            self.motivos.append(f"{nombre} ({etiqueta}): {detalle}" if detalle else nombre)


def _comprobar_archivo(
    acumulador: _Acumulador,
    ruta: str,
    cwd: str | None,
    min_bytes: int,
) -> str | None:
    """Comprueba existencia/tamaño de un archivo. Devuelve la ruta real."""
    if not validar_ruta_archivo(ruta):
        acumulador.anota(
            f"archivo:{ruta}", False,
            "ruta no válida (absoluta, oculta o con traversal)",
        )
        return None
    ruta_real = _resolver_ruta(ruta, cwd)
    if not os.path.exists(ruta_real):
        acumulador.anota(f"archivo:{ruta}", False, "no existe en disco")
        return None
    if not os.path.isfile(ruta_real):
        acumulador.anota(f"archivo:{ruta}", False, "no es un archivo regular")
        return None
    tamano = os.path.getsize(ruta_real)
    if tamano <= 0:
        acumulador.anota(f"archivo:{ruta}", False, "está vacío (0 bytes)")
        return ruta_real
    if min_bytes > 0 and tamano < min_bytes:
        acumulador.anota(
            f"archivo:{ruta}", False,
            f"tamaño {tamano} bytes < mínimo {min_bytes}",
        )
        return ruta_real
    acumulador.anota(f"archivo:{ruta}", True, f"{tamano} bytes")
    return ruta_real


def verificar_contrato(
    contrato: Any,
    resultado: Any = None,
    *,
    cwd: str | None = None,
    comprobar_sospechoso: bool = False,
) -> ResultadoVerificacion:
    """Comprueba un contrato de aceptación sobre un resultado/artefacto.

    Args:
        contrato: ``ContratoAceptacion``, dict o None.
        resultado: resultado devuelto por el ejecutor (dict/str/...).
        cwd: directorio base para rutas relativas (por defecto ``os.getcwd()``).
        comprobar_sospechoso: activa el gate Nivel 1 (resultado vacío).
    """
    acumulador = _Acumulador()

    if comprobar_sospechoso:
        sospechoso, motivo = es_resultado_sospechoso(resultado)
        acumulador.anota(
            "resultado_no_vacio", not sospechoso,
            motivo if sospechoso else "",
        )

    normalizado = _normalizar_contrato(contrato)
    if normalizado is None:
        # Sin contrato: solo cuenta el gate de resultado sospechoso.
        verificado = bool(comprobar_sospechoso)
        return ResultadoVerificacion(
            aceptado=not acumulador.motivos,
            verificado=verificado,
            comprobaciones=acumulador.comprobaciones,
            motivos=acumulador.motivos,
        )

    min_bytes = int(normalizado.get("min_bytes") or 0)

    # ── Archivos declarados ──
    rutas_reales: dict[str, str] = {}
    for ruta in normalizado.get("archivos") or []:
        real = _comprobar_archivo(acumulador, ruta, cwd, min_bytes)
        if real:
            rutas_reales[ruta] = real

    # ── Imágenes sueltas que deben ser raster reales ──
    formato_exigido = normalizado.get("formato_imagen")
    for ruta in normalizado.get("imagenes") or []:
        real = _comprobar_archivo(acumulador, ruta, cwd, min_bytes)
        if real is None:
            continue
        rutas_reales[ruta] = real
        formato = formato_imagen_real(real)
        if formato is None:
            acumulador.anota(
                f"imagen:{ruta}", False,
                "los bytes no son una imagen raster legible",
            )
        elif formato_exigido and formato != formato_exigido:
            acumulador.anota(
                f"imagen:{ruta}", False,
                f"formato real {formato} != exigido {formato_exigido}",
            )
        else:
            acumulador.anota(f"imagen:{ruta}", True, f"formato real {formato}")

    # ── JSON parseable ──
    if normalizado.get("json_parseable"):
        _comprobar_json(acumulador, normalizado, resultado, rutas_reales)

    # ── Claves requeridas ──
    claves_requeridas = normalizado.get("claves_requeridas") or []
    if claves_requeridas:
        _comprobar_claves(acumulador, claves_requeridas, resultado, rutas_reales)

    # ── Longitud mínima de texto ──
    min_caracteres = int(normalizado.get("min_caracteres") or 0)
    if min_caracteres > 0:
        texto = _textos_del_resultado(resultado)
        if not texto:
            for real in rutas_reales.values():
                if real.lower().endswith((".txt", ".md", ".csv", ".html", ".json")):
                    try:
                        with open(real, encoding="utf-8", errors="replace") as manejador:
                            texto = manejador.read()
                        break
                    except OSError:
                        continue
        longitud = len(texto)
        acumulador.anota(
            "min_caracteres", longitud >= min_caracteres,
            f"{longitud} caracteres (mínimo {min_caracteres})",
        )

    # ── Imágenes incrustadas en documento ──
    min_imagenes = int(normalizado.get("min_imagenes") or 0)
    if normalizado.get("requiere_imagen") and min_imagenes <= 0:
        min_imagenes = 1
    if min_imagenes > 0:
        _comprobar_imagenes_documento(acumulador, rutas_reales, min_imagenes)

    # ── Items y errores (resultado de Loop / listas) ──
    min_items = int(normalizado.get("min_items") or 0)
    if min_items > 0:
        items = _items_del_resultado(resultado)
        acumulador.anota(
            "min_items", items >= min_items,
            f"{items} items (mínimo {min_items})",
        )

    max_errores = normalizado.get("max_errores")
    if max_errores is not None:
        errores = _errores_del_resultado(resultado)
        acumulador.anota(
            "max_errores", errores <= int(max_errores),
            f"{errores} errores (máximo {max_errores})",
        )

    return ResultadoVerificacion(
        aceptado=not acumulador.motivos,
        verificado=True,
        comprobaciones=acumulador.comprobaciones,
        motivos=acumulador.motivos,
    )


def _comprobar_json(
    acumulador: _Acumulador,
    contrato: dict,
    resultado: Any,
    rutas_reales: dict[str, str],
) -> None:
    """Comprueba que el JSON declarado (archivo o clave) parsea."""
    candidatos = [
        (nombre, real) for nombre, real in rutas_reales.items()
        if real.lower().endswith(".json")
    ]
    if candidatos:
        for nombre, real in candidatos:
            try:
                with open(real, encoding="utf-8") as manejador:
                    json.loads(manejador.read())
                acumulador.anota(f"json:{nombre}", True, "parsea correctamente")
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
                acumulador.anota(f"json:{nombre}", False, f"no parseable: {e}")
        return

    # Sin archivo .json declarado: se acepta la clave 'json' del resultado.
    if isinstance(resultado, dict) and "json" in resultado:
        valor = resultado.get("json")
        if valor is None:
            crudo = resultado.get("body") or resultado.get("respuesta") or ""
            try:
                json.loads(crudo)
                acumulador.anota("json:resultado", True, "el cuerpo parsea")
            except (TypeError, json.JSONDecodeError) as e:
                acumulador.anota("json:resultado", False, f"no parseable: {e}")
        else:
            acumulador.anota("json:resultado", True, "clave 'json' presente")
        return

    # No hay nada que parsear: no se puede dar por verificado.
    acumulador.anota(
        "json", False,
        "el contrato exige JSON parseable pero no hay archivo .json ni clave 'json'",
        no_verificable=True,
    )


def _comprobar_claves(
    acumulador: _Acumulador,
    claves: list[str],
    resultado: Any,
    rutas_reales: dict[str, str],
) -> None:
    disponibles = _claves_del_resultado(resultado)
    if not disponibles:
        for real in rutas_reales.values():
            if real.lower().endswith(".json"):
                try:
                    with open(real, encoding="utf-8") as manejador:
                        datos = json.loads(manejador.read())
                    if isinstance(datos, dict):
                        disponibles = set(datos.keys())
                    break
                except (OSError, json.JSONDecodeError):
                    continue
    faltan = [clave for clave in claves if clave not in disponibles]
    if disponibles:
        acumulador.anota(
            "claves_requeridas", not faltan,
            f"faltan: {faltan}" if faltan else f"presentes: {claves}",
        )
    else:
        acumulador.anota(
            "claves_requeridas", False,
            f"no hay resultado con claves para comprobar {claves}",
            no_verificable=True,
        )


def _comprobar_imagenes_documento(
    acumulador: _Acumulador,
    rutas_reales: dict[str, str],
    min_imagenes: int,
) -> None:
    documentos = [
        (nombre, real) for nombre, real in rutas_reales.items()
        if os.path.splitext(real)[1].lower() in _MEDIOS_ZIP
        or os.path.splitext(real)[1].lower() == ".pdf"
    ]
    if not documentos:
        acumulador.anota(
            "imagenes_documento", False,
            "el contrato exige imágenes pero no declara ningún documento",
            no_verificable=True,
        )
        return

    for nombre, real in documentos:
        cantidad, detalles = _imagenes_en_documento(real)
        if cantidad is None:
            acumulador.anota(
                f"imagenes_documento:{nombre}", False, "; ".join(detalles),
                no_verificable=True,
            )
        else:
            acumulador.anota(
                f"imagenes_documento:{nombre}", cantidad >= min_imagenes,
                f"{cantidad} imágenes raster (mínimo {min_imagenes})"
                + (f" | {'; '.join(detalles)}" if detalles else ""),
            )


# ============================================================
# API DE ALTO NIVEL
# ============================================================

def verificar_agente(
    agente: Any,
    resultado: Any,
    *,
    cwd: str | None = None,
) -> ResultadoVerificacion | None:
    """Verifica la salida de un ``Agente`` según su contrato y su criticidad.

    Devuelve ``None`` si no hay nada que verificar (ni contrato ni
    ``es_critico``): distinguir «no verificable» de «verificado» es un
    guardrail, así que no se inventa un veredicto positivo.
    """
    contrato = getattr(agente, "contrato_aceptacion", None)
    es_critico = bool(getattr(agente, "es_critico", False))

    normalizado = _normalizar_contrato(contrato)

    # Un documento con imágenes declarado en el contrato se localiza por sus
    # propios archivos; si el contrato exige imágenes pero no lista el
    # documento, se usa el 'archivo_destino' del agente File.
    if normalizado is not None:
        destino = getattr(agente, "archivo_destino", "") or ""
        exige_imagenes = (
            normalizado.get("requiere_imagen")
            or int(normalizado.get("min_imagenes") or 0) > 0
        )
        if exige_imagenes and destino and destino not in (normalizado.get("archivos") or []):
            normalizado = dict(normalizado)
            normalizado["archivos"] = list(normalizado.get("archivos") or []) + [destino]

    if normalizado is None and not es_critico:
        return None

    return verificar_contrato(
        normalizado,
        resultado,
        cwd=cwd,
        comprobar_sospechoso=es_critico or bool(normalizado),
    )
