# storage/database/executions.py
"""Repositorio de ejecuciones (CRUD principal)."""
import sqlite3
import json
import gzip
import base64
import logging
from datetime import datetime
from typing import List, Dict, Optional, Any

from .connection import ConnectionManager
from .audit import AuditRepository
from .schema import COMPRESSION_THRESHOLD

logger = logging.getLogger(__name__)


class ExecutionRepository:
    """Operaciones CRUD sobre ejecuciones y sus agentes."""

    def __init__(self, conn_mgr: ConnectionManager, audit: AuditRepository):
        self.conn_mgr = conn_mgr
        self.audit = audit

    # ============================================================
    # NORMALIZACIÓN
    # ============================================================
    @staticmethod
    def _normalizar_estado(estado: str) -> str:
        """Normaliza el estado de un agente (case-insensitive)."""
        if not estado:
            return "pendiente"

        estado_lower = estado.lower().strip()
        mapeo = {
            "completado": "Completado",
            "complete": "Completado",
            "completed": "Completado",
            "error": "Error",
            "failed": "Error",
            "fail": "Error",
            "cancelado": "Cancelado",
            "canceled": "Cancelado",
            "cancelled": "Cancelado",
            "pendiente": "Pendiente",
            "pending": "Pendiente",
            "ejecutando": "Ejecutando",
            "running": "Ejecutando",
        }
        return mapeo.get(estado_lower, estado)

    # ============================================================
    # GUARDAR
    # ============================================================
    def guardar_ejecucion(
        self,
        agentes: List[Dict],
        duracion_total: float,
        estado: str = "completada",
        tags: List[str] = None,
        notas: str = "",
        ejecutor: str = ""
    ) -> int:
        """Guarda una ejecución completa con todos sus agentes."""
        if not agentes:
            raise ValueError("No hay agentes para guardar")

        for i, agente in enumerate(agentes):
            if not isinstance(agente, dict):
                raise ValueError(f"Agente {i} no es un diccionario")
            if 'nombre' not in agente:
                raise ValueError(f"Agente {i} sin campo 'nombre'")
            if 'estado' not in agente:
                raise ValueError(f"Agente {i} sin campo 'estado'")

        tags_str = ",".join(tags) if tags else ""

        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()

                completados = 0
                errores = 0
                cancelados = 0

                for a in agentes:
                    estado_norm = self._normalizar_estado(a.get('estado', ''))
                    if estado_norm == "Completado":
                        completados += 1
                    elif estado_norm == "Error":
                        errores += 1
                    elif estado_norm == "Cancelado":
                        cancelados += 1

                cursor.execute('''
                    INSERT INTO ejecuciones (
                        fecha, duracion_total, agentes_total,
                        completados, errores, cancelados,
                        estado, ejecutor, tags, notas
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    datetime.now().isoformat(),
                    float(duracion_total),
                    len(agentes),
                    completados,
                    errores,
                    cancelados,
                    estado,
                    ejecutor,
                    tags_str,
                    notas
                ))

                ejecucion_id = cursor.lastrowid
                self._insertar_agentes_lote(conn, ejecucion_id, agentes)
                self.audit.registrar(
                    conn,
                    accion="guardar_ejecucion",
                    detalle=(
                        f"Ejecución {ejecucion_id}: {len(agentes)} agentes, "
                        f"{duracion_total:.2f}s, "
                        f"✅{completados} ❌{errores} ⛔{cancelados}"
                    )
                )

                logger.info(
                    f"Ejecución guardada: ID={ejecucion_id}, "
                    f"{len(agentes)} agentes, "
                    f"✅{completados} ❌{errores} ⛔{cancelados}"
                )
                return ejecucion_id

        except sqlite3.Error as e:
            logger.error(f"Error guardando ejecución: {e}")
            raise RuntimeError(f"Error al guardar la ejecución: {e}")

    def _insertar_agentes_lote(
        self,
        conn: sqlite3.Connection,
        ejecucion_id: int,
        agentes: List[Dict]
    ):
        """Inserta múltiples agentes de una ejecución."""
        cursor = conn.cursor()
        datos = []

        for orden, agente in enumerate(agentes):
            dependencias = agente.get('dependencias', [])
            if isinstance(dependencias, str):
                try:
                    dependencias = json.loads(dependencias)
                except (json.JSONDecodeError, ValueError):
                    dependencias = []
            if not isinstance(dependencias, list):
                dependencias = []

            resultado = agente.get('resultado', {})
            if isinstance(resultado, dict):
                resultado_str = self._comprimir_json(resultado)
            elif resultado is None:
                resultado_str = ''
            else:
                resultado_str = self._comprimir_json({'resultado': str(resultado)})

            estado = self._normalizar_estado(agente.get('estado', 'Pendiente'))

            datos.append((
                ejecucion_id,
                str(agente.get('id', '')),
                str(agente.get('nombre', '')),
                str(agente.get('tipo', 'Desconocido')),
                estado,
                float(agente.get('duracion', 0.0) or 0.0),
                json.dumps(dependencias, ensure_ascii=False),
                resultado_str,
                str(agente.get('error', '') or ''),
                orden,
                int(agente.get('progreso', 0) or 0)
            ))

        if datos:
            cursor.executemany('''
                INSERT INTO agentes_ejecucion (
                    ejecucion_id, agente_id, nombre, tipo, estado,
                    duracion, dependencias, resultado, error, orden, progreso
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', datos)

    # ============================================================
    # COMPRESIÓN JSON
    # ============================================================
    def _comprimir_json(self, data: Any) -> str:
        try:
            json_str = json.dumps(data, ensure_ascii=False, default=str)
            if len(json_str) <= COMPRESSION_THRESHOLD:
                return json_str
            compressed = gzip.compress(json_str.encode('utf-8'))
            return "GZIP:" + base64.b64encode(compressed).decode('ascii')
        except Exception as e:
            logger.warning(f"Error comprimiendo JSON: {e}")
            return json.dumps({'error': 'Error al serializar', 'detalle': str(e)})

    def _descomprimir_json(self, data: str) -> Any:
        if not data or data == 'null' or data == '{}':
            return {}
        try:
            if data.startswith("GZIP:"):
                compressed = base64.b64decode(data[5:].encode('ascii'))
                decompressed = gzip.decompress(compressed).decode('utf-8')
                return json.loads(decompressed)
            else:
                return json.loads(data)
        except Exception as e:
            logger.warning(f"Error descomprimiendo JSON: {e}")
            return {'error': 'Error al deserializar', 'raw': data[:100]}

    # ============================================================
    # CONSULTAS
    # ============================================================
    def obtener_historial(
        self,
        limit: int = 20,
        offset: int = 0,
        estado: str = None
    ) -> List[Dict]:
        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                query = 'SELECT * FROM ejecuciones WHERE 1=1'
                params = []
                if estado:
                    query += " AND estado = ?"
                    params.append(estado)
                query += " ORDER BY fecha DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])
                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error obteniendo historial: {e}")
            return []

    def obtener_ejecucion(self, ejecucion_id: int) -> Optional[Dict]:
        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute('SELECT * FROM ejecuciones WHERE id = ?', (ejecucion_id,))
                result = cursor.fetchone()
                return dict(result) if result else None
        except sqlite3.Error as e:
            logger.error(f"Error obteniendo ejecución {ejecucion_id}: {e}")
            return None

    def obtener_detalle_ejecucion(self, ejecucion_id: int) -> List[Dict]:
        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute('''
                    SELECT * FROM agentes_ejecucion
                    WHERE ejecucion_id = ?
                    ORDER BY orden, id
                ''', (ejecucion_id,))

                resultados = []
                for row in cursor.fetchall():
                    r = dict(row)
                    try:
                        r['dependencias'] = json.loads(r['dependencias']) if r['dependencias'] else []
                    except (json.JSONDecodeError, ValueError):
                        r['dependencias'] = []
                    r['resultado'] = self._descomprimir_json(r.get('resultado', ''))
                    resultados.append(r)
                return resultados
        except sqlite3.Error as e:
            logger.error(f"Error obteniendo detalle de ejecución {ejecucion_id}: {e}")
            return []

    def buscar_ejecuciones(
        self,
        texto: str = "",
        estado: str = None,
        desde: str = None,
        hasta: str = None,
        tags: List[str] = None,
        limit: int = 50
    ) -> List[Dict]:
        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                query = "SELECT * FROM ejecuciones WHERE 1=1"
                params = []
                if texto:
                    query += " AND (notas LIKE ? OR tags LIKE ?)"
                    params.extend([f"%{texto}%", f"%{texto}%"])
                if estado:
                    query += " AND estado = ?"
                    params.append(estado)
                if desde:
                    query += " AND fecha >= ?"
                    params.append(desde)
                if hasta:
                    query += " AND fecha <= ?"
                    params.append(hasta)
                if tags:
                    query += " AND tags GLOB ?"
                    params.append(f"*{','.join(tags)}*")
                query += " ORDER BY fecha DESC LIMIT ?"
                params.append(limit)
                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        except sqlite3.Error as e:
            logger.error(f"Error buscando ejecuciones: {e}")
            return []

    # ============================================================
    # ELIMINAR
    # ============================================================
    def eliminar_ejecucion(self, ejecucion_id: int) -> bool:
        try:
            with self.conn_mgr.transaction() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT id FROM ejecuciones WHERE id = ?", (ejecucion_id,))
                if not cursor.fetchone():
                    return False
                cursor.execute("DELETE FROM ejecuciones WHERE id = ?", (ejecucion_id,))
                self.audit.registrar(
                    conn,
                    accion="eliminar_ejecucion",
                    detalle=f"Ejecución {ejecucion_id} eliminada"
                )
                logger.info(f"Ejecución {ejecucion_id} eliminada")
                return True
        except sqlite3.Error as e:
            logger.error(f"Error eliminando ejecución {ejecucion_id}: {e}")
            return False


__all__ = ["ExecutionRepository"]