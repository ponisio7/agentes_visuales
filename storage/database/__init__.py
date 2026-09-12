# storage/database/__init__.py
"""Gestor de persistencia SQLite — API pública estable.

La clase Database es una fachada que compone repositorios especializados.
Toda la API pública original se mantiene intacta:
    db.guardar_ejecucion(...)
    db.obtener_historial(...)
    db.obtener_estadisticas(...)
    ...
"""
import logging
from typing import List, Dict, Optional, Tuple, Any

from .schema import DEFAULT_DB_PATH, DB_VERSION, CLEANUP_DAYS
from .connection import ConnectionManager
from .migrations import SchemaManager
from .executions import ExecutionRepository
from .statistics import StatisticsRepository
from .maintenance import MaintenanceRepository
from .audit import AuditRepository
from .models import Ejecucion, AgenteEjecucion

logger = logging.getLogger(__name__)


class Database:
    """
    Gestor de persistencia SQLite para historial de ejecuciones.

    Esta clase actúa como FACHADA. Compone repositorios especializados
    y delega las llamadas para mantener la API pública original.
    """

    def __init__(self, db_path: str = DEFAULT_DB_PATH):
        self.db_path = db_path
        self._initialized = False

        # Infraestructura
        self._conn_mgr = ConnectionManager(db_path)
        self._schema_mgr = SchemaManager(self._conn_mgr)

        # Repositorios
        self._audit = AuditRepository(self._conn_mgr)
        self._executions = ExecutionRepository(self._conn_mgr, self._audit)
        self._statistics = StatisticsRepository(self._conn_mgr)
        self._maintenance = MaintenanceRepository(self._conn_mgr)

        try:
            # 1. Crear estructura base
            self._schema_mgr.init_db()
            # 2. Ejecutar migraciones ANTES de verificar
            self._schema_mgr.migrar_db()
            # 3. Verificación adicional del esquema
            self._schema_mgr.verificar_esquema()
            # 4. Crear/verificar índices
            self._schema_mgr.crear_indices()

            self._initialized = True
            logger.info(f"Base de datos inicializada: {db_path} (versión {DB_VERSION})")
        except Exception as e:
            self._initialized = False
            logger.error(f"Error inicializando la base de datos '{db_path}': {e}")
            try:
                self._conn_mgr.close_connection()
            except Exception:
                pass
            raise

    # ============================================================
    # DELEGACIÓN A REPOSITORIOS
    # ============================================================
    # -- Ejecuciones --
    def guardar_ejecucion(self, *args, **kwargs):
        return self._executions.guardar_ejecucion(*args, **kwargs)

    def obtener_historial(self, *args, **kwargs):
        return self._executions.obtener_historial(*args, **kwargs)

    def obtener_ejecucion(self, *args, **kwargs):
        return self._executions.obtener_ejecucion(*args, **kwargs)

    def obtener_detalle_ejecucion(self, *args, **kwargs):
        return self._executions.obtener_detalle_ejecucion(*args, **kwargs)

    def buscar_ejecuciones(self, *args, **kwargs):
        return self._executions.buscar_ejecuciones(*args, **kwargs)

    def eliminar_ejecucion(self, *args, **kwargs):
        return self._executions.eliminar_ejecucion(*args, **kwargs)

    # -- Estadísticas --
    def obtener_estadisticas(self, *args, **kwargs):
        return self._statistics.obtener_estadisticas(*args, **kwargs)

    def obtener_info_db(self, *args, **kwargs):
        return self._statistics.obtener_info_db(*args, **kwargs)

    # -- Mantenimiento --
    def cleanup_old_records(self, *args, **kwargs):
        return self._maintenance.cleanup_old_records(*args, **kwargs)

    def limpiar_ejecuciones_antiguas(self, *args, **kwargs):
        return self._maintenance.limpiar_ejecuciones_antiguas(*args, **kwargs)

    def compactar_db(self, *args, **kwargs):
        return self._maintenance.compactar_db(*args, **kwargs)

    def verificar_integridad(self, *args, **kwargs):
        return self._maintenance.verificar_integridad(*args, **kwargs)

    def backup(self, *args, **kwargs):
        return self._maintenance.backup(*args, **kwargs)

    def restore_backup(self, *args, **kwargs):
        return self._maintenance.restore_backup(*args, **kwargs)

    def maintenance(self, *args, **kwargs):
        return self._maintenance.maintenance(*args, **kwargs)

    def _check_and_cleanup(self, *args, **kwargs):
        return self._maintenance._check_and_cleanup(*args, **kwargs)

    # -- Auditoría --
    def obtener_auditoria(self, *args, **kwargs):
        return self._audit.obtener_auditoria(*args, **kwargs)

    # -- Esquema --
    def diagnostico(self, *args, **kwargs):
        return self._schema_mgr.diagnostico(*args, **kwargs)

    def reparar_esquema(self, *args, **kwargs):
        return self._schema_mgr.reparar_esquema(*args, **kwargs)

    # ============================================================
    # EXPORTAR / IMPORTAR
    # ============================================================
    def exportar_json(self, ruta: str, ejecucion_id: Optional[int] = None) -> bool:
        """Exporta datos a JSON."""
        import json
        from datetime import datetime
        try:
            if ejecucion_id:
                ejecucion = self.obtener_ejecucion(ejecucion_id)
                if not ejecucion:
                    return False
                agentes = self.obtener_detalle_ejecucion(ejecucion_id)
                data = {'ejecucion': ejecucion, 'agentes': agentes}
            else:
                historial = self.obtener_historial(limit=1000)
                data = {
                    'exportado': datetime.now().isoformat(),
                    'total': len(historial),
                    'ejecuciones': historial
                }
            with open(ruta, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False, default=str)
            logger.info(f"Datos exportados a {ruta}")
            return True
        except Exception as e:
            logger.error(f"Error exportando datos: {e}")
            return False

    def importar_json(self, ruta: str) -> int:
        """Importa datos desde JSON."""
        import json
        try:
            with open(ruta, 'r', encoding='utf-8') as f:
                data = json.load(f)

            importados = 0
            if 'ejecuciones' in data:
                for ejec in data['ejecuciones']:
                    if 'agentes' in ejec:
                        agentes = ejec.pop('agentes', [])
                        self.guardar_ejecucion(agentes, ejec.get('duracion_total', 0))
                        importados += 1
            elif 'ejecucion' in data and 'agentes' in data:
                self.guardar_ejecucion(
                    data['agentes'],
                    data['ejecucion'].get('duracion_total', 0)
                )
                importados = 1

            logger.info(f"Importados {importados} registros desde {ruta}")
            return importados
        except Exception as e:
            logger.error(f"Error importando datos: {e}")
            return 0

    # ============================================================
    # CICLO DE VIDA
    # ============================================================
    def close(self):
        try:
            self._conn_mgr.close_connection()
            logger.info("Conexiones a base de datos cerradas")
        except Exception as e:
            logger.warning(f"Error cerrando conexiones: {e}")

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


__all__ = [
    "Database",
    "Ejecucion",
    "AgenteEjecucion",
    "DEFAULT_DB_PATH",
    "DB_VERSION",
]