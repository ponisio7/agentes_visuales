from __future__ import annotations
# core/executors/file_executor.py
"""Ejecutor de agentes File."""

import csv
import io
import json
import logging
import os
import re
import shutil
import tempfile
from typing import Any

import markdown as md_lib  # nuevo
import requests
import weasyprint  # nuevo

from core.agent import Agente
from core.cancellation import CancellationToken

from .content_extractor import (
    extraer_contenido_relevante,
    sustituir_variables,
    variables_disponibles,
)
from .security import DANGEROUS_DIRS, MAX_BYTES_LECTURA_ARCHIVO, validar_ruta_archivo

logger = logging.getLogger(__name__)

# ── Extensiones con formato de escritura especializado ──
EXTENSIONES_ESCRITURA_ESPECIALIZADA = {".docx", ".xlsx", ".pdf", ".md", ".markdown"}

# ── Extensiones que deben escribirse como TEXTO PLANO ──
# Si el contenido llega como dict y la extensión está aquí, hay que
# desenvolver el dict para extraer el string útil (NO serializar a JSON).
EXTENSIONES_TEXTO_PLANO = {
    ".html", ".htm", ".xml", ".svg", ".css", ".js",
    ".txt", ".csv", ".tsv", ".log", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".conf",
}


def _escapar_xml(texto: str) -> str:
    """Escapa caracteres especiales para el motor de markup de reportlab."""
    return (
        str(texto)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )



def _celda_segura(valor: Any) -> Any:
    """Convierte un valor arbitrario en algo que openpyxl pueda escribir en una celda."""
    if valor is None:
        return ""
    if isinstance(valor, (str, int, float, bool)):
        return valor
    if isinstance(valor, (dict, list)):
        return json.dumps(valor, ensure_ascii=False, default=str)
    return str(valor)


def _parece_csv(texto: str) -> bool:
    """Heurística simple para detectar si un string es contenido CSV."""
    lineas = [linea for linea in texto.strip().split("\n") if linea.strip()]
    if len(lineas) < 2:
        return False
    conteos = [linea.count(",") for linea in lineas[:5]]
    return conteos[0] > 0 and len(set(conteos)) == 1


def _parsear_csv_simple(texto: str) -> list[list[str]]:
    """Parsea un string CSV a una lista de filas."""
    reader = csv.reader(io.StringIO(texto))
    return [fila for fila in reader]


class FileExecutor:
    """Operaciones con archivos: leer, escribir, copiar, mover, eliminar."""

    # ── Descarga de imágenes para inserción en .docx ──
    _DOCX_IMAGE_DOWNLOAD_TIMEOUT = 15  # segundos
    _DOCX_IMAGE_MAX_BYTES = 5 * 1024 * 1024  # 5 MB por imagen
    _DOCX_IMAGE_ALLOWED_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")

    @staticmethod
    def actualizar_progreso(agente, progreso, mensaje=""):
        agente.progreso = min(100, max(0, progreso))
        if mensaje:
            agente.mensaje = mensaje
        if hasattr(agente, '_bridge') and agente._bridge is not None:
            try:
                agente._bridge.agente_actualizado.emit(agente.id)
            except Exception:
                pass

    @classmethod
    def ejecutar(
        cls,
        agente: Agente,
        contexto: dict,
        cancellation_token: CancellationToken | None = None
    ) -> tuple[bool, str, dict]:
        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {'error': 'cancelled'}

        cls.actualizar_progreso(agente, 20, "Preparando operación de archivo...")

        variables = variables_disponibles(agente, contexto)
        origen = sustituir_variables(agente.archivo_origen, variables)
        destino = sustituir_variables(agente.archivo_destino, variables)
        operacion = agente.operacion_file or "leer"

        cls.actualizar_progreso(agente, 40, f"Operación: {operacion}")

        # ── Validación según operación ──
        ruta_archivo = None
        if operacion in ("leer", "eliminar"):
            ruta_archivo = origen
            if not ruta_archivo:
                cls.actualizar_progreso(agente, 100, "Ruta no especificada")
                return False, f"No se especificó ruta para operación: {operacion}", {}
            if not validar_ruta_archivo(ruta_archivo):
                cls.actualizar_progreso(agente, 100, "Ruta inválida")
                return False, f"Ruta inválida: {ruta_archivo}", {}
        elif operacion in ("escribir", "copiar", "mover"):
            if not destino:
                cls.actualizar_progreso(agente, 100, "Destino no especificado")
                return False, f"No se especificó destino para operación: {operacion}", {}
            if not validar_ruta_archivo(destino):
                cls.actualizar_progreso(agente, 100, "Ruta destino inválida")
                return False, f"Ruta destino inválida: {destino}", {}
            if operacion in ("copiar", "mover") and origen:
                if not validar_ruta_archivo(origen):
                    cls.actualizar_progreso(agente, 100, "Ruta origen inválida")
                    return False, f"Ruta origen inválida: {origen}", {}
                ruta_archivo = origen
            else:
                ruta_archivo = destino
        else:
            cls.actualizar_progreso(agente, 100, f"Operación no soportada: {operacion}")
            return False, f"Operación de archivo no soportada: {operacion}", {}

        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de operación", {'error': 'cancelled'}

        try:
            # ══════════════════════════════════════════════════════════
            # LEER
            # ══════════════════════════════════════════════════════════
            if operacion == "leer":
                if not os.path.exists(ruta_archivo):
                    cls.actualizar_progreso(agente, 100, "Archivo no encontrado")
                    return False, f"Archivo no encontrado: {ruta_archivo}", {}
                if not os.path.isfile(ruta_archivo):
                    cls.actualizar_progreso(agente, 100, "No es un archivo")
                    return False, f"No es un archivo: {ruta_archivo}", {}

                tamaño = os.path.getsize(ruta_archivo)
                if tamaño > MAX_BYTES_LECTURA_ARCHIVO:
                    cls.actualizar_progreso(agente, 100, "Archivo demasiado grande")
                    return False, (
                        f"Archivo demasiado grande ({tamaño} bytes > "
                        f"{MAX_BYTES_LECTURA_ARCHIVO} límite)"
                    ), {"archivo": ruta_archivo, "tamaño": tamaño}

                cls.actualizar_progreso(agente, 70, "Leyendo archivo...")
                contenido = ""
                chunk_size = 8192
                with open(ruta_archivo, encoding='utf-8', errors='replace') as f:
                    while True:
                        if cancellation_token and cancellation_token.esta_cancelado():
                            return False, "Cancelado durante lectura", {
                                'error': 'cancelled',
                                'archivo': ruta_archivo,
                                'bytes_leidos': len(contenido)
                            }
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        contenido += chunk

                cls.actualizar_progreso(agente, 100, "Archivo leído")

                resultado = {
                    "archivo": ruta_archivo,
                    "tamaño": tamaño,
                    "contenido": contenido,
                    "total_caracteres": len(contenido)
                }
                if ruta_archivo.endswith('.json'):
                    try:
                        resultado["json"] = json.loads(contenido)
                    except json.JSONDecodeError:
                        pass

                modo_salida = getattr(agente, 'modo_salida_file', 'auto') or 'auto'
                resultado = cls._aplicar_modo_salida_file(resultado, modo_salida, contenido)

                return True, (
                    f"Archivo leído: {ruta_archivo} ({len(contenido)} caracteres)"
                ), resultado

            # ══════════════════════════════════════════════════════════
            # ESCRIBIR
            # ══════════════════════════════════════════════════════════
            elif operacion == "escribir":
                contenido = None

                if 'contenido' in contexto:
                    contenido = contexto['contenido']
                elif 'resultado' in contexto:
                    contenido = contexto['resultado']
                elif contexto:
                    if len(contexto) == 1:
                        valor_unico = next(iter(contexto.values()))
                        contenido = extraer_contenido_relevante(valor_unico)
                    else:
                        for valor in contexto.values():
                            if not isinstance(valor, dict):
                                continue
                            candidato = extraer_contenido_relevante(valor)
                            if candidato is not valor:
                                contenido = candidato
                                break
                        if contenido is None:
                            primer_valor = next(iter(contexto.values()))
                            contenido = extraer_contenido_relevante(primer_valor)

                if contenido is None:
                    claves_contexto = list(contexto.keys()) if contexto else []
                    mensaje_error = (
                        f"File: no se encontró contenido para escribir en "
                        f"'{ruta_archivo}'. El agente '{agente.nombre}' no "
                        f"recibió datos útiles de sus dependencias. "
                        f"Claves del contexto: "
                        f"{claves_contexto if claves_contexto else '(vacío)'}"
                    )
                    logger.error(mensaje_error)
                    cls.actualizar_progreso(agente, 100, "❌ Sin contenido")
                    return False, mensaje_error, {
                        'error': 'no_content',
                        'archivo': ruta_archivo,
                        'operacion': operacion,
                        'contexto_claves': claves_contexto,
                        'contexto_preview': str(contexto)[:500] if contexto else '',
                    }

                # ── Dispatch por extensión a formatos especializados ──
                extension = os.path.splitext(ruta_archivo)[1].lower()
                if extension == ".docx":
                    return cls._file_escribir_docx(
                        agente, ruta_archivo, contenido, contexto, cancellation_token
                    )
                elif extension == ".xlsx":
                    return cls._file_escribir_xlsx(
                        agente, ruta_archivo, contenido, contexto, cancellation_token
                    )
                elif extension == ".pdf":
                    return cls._file_escribir_pdf(
                        agente, ruta_archivo, contenido, contexto, cancellation_token
                    )
                elif extension in (".md", ".markdown"):
                    return cls._file_escribir_markdown(
                        agente, ruta_archivo, contenido, contexto, cancellation_token
                    )

                extension_actual = os.path.splitext(ruta_archivo)[1].lower()

                if isinstance(contenido, str):
                    contenido_str = contenido

                elif isinstance(contenido, (dict, list)) and extension_actual in EXTENSIONES_TEXTO_PLANO:
                    # ✅ FIX 3: para extensiones web/plano, desenvolver el dict
                    # en lugar de serializar a JSON.
                    contenido_str = cls._desenvolver_contenido_web(contenido)
                    if contenido_str is None:
                        claves = list(contenido.keys()) if isinstance(contenido, dict) else f'list[{len(contenido)}]'
                        mensaje_error = (
                            f"File.escribir: no se pudo extraer texto útil del dict/list "
                            f"para '{ruta_archivo}'. Claves: {claves}"
                        )
                        logger.error(mensaje_error)
                        cls.actualizar_progreso(agente, 100, "❌ Contrato de contenido roto")
                        return False, mensaje_error, {
                            "error": "no_known_text_keys",
                            "archivo": ruta_archivo,
                            "claves_presentes": claves,
                        }

                elif isinstance(contenido, (dict, list)):
                    # Para .json u otras extensiones estructuradas: sí serializar a JSON
                    contenido_str = json.dumps(
                        contenido, indent=2, default=str, ensure_ascii=False
                    )

                elif isinstance(contenido, (int, float, bool)):
                    contenido_str = str(contenido)

                else:
                    contenido_str = str(contenido)

                directorio = os.path.dirname(ruta_archivo)
                if directorio:
                    os.makedirs(directorio, exist_ok=True)

                cls.actualizar_progreso(agente, 70, "Escribiendo archivo...")

                chunk_size = 8192
                with open(ruta_archivo, 'w', encoding='utf-8') as f:
                    for i in range(0, len(contenido_str), chunk_size):
                        if cancellation_token and cancellation_token.esta_cancelado():
                            return False, "Cancelado durante escritura", {
                                'error': 'cancelled',
                                'archivo': ruta_archivo,
                                'caracteres_escritos': i
                            }
                        f.write(contenido_str[i:i + chunk_size])

                cls.actualizar_progreso(agente, 100, "Archivo escrito")

                resultado = {
                    "archivo": ruta_archivo,
                    "caracteres_escritos": len(contenido_str),
                    "contenido_preview": (
                        contenido_str[:200] if len(contenido_str) > 200 else contenido_str
                    )
                }
                return True, (
                    f"Archivo escrito: {ruta_archivo} ({len(contenido_str)} caracteres)"
                ), resultado

            # ══════════════════════════════════════════════════════════
            # COPIAR
            # ══════════════════════════════════════════════════════════
            elif operacion == "copiar":
                if not os.path.exists(origen):
                    cls.actualizar_progreso(agente, 100, "Origen no encontrado")
                    return False, f"Archivo origen no encontrado: {origen}", {}
                if not os.path.isfile(origen):
                    cls.actualizar_progreso(agente, 100, "No es un archivo")
                    return False, f"No es un archivo: {origen}", {}

                directorio_destino = os.path.dirname(destino)
                if directorio_destino:
                    os.makedirs(directorio_destino, exist_ok=True)

                cls.actualizar_progreso(agente, 70, "Copiando archivo...")
                shutil.copy2(origen, destino)
                cls.actualizar_progreso(agente, 100, "Archivo copiado")

                resultado = {
                    "origen": origen, "destino": destino,
                    "tamaño": os.path.getsize(destino)
                }
                return True, f"Archivo copiado: {origen} → {destino}", resultado

            # ══════════════════════════════════════════════════════════
            # MOVER
            # ══════════════════════════════════════════════════════════
            elif operacion == "mover":
                if not os.path.exists(origen):
                    cls.actualizar_progreso(agente, 100, "Origen no encontrado")
                    return False, f"Archivo origen no encontrado: {origen}", {}
                if not os.path.isfile(origen):
                    cls.actualizar_progreso(agente, 100, "No es un archivo")
                    return False, f"No es un archivo: {origen}", {}

                directorio_destino = os.path.dirname(destino)
                if directorio_destino:
                    os.makedirs(directorio_destino, exist_ok=True)

                cls.actualizar_progreso(agente, 70, "Moviendo archivo...")
                shutil.move(origen, destino)
                cls.actualizar_progreso(agente, 100, "Archivo movido")

                resultado = {
                    "origen": origen, "destino": destino,
                    "tamaño": os.path.getsize(destino) if os.path.exists(destino) else None
                }
                return True, f"Archivo movido: {origen} → {destino}", resultado

            # ══════════════════════════════════════════════════════════
            # ELIMINAR
            # ══════════════════════════════════════════════════════════
            elif operacion == "eliminar":
                if not os.path.exists(ruta_archivo):
                    cls.actualizar_progreso(agente, 100, "Archivo no encontrado")
                    return False, f"Archivo no encontrado: {ruta_archivo}", {}

                # Defensa extra: nunca borrar el propio directorio de trabajo
                # ni nada fuera de él (cubre symlinks y rutas resueltas).
                cwd_real = os.path.realpath(os.getcwd())
                objetivo_real = os.path.realpath(ruta_archivo)
                if (objetivo_real == cwd_real
                        or not objetivo_real.startswith(cwd_real + os.sep)):
                    cls.actualizar_progreso(agente, 100, "No se permite eliminar")
                    return False, (
                        f"No se permite eliminar fuera del directorio de trabajo: "
                        f"{ruta_archivo}"
                    ), {}

                if os.path.isdir(ruta_archivo):
                    if (ruta_archivo in DANGEROUS_DIRS or
                            os.path.dirname(ruta_archivo) in DANGEROUS_DIRS):
                        cls.actualizar_progreso(agente, 100, "No se permite eliminar")
                        return False, (
                            f"No se permite eliminar directorios del sistema: {ruta_archivo}"
                        ), {}
                    cls.actualizar_progreso(agente, 70, "Eliminando directorio...")
                    shutil.rmtree(ruta_archivo)
                    mensaje = f"Directorio eliminado: {ruta_archivo}"
                else:
                    cls.actualizar_progreso(agente, 70, "Eliminando archivo...")
                    os.remove(ruta_archivo)
                    mensaje = f"Archivo eliminado: {ruta_archivo}"

                cls.actualizar_progreso(agente, 100, "Eliminado")
                return True, mensaje, {"archivo": ruta_archivo, "eliminado": True}

            else:
                cls.actualizar_progreso(agente, 100, f"Operación no soportada: {operacion}")
                return False, f"Operación de archivo no soportada: {operacion}", {}

        except PermissionError as e:
            cls.actualizar_progreso(agente, 100, "Permiso denegado")
            return False, (
                f"Permiso denegado para operación '{operacion}' en: "
                f"{ruta_archivo} - {e}"
            ), {
                'error': 'permission_denied',
                'archivo': ruta_archivo,
                'operacion': operacion
            }
        except OSError as e:
            cls.actualizar_progreso(agente, 100, f"Error: {str(e)[:50]}")
            return False, f"Error en operación de archivo: {str(e)}", {
                'error': 'os_error', 'archivo': ruta_archivo,
                'operacion': operacion, 'detalle': str(e)
            }
        except Exception as e:
            cls.actualizar_progreso(agente, 100, f"Error: {str(e)[:50]}")
            return False, f"Error inesperado en operación de archivo: {str(e)}", {
                'error': 'unexpected', 'archivo': ruta_archivo,
                'operacion': operacion, 'detalle': str(e)
            }

    @staticmethod
    def _desenvolver_contenido_web(contenido: Any) -> str | None:
        """
        Desenvuelve un dict/list buscando la primera clave de contenido textual
        útil para formatos web/plano. Devuelve None si no encuentra nada.

        Estrategia:
          1. Si es str, devolverlo tal cual (con intento de parseo JSON si parece JSON).
          2. Si es dict, buscar claves conocidas de contenido web/texto.
          3. Si es dict con UNA sola clave, devolver su valor si es str.
          4. Si es list de strings, unirlos con saltos de línea.
          5. Si es list de dicts, desenvolver el primero.
          6. Si nada funciona, devolver None.
        """
        if isinstance(contenido, str):
            # ✅ Si el string parece JSON, intentar desenrollarlo
            s = contenido.strip()
            if s.startswith('{'):
                try:
                    data = json.loads(s)
                    if isinstance(data, dict):
                        for clave in ("html", "css", "js", "svg", "xml", "contenido", "texto", "body"):
                            if clave in data and isinstance(data[clave], str):
                                return data[clave]
                except (json.JSONDecodeError, ValueError):
                    pass
            return contenido

        if isinstance(contenido, dict):
            CLAVES_WEB = (
                # Contenido web/texto crudo
                "html", "css", "js", "javascript", "svg", "xml",
                "contenido", "content", "texto", "text",
                "body", "cuerpo", "codigo", "code",
                "source", "fuente",
                # Respuestas de agente
                "json", "respuesta_limpia", "respuesta", "resultado", "output",
                "markdown", "md",
            )
            for clave in CLAVES_WEB:
                if clave in contenido and contenido[clave]:
                    valor = contenido[clave]
                    if isinstance(valor, str):
                        # Recursivo: si ese valor es JSON string con html dentro, desenvolverlo
                        rec = FileExecutor._desenvolver_contenido_web(valor)
                        # Si rec es distinto y parece haber resuelto JSON interno, preferirlo
                        # pero si rec == valor (no era JSON), devolver valor
                        return rec if rec is not None else valor
                    # Anidamiento un nivel más: {"html": {"html": "..."}}
                    anidado = FileExecutor._desenvolver_contenido_web(valor)
                    if anidado is not None:
                        return anidado

            # Fallback: si el dict tiene UNA sola clave str, usar su valor
            if len(contenido) == 1:
                unico = next(iter(contenido.values()))
                if isinstance(unico, str):
                    return unico

            return None

        if isinstance(contenido, (list, tuple)):
            # Lista de strings → unirlos con saltos de línea
            if all(isinstance(x, str) for x in contenido):
                return "\n".join(contenido)
            # Lista de dicts → intentar desenvolver el primero
            for item in contenido:
                resultado = FileExecutor._desenvolver_contenido_web(item)
                if resultado is not None:
                    return resultado
            return None

        return None

    @staticmethod
    def _aplicar_modo_salida_file(resultado: dict, modo: str, contenido_texto: str) -> dict:
        """Ajusta el resultado devuelto por un agente File según el modo elegido."""
        modo = (modo or "auto").lower()
        if modo == "auto":
            return resultado
        if modo in ("contenido", "texto"):
            return {
                "archivo": resultado.get("archivo", ""),
                "tamaño": resultado.get("tamaño", len(contenido_texto)),
                "contenido": contenido_texto,
                "total_caracteres": len(contenido_texto),
                "modo_salida": "contenido",
            }
        if modo == "json":
            json_data = resultado.get("json")
            if json_data is None:
                try:
                    json_data = json.loads(contenido_texto)
                except (json.JSONDecodeError, ValueError):
                    json_data = None
            return {
                "archivo": resultado.get("archivo", ""),
                "tamaño": resultado.get("tamaño", len(contenido_texto)),
                "json": json_data,
                "modo_salida": "json",
            }
        return resultado

    # ══════════════════════════════════════════════════════════════
    # Formatos de escritura especializados (docx / xlsx / pdf / md)
    # ══════════════════════════════════════════════════════════════

    @staticmethod
    def _extraer_titulo_y_contenido(contenido: Any):
        """Si `contenido` es un dict, extrae 'titulo'/'title' y el cuerpo real."""
        titulo = None
        if isinstance(contenido, dict):
            titulo = contenido.get("titulo") or contenido.get("title")
            for clave in ("cuento", "contenido", "texto", "respuesta_limpia", "respuesta"):
                if clave in contenido:
                    contenido = contenido[clave]
                    break
        return titulo, contenido

    CLAVES_TEXTO_VALIDAS = (
        "cuento", "texto", "contenido", "respuesta_limpia", "respuesta",
        "historia", "informe", "articulo", "cuerpo", "documento", "markdown",
    )

    @classmethod
    def _extraer_texto_o_fallar(
        cls,
        contenido: Any,
        ruta_archivo: str,
        contexto_formato: str,
    ) -> tuple[str | None, str | None]:
        """
        Extrae el texto útil de `contenido`. Devuelve (texto, None) si OK,
        o (None, mensaje_error) si el dict no tiene claves reconocidas.

        NO serializa dicts a JSON como fallback: eso produce PDFs/MDs con
        JSON basura en lugar del contenido esperado.
        """
        if isinstance(contenido, str):
            return contenido, None

        if isinstance(contenido, dict):
            for clave in cls.CLAVES_TEXTO_VALIDAS:
                if clave in contenido and contenido[clave]:
                    valor = contenido[clave]
                    if isinstance(valor, str):
                        return valor, None
                    # Si la clave existe pero no es str, seguir buscando
            claves_presentes = [k for k in contenido.keys() if not k.startswith("_")]
            mensaje = (
                f"File.escribir_{contexto_formato}: el dict de contenido no tiene "
                f"ninguna clave de texto reconocida. Claves presentes: {claves_presentes}. "
                f"Claves esperadas: {list(cls.CLAVES_TEXTO_VALIDAS)}. "
                f"El agente que produjo este contenido probablemente devolvió "
                f"un JSON con claves no soportadas. Archivo: {ruta_archivo}"
            )
            logger.error(mensaje)
            return None, mensaje

        if isinstance(contenido, (list, tuple)):
            mensaje = (
                f"File.escribir_{contexto_formato}: contenido es {type(contenido).__name__}, "
                f"no str ni dict. Archivo: {ruta_archivo}"
            )
            logger.error(mensaje)
            return None, mensaje

        # Fallback: convertir a str (números, None, etc.)
        return str(contenido), None

    @classmethod
    def _descargar_imagen_temporal(cls, url: str) -> str | None:
        """
        Descarga una imagen desde una URL a un archivo temporal.
        Devuelve la ruta del archivo o None si falla o no es una imagen válida.
        """
        if not url or not isinstance(url, str):
            return None
        if not url.startswith(("http://", "https://")):
            return None

        tmp_path = cls._descargar_a_temporal(url)
        if tmp_path is None:
            return None

        if not cls._validar_imagen_descargada(tmp_path, url):
            cls._borrar_temporal_silencioso(tmp_path)
            return None

        return tmp_path

    @classmethod
    def _descargar_a_temporal(cls, url: str) -> str | None:
        """Descarga la URL a un archivo temporal, respetando el límite de tamaño."""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            ext = os.path.splitext(parsed.path)[1].lower()
            if ext not in cls._DOCX_IMAGE_ALLOWED_EXT:
                ext = ".jpg"  # fallback

            resp = requests.get(
                url,
                timeout=cls._DOCX_IMAGE_DOWNLOAD_TIMEOUT,
                stream=True,
                headers={"User-Agent": "Agentes-Visuales/1.0"},
            )
            resp.raise_for_status()

            content_type = resp.headers.get("Content-Type", "").lower()
            if content_type and not content_type.startswith("image/"):
                logger.warning(f"URL {url} devolvió Content-Type no-imagen: {content_type}")
                return None

            tmp = tempfile.NamedTemporaryFile(suffix=ext, delete=False, prefix="av_img_")
            try:
                total = 0
                for chunk in resp.iter_content(chunk_size=8192):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > cls._DOCX_IMAGE_MAX_BYTES:
                        tmp.close()
                        os.unlink(tmp.name)
                        logger.warning(f"Imagen demasiado grande: {url}")
                        return None
                    tmp.write(chunk)
                tmp.close()
                return tmp.name
            except Exception:
                try:
                    tmp.close()
                except Exception:
                    pass
                cls._borrar_temporal_silencioso(tmp.name)
                raise

        except Exception as e:
            logger.warning(f"No se pudo descargar imagen {url}: {e}")
            return None

    @classmethod
    def _validar_imagen_descargada(cls, tmp_path: str, url: str) -> bool:
        """
        Comprueba que el archivo descargado no esté vacío/truncado y que,
        si Pillow está disponible, sea una imagen legible.
        """
        tamaño_final = os.path.getsize(tmp_path)
        if tamaño_final < 100:
            logger.warning(
                f"Imagen descargada vacía o corrupta: {url} "
                f"({tamaño_final} bytes). Se descarta."
            )
            return False

        try:
            from PIL import Image as PILImage
        except ImportError:
            return True  # Pillow no instalado: nos conformamos con la validación de tamaño

        try:
            with PILImage.open(tmp_path) as im:
                im.verify()
        except Exception as e:
            logger.warning(f"Imagen descargada no es válida: {url} ({e}). Se descarta.")
            return False

        return True

    @classmethod
    def _borrar_temporal_silencioso(cls, path: str) -> None:
        """Borra un archivo temporal ignorando errores (archivo ya borrado, permisos, etc.)."""
        try:
            os.unlink(path)
        except Exception:
            pass

    @classmethod
    def _file_escribir_docx(
        cls,
        agente: Agente,
        ruta_archivo: str,
        contenido: Any,
        contexto: dict,
        cancellation_token: CancellationToken | None = None,
    ) -> tuple[bool, str, dict]:
        try:
            from docx import Document
            from docx.enum.text import WD_ALIGN_PARAGRAPH
            from docx.shared import Inches
        except ImportError:
            return False, (
                "python-docx no está instalado. Instálalo con: "
                "pip install python-docx"
            ), {"error": "missing_dependency", "dep": "python-docx"}

        directorio = os.path.dirname(ruta_archivo)
        if directorio:
            os.makedirs(directorio, exist_ok=True)

        cls.actualizar_progreso(agente, 70, "Escribiendo .docx...")

        # 1. Extraer título, texto e imágenes por separado
        titulo = None
        imagenes = []

        # ✅ DESPUÉS
        if isinstance(contenido, dict):
            titulo = contenido.get("titulo") or contenido.get("title")
            imagenes = contenido.get("imagenes") or contenido.get("images") or []

            CLAVES_TEXTO_VALIDAS = (
                "cuento", "texto", "contenido", "respuesta_limpia", "respuesta",
                "historia", "informe", "articulo", "cuerpo", "documento",
            )
            for clave in CLAVES_TEXTO_VALIDAS:
                if clave in contenido and contenido[clave]:
                    contenido = contenido[clave]
                    break
            else:
                # ✅ NUEVO: fallar con mensaje claro en lugar de serializar el dict entero
                claves_presentes = [k for k in contenido.keys() if not k.startswith("_")]
                mensaje = (
                    f"File.escribir_docx: el dict de contenido no tiene ninguna "
                    f"clave de texto reconocida. Claves presentes: {claves_presentes}. "
                    f"Claves esperadas: {list(CLAVES_TEXTO_VALIDAS)}. "
                    f"El agente que produjo este contenido probablemente devolvió "
                    f"un JSON con claves no soportadas."
                )
                logger.error(mensaje)
                cls.actualizar_progreso(agente, 100, "❌ Contrato de contenido roto")
                return False, mensaje, {
                    "error": "no_known_text_keys",
                    "archivo": ruta_archivo,
                    "claves_presentes": claves_presentes,
                    "claves_esperadas": list(CLAVES_TEXTO_VALIDAS),
                    "contenido_preview": str(contenido)[:500],
                }

        if isinstance(contenido, str):
            texto = contenido
        elif isinstance(contenido, (dict, list)):
            texto = json.dumps(contenido, indent=2, ensure_ascii=False, default=str)
        else:
            texto = str(contenido)

        if not texto.strip() and not imagenes:
            return False, f"File.escribir_docx: contenido vacío para '{ruta_archivo}'", {
                "error": "empty_content", "archivo": ruta_archivo
            }

        doc = Document()
        if titulo:
            h = doc.add_heading(str(titulo), level=1)
            h.alignment = WD_ALIGN_PARAGRAPH.CENTER

        for linea in texto.split("\n"):
            if cancellation_token and cancellation_token.esta_cancelado():
                return False, "Cancelado durante .docx", {"error": "cancelled"}
            stripped = linea.strip()
            if not stripped:
                doc.add_paragraph()
                continue
            if stripped.startswith("### "):
                doc.add_heading(stripped[4:], level=3)
            elif stripped.startswith("## "):
                doc.add_heading(stripped[3:], level=2)
            elif stripped.startswith("# "):
                doc.add_heading(stripped[2:], level=1)
            elif stripped.startswith(("- ", "* ")):
                doc.add_paragraph(stripped[2:], style="List Bullet")
            elif re.match(r"^\d+\.\s", stripped):
                doc.add_paragraph(re.sub(r"^\d+\.\s", "", stripped), style="List Number")
            else:
                doc.add_paragraph(stripped)

        # ── Procesar imágenes ──
        temporales_a_limpiar = []
        insertadas = 0
        fallidas = []

        for idx, img_item in enumerate(imagenes):
            if cancellation_token and cancellation_token.esta_cancelado():
                for t in temporales_a_limpiar:
                    try:
                        os.unlink(t)
                    except Exception:
                        pass
                return False, "Cancelado durante descarga de imágenes", {"error": "cancelled"}

            # Aceptar dict {"url": ..., "descripcion": ...} o string directo
            if isinstance(img_item, dict):
                url = img_item.get("url") or img_item.get("src") or ""
                descripcion = img_item.get("descripcion") or img_item.get("alt") or ""
            elif isinstance(img_item, str):
                url = img_item
                descripcion = ""
            else:
                fallidas.append(f"item {idx}: tipo no soportado {type(img_item).__name__}")
                continue

            # ¿Es una ruta local ya existente?
            if os.path.exists(url):
                ruta_local = url
            else:
                # Intentar descargar
                ruta_local = cls._descargar_imagen_temporal(url)
                if ruta_local:
                    temporales_a_limpiar.append(ruta_local)

            if not ruta_local:
                fallidas.append(f"item {idx}: {url[:80]}")
                continue

            try:
                doc.add_picture(ruta_local, width=Inches(4.5))
                if descripcion:
                    p = doc.add_paragraph(str(descripcion))
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                insertadas += 1
            except Exception as e:
                fallidas.append(f"item {idx}: error insertando {ruta_local}: {e}")

        # Limpiar temporales SIEMPRE
        for t in temporales_a_limpiar:
            try:
                os.unlink(t)
            except Exception:
                pass

        try:
            doc.save(ruta_archivo)
        except OSError as e:
            return False, f"File.escribir_docx: error al guardar: {e}", {
                "error": "os_error", "archivo": ruta_archivo, "detalle": str(e)
            }

        if not os.path.exists(ruta_archivo) or os.path.getsize(ruta_archivo) == 0:
            return False, f"File.escribir_docx: '{ruta_archivo}' no se creó", {
                "error": "write_failed", "archivo": ruta_archivo
            }

        tamaño = os.path.getsize(ruta_archivo)

        mensaje = f"Archivo .docx escrito: {ruta_archivo} ({tamaño} bytes)"
        if insertadas:
            mensaje += f" con {insertadas} imagen(es)"
        if fallidas:
            mensaje += f". Fallaron {len(fallidas)} imagen(es)"

        cls.actualizar_progreso(agente, 100, "Archivo .docx escrito")

        return True, mensaje, {
            "archivo": ruta_archivo,
            "ruta_absoluta": os.path.abspath(ruta_archivo),
            "tamaño": tamaño,
            "bytes_en_disco": tamaño,
            "contenido": texto[:500],
            "formato": "docx",
            "imagenes_insertadas": insertadas,
            "imagenes_fallidas": fallidas,
        }

    @classmethod
    def _file_escribir_xlsx(
        cls,
        agente: Agente,
        ruta_archivo: str,
        contenido: Any,
        contexto: dict,
        cancellation_token: CancellationToken | None = None,
    ) -> tuple[bool, str, dict]:
        try:
            import openpyxl
            from openpyxl.styles import Alignment, Font, PatternFill
        except ImportError:
            return False, (
                "openpyxl no está instalado. Instálalo con: pip install openpyxl"
            ), {"error": "missing_dependency", "dep": "openpyxl"}

        directorio = os.path.dirname(ruta_archivo)
        if directorio:
            os.makedirs(directorio, exist_ok=True)

        cls.actualizar_progreso(agente, 70, "Escribiendo .xlsx...")

        # ── autodetectar string CSV y parsearlo ──
        if isinstance(contenido, str) and _parece_csv(contenido):
            filas_csv = _parsear_csv_simple(contenido)
            if filas_csv:
                logger.info(
                    f"File.escribir_xlsx: contenido era string CSV, "
                    f"parseado a {len(filas_csv)} filas x "
                    f"{max(len(f) for f in filas_csv)} columnas"
                )
                contenido = filas_csv

        # ── NUEVO: desempaquetar dicts con clave "de datos" ──
        # El LLM a veces envuelve la lista en un dict como:
        #     {'filas': [...], 'total': N}
        # Si detectamos esa estructura, usamos directamente la lista interna
        # para que se aplique el formateo de filas/columnas correcto.
        CLAVES_DATOS = (
            'filas', 'datos', 'items', 'rows', 'data',
            'registros', 'values', 'registros_datos', 'resultados',
        )
        if isinstance(contenido, dict):
            for clave in CLAVES_DATOS:
                if clave in contenido and isinstance(contenido[clave], list):
                    logger.info(
                        f"File.escribir_xlsx: dict con clave '{clave}' "
                        f"detectado ({len(contenido[clave])} items). "
                        f"Expandiendo la lista interna."
                    )
                    contenido = contenido[clave]
                    break

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Datos"

        def _aplicar_estilo_cabecera(col_idx):
            cell = ws.cell(row=1, column=col_idx)
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill(
                start_color="4472C4", end_color="4472C4", fill_type="solid"
            )
            cell.alignment = Alignment(horizontal="center")

        if isinstance(contenido, list) and contenido:
            if isinstance(contenido[0], dict):
                # Lista de dicts → una fila por dict, cabeceras = claves
                headers = list(contenido[0].keys())
                ws.append(headers)
                for col_idx, _ in enumerate(headers, start=1):
                    _aplicar_estilo_cabecera(col_idx)
                for row in contenido:
                    if cancellation_token and cancellation_token.esta_cancelado():
                        return False, "Cancelado durante .xlsx", {
                            "error": "cancelled", "archivo": ruta_archivo
                        }
                    ws.append([_celda_segura(row.get(h, "")) for h in headers])
            elif isinstance(contenido[0], (list, tuple)):
                # Lista de listas → la primera fila son las cabeceras
                ws.append([_celda_segura(v) for v in contenido[0]])
                for col_idx, _ in enumerate(contenido[0], start=1):
                    _aplicar_estilo_cabecera(col_idx)
                for row in contenido[1:]:
                    if cancellation_token and cancellation_token.esta_cancelado():
                        return False, "Cancelado durante .xlsx", {
                            "error": "cancelled", "archivo": ruta_archivo
                        }
                    ws.append([_celda_segura(v) for v in row])
            else:
                # Lista plana de strings/números → una columna
                for item in contenido:
                    ws.append([_celda_segura(item)])
        elif isinstance(contenido, dict):
            # Dict → clave/valor en dos columnas
            for k, v in contenido.items():
                ws.append([str(k), _celda_segura(v)])
        else:
            # String plano sin comas / número / None → una celda
            ws.append([_celda_segura(contenido)])

        try:
            wb.save(ruta_archivo)
        except OSError as e:
            return False, f"File.escribir_xlsx: error al guardar '{ruta_archivo}': {e}", {
                "error": "os_error", "archivo": ruta_archivo, "detalle": str(e)
            }

        if not os.path.exists(ruta_archivo) or os.path.getsize(ruta_archivo) == 0:
            return False, f"File.escribir_xlsx: '{ruta_archivo}' no se creó o está vacío", {
                "error": "write_failed", "archivo": ruta_archivo
            }

        tamaño = os.path.getsize(ruta_archivo)
        cls.actualizar_progreso(agente, 100, "Archivo .xlsx escrito")

        return True, f"Archivo .xlsx escrito: {ruta_archivo} ({tamaño} bytes)", {
            "archivo": ruta_archivo,
            "ruta_absoluta": os.path.abspath(ruta_archivo),
            "tamaño": tamaño,
            "bytes_en_disco": tamaño,
            "formato": "xlsx",
        }

        # ── CSS embebido para WeasyPrint ──
    _PDF_CSS = """
    @page {
        size: A4;
        margin: 2cm 2cm 2cm 2cm;
        @bottom-center {
            content: counter(page) " / " counter(pages);
            font-size: 9pt;
            color: #888;
        }
    }
    body {
        font-family: "DejaVu Sans", "Liberation Sans", sans-serif;
        font-size: 11pt;
        line-height: 1.5;
        color: #222;
    }
    h1 { font-size: 22pt; margin-top: 0; border-bottom: 2px solid #333; padding-bottom: 6px; }
    h2 { font-size: 16pt; margin-top: 18pt; color: #1a1a1a; }
    h3 { font-size: 13pt; margin-top: 14pt; color: #333; }
    h4 { font-size: 11pt; margin-top: 10pt; color: #444; }
    p { margin: 6pt 0; }
    ul, ol { margin: 6pt 0 6pt 20pt; }
    li { margin: 3pt 0; }
    code {
        font-family: "DejaVu Sans Mono", monospace;
        background: #f4f4f4;
        padding: 1px 4px;
        border-radius: 3px;
        font-size: 10pt;
    }
    pre {
        background: #f4f4f4;
        padding: 8pt;
        border-radius: 4px;
        overflow-x: auto;
        font-size: 9pt;
    }
    pre code { background: none; padding: 0; }
    blockquote {
        border-left: 3px solid #ccc;
        margin-left: 0;
        padding-left: 12pt;
        color: #555;
    }
    table {
        border-collapse: collapse;
        width: 100%;
        margin: 8pt 0;
        font-size: 10pt;
    }
    th, td {
        border: 1px solid #ccc;
        padding: 5pt 8pt;
        text-align: left;
    }
    th { background: #f0f0f0; font-weight: bold; }
    img { max-width: 100%; height: auto; }
    hr { border: none; border-top: 1px solid #ddd; margin: 12pt 0; }
    """

    @classmethod
    def _file_escribir_pdf(
        cls,
        agente: Agente,
        ruta_archivo: str,
        contenido: Any,
        contexto: dict,
        cancellation_token: CancellationToken | None = None,
    ) -> tuple[bool, str, dict]:
        """
        Escribe un PDF a partir de contenido Markdown o texto.

        Pipeline: Markdown → HTML (librería `markdown`) → PDF (WeasyPrint).

        Si el contenido es un dict, se extrae el texto útil antes de
        convertirlo. Si es texto plano sin Markdown, WeasyPrint lo
        renderiza igual (los `\n` se convierten en párrafos).
        """
        # ── 1. Extraer el texto útil del contenido ──
        titulo = None
        imagenes = []
        if isinstance(contenido, dict):
            titulo = contenido.get("titulo") or contenido.get("title")
            imagenes = contenido.get("imagenes") or contenido.get("images") or []

        texto, error = cls._extraer_texto_o_fallar(contenido, ruta_archivo, "pdf")
        if error:
            cls.actualizar_progreso(agente, 100, "❌ Contrato de contenido roto")
            return False, error, {
                "error": "no_known_text_keys",
                "archivo": ruta_archivo,
            }

        if not texto.strip() and not imagenes:
            return False, (
                f"File.escribir_pdf: contenido vacío para '{ruta_archivo}'"
            ), {"error": "empty_content", "archivo": ruta_archivo}

        directorio = os.path.dirname(ruta_archivo)
        if directorio:
            os.makedirs(directorio, exist_ok=True)

        cls.actualizar_progreso(agente, 60, "Convirtiendo Markdown a HTML...")

        # ── 2. Markdown → HTML ──
        try:
            # ✅ Normalizar H1: si el contenido empieza por '## Título' y el título
            # del dict coincide, subirlo a '# Título' para que el CSS lo renderice
            # como H1 (más grande, con línea inferior).
            if titulo and isinstance(texto, str):
                texto_strip = texto.lstrip()
                match_h2 = re.match(r'^##\s+(.+?)\s*\n', texto_strip)
                if match_h2:
                    h2_texto = match_h2.group(1).strip()
                    if h2_texto.lower() == str(titulo).strip().lower():
                        texto = '# ' + h2_texto + '\n' + texto_strip[match_h2.end():]
            html_cuerpo = md_lib.markdown(
                texto,
                extensions=["extra", "tables", "fenced_code", "codehilite",
                            "sane_lists", "nl2br"],
            )
        except Exception as e:
            return False, f"File.escribir_pdf: error Markdown→HTML: {e}", {
                "error": "markdown_error", "archivo": ruta_archivo, "detalle": str(e)
            }

        # ── 3. Insertar imágenes locales o URLs en el HTML ──
        # (si vienen en `imagenes`, se añaden al final del HTML)
        if imagenes:
            partes_img = ['<h2>Imágenes</h2>']
            for img_item in imagenes:
                if isinstance(img_item, dict):
                    url = img_item.get("url") or img_item.get("src") or ""
                    desc = img_item.get("descripcion") or img_item.get("alt") or ""
                elif isinstance(img_item, str):
                    url = img_item
                    desc = ""
                else:
                    continue
                if not url:
                    continue
                # Si es un archivo local, referenciarlo por file://
                src = url
                if os.path.exists(url):
                    src = "file://" + os.path.abspath(url)
                partes_img.append(
                    f'<p><img src="{src}" alt="{desc}" />'
                    + (f'<br/><em>{desc}</em>' if desc else '')
                    + '</p>'
                )
            html_cuerpo += "\n" + "\n".join(partes_img)

        html_completo = (
            "<!DOCTYPE html><html><head><meta charset='utf-8'>"
            f"<style>{cls._PDF_CSS}</style></head><body>"
            f"{html_cuerpo}"
            "</body></html>"
        )

        cls.actualizar_progreso(agente, 80, "Renderizando PDF (WeasyPrint)...")

        # ── 4. HTML → PDF ──
        try:
            weasyprint.HTML(string=html_completo).write_pdf(ruta_archivo)
        except Exception as e:
            return False, f"File.escribir_pdf: error WeasyPrint: {e}", {
                "error": "weasyprint_error",
                "archivo": ruta_archivo,
                "detalle": str(e),
            }

        if not os.path.exists(ruta_archivo) or os.path.getsize(ruta_archivo) == 0:
            return False, (
                f"File.escribir_pdf: '{ruta_archivo}' no se creó o está vacío"
            ), {"error": "write_failed", "archivo": ruta_archivo}

        tamaño = os.path.getsize(ruta_archivo)
        cls.actualizar_progreso(agente, 100, "PDF generado")

        mensaje = f"Archivo .pdf escrito: {ruta_archivo} ({tamaño} bytes)"
        return True, mensaje, {
            "archivo": ruta_archivo,
            "ruta_absoluta": os.path.abspath(ruta_archivo),
            "tamaño": tamaño,
            "bytes_en_disco": tamaño,
            "formato": "pdf",
            "motor": "weasyprint",
            "imagenes_insertadas": len(imagenes),
        }

    @classmethod
    def _file_escribir_markdown(
        cls,
        agente: Agente,
        ruta_archivo: str,
        contenido: Any,
        contexto: dict,
        cancellation_token: CancellationToken | None = None,
    ) -> tuple[bool, str, dict]:
        """Escribe contenido como Markdown. A diferencia de 'escribir' plano,
        antepone un título como cabecera '#' si viene en un dict."""
        directorio = os.path.dirname(ruta_archivo)
        if directorio:
            os.makedirs(directorio, exist_ok=True)

        cls.actualizar_progreso(agente, 70, "Escribiendo .md...")

        titulo, contenido = cls._extraer_titulo_y_contenido(contenido)

        texto, error = cls._extraer_texto_o_fallar(contenido, ruta_archivo, "markdown")
        if error:
            cls.actualizar_progreso(agente, 100, "❌ Contrato de contenido roto")
            return False, error, {
                "error": "no_known_text_keys",
                "archivo": ruta_archivo,
            }

        if titulo:
            texto = f"# {titulo}\n\n{texto}"

        if not texto.strip():
            return False, f"File.escribir_markdown: contenido vacío para '{ruta_archivo}'", {
                "error": "empty_content", "archivo": ruta_archivo
            }

        try:
            chunk_size = 8192
            with open(ruta_archivo, "w", encoding="utf-8") as f:
                for i in range(0, len(texto), chunk_size):
                    if cancellation_token and cancellation_token.esta_cancelado():
                        return False, "Cancelado durante escritura de .md", {
                            "error": "cancelled",
                            "archivo": ruta_archivo,
                            "caracteres_escritos": i,
                        }
                    f.write(texto[i:i + chunk_size])
        except OSError as e:
            return False, f"File.escribir_markdown: error al guardar '{ruta_archivo}': {e}", {
                "error": "os_error", "archivo": ruta_archivo, "detalle": str(e)
            }

        if not os.path.exists(ruta_archivo) or os.path.getsize(ruta_archivo) == 0:
            return False, f"File.escribir_markdown: '{ruta_archivo}' no se creó o está vacío", {
                "error": "write_failed", "archivo": ruta_archivo
            }

        tamaño = os.path.getsize(ruta_archivo)
        cls.actualizar_progreso(agente, 100, "Archivo .md escrito")

        return True, f"Archivo .md escrito: {ruta_archivo} ({tamaño} bytes)", {
            "archivo": ruta_archivo,
            "ruta_absoluta": os.path.abspath(ruta_archivo),
            "tamaño": tamaño,
            "bytes_en_disco": tamaño,
            "contenido_preview": texto[:500],
            "formato": "md",
        }