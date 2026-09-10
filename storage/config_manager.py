# storage/config_manager.py - VERSIÓN REFACTORIZADA Y SEGURA
"""
Gestor de configuraciones de agentes con persistencia segura.

Mejoras implementadas:
- ✅ Path traversal prevention robusta (con logging de redirecciones)
- ✅ Escritura atómica con journaling
- ✅ Validación de integridad de archivos
- ✅ Manejo de errores granular
- ✅ Caché de configuraciones para rendimiento
- ✅ Soporte para versionado de esquemas
- ✅ Limpieza automática de archivos huérfanos
- ✅ Logging de redirecciones de rutas
- ✅ Validación de rutas con warning
"""

import json
import os
import re
import dataclasses
import shutil
import time
import threading
import logging
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime
from pathlib import Path

from core.agent import Agente, TipoAgente

# Configurar logger
logger = logging.getLogger(__name__)


class ConfigError(Exception):
    """Excepción base para errores de configuración"""
    pass


class ConfigIntegrityError(ConfigError):
    """Error de integridad en archivo de configuración"""
    pass


class ConfigSecurityError(ConfigError):
    """Error de seguridad (path traversal, etc.)"""
    pass


class ConfigManager:
    """
    Gestor de configuraciones de agentes con persistencia segura.

    Características de seguridad:
    - Path traversal prevention
    - Escritura atómica (archivo temporal + rename)
    - Validación de integridad (schema version)
    - Límites de tamaño de archivo
    - Sanitización de nombres de archivo
    - ✅ Logging de redirecciones de rutas

    Características de rendimiento:
    - Caché LRU de configuraciones cargadas
    - Lazy loading
    - Thread-safe con RLock
    """

    # ============================================================
    # CONSTANTES DE SEGURIDAD
    # ============================================================
    MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
    MAX_CONFIGS_IN_CACHE = 20
    SCHEMA_VERSION = "2.0"
    ALLOWED_EXTENSIONS = {'.json'}
    # Caracteres permitidos en nombres de archivo (whitelist)
    SAFE_FILENAME_PATTERN = re.compile(r'^[A-Za-z0-9_\-]+$')

    def __init__(self, config_dir: str = "configs", max_cache: int = 20):
        """
        Inicializa el gestor de configuraciones.

        Args:
            config_dir: Directorio donde se almacenan las configuraciones
            max_cache: Número máximo de configuraciones en caché

        Raises:
            ConfigError: Si no se puede crear el directorio
        """
        self.config_dir = os.path.abspath(config_dir)
        self.max_cache = max_cache or self.MAX_CONFIGS_IN_CACHE
        self._config_dir_original = config_dir  # ✅ Guardar para logging

        # Crear directorio de configuraciones con permisos seguros
        try:
            os.makedirs(self.config_dir, mode=0o750, exist_ok=True)
        except OSError as e:
            raise ConfigError(f"No se pudo crear el directorio de configuraciones: {e}")

        # Caché de configuraciones cargadas (LRU simple)
        self._cache: Dict[str, Dict] = {}
        self._cache_accessed: Dict[str, float] = {}
        self._cache_lock = threading.RLock()

        # Archivos de journal para recuperación
        self._journal_dir = os.path.join(self.config_dir, ".journal")
        try:
            os.makedirs(self._journal_dir, mode=0o750, exist_ok=True)
        except OSError:
            pass  # El journal es opcional

        # Limpiar archivos temporales y journals antiguos al iniciar
        self._cleanup_orphaned_files()

        logger.info(f"ConfigManager inicializado: {self.config_dir}")

    # ============================================================
    # UTILIDADES DE SEGURIDAD
    # ============================================================

    def _sanitizar(self, nombre: str) -> str:
        """
        Convierte un nombre arbitrario en un componente de nombre de archivo seguro.
        """
        if not nombre or not isinstance(nombre, str):
            return "config_default"

        # 1. Reemplazar caracteres no permitidos por '_'
        seguro = re.sub(r'[^A-Za-z0-9_\-]+', '_', nombre.strip())

        # 2. Eliminar guiones bajos múltiples
        seguro = re.sub(r'_+', '_', seguro)

        # 3. Eliminar guiones bajos al inicio y final
        seguro = seguro.strip('_')

        # 4. Si el resultado está vacío, usar un nombre por defecto
        if not seguro:
            return "config_default"

        # 5. Limitar longitud
        if len(seguro) > 200:
            seguro = seguro[:200]

        return seguro

    def _ruta_segura(self, filename: str, allow_subdirs: bool = False) -> str:
        """
        Resuelve un nombre de archivo y verifica que esté dentro de config_dir.

        Args:
            filename: Nombre del archivo o ruta relativa
            allow_subdirs: Si se permiten subdirectorios

        Returns:
            str: Ruta absoluta segura dentro de config_dir

        Raises:
            ConfigSecurityError: Si se detecta path traversal o nombre inválido
        """
        # Validar entrada
        if not filename or not filename.strip():
            raise ConfigSecurityError("Nombre de archivo vacío")

        filename = filename.strip()

        # Verificar extensión permitida
        ext = os.path.splitext(filename)[1].lower()
        if ext and ext not in self.ALLOWED_EXTENSIONS:
            raise ConfigSecurityError(f"Extensión no permitida: '{ext}'")

        # Normalizar y verificar path traversal
        normalized = os.path.normpath(filename)

        # Detectar intentos de salir del directorio
        if normalized.startswith('..') or os.path.isabs(normalized):
            raise ConfigSecurityError(f"Path traversal detectado: '{filename}'")

        # Si no se permiten subdirectorios, verificar que no haya path separators
        if not allow_subdirs and (os.path.sep in normalized or os.path.altsep and os.path.altsep in normalized):
            raise ConfigSecurityError(f"Subdirectorios no permitidos: '{filename}'")

        # Construir ruta absoluta
        ruta = os.path.realpath(os.path.join(self.config_dir, normalized))

        # Verificar que la ruta resultante está dentro de config_dir
        if not ruta.startswith(self.config_dir + os.sep) and ruta != self.config_dir:
            raise ConfigSecurityError(f"Ruta fuera del directorio de configuraciones: '{filename}'")

        return ruta

    # ============================================================
    # ESCRITURA ATÓMICA (CORREGIDA)
    # ============================================================

    def _escribir_json_atomico(self, ruta: str, data: Dict, use_journal: bool = True):
        """
        Escribe un archivo JSON de forma atómica usando un archivo temporal.
        """
        # ✅ CORREGIDO: Lanzar error en lugar de redirigir silenciosamente
        try:
            ruta_segura = self._ruta_segura(os.path.basename(ruta), allow_subdirs=True)
            if os.path.normpath(ruta) != os.path.normpath(ruta_segura):
                raise ConfigSecurityError(f"Intento de escritura fuera del directorio permitido: '{ruta}'")
        except ConfigSecurityError as e:
            raise ConfigError(f"Ruta de destino no segura: {ruta} - {e}")

        # Asegurar que el directorio padre exista
        directorio = os.path.dirname(ruta)
        if directorio and not os.path.exists(directorio):
            try:
                os.makedirs(directorio, mode=0o750, exist_ok=True)
            except OSError as e:
                raise ConfigError(f"No se pudo crear directorio: {e}")

        # Archivo temporal en el mismo directorio
        ruta_tmp = f"{ruta}.tmp_{os.getpid()}_{int(time.time()*1000)}"
        try:
            # ✅ TODO el manejo del archivo DENTRO del bloque with
            with open(ruta_tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
                f.flush()
                os.fsync(f.fileno())
            
            os.replace(ruta_tmp, ruta)
            
            if use_journal:
                self._write_journal(ruta, data, 'write')
                
            self._invalidate_cache(ruta)
            logger.debug(f"Archivo escrito atómicamente: {ruta}")
        except Exception as e:
            if os.path.exists(ruta_tmp):
                try:
                    os.unlink(ruta_tmp)
                except:
                    pass
            raise ConfigError(f"Error al escribir archivo: {e}")

    def _validar_archivo_config(self, ruta: str) -> Dict:
        """
        Valida la integridad de un archivo de configuración.

        Args:
            ruta: Ruta al archivo a validar

        Returns:
            Dict: Datos del archivo validado

        Raises:
            ConfigIntegrityError: Si el archivo está corrupto o es inválido
        """
        # Verificar que el archivo existe
        if not os.path.exists(ruta):
            raise ConfigIntegrityError(f"Archivo no encontrado: {ruta}")

        # Verificar tamaño
        try:
            size = os.path.getsize(ruta)
            if size > self.MAX_FILE_SIZE:
                raise ConfigIntegrityError(
                    f"Archivo demasiado grande: {size} bytes > {self.MAX_FILE_SIZE} límite"
                )
            if size == 0:
                raise ConfigIntegrityError("Archivo vacío")
        except OSError as e:
            raise ConfigIntegrityError(f"Error al leer tamaño del archivo: {e}")

        # Leer y validar JSON
        try:
            with open(ruta, 'r', encoding='utf-8') as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            # Intentar recuperar de journal
            self._recover_from_journal(ruta)
            # Si no se pudo recuperar, lanzar error
            raise ConfigIntegrityError(f"JSON inválido: {e}")
        except UnicodeDecodeError as e:
            raise ConfigIntegrityError(f"Encoding inválido: {e}")
        except OSError as e:
            raise ConfigIntegrityError(f"Error al leer archivo: {e}")

        # Validar estructura mínima
        if not isinstance(data, dict):
            raise ConfigIntegrityError("El archivo debe contener un objeto JSON")

        # Verificar versión de schema
        schema_version = data.get('version', '1.0')
        if schema_version != self.SCHEMA_VERSION:
            # Intentar migrar si es versión anterior
            if schema_version.startswith('1.'):
                data = self._migrar_v1_a_v2(data)
            else:
                raise ConfigIntegrityError(
                    f"Versión de schema no soportada: {schema_version}"
                )

        # Verificar que tiene agentes
        if 'agentes' not in data:
            raise ConfigIntegrityError("Archivo sin lista de agentes")

        if not isinstance(data['agentes'], list):
            raise ConfigIntegrityError("'agentes' debe ser una lista")

        return data

    def _migrar_v1_a_v2(self, data: Dict) -> Dict:
        """
        Migra una configuración de versión 1.x a 2.0.
        """
        # Añadir versión
        data['version'] = '2.0'

        # Asegurar que cada agente tenga los campos necesarios
        for agente in data.get('agentes', []):
            # Añadir campo continuar_en_error si no existe
            if 'continuar_en_error' not in agente:
                agente['continuar_en_error'] = False
            # Asegurar que timeout_loop existe
            if 'timeout_loop' not in agente:
                agente['timeout_loop'] = 300

        return data

    # ============================================================
    # RECUPERACIÓN DE ARCHIVOS
    # ============================================================

    def _recover_from_journal(self, ruta: str) -> bool:
        """
        Intenta recuperar un archivo desde el journal.

        Args:
            ruta: Ruta del archivo corrupto

        Returns:
            bool: True si se recuperó exitosamente
        """
        if not self._journal_dir or not os.path.exists(self._journal_dir):
            return False

        basename = os.path.basename(ruta)
        journal_path = os.path.join(self._journal_dir, f"{basename}.journal")

        if not os.path.exists(journal_path):
            return False

        try:
            # Leer última entrada del journal
            with open(journal_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            if not lines:
                return False

            # La última línea es la más reciente
            last_entry = json.loads(lines[-1])
            if last_entry.get('type') == 'write' and 'data' in last_entry:
                # Restaurar desde journal
                with open(ruta, 'w', encoding='utf-8') as f:
                    json.dump(last_entry['data'], f, indent=2, ensure_ascii=False)
                logger.info(f"Archivo recuperado desde journal: {ruta}")
                return True

        except Exception as e:
            logger.warning(f"Error recuperando desde journal: {e}")

        return False

    def _write_journal(self, ruta: str, data: Dict, operation: str = 'write'):
        """
        Escribe una entrada en el journal para recuperación.

        Args:
            ruta: Ruta del archivo
            data: Datos escritos
            operation: Tipo de operación ('write', 'delete')
        """
        if not self._journal_dir:
            return

        try:
            basename = os.path.basename(ruta)
            journal_path = os.path.join(self._journal_dir, f"{basename}.journal")

            entry = {
                'timestamp': time.time(),
                'type': operation,
                'ruta': ruta,
                'data': data if operation == 'write' else None
            }

            with open(journal_path, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, default=str) + '\n')

            # Limitar tamaño del journal
            self._rotate_journal(journal_path)

        except Exception as e:
            logger.warning(f"Error escribiendo journal: {e}")

    def _rotate_journal(self, journal_path: str, max_entries: int = 100):
        """
        Rota el journal para evitar que crezca indefinidamente.

        Args:
            journal_path: Ruta del journal
            max_entries: Número máximo de entradas
        """
        try:
            with open(journal_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            if len(lines) > max_entries:
                # Mantener solo las últimas max_entries
                with open(journal_path, 'w', encoding='utf-8') as f:
                    f.writelines(lines[-max_entries:])
        except Exception:
            pass

    # ============================================================
    # CACHÉ
    # ============================================================

    def _get_cache_key(self, ruta: str) -> str:
        """Genera una clave de caché a partir de la ruta."""
        return os.path.normpath(ruta)

    def _invalidate_cache(self, ruta: str):
        """Invalidar una entrada de caché."""
        with self._cache_lock:
            key = self._get_cache_key(ruta)
            self._cache.pop(key, None)
            self._cache_accessed.pop(key, None)

    def _get_from_cache(self, ruta: str) -> Optional[Dict]:
        """Obtener datos de la caché."""
        with self._cache_lock:
            key = self._get_cache_key(ruta)
            if key in self._cache:
                self._cache_accessed[key] = time.time()
                return self._cache[key]
            return None

    def _put_in_cache(self, ruta: str, data: Dict):
        """Guardar datos en la caché."""
        with self._cache_lock:
            key = self._get_cache_key(ruta)

            # Si la caché está llena, eliminar el elemento menos recientemente usado
            if len(self._cache) >= self.max_cache:
                oldest = min(self._cache_accessed, key=self._cache_accessed.get)
                self._cache.pop(oldest, None)
                self._cache_accessed.pop(oldest, None)

            self._cache[key] = data
            self._cache_accessed[key] = time.time()

    # ============================================================
    # LIMPIEZA DE ARCHIVOS HUÉRFANOS
    # ============================================================

    def _cleanup_orphaned_files(self, max_age_seconds: int = 3600):
        """
        Limpia archivos temporales y journals antiguos.
        """
        try:
            now = time.time()
            
            # 1. Limpiar archivos temporales en el directorio principal
            for filename in os.listdir(self.config_dir):
                ruta = os.path.join(self.config_dir, filename)
                if filename.endswith('.tmp') or '.tmp_' in filename:
                    try:
                        mtime = os.path.getmtime(ruta)
                        if now - mtime > max_age_seconds:
                            os.unlink(ruta)
                            logger.debug(f"Archivo temporal eliminado: {ruta}")
                    except Exception:
                        pass
            
            # 2. ✅ CORREGIDO: Limpiar journals antiguos DENTRO del directorio .journal
            if self._journal_dir and os.path.exists(self._journal_dir):
                for filename in os.listdir(self._journal_dir):
                    if filename.endswith('.journal'):
                        ruta = os.path.join(self._journal_dir, filename)
                        try:
                            mtime = os.path.getmtime(ruta)
                            if now - mtime > max_age_seconds * 24:  # 24 horas
                                os.unlink(ruta)
                                logger.debug(f"Journal antiguo eliminado: {ruta}")
                        except Exception:
                            pass
                            
        except Exception as e:
            logger.warning(f"Error limpiando archivos huérfanos: {e}")

    # ============================================================
    # OPERACIONES PRINCIPALES
    # ============================================================

    def guardar(self, agentes: Dict[str, Agente], nombre: str, descripcion: str = "") -> str:
        """
        Guarda la configuración completa de agentes.

        Args:
            agentes: Diccionario de agentes por ID
            nombre: Nombre de la configuración
            descripcion: Descripción opcional

        Returns:
            str: Ruta del archivo guardado

        Raises:
            ConfigError: Si falla el guardado
        """
        if not agentes:
            raise ConfigError("No hay agentes para guardar")

        # Validar nombre
        nombre = nombre.strip()
        if not nombre:
            raise ConfigError("El nombre de configuración es obligatorio")

        # Construir datos
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nombre_seguro = self._sanitizar(nombre)

        data = {
            "version": self.SCHEMA_VERSION,
            "nombre": nombre,
            "descripcion": descripcion,
            "fecha_creacion": timestamp,
            "total_agentes": len(agentes),
            "agentes": [self._agente_a_dict(a) for a in agentes.values()],
        }

        # Generar nombre de archivo (incluyendo timestamp para unicidad)
        timestamp_file = timestamp.replace(' ', '_').replace(':', '-')
        filename = f"{nombre_seguro}_{timestamp_file}.json"
        ruta = os.path.join(self.config_dir, filename)

        # Guardar atómicamente
        self._escribir_json_atomico(ruta, data)

        logger.info(f"Configuración guardada: {ruta}")
        return ruta

    def cargar(self, ruta: str) -> List[Dict]:
        """
        Carga una configuración desde un archivo.

        Args:
            ruta: Ruta al archivo de configuración

        Returns:
            List[Dict]: Lista de agentes serializados

        Raises:
            ConfigError: Si no se puede cargar
        """
        # Verificar caché
        cached = self._get_from_cache(ruta)
        if cached is not None:
            return cached.get('agentes', [])

        # Validar y cargar
        try:
            data = self._validar_archivo_config(ruta)
            agentes = data.get('agentes', [])

            # Guardar en caché
            self._put_in_cache(ruta, data)

            logger.debug(f"Configuración cargada: {ruta}")
            return agentes

        except ConfigIntegrityError as e:
            raise ConfigError(f"Error de integridad en {ruta}: {e}")
        except Exception as e:
            raise ConfigError(f"Error al cargar {ruta}: {e}")

    def cargar_por_nombre(self, nombre: str) -> Optional[List[Dict]]:
        """
        Carga la configuración más reciente con ese nombre.

        Args:
            nombre: Nombre de la configuración

        Returns:
            Optional[List[Dict]]: Agentes serializados o None si no se encuentra
        """
        nombre_seguro = self._sanitizar(nombre)
        archivos = self.listar_configuraciones()

        # Buscar archivos que comiencen con el nombre
        prefijo = f"{nombre_seguro}_"
        matching = [f for f in archivos if f.startswith(prefijo) and f.endswith('.json')]

        if not matching:
            return None

        # Ordenar por fecha (más reciente primero)
        # El timestamp está en el nombre después del prefijo
        matching.sort(reverse=True)

        try:
            ruta = os.path.join(self.config_dir, matching[0])
            return self.cargar(ruta)
        except ConfigError:
            return None

    def listar_configuraciones(self) -> List[str]:
        """
        Lista todas las configuraciones guardadas.

        Returns:
            List[str]: Nombres de archivo de configuraciones válidas
        """
        configs = []
        try:
            for filename in os.listdir(self.config_dir):
                # Solo archivos JSON, excluyendo temporales y journals
                if (filename.endswith('.json') and
                    not filename.endswith('.tmp') and
                    not filename.startswith('.')):
                    ruta = os.path.join(self.config_dir, filename)
                    if os.path.isfile(ruta):
                        configs.append(filename)
        except OSError as e:
            logger.warning(f"Error listando configuraciones: {e}")

        return sorted(configs)

    def listar_detalles(self) -> List[Dict]:
        """
        Lista configuraciones con metadatos y estadísticas.

        Returns:
            List[Dict]: Detalles de cada configuración
        """
        detalles = []

        for filename in self.listar_configuraciones():
            ruta = os.path.join(self.config_dir, filename)
            try:
                # Intentar cargar desde caché primero
                data = self._get_from_cache(ruta)
                if data is None:
                    data = self._validar_archivo_config(ruta)
                    self._put_in_cache(ruta, data)

                agentes = data.get('agentes', [])

                # Estadísticas por tipo
                tipos = {}
                for agente in agentes:
                    tipo = agente.get('tipo', 'Desconocido')
                    tipos[tipo] = tipos.get(tipo, 0) + 1

                # Contar loops
                loops = sum(1 for a in agentes if a.get('tipo') == 'Loop')

                detalles.append({
                    "archivo": filename,
                    "nombre": data.get("nombre", filename.replace('.json', '')),
                    "descripcion": data.get("descripcion", ""),
                    "fecha": data.get("fecha_creacion", ""),
                    "total_agentes": data.get("total_agentes", len(agentes)),
                    "tipos": tipos,
                    "loops": loops,
                    "ruta": ruta,
                    "version": data.get("version", "1.0")
                })

            except (ConfigIntegrityError, ConfigError) as e:
                # Archivo corrupto, pero listarlo igual con advertencia
                logger.warning(f"Archivo corrupto: {filename} - {e}")
                detalles.append({
                    "archivo": filename,
                    "nombre": filename.replace('.json', '') + " ⚠️",
                    "descripcion": f"Corrupto: {str(e)}",
                    "fecha": "",
                    "total_agentes": 0,
                    "tipos": {},
                    "loops": 0,
                    "ruta": ruta,
                    "version": "unknown",
                    "corrupto": True
                })
            except Exception as e:
                logger.error(f"Error procesando {filename}: {e}")
                detalles.append({
                    "archivo": filename,
                    "nombre": filename.replace('.json', '') + " ❌",
                    "descripcion": f"Error: {str(e)}",
                    "fecha": "",
                    "total_agentes": 0,
                    "tipos": {},
                    "loops": 0,
                    "ruta": ruta,
                    "version": "unknown",
                    "corrupto": True
                })

        return detalles

    def eliminar(self, nombre_archivo: str) -> bool:
        """
        Elimina un archivo de configuración.

        Args:
            nombre_archivo: Nombre del archivo a eliminar

        Returns:
            bool: True si se eliminó correctamente
        """
        try:
            # Verificar que el archivo existe y está dentro de config_dir
            ruta = self._ruta_segura(nombre_archivo, allow_subdirs=True)

            if not os.path.exists(ruta):
                return False

            if not os.path.isfile(ruta):
                return False

            # Escribir journal de eliminación
            self._write_journal(ruta, {'deleted': True}, 'delete')

            # Eliminar archivo
            os.unlink(ruta)

            # Invalidar caché
            self._invalidate_cache(ruta)

            # Eliminar journal asociado
            if self._journal_dir:
                basename = os.path.basename(ruta)
                journal_path = os.path.join(self._journal_dir, f"{basename}.journal")
                if os.path.exists(journal_path):
                    try:
                        os.unlink(journal_path)
                    except:
                        pass

            logger.info(f"Configuración eliminada: {ruta}")
            return True

        except (ConfigSecurityError, OSError) as e:
            logger.warning(f"Error eliminando {nombre_archivo}: {e}")
            return False

    def eliminar_vieja(self, nombre: str, keep_last: int = 5) -> int:
        """
        Elimina configuraciones antiguas de un mismo nombre,
        manteniendo solo las más recientes.

        Args:
            nombre: Nombre base de la configuración
            keep_last: Número de versiones a mantener

        Returns:
            int: Número de archivos eliminados
        """
        nombre_seguro = self._sanitizar(nombre)
        prefijo = f"{nombre_seguro}_"

        archivos = [f for f in self.listar_configuraciones()
                    if f.startswith(prefijo) and f.endswith('.json')]

        if len(archivos) <= keep_last:
            return 0

        # Ordenar por fecha (asumiendo que el timestamp está en el nombre)
        archivos.sort(reverse=True)

        eliminados = 0
        for archivo in archivos[keep_last:]:
            if self.eliminar(archivo):
                eliminados += 1

        if eliminados > 0:
            logger.info(f"Eliminadas {eliminados} configuraciones antiguas de '{nombre}'")

        return eliminados

    # ============================================================
    # EXPORTAR / IMPORTAR
    # ============================================================

    # storage/config_manager.py - MÉTODOS EXPORTAR/IMPORTAR CORREGIDOS

    def exportar_a_json(self, agentes: Dict[str, Agente], ruta: str) -> str:
        """
        Exporta agentes a un archivo JSON para compartir.
        
        ✅ MODIFICADO: Permite rutas fuera de config_dir (seleccionadas por el usuario)
        """
        if not agentes:
            raise ConfigError("No hay agentes para exportar")
        
        data = {
            "version": self.SCHEMA_VERSION,
            "exportado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_agentes": len(agentes),
            "agentes": [self._agente_a_dict(a) for a in agentes.values()],
        }
        
        # ✅ ELIMINADA la restricción de directorio
        # El usuario ha seleccionado esta ruta con QFileDialog, confiamos en ella
        
        # Validar extensión (solo advertencia, no bloqueo)
        ext = os.path.splitext(ruta)[1].lower()
        if ext and ext not in self.ALLOWED_EXTENSIONS:
            # Solo advertir, pero permitir
            logger.warning(f"Extensión no estándar para JSON: '{ext}' en {ruta}")
        
        # Crear directorio si no existe
        directorio = os.path.dirname(ruta)
        if directorio and not os.path.exists(directorio):
            try:
                os.makedirs(directorio, mode=0o750, exist_ok=True)
            except OSError as e:
                raise ConfigError(f"No se pudo crear directorio: {e}")
        
        # Escribir (sin journal para exportaciones)
        self._escribir_json_atomico_sin_restriccion(ruta, data, use_journal=False)
        logger.info(f"Exportación completada: {ruta}")
        return ruta

    def importar_desde_json(self, ruta: str, validar_agentes: bool = True) -> List[Dict]:
        """
        Importa agentes desde un archivo JSON seleccionado por el usuario.
        
        ✅ MODIFICADO: Permite rutas fuera de config_dir (seleccionadas por el usuario)
        """
        # ── Validar que el archivo existe ──
        if not os.path.exists(ruta):
            raise ConfigError(f"Archivo no encontrado: {ruta}")
        
        if not os.path.isfile(ruta):
            raise ConfigError(f"La ruta no es un archivo: {ruta}")
        
        # ✅ ELIMINADA la restricción de directorio
        # El usuario ha seleccionado esta ruta con QFileDialog, confiamos en ella
        
        # Validar extensión (solo advertencia, no bloqueo)
        ext = os.path.splitext(ruta)[1].lower()
        if ext and ext not in self.ALLOWED_EXTENSIONS:
            logger.warning(f"Extensión no estándar para JSON: '{ext}' en {ruta}")
        
        # Validar integridad del archivo
        try:
            data = self._validar_archivo_config(ruta)
        except ConfigIntegrityError as e:
            raise ConfigError(f"Archivo inválido o corrupto: {e}")
        
        agentes = data.get('agentes', [])
        
        if validar_agentes:
            for i, agente in enumerate(agentes):
                if not isinstance(agente, dict):
                    raise ConfigError(f"Agente {i} no es un diccionario")
                if 'nombre' not in agente:
                    raise ConfigError(f"Agente {i} sin nombre")
                if 'tipo' not in agente:
                    raise ConfigError(f"Agente {i} sin tipo")
        
        logger.info(f"Importación completada: {len(agentes)} agentes desde {ruta}")
        return agentes

    def _escribir_json_atomico_sin_restriccion(self, ruta: str, data: Dict, use_journal: bool = True):
        """
        Escribe un archivo JSON de forma atómica SIN restricción de directorio.
        Usado exclusivamente para exportaciones iniciadas por el usuario.
        """
        # Asegurar que el directorio padre exista
        directorio = os.path.dirname(ruta)
        if directorio and not os.path.exists(directorio):
            try:
                os.makedirs(directorio, mode=0o750, exist_ok=True)
            except OSError as e:
                raise ConfigError(f"No se pudo crear directorio: {e}")

        # Archivo temporal en el mismo directorio
        ruta_tmp = f"{ruta}.tmp_{os.getpid()}_{int(time.time()*1000)}"
        try:
            with open(ruta_tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
                f.flush()
                os.fsync(f.fileno())
            
            os.replace(ruta_tmp, ruta)
            
            if use_journal:
                self._write_journal(ruta, data, 'write')
            
            # Invalidar caché solo si la ruta está dentro de config_dir
            try:
                if ruta.startswith(self.config_dir):
                    self._invalidate_cache(ruta)
            except Exception:
                pass
            
            logger.debug(f"Archivo escrito atómicamente: {ruta}")
        except Exception as e:
            if os.path.exists(ruta_tmp):
                try:
                    os.unlink(ruta_tmp)
                except:
                    pass
            raise ConfigError(f"Error al escribir archivo: {e}")

    # ============================================================
    # MÉTODOS PARA LOOP
    # ============================================================

    def listar_loops(self) -> List[Dict]:
        """
        Lista todas las configuraciones que contienen agentes LOOP.

        Returns:
            List[Dict]: Configuraciones con loops
        """
        loops = []

        for filename in self.listar_configuraciones():
            ruta = os.path.join(self.config_dir, filename)

            try:
                data = self._validar_archivo_config(ruta)
                agentes = data.get('agentes', [])

                agentes_loop = [a for a in agentes if a.get('tipo') == 'Loop']

                if agentes_loop:
                    loops.append({
                        "archivo": filename,
                        "nombre": data.get("nombre", filename),
                        "total_loops": len(agentes_loop),
                        "loops": [{
                            "nombre": a.get('nombre', 'Sin nombre'),
                            "fuente_items": a.get('fuente_items', ''),
                            "max_iteraciones": a.get('max_iteraciones', 100),
                            "continuar_en_error": a.get('continuar_en_error', False)
                        } for a in agentes_loop],
                        "ruta": ruta
                    })
            except Exception as e:
                logger.warning(f"Error procesando {filename} para loops: {e}")
                continue

        return loops

    def validar_configuracion_loop(self, agente_dict: Dict) -> Tuple[bool, str]:
        """
        Valida la configuración de un agente LOOP antes de guardar.

        Args:
            agente_dict: Diccionario del agente

        Returns:
            Tuple[bool, str]: (es_válido, mensaje_error)
        """
        if agente_dict.get('tipo') != 'Loop':
            return True, "No es un agente LOOP"

        # Validar fuente_items
        fuente = agente_dict.get('fuente_items', '').strip()
        if not fuente:
            return False, "Loop: 'fuente_items' es obligatorio"

        if '.' not in fuente:
            return False, f"Loop: 'fuente_items' debe tener formato 'Dependencia.clave' (actual: '{fuente}')"

        # Validar código
        codigo = agente_dict.get('codigo_por_item', '').strip()
        if not codigo:
            return False, "Loop: 'codigo_por_item' es obligatorio"

        # Validar límites
        max_iter = agente_dict.get('max_iteraciones', 0)
        if max_iter < 1:
            return False, f"Loop: 'max_iteraciones' debe ser >= 1 (actual: {max_iter})"

        timeout = agente_dict.get('timeout_loop', 0)
        if timeout < 1:
            return False, f"Loop: 'timeout_loop' debe ser >= 1 (actual: {timeout})"

        return True, ""

    # ============================================================
    # SERIALIZACIÓN
    # ============================================================

    @staticmethod
    def _agente_a_dict(agente: Agente) -> Dict:
        """
        Serializa un Agente completo, excluyendo campos de runtime.

        Args:
            agente: Instancia de Agente

        Returns:
            Dict: Datos serializados del agente
        """
        agente_dict = dataclasses.asdict(agente)

        # Convertir TipoAgente a string
        if isinstance(agente_dict.get('tipo'), TipoAgente):
            agente_dict['tipo'] = agente_dict['tipo'].value
        elif hasattr(agente.tipo, 'value'):
            agente_dict['tipo'] = agente.tipo.value

        # Excluir campos de runtime
        for campo in Agente.CAMPOS_RUNTIME:
            agente_dict.pop(campo, None)

        return agente_dict

    # ============================================================
    # UTILIDADES ADICIONALES
    # ============================================================

    def obtener_ruta_absoluta(self, nombre_archivo: str) -> Optional[str]:
        """
        Obtiene la ruta absoluta de un archivo de configuración.

        Args:
            nombre_archivo: Nombre del archivo

        Returns:
            Optional[str]: Ruta absoluta o None si no existe
        """
        try:
            ruta = self._ruta_segura(nombre_archivo, allow_subdirs=True)
            if os.path.exists(ruta) and os.path.isfile(ruta):
                return ruta
        except ConfigSecurityError:
            pass
        return None

    def existe(self, nombre_archivo: str) -> bool:
        """
        Verifica si un archivo de configuración existe.

        Args:
            nombre_archivo: Nombre del archivo

        Returns:
            bool: True si existe
        """
        try:
            ruta = self._ruta_segura(nombre_archivo, allow_subdirs=True)
            return os.path.exists(ruta) and os.path.isfile(ruta)
        except ConfigSecurityError:
            return False

    def obtener_tamano(self, nombre_archivo: str) -> Optional[int]:
        """
        Obtiene el tamaño de un archivo de configuración.

        Args:
            nombre_archivo: Nombre del archivo

        Returns:
            Optional[int]: Tamaño en bytes o None si no existe
        """
        try:
            ruta = self._ruta_segura(nombre_archivo, allow_subdirs=True)
            if os.path.exists(ruta) and os.path.isfile(ruta):
                return os.path.getsize(ruta)
        except (ConfigSecurityError, OSError):
            pass
        return None

    def limpiar_cache(self):
        """Limpia toda la caché de configuraciones."""
        with self._cache_lock:
            self._cache.clear()
            self._cache_accessed.clear()
            logger.debug("Caché de configuraciones limpiada")

    def obtener_info(self) -> Dict:
        """
        Obtiene información sobre el gestor de configuraciones.

        Returns:
            Dict: Información del gestor
        """
        return {
            "config_dir": self.config_dir,
            "cache_size": len(self._cache),
            "max_cache": self.max_cache,
            "total_configs": len(self.listar_configuraciones()),
            "schema_version": self.SCHEMA_VERSION,
            "journal_enabled": self._journal_dir is not None
        }

    def __repr__(self) -> str:
        return f"ConfigManager(config_dir='{self.config_dir}', cache_size={len(self._cache)})"