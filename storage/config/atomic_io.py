"""Escritura atómica y journaling para archivos de configuración."""

import os
import json
import time
import logging
from typing import Dict, Optional

from .security import (
    ConfigError,
    ConfigIntegrityError,
    ConfigSecurityError,
    MAX_FILE_SIZE,
    SCHEMA_VERSION,
)

logger = logging.getLogger(__name__)


class AtomicWriter:
    """
    Escritura atómica de JSON con journaling opcional.

    Uso:
        writer = AtomicWriter(journal_dir="/ruta/.journal")
        writer.escribir(ruta, data, use_journal=True)
        writer.escribir_sin_restriccion(ruta_export, data)
    """

    def __init__(self, journal_dir: Optional[str] = None):
        self._journal_dir = journal_dir
        if self._journal_dir:
            try:
                os.makedirs(self._journal_dir, mode=0o750, exist_ok=True)
            except OSError:
                self._journal_dir = None  # Journal opcional

    # ============================================================
    # ESCRITURA ATÓMICA
    # ============================================================

    def escribir(self, ruta: str, data: Dict, use_journal: bool = True):
        """Escribe JSON de forma atómica con validación de ruta."""
        from .security import ruta_segura as _ruta_segura

        # Validar que la ruta esté dentro del directorio permitido
        try:
            ruta_segura_val = _ruta_segura(
                os.path.dirname(ruta), os.path.basename(ruta), allow_subdirs=True
            )
            if os.path.normpath(ruta) != os.path.normpath(ruta_segura_val):
                raise ConfigSecurityError(
                    f"Intento de escritura fuera del directorio permitido: '{ruta}'"
                )
        except ConfigSecurityError as e:
            raise ConfigError(f"Ruta de destino no segura: {ruta} - {e}")

        self._escribir_atomico(ruta, data, use_journal)

    def escribir_sin_restriccion(self, ruta: str, data: Dict, use_journal: bool = False):
        """
        Escritura atómica SIN restricción de directorio.
        Usado para exportaciones iniciadas por el usuario (QFileDialog).
        """
        self._escribir_atomico(ruta, data, use_journal)

    def _escribir_atomico(self, ruta: str, data: Dict, use_journal: bool):
        """Implementación común de escritura atómica."""
        # Asegurar directorio padre
        directorio = os.path.dirname(ruta)
        if directorio and not os.path.exists(directorio):
            try:
                os.makedirs(directorio, mode=0o750, exist_ok=True)
            except OSError as e:
                raise ConfigError(f"No se pudo crear directorio: {e}")

        ruta_tmp = f"{ruta}.tmp_{os.getpid()}_{int(time.time()*1000)}"
        try:
            with open(ruta_tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
                f.flush()
                os.fsync(f.fileno())

            os.replace(ruta_tmp, ruta)

            if use_journal:
                self._write_journal(ruta, data, 'write')

            logger.debug(f"Archivo escrito atómicamente: {ruta}")
        except Exception as e:
            if os.path.exists(ruta_tmp):
                try:
                    os.unlink(ruta_tmp)
                except Exception:
                    pass
            raise ConfigError(f"Error al escribir archivo: {e}")

    # ============================================================
    # JOURNAL
    # ============================================================

    def _write_journal(self, ruta: str, data: Dict, operation: str = 'write'):
        """Escribe una entrada en el journal para recuperación."""
        if not self._journal_dir:
            return
        try:
            basename = os.path.basename(ruta)
            journal_path = os.path.join(self._journal_dir, f"{basename}.journal")

            entry = {
                'timestamp': time.time(),
                'type': operation,
                'ruta': ruta,
                'data': data if operation == 'write' else None,
            }

            with open(journal_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, default=str) + '\n')

            self._rotate_journal(journal_path)
        except Exception as e:
            logger.warning(f"Error escribiendo journal: {e}")

    def _rotate_journal(self, journal_path: str, max_entries: int = 100):
        """Rota el journal para evitar crecimiento indefinido."""
        try:
            with open(journal_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            if len(lines) > max_entries:
                with open(journal_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines[-max_entries:])
        except Exception:
            pass

    def recover_from_journal(self, ruta: str) -> bool:
        """Intenta recuperar un archivo desde el journal."""
        if not self._journal_dir or not os.path.exists(self._journal_dir):
            return False

        basename = os.path.basename(ruta)
        journal_path = os.path.join(self._journal_dir, f"{basename}.journal")
        if not os.path.exists(journal_path):
            return False

        try:
            with open(journal_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            if not lines:
                return False

            last_entry = json.loads(lines[-1])
            if last_entry.get('type') == 'write' and 'data' in last_entry:
                with open(ruta, 'w', encoding='utf-8') as f:
                    json.dump(last_entry['data'], f, indent=2, ensure_ascii=False)
                logger.info(f"Archivo recuperado desde journal: {ruta}")
                return True
        except Exception as e:
            logger.warning(f"Error recuperando desde journal: {e}")

        return False

    def eliminar_journal(self, ruta: str):
        """Elimina el journal asociado a un archivo."""
        if not self._journal_dir:
            return
        basename = os.path.basename(ruta)
        journal_path = os.path.join(self._journal_dir, f"{basename}.journal")
        if os.path.exists(journal_path):
            try:
                os.unlink(journal_path)
            except Exception:
                pass

    def cleanup_orphaned(self, config_dir: str, max_age_seconds: int = 3600):
        """Limpia archivos temporales y journals antiguos."""
        try:
            now = time.time()

            # 1. Temporales en config_dir
            for filename in os.listdir(config_dir):
                ruta = os.path.join(config_dir, filename)
                if filename.endswith('.tmp') or '.tmp_' in filename:
                    try:
                        if now - os.path.getmtime(ruta) > max_age_seconds:
                            os.unlink(ruta)
                            logger.debug(f"Temporal eliminado: {ruta}")
                    except Exception:
                        pass

            # 2. Journals antiguos
            if self._journal_dir and os.path.exists(self._journal_dir):
                for filename in os.listdir(self._journal_dir):
                    if filename.endswith('.journal'):
                        ruta = os.path.join(self._journal_dir, filename)
                        try:
                            if now - os.path.getmtime(ruta) > max_age_seconds * 24:
                                os.unlink(ruta)
                                logger.debug(f"Journal antiguo eliminado: {ruta}")
                        except Exception:
                            pass
        except Exception as e:
            logger.warning(f"Error limpiando archivos huérfanos: {e}")


# ============================================================
# VALIDACIÓN DE INTEGRIDAD
# ============================================================

def validar_archivo_config(
    ruta: str,
    atomic_writer: Optional[AtomicWriter] = None,
    schema_version: str = SCHEMA_VERSION,
) -> Dict:
    """
    Valida la integridad de un archivo de configuración.

    Args:
        ruta: Ruta al archivo a validar
        atomic_writer: Writer para recuperación desde journal
        schema_version: Versión de schema esperada

    Returns:
        Dict: Datos del archivo validado

    Raises:
        ConfigIntegrityError: Si el archivo está corrupto o es inválido
    """
    if not os.path.exists(ruta):
        raise ConfigIntegrityError(f"Archivo no encontrado: {ruta}")

    # Verificar tamaño
    try:
        size = os.path.getsize(ruta)
        if size > MAX_FILE_SIZE:
            raise ConfigIntegrityError(
                f"Archivo demasiado grande: {size} bytes > {MAX_FILE_SIZE} límite"
            )
        if size == 0:
            raise ConfigIntegrityError("Archivo vacío")
    except OSError as e:
        raise ConfigIntegrityError(f"Error al leer tamaño del archivo: {e}")

    # Leer JSON
    try:
        with open(ruta, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        if atomic_writer:
            atomic_writer.recover_from_journal(ruta)
        raise ConfigIntegrityError(f"JSON inválido: {e}")
    except UnicodeDecodeError as e:
        raise ConfigIntegrityError(f"Encoding inválido: {e}")
    except OSError as e:
        raise ConfigIntegrityError(f"Error al leer archivo: {e}")

    if not isinstance(data, dict):
        raise ConfigIntegrityError("El archivo debe contener un objeto JSON")

    # Verificar versión de schema
    version = data.get('version', '1.0')
    if version != schema_version:
        if version.startswith('1.'):
            from .serialization import migrar_v1_a_v2
            data = migrar_v1_a_v2(data)
        else:
            raise ConfigIntegrityError(f"Versión de schema no soportada: {version}")

    if 'agentes' not in data:
        raise ConfigIntegrityError("Archivo sin lista de agentes")
    if not isinstance(data['agentes'], list):
        raise ConfigIntegrityError("'agentes' debe ser una lista")

    return data