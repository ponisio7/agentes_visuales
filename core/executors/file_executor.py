# core/executors/file_executor.py
"""Ejecutor de agentes File."""

import os
import json
import shutil
import logging
from typing import Dict, Tuple, Optional

from core.agent import Agente
from core.cancellation import CancellationToken

from .security import (
    MAX_BYTES_LECTURA_ARCHIVO, DANGEROUS_DIRS, validar_ruta_archivo
)
from .content_extractor import (
    variables_disponibles, sustituir_variables, extraer_contenido_relevante
)

logger = logging.getLogger(__name__)


class FileExecutor:
    """Operaciones con archivos: leer, escribir, copiar, mover, eliminar."""

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
        contexto: Dict,
        cancellation_token: Optional[CancellationToken] = None
    ) -> Tuple[bool, str, Dict]:
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
                with open(ruta_archivo, 'r', encoding='utf-8', errors='replace') as f:
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
                        for clave, valor in contexto.items():
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

                if isinstance(contenido, str):
                    contenido_str = contenido
                elif isinstance(contenido, (dict, list)):
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
    def _aplicar_modo_salida_file(resultado: Dict, modo: str, contenido_texto: str) -> Dict:
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