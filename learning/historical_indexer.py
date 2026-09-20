"""
learning/historical_indexer.py
Reindexado del histórico y éxito REAL (V3.8-4).

El retrieval de casos similares (H8) solo tiene algo que recuperar si las
ejecuciones antiguas tienen ``problema_embedding``. Además, la columna
``aceptada`` se añadió en H5 con ``DEFAULT 1``, así que **todas** las
ejecuciones previas quedaron marcadas como «aceptadas» aunque tuvieran
errores: «el 100 % de éxito» era mentira.

Este módulo cierra las dos brechas:

    ejecuciones antiguas
           ↓
    calcular embedding del problema
           ↓
    determinar ÉXITO REAL (aceptación + errores + feedback del usuario)
           ↓
    guardar (embedding, exito_real, indexado_fecha)
           ↓
    retrieval (H8) ya puede usarlas

El resultado del reindexado se puede inspeccionar con ``CaseRecord``, que
reúne problema, objetivo, plan, agentes, tipos, contrato, resultado,
aceptación, errores, intentos de Plan B, tiempo, coste, feedback y lecciones.

Uso:

    python -m learning.historical_indexer --db agent_history.db
    python -m learning.historical_indexer --dry-run --limite 100

Sin ``sentence-transformers`` el reindexado de embeddings se omite, pero el
éxito real sí se calcula (no depende del modelo).
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH_POR_DEFECTO = "agent_history.db"

# Motivos del veredicto de éxito real (auditables).
MOTIVO_SIN_PROBLEMA = "sin_problema"
MOTIVO_FEEDBACK_NEGATIVO = "feedback_negativo"
MOTIVO_FEEDBACK_POSITIVO = "feedback_positivo"
MOTIVO_NO_ACEPTADA = "no_aceptada"
MOTIVO_EJECUCION_INCOMPLETA = "ejecucion_incompleta"
MOTIVO_ACEPTADA_SIN_ERRORES = "aceptada_sin_errores"

# Por debajo de este score (feedback de plan) el caso se considera fallido.
UMBRAL_FEEDBACK_NEGATIVO = -0.5
# Por encima de este score el feedback confirma el éxito.
UMBRAL_FEEDBACK_POSITIVO = 0.5


@dataclass
class CaseRecord:
    """Caso del histórico con todo lo que el retrieval querría saber.

    Es la representación rica de una ejecución: no solo «problema + plan»,
    sino el contexto para decidir si el enfoque merece reutilizarse.
    """

    ejecucion_id: int
    problema: str = ""
    objetivo: str = ""
    plan_json: str = ""
    agentes: list[dict[str, Any]] = field(default_factory=list)
    tipos: list[str] = field(default_factory=list)
    contrato: dict[str, Any] = field(default_factory=dict)
    resultado: str = ""
    aceptada: bool | None = None
    motivo_fallo: str = ""
    errores: int = 0
    intentos_plan_b: int = 0
    duracion: float = 0.0
    llamadas_llm: int = 0
    tokens_total: int = 0
    coste: float = 0.0
    feedback: list[dict[str, Any]] = field(default_factory=list)
    lecciones: list[str] = field(default_factory=list)
    exito_real: bool | None = None
    exito_real_motivo: str = ""
    fecha: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ejecucion_id": self.ejecucion_id,
            "problema": self.problema,
            "objetivo": self.objetivo,
            "agentes": list(self.agentes),
            "tipos": list(self.tipos),
            "contrato": dict(self.contrato),
            "resultado": self.resultado,
            "aceptada": self.aceptada,
            "motivo_fallo": self.motivo_fallo,
            "errores": self.errores,
            "intentos_plan_b": self.intentos_plan_b,
            "duracion": self.duracion,
            "llamadas_llm": self.llamadas_llm,
            "tokens_total": self.tokens_total,
            "coste": self.coste,
            "feedback": list(self.feedback),
            "lecciones": list(self.lecciones),
            "exito_real": self.exito_real,
            "exito_real_motivo": self.exito_real_motivo,
            "fecha": self.fecha,
        }


def _a_bool(valor: Any) -> bool | None:
    if valor is None:
        return None
    try:
        return bool(int(valor))
    except (TypeError, ValueError):
        return None


class HistoricalIndexer:
    """Reindexa el histórico: embeddings + éxito real."""

    def __init__(
        self,
        db_path: str = DB_PATH_POR_DEFECTO,
        matcher: Any = None,
        *,
        umbral_negativo: float = UMBRAL_FEEDBACK_NEGATIVO,
        umbral_positivo: float = UMBRAL_FEEDBACK_POSITIVO,
    ):
        self.db_path = str(db_path)
        self._matcher = matcher
        self.umbral_negativo = float(umbral_negativo)
        self.umbral_positivo = float(umbral_positivo)

    # ------------------------------------------------------------
    # Matcher perezoso (no carga el modelo si no hace falta)
    # ------------------------------------------------------------
    def matcher(self) -> Any:
        if self._matcher is None:
            from .embedding_matcher import obtener_matcher

            self._matcher = obtener_matcher()
        return self._matcher

    # ------------------------------------------------------------
    # Esquema
    # ------------------------------------------------------------
    @staticmethod
    def _columnas(conn: sqlite3.Connection, tabla: str) -> set[str]:
        return {fila[1] for fila in conn.execute(f"PRAGMA table_info({tabla})")}

    def _tablas(self, conn: sqlite3.Connection) -> set[str]:
        return {
            fila[0]
            for fila in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }

    # ------------------------------------------------------------
    # Éxito real
    # ------------------------------------------------------------
    def calcular_exito_real(
        self,
        fila: dict[str, Any],
        feedback: list[dict[str, Any]] | None = None,
    ) -> tuple[bool | None, str]:
        """Veredicto compuesto y conservador de si la ejecución sirvió.

        Orden de decisión:

        1. Sin problema → indeterminado (no sirve para retrieval).
        2. Feedback de plan claramente negativo → fallo (el usuario manda).
        3. Feedback de plan claramente positivo → éxito.
        4. ``aceptada`` explícitamente 0 → fallo.
        5. Errores/cancelados o estado no completado → fallo (las filas
           antiguas tenían ``aceptada=1`` por defecto: aquí se corrigen).
        6. Aceptada y todos los agentes completados → éxito.
        7. Cualquier otro caso → indeterminado (mejor no usarlo como ejemplo).
        """
        problema = (fila.get("problema") or "").strip()
        if not problema:
            return None, MOTIVO_SIN_PROBLEMA

        feedback = feedback or []
        scores_plan = [
            float(f["score"]) for f in feedback
            if str(f.get("alcance") or "plan") == "plan"
            and f.get("score") is not None
        ]
        if scores_plan:
            if min(scores_plan) <= self.umbral_negativo:
                return False, MOTIVO_FEEDBACK_NEGATIVO
            if max(scores_plan) >= self.umbral_positivo:
                return True, MOTIVO_FEEDBACK_POSITIVO

        aceptada = _a_bool(fila.get("aceptada"))
        if aceptada is False:
            return False, MOTIVO_NO_ACEPTADA

        errores = int(fila.get("errores") or 0)
        cancelados = int(fila.get("cancelados") or 0)
        estado = str(fila.get("estado") or "").strip().lower()
        completados = int(fila.get("completados") or 0)
        agentes_total = int(fila.get("agentes_total") or 0)

        if errores > 0 or cancelados > 0:
            return False, MOTIVO_EJECUCION_INCOMPLETA
        if estado and estado not in ("completada", "completado", "ok", "success"):
            return False, MOTIVO_EJECUCION_INCOMPLETA
        if agentes_total and completados < agentes_total:
            return False, MOTIVO_EJECUCION_INCOMPLETA

        if aceptada is True:
            return True, MOTIVO_ACEPTADA_SIN_ERRORES
        return None, "indeterminado"

    # ------------------------------------------------------------
    # Candidatos
    # ------------------------------------------------------------
    def candidatos(
        self,
        *,
        forzar: bool = False,
        limite: int | None = None,
        modelo: str | None = None,
    ) -> list[dict[str, Any]]:
        """Filas que necesitan indexado (o todas si ``forzar``)."""
        with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            columnas = self._columnas(conn, "ejecuciones")
            if "problema" not in columnas:
                return []

            opcionales = [
                c for c in (
                    "plan_json", "resultado", "aceptada", "motivo_fallo",
                    "problema_embedding", "problema_embedding_model",
                    "exito_real", "exito_real_motivo", "llamadas_llm",
                    "tokens_total", "coste",
                ) if c in columnas
            ]
            base = [
                "id", "fecha", "problema", "duracion_total", "agentes_total",
                "completados", "errores", "cancelados", "estado",
            ]
            seleccion = ", ".join(base + opcionales)
            filas = conn.execute(
                f"""SELECT {seleccion} FROM ejecuciones
                    WHERE problema IS NOT NULL AND problema != ''
                    ORDER BY id DESC"""
            ).fetchall()

        resultado: list[dict[str, Any]] = []
        for fila in filas:
            datos = dict(fila)
            if not forzar and not self._necesita_indexado(datos, modelo):
                continue
            resultado.append(datos)
            if limite is not None and len(resultado) >= int(limite):
                break
        return resultado

    @staticmethod
    def _necesita_indexado(fila: dict[str, Any], modelo: str | None) -> bool:
        """True si le falta el embedding, el modelo cambió o no hay veredicto."""
        if fila.get("exito_real") is None:
            return True
        if fila.get("problema_embedding") is None:
            return True
        if modelo is not None and fila.get("problema_embedding_model") != modelo:
            return True
        return False

    # ------------------------------------------------------------
    # Feedback y Plan B
    # ------------------------------------------------------------
    def feedback_de(self, ejecucion_id: int) -> list[dict[str, Any]]:
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")
                if "feedback_usuario" not in self._tablas(conn):
                    return []
                filas = conn.execute(
                    """SELECT alcance, score, comentario, fecha
                       FROM feedback_usuario WHERE ejecucion_id = ?""",
                    (ejecucion_id,),
                ).fetchall()
                return [dict(f) for f in filas]
        except Exception as e:
            logger.debug(f"Indexer: feedback no disponible: {e}")
            return []

    def intentos_plan_b(self, ejecucion_id: int) -> int:
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                if "reparaciones_plan" not in self._tablas(conn):
                    return 0
                fila = conn.execute(
                    "SELECT COUNT(*) FROM reparaciones_plan WHERE ejecucion_id = ?",
                    (ejecucion_id,),
                ).fetchone()
                return int(fila[0]) if fila else 0
        except Exception as e:
            logger.debug(f"Indexer: reparaciones no disponibles: {e}")
            return 0

    # ------------------------------------------------------------
    # CaseRecord
    # ------------------------------------------------------------
    def construir_case(self, ejecucion_id: int) -> CaseRecord | None:
        """Reúne todo lo conocido de una ejecución en un ``CaseRecord``."""
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")
                fila = conn.execute(
                    "SELECT * FROM ejecuciones WHERE id = ?", (ejecucion_id,)
                ).fetchone()
                if fila is None:
                    return None
                datos = dict(fila)

                agentes: list[dict[str, Any]] = []
                if "agentes_ejecucion" in self._tablas(conn):
                    agentes = [
                        dict(a) for a in conn.execute(
                            """SELECT nombre, tipo, estado, duracion, error,
                                      dependencias, orden
                               FROM agentes_ejecucion
                               WHERE ejecucion_id = ? ORDER BY orden, id""",
                            (ejecucion_id,),
                        ).fetchall()
                    ]
                aceptada = _a_bool(datos.get("aceptada"))
                exito_real = _a_bool(datos.get("exito_real"))
                exito_motivo = str(datos.get("exito_real_motivo") or "")
                plan_json = datos.get("plan_json") or ""
        except Exception as e:
            logger.debug(f"Indexer: no se pudo construir el caso {ejecucion_id}: {e}")
            return None

        objetivo, tipos, contrato = self._resumen_plan(plan_json)
        feedback = self.feedback_de(ejecucion_id)
        if exito_real is None:
            # Aún sin veredicto guardado (o BD sin la columna): se calcula
            # ahora para que el caso se pueda inspeccionar igualmente.
            exito_real, exito_motivo = self.calcular_exito_real(datos, feedback)

        case = CaseRecord(
            ejecucion_id=int(ejecucion_id),
            problema=str(datos.get("problema") or ""),
            objetivo=objetivo,
            plan_json=plan_json,
            agentes=agentes,
            tipos=tipos,
            contrato=contrato,
            resultado=str(datos.get("resultado") or "")[:2000],
            aceptada=aceptada,
            motivo_fallo=str(datos.get("motivo_fallo") or ""),
            errores=int(datos.get("errores") or 0),
            intentos_plan_b=self.intentos_plan_b(ejecucion_id),
            duracion=float(datos.get("duracion_total") or 0.0),
            llamadas_llm=int(datos.get("llamadas_llm") or 0),
            tokens_total=int(datos.get("tokens_total") or 0),
            coste=float(datos.get("coste") or 0.0),
            feedback=feedback,
            exito_real=exito_real,
            exito_real_motivo=exito_motivo,
            fecha=str(datos.get("fecha") or ""),
        )
        case.lecciones = self._lecciones(case)
        return case

    @staticmethod
    def _resumen_plan(plan_json: str) -> tuple[str, list[str], dict[str, Any]]:
        """(título, tipos, contrato) del plan serializado."""
        try:
            datos = json.loads(plan_json) if plan_json else {}
        except (TypeError, ValueError):
            return "", [], {}
        if not isinstance(datos, dict):
            return "", [], {}
        pasos = datos.get("pasos") or []
        tipos = [
            str(p.get("tipo_agente") or "") for p in pasos
            if isinstance(p, dict) and p.get("tipo_agente")
        ]
        contrato = {
            "pasos_criticos": [
                str(p.get("nombre") or "") for p in pasos
                if isinstance(p, dict) and p.get("es_critico")
            ],
            "pasos_con_contrato": [
                str(p.get("nombre") or "") for p in pasos
                if isinstance(p, dict) and p.get("tiene_contrato")
            ],
        }
        return str(datos.get("titulo") or ""), tipos, contrato

    def _lecciones(self, case: CaseRecord) -> list[str]:
        """Lecciones deterministas del caso (sin LLM)."""
        lecciones: list[str] = []
        if case.exito_real is False:
            detalle = case.motivo_fallo or case.exito_real_motivo or "sin detalle"
            lecciones.append(f"No reutilizar sin cambios: falló ({detalle[:200]}).")
        if case.intentos_plan_b > 0:
            lecciones.append(
                f"Necesitó {case.intentos_plan_b} intento(s) de Plan B."
            )
        for f in case.feedback:
            comentario = str(f.get("comentario") or "").strip()
            if comentario:
                lecciones.append(f"Feedback: {comentario[:200]}")
        return lecciones

    # ------------------------------------------------------------
    # Reindexado
    # ------------------------------------------------------------
    def reindexar(
        self,
        *,
        limite: int | None = None,
        dry_run: bool = False,
        forzar: bool = False,
    ) -> dict[str, Any]:
        """Calcula embedding + éxito real y los persiste.

        Devuelve un resumen con contadores. Con ``dry_run`` no escribe nada.
        """
        estadisticas: dict[str, Any] = {
            "revisadas": 0,
            "indexadas": 0,
            "sin_cambio": 0,
            "exito_real_positivos": 0,
            "exito_real_negativos": 0,
            "exito_real_indeterminados": 0,
            "embeddings_calculados": 0,
            "embeddings_no_disponibles": 0,
            "errores": 0,
            "dry_run": bool(dry_run),
        }

        filas = self.candidatos(forzar=forzar, limite=limite)
        if not filas:
            logger.info("Indexer: no hay ejecuciones que reindexar")
            return estadisticas

        matcher = None
        modelo = None
        try:
            matcher = self.matcher()
            modelo = getattr(matcher, "modelo", None)
        except Exception as e:
            # Sin embeddings se puede seguir: el éxito real no depende de ellos.
            logger.info(f"Indexer: embeddings no disponibles ({e}); solo éxito real")

        filas = self.candidatos(forzar=forzar, limite=limite, modelo=modelo)

        for fila in filas:
            estadisticas["revisadas"] += 1
            ejecucion_id = int(fila["id"])
            feedback = self.feedback_de(ejecucion_id)
            exito_real, motivo = self.calcular_exito_real(fila, feedback)

            if exito_real is True:
                estadisticas["exito_real_positivos"] += 1
            elif exito_real is False:
                estadisticas["exito_real_negativos"] += 1
            else:
                estadisticas["exito_real_indeterminados"] += 1

            # Se conserva el embedding previo si el cálculo no está disponible:
            # reindexar no debe BORRAR lo que ya había.
            vector = fila.get("problema_embedding")
            modelo_fila = fila.get("problema_embedding_model") or ""
            if matcher is not None and fila.get("problema"):
                try:
                    calculado = matcher.calcular(fila["problema"])
                except Exception as e:
                    logger.debug(f"Indexer: embedding de {ejecucion_id} falló: {e}")
                    calculado = None
                if calculado is not None:
                    vector = calculado
                    modelo_fila = modelo or ""
                    estadisticas["embeddings_calculados"] += 1
                else:
                    estadisticas["embeddings_no_disponibles"] += 1

            if dry_run:
                continue

            try:
                with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                    conn.execute("PRAGMA busy_timeout=10000")
                    conn.execute(
                        """UPDATE ejecuciones
                             SET problema_embedding = ?,
                                 problema_embedding_model = ?,
                                 exito_real = ?,
                                 exito_real_motivo = ?,
                                 indexado_fecha = ?
                           WHERE id = ?""",
                        (
                            vector,
                            modelo_fila,
                            None if exito_real is None else (1 if exito_real else 0),
                            motivo,
                            datetime.now().isoformat(),
                            ejecucion_id,
                        ),
                    )
                    conn.commit()
                estadisticas["indexadas"] += 1
            except Exception as e:
                estadisticas["errores"] += 1
                logger.warning(f"Indexer: no se pudo actualizar {ejecucion_id}: {e}")

        logger.info(
            f"🧠 Indexer: {estadisticas['indexadas']}/{estadisticas['revisadas']} "
            f"reindexadas · +{estadisticas['exito_real_positivos']} "
            f"-{estadisticas['exito_real_negativos']} "
            f"?{estadisticas['exito_real_indeterminados']}"
        )
        return estadisticas

    def estadisticas(self) -> dict[str, Any]:
        """Foto del histórico indexado (para diagnóstico)."""
        datos = {
            "total_con_problema": 0,
            "con_embedding": 0,
            "sin_embedding": 0,
            "exito_real_positivo": 0,
            "exito_real_negativo": 0,
            "exito_real_indeterminado": 0,
        }
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                columnas = self._columnas(conn, "ejecuciones")
                if "problema" not in columnas:
                    return datos
                datos["total_con_problema"] = int(conn.execute(
                    "SELECT COUNT(*) FROM ejecuciones "
                    "WHERE problema IS NOT NULL AND problema != ''"
                ).fetchone()[0])
                datos["con_embedding"] = int(conn.execute(
                    "SELECT COUNT(*) FROM ejecuciones WHERE problema_embedding IS NOT NULL"
                ).fetchone()[0])
                datos["sin_embedding"] = int(conn.execute(
                    "SELECT COUNT(*) FROM ejecuciones "
                    "WHERE problema_embedding IS NULL AND problema != ''"
                ).fetchone()[0])
                if "exito_real" in columnas:
                    for etiqueta, valor in (("positivo", 1), ("negativo", 0)):
                        datos[f"exito_real_{etiqueta}"] = int(conn.execute(
                            "SELECT COUNT(*) FROM ejecuciones WHERE exito_real = ?",
                            (valor,),
                        ).fetchone()[0])
                    datos["exito_real_indeterminado"] = int(conn.execute(
                        "SELECT COUNT(*) FROM ejecuciones "
                        "WHERE problema != '' AND exito_real IS NULL"
                    ).fetchone()[0])
        except Exception as e:
            logger.debug(f"Indexer: no se pudieron calcular estadísticas: {e}")
        return datos


# ============================================================
# CLI
# ============================================================

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reindexa el histórico: embeddings + éxito real (V3.8-4)."
    )
    parser.add_argument("--db", default=DB_PATH_POR_DEFECTO, help="ruta de la BD")
    parser.add_argument("--limite", type=int, default=None, help="máximo de filas")
    parser.add_argument("--dry-run", action="store_true", help="no escribir nada")
    parser.add_argument("--forzar", action="store_true", help="reindexar todo")
    parser.add_argument("--estadisticas", action="store_true",
                        help="solo mostrar la foto del histórico")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")
    indexer = HistoricalIndexer(args.db)

    if args.estadisticas:
        print(json.dumps(indexer.estadisticas(), indent=2, ensure_ascii=False))
        return 0

    resumen = indexer.reindexar(
        limite=args.limite, dry_run=args.dry_run, forzar=args.forzar
    )
    print(json.dumps(resumen, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "CaseRecord",
    "HistoricalIndexer",
    "DB_PATH_POR_DEFECTO",
    "MOTIVO_ACEPTADA_SIN_ERRORES",
    "MOTIVO_EJECUCION_INCOMPLETA",
    "MOTIVO_FEEDBACK_NEGATIVO",
    "MOTIVO_FEEDBACK_POSITIVO",
    "MOTIVO_NO_ACEPTADA",
    "MOTIVO_SIN_PROBLEMA",
    "main",
]
