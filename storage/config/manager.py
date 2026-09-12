"""ConfigManager: fachada principal del gestor de configuraciones."""

import os
import logging
from typing import Dict, List, Optional, Tuple, Any
from datetime import datetime

from core.agent import Agente

from .security import (
    ConfigError,
    ConfigIntegrityError,
    ConfigSecurityError,
    SCHEMA_VERSION,
    sanitizar,
    ruta_segura,
)
from .atomic_io import AtomicWriter, validar_archivo_config
from .cache import ConfigCache
from .serialization import agente_a_dict

logger = logging.getLogger(__name__)


class ConfigManager:
    """
    Gestor de configuraciones de agentes con persistencia segura.

    Compone:
    - AtomicWriter: escritura atómica + journaling
    - ConfigCache: caché LRU thread-safe
    - security: validación de rutas y sanitización
    - serialization: conversión de agentes

    Características de seguridad:
    - Path traversal prevention
    - Escritura atómica (archivo temporal + rename)
    - Validación de integridad (schema version)
    - Límites de tamaño de archivo
    - Sanitización de nombres de archivo
    - Logging de redirecciones de rutas

    Características de rendimiento:
    - Caché LRU de configuraciones cargadas
    - Lazy loading
    - Thread-safe
    """

    MAX_CONFIGS_IN_CACHE = 20
    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, config_dir: str = "configs", max_cache: int = 20):
        self.config_dir = os.path.abspath(config_dir)
        self._config_dir_original = config_dir

        # Crear directorio con permisos seguros
        try:
            os.makedirs(self.config_dir, mode=0o750, exist_ok=True)
        except OSError as e:
            raise ConfigError(f"No se pudo crear el directorio de configuraciones: {e}")

        # Journal dir
        journal_dir = os.path.join(self.config_dir, ".journal")
        self._writer = AtomicWriter(journal_dir=journal_dir)
        self._journal_dir = self._writer._journal_dir

        # Caché
        self._cache = ConfigCache(max_size=max_cache or self.MAX_CONFIGS_IN_CACHE)

        # Limpieza inicial
        self._writer.cleanup_orphaned(self.config_dir)

        logger.info(f"ConfigManager inicializado: {self.config_dir}")

    # ============================================================
    # UTILIDADES DE SEGURIDAD (delegadas)
    # ============================================================

    def _sanitizar(self, nombre: str) -> str:
        return sanitizar(nombre)

    def _ruta_segura(self, filename: str, allow_subdirs: bool = False) -> str:
        return ruta_segura(self.config_dir, filename, allow_subdirs)

    # ============================================================
    # OPERACIONES PRINCIPALES
    # ============================================================

    def guardar(self, agentes: Dict[str, Agente], nombre: str, descripcion: str = "") -> str:
        """Guarda la configuración completa de agentes."""
        if not agentes:
            raise ConfigError("No hay agentes para guardar")

        nombre = nombre.strip()
        if not nombre:
            raise ConfigError("El nombre de configuración es obligatorio")

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        nombre_seguro = self._sanitizar(nombre)

        data = {
            "version": self.SCHEMA_VERSION,
            "nombre": nombre,
            "descripcion": descripcion,
            "fecha_creacion": timestamp,
            "total_agentes": len(agentes),
            "agentes": [agente_a_dict(a) for a in agentes.values()],
        }

        timestamp_file = timestamp.replace(' ', '_').replace(':', '-')
        filename = f"{nombre_seguro}_{timestamp_file}.json"
        ruta = os.path.join(self.config_dir, filename)

        self._writer.escribir(ruta, data)
        self._cache.invalidate(ruta)

        logger.info(f"Configuración guardada: {ruta}")
        return ruta

    def cargar(self, ruta: str) -> List[Dict]:
        """Carga una configuración desde un archivo."""
        cached = self._cache.get(ruta)
        if cached is not None:
            return cached.get('agentes', [])

        try:
            data = validar_archivo_config(ruta, self._writer, self.SCHEMA_VERSION)
            agentes = data.get('agentes', [])
            self._cache.put(ruta, data)
            logger.debug(f"Configuración cargada: {ruta}")
            return agentes
        except ConfigIntegrityError as e:
            raise ConfigError(f"Error de integridad en {ruta}: {e}")
        except Exception as e:
            raise ConfigError(f"Error al cargar {ruta}: {e}")

    def cargar_por_nombre(self, nombre: str) -> Optional[List[Dict]]:
        """Carga la configuración más reciente con ese nombre."""
        nombre_seguro = self._sanitizar(nombre)
        archivos = self.listar_configuraciones()
        prefijo = f"{nombre_seguro}_"
        matching = [f for f in archivos if f.startswith(prefijo) and f.endswith('.json')]

        if not matching:
            return None

        matching.sort(reverse=True)

        try:
            ruta = os.path.join(self.config_dir, matching[0])
            return self.cargar(ruta)
        except ConfigError:
            return None

    def listar_configuraciones(self) -> List[str]:
        """Lista todas las configuraciones guardadas."""
        configs = []
        try:
            for filename in os.listdir(self.config_dir):
                if (filename.endswith('.json')
                        and not filename.endswith('.tmp')
                        and not filename.startswith('.')):
                    ruta = os.path.join(self.config_dir, filename)
                    if os.path.isfile(ruta):
                        configs.append(filename)
        except OSError as e:
            logger.warning(f"Error listando configuraciones: {e}")
        return sorted(configs)

    def listar_detalles(self) -> List[Dict]:
        """Lista configuraciones con metadatos y estadísticas."""
        detalles = []

        for filename in self.listar_configuraciones():
            ruta = os.path.join(self.config_dir, filename)
            try:
                data = self._cache.get(ruta)
                if data is None:
                    data = validar_archivo_config(ruta, self._writer, self.SCHEMA_VERSION)
                    self._cache.put(ruta, data)

                agentes = data.get('agentes', [])

                tipos: Dict[str, int] = {}
                for agente in agentes:
                    tipo = agente.get('tipo', 'Desconocido')
                    tipos[tipo] = tipos.get(tipo, 0) + 1

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
                    "version": data.get("version", "1.0"),
                })
            except (ConfigIntegrityError, ConfigError) as e:
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
                    "corrupto": True,
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
                    "corrupto": True,
                })

        return detalles

    def eliminar(self, nombre_archivo: str) -> bool:
        """Elimina un archivo de configuración."""
        try:
            ruta = self._ruta_segura(nombre_archivo, allow_subdirs=True)
            if not os.path.exists(ruta) or not os.path.isfile(ruta):
                return False

            self._writer._write_journal(ruta, {'deleted': True}, 'delete')
            os.unlink(ruta)
            self._cache.invalidate(ruta)
            self._writer.eliminar_journal(ruta)

            logger.info(f"Configuración eliminada: {ruta}")
            return True
        except (ConfigSecurityError, OSError) as e:
            logger.warning(f"Error eliminando {nombre_archivo}: {e}")
            return False

    def eliminar_vieja(self, nombre: str, keep_last: int = 5) -> int:
        """Elimina configuraciones antiguas manteniendo las más recientes."""
        nombre_seguro = self._sanitizar(nombre)
        prefijo = f"{nombre_seguro}_"
        archivos = [f for f in self.listar_configuraciones()
                    if f.startswith(prefijo) and f.endswith('.json')]

        if len(archivos) <= keep_last:
            return 0

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

    def exportar_a_json(self, agentes: Dict[str, Agente], ruta: str) -> str:
        """Exporta agentes a un archivo JSON (ruta elegida por el usuario)."""
        if not agentes:
            raise ConfigError("No hay agentes para exportar")

        data = {
            "version": self.SCHEMA_VERSION,
            "exportado": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "total_agentes": len(agentes),
            "agentes": [agente_a_dict(a) for a in agentes.values()],
        }

        ext = os.path.splitext(ruta)[1].lower()
        if ext and ext not in {'.json'}:
            logger.warning(f"Extensión no estándar para JSON: '{ext}' en {ruta}")

        self._writer.escribir_sin_restriccion(ruta, data, use_journal=False)

        # Invalidar caché solo si está dentro de config_dir
        try:
            if ruta.startswith(self.config_dir):
                self._cache.invalidate(ruta)
        except Exception:
            pass

        logger.info(f"Exportación completada: {ruta}")
        return ruta

    def importar_desde_json(self, ruta: str, validar_agentes: bool = True) -> List[Dict]:
        """Importa agentes desde un archivo JSON (ruta elegida por el usuario)."""
        if not os.path.exists(ruta):
            raise ConfigError(f"Archivo no encontrado: {ruta}")
        if not os.path.isfile(ruta):
            raise ConfigError(f"La ruta no es un archivo: {ruta}")

        ext = os.path.splitext(ruta)[1].lower()
        if ext and ext not in {'.json'}:
            logger.warning(f"Extensión no estándar para JSON: '{ext}' en {ruta}")

        try:
            data = validar_archivo_config(ruta, self._writer, self.SCHEMA_VERSION)
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

    # ============================================================
    # MÉTODOS PARA LOOP
    # ============================================================

    def listar_loops(self) -> List[Dict]:
        """Lista todas las configuraciones que contienen agentes LOOP."""
        loops = []

        for filename in self.listar_configuraciones():
            ruta = os.path.join(self.config_dir, filename)
            try:
                data = validar_archivo_config(ruta, self._writer, self.SCHEMA_VERSION)
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
                            "continuar_en_error": a.get('continuar_en_error', False),
                        } for a in agentes_loop],
                        "ruta": ruta,
                    })
            except Exception as e:
                logger.warning(f"Error procesando {filename} para loops: {e}")
                continue

        return loops

    def validar_configuracion_loop(self, agente_dict: Dict) -> Tuple[bool, str]:
        """Valida la configuración de un agente LOOP antes de guardar."""
        if agente_dict.get('tipo') != 'Loop':
            return True, "No es un agente LOOP"

        fuente = agente_dict.get('fuente_items', '').strip()
        if not fuente:
            return False, "Loop: 'fuente_items' es obligatorio"
        if '.' not in fuente:
            return False, f"Loop: 'fuente_items' debe tener formato 'Dependencia.clave' (actual: '{fuente}')"

        codigo = agente_dict.get('codigo_por_item', '').strip()
        if not codigo:
            return False, "Loop: 'codigo_por_item' es obligatorio"

        max_iter = agente_dict.get('max_iteraciones', 0)
        if max_iter < 1:
            return False, f"Loop: 'max_iteraciones' debe ser >= 1 (actual: {max_iter})"

        timeout = agente_dict.get('timeout_loop', 0)
        if timeout < 1:
            return False, f"Loop: 'timeout_loop' debe ser >= 1 (actual: {timeout})"

        return True, ""

    # ============================================================
    # UTILIDADES ADICIONALES
    # ============================================================

    def obtener_ruta_absoluta(self, nombre_archivo: str) -> Optional[str]:
        """Obtiene la ruta absoluta de un archivo de configuración."""
        try:
            ruta = self._ruta_segura(nombre_archivo, allow_subdirs=True)
            if os.path.exists(ruta) and os.path.isfile(ruta):
                return ruta
        except ConfigSecurityError:
            pass
        return None

    def existe(self, nombre_archivo: str) -> bool:
        """Verifica si un archivo de configuración existe."""
        try:
            ruta = self._ruta_segura(nombre_archivo, allow_subdirs=True)
            return os.path.exists(ruta) and os.path.isfile(ruta)
        except ConfigSecurityError:
            return False

    def obtener_tamano(self, nombre_archivo: str) -> Optional[int]:
        """Obtiene el tamaño de un archivo de configuración."""
        try:
            ruta = self._ruta_segura(nombre_archivo, allow_subdirs=True)
            if os.path.exists(ruta) and os.path.isfile(ruta):
                return os.path.getsize(ruta)
        except (ConfigSecurityError, OSError):
            pass
        return None

    def limpiar_cache(self):
        """Limpia toda la caché de configuraciones."""
        self._cache.clear()
        logger.debug("Caché de configuraciones limpiada")

    def obtener_info(self) -> Dict:
        """Obtiene información sobre el gestor de configuraciones."""
        return {
            "config_dir": self.config_dir,
            "cache_size": self._cache.size(),
            "max_cache": self._cache.max_size,
            "total_configs": len(self.listar_configuraciones()),
            "schema_version": self.SCHEMA_VERSION,
            "journal_enabled": self._journal_dir is not None,
        }

    def __repr__(self) -> str:
        return f"ConfigManager(config_dir='{self.config_dir}', cache_size={self._cache.size()})"