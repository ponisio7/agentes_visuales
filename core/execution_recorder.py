#core/execution_recorder.py — FIX Bug #7
from __future__ import annotations

import json
import logging
import sqlite3  # ← añadir
import threading
from contextlib import closing  # ← añadir
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)


def _estado_desde_aceptacion(scheduler) -> str:
    """Etiqueta honesta de la ejecución: 'completada' solo si pasa aceptación.

    Antes el estado estaba fijo en 'completada' (la ejecución 469 se guardó
    como completada con errores=1). Ahora lo decide el gate de aceptación del
    scheduler; si el scheduler no lo expone, se cae a contar errores.
    """
    try:
        aceptacion = scheduler.obtener_resultado_aceptacion()
        return "completada" if aceptacion.get("aceptada") else "fallida"
    except Exception as e:
        logger.debug(f"No se pudo consultar la aceptación: {e}")
    try:
        errores = 0
        for agente in scheduler.agentes.values():
            estado = getattr(agente, "estado", None)
            if getattr(estado, "value", str(estado)) == "Error":
                errores += 1
        return "fallida" if errores else "completada"
    except Exception:
        return "completada"


def _aceptacion_desde_scheduler(scheduler) -> dict:
    """Veredicto de aceptación del scheduler (o {} si no está disponible)."""
    try:
        return scheduler.obtener_resultado_aceptacion() or {}
    except Exception as e:
        logger.debug(f"No se pudo consultar la aceptación: {e}")
        return {}


def _serializar_plan(plan) -> str:
    """Resumen JSON del plan usado (H5), seguro de guardar en SQLite."""
    if plan is None:
        return ""
    try:
        datos = {
            "id": getattr(plan, "id", ""),
            "titulo": getattr(plan, "titulo", ""),
            "problema_original": getattr(plan, "problema_original", ""),
            "pasos": [
                {
                    "orden": getattr(p, "orden", 0),
                    "nombre": getattr(p, "nombre", ""),
                    "tipo_agente": getattr(p, "tipo_agente", ""),
                    "es_critico": bool(getattr(p, "es_critico", False)),
                    "tiene_contrato": getattr(p, "aceptacion", None) is not None,
                }
                for p in (getattr(plan, "pasos", None) or [])
            ],
        }
        return json.dumps(datos, ensure_ascii=False, default=str)[:20000]
    except Exception as e:
        logger.debug(f"No se pudo serializar el plan: {e}")
        return ""


def registrar_ejecucion_en_aprendizaje(
    scheduler, db, plan, problema: str, duracion_total: float,
    estado: str | None = None,
) -> int | None:
    # 0. Etiqueta real de éxito (H6) y veredicto de aceptación: se calculan
    #    ANTES del hilo para no depender de un scheduler que la GUI/workers
    #    pueden mutar después.
    aceptacion = _aceptacion_desde_scheduler(scheduler)
    if estado is None:
        estado = (
            "completada" if aceptacion.get("aceptada")
            else ("fallida" if aceptacion else _estado_desde_aceptacion(scheduler))
        )

    problema_snap = problema or ""
    resumen_plan_snap = _construir_resumen_plan(scheduler, plan) if plan is not None else ""
    aceptada_final = bool(aceptacion.get("aceptada", estado == "completada"))
    motivos = aceptacion.get("motivos") or []
    motivo_fallo = "" if aceptada_final else "; ".join(str(m) for m in motivos)[:2000]

    # V3.8-2: consumo de presupuesto (llamadas/tokens/coste) de la ejecución.
    # Best-effort: si el scheduler no lo expone, se guardan ceros.
    consumo: dict = {}
    try:
        consumo = (scheduler.presupuesto.resumen() or {}).get("consumo") or {}
    except Exception as e:
        logger.debug(f"No se pudo leer el presupuesto: {e}")

    # V4.0-4: la auto-crítica corre en el hilo de aprendizaje; se captura el
    # presupuesto aquí para no tocar el scheduler desde otro hilo y para que
    # una ejecución que ya agotó su presupuesto no gaste más en reescribir.
    presupuesto_snap = getattr(scheduler, "presupuesto", None)

    # 1. Guardar en DB - incluir plan original si hubo Plan B, deduplicando por id
    try:
        vistos_db: set = set()
        agentes_data = []
        for a in getattr(scheduler, "_agentes_plan_original", []):
            aid = getattr(a, "id", None)
            if aid is not None and aid in vistos_db: continue
            if aid is not None: vistos_db.add(aid)
            try:
                agentes_data.append(a.to_dict())
            except Exception as e:
                logger.warning(f"No se pudo serializar agente {aid}: {e}")
        for a in scheduler.agentes.values():
            aid = getattr(a, "id", None)
            if aid is not None and aid in vistos_db: continue
            if aid is not None: vistos_db.add(aid)
            try:
                agentes_data.append(a.to_dict())
            except Exception as e:
                logger.warning(f"No se pudo serializar agente {aid}: {e}")
        ejecucion_id = db.guardar_ejecucion(
            agentes_data,
            duracion_total,
            estado=estado,
            problema=problema_snap,
            plan_json=_serializar_plan(plan),
            resultado=resumen_plan_snap,
            aceptada=1 if aceptada_final else 0,
            motivo_fallo=motivo_fallo,
            llamadas_llm=int(consumo.get("llamadas", 0) or 0),
            tokens_total=int(consumo.get("tokens_total", 0) or 0),
            coste=float(consumo.get("coste", 0.0) or 0.0),
        )
    except Exception as e:
        logger.warning(f"No se pudo guardar la ejecución: {e}")
        return None
    agentes_snapshot = _construir_snapshot(scheduler)
    db_path = db.db_path
    plan_snap = plan
    
    def _worker():
        try:
            from core.llm_client import obtener_llm_client_compartido
            from learning import obtener_learning_engine
            engine = obtener_learning_engine(db_path=db_path, llm_client=obtener_llm_client_compartido())
            if engine is None: return

            # ✅ H8: embedding del problema para el retrieval de casos
            #    similares. Best-effort: si no hay sentence-transformers se
            #    omite sin afectar al resto del aprendizaje.
            if problema_snap:
                try:
                    from learning.embedding_matcher import obtener_matcher

                    matcher = obtener_matcher()
                    vector = matcher.calcular(problema_snap)
                    if vector is not None:
                        with closing(sqlite3.connect(db_path, timeout=10)) as conn:
                            conn.execute(
                                "UPDATE ejecuciones SET problema_embedding = ?, "
                                "problema_embedding_model = ? WHERE id = ?",
                                (vector, matcher.modelo, ejecucion_id),
                            )
                            conn.commit()
                except Exception as e:
                    logger.debug(f"No se pudo guardar el embedding del problema: {e}")

            # ✅ FASE 4c: registrar usos de prompts reescritos.
            # Cada agente LLM que usó una versión reescrita (activa o
            # candidata) deja constancia para poder atribuir el score.
            #
            # ✅ V4.0-AB: si el builder eligió el INCUMBENTE (prompt original)
            # pero había una firma A/B identificada, se registra el uso del
            # brazo de CONTROL. Sin esto el candidato no tiene referencia con
            # la que compararse y la promoción es imposible en arranque en frío.
            try:
                from learning.prompt_ab_evaluator import PromptABEvaluator
                ab = PromptABEvaluator(db_path)
                for item in agentes_snapshot:
                    pid = getattr(item["proxy"], "prompt_reescrito_id", 0)
                    firma = getattr(item["proxy"], "prompt_firma", "") or ""
                    if pid:
                        # H2: se registra POR QUÉ se eligió la variante.
                        ab.registrar_uso(
                            pid,
                            ejecucion_id,
                            motivo=getattr(item["proxy"], "prompt_reescrito_motivo", ""),
                        )
                    elif firma:
                        ab.registrar_uso_baseline(
                            firma,
                            ejecucion_id,
                            motivo=getattr(item["proxy"], "prompt_reescrito_motivo", "")
                            or "control A/B: prompt original",
                        )
            except Exception as e:
                logger.debug(f"AB registrar_uso falló: {e}")

            # ── Aprendizaje por agente (ya existía) ──
            for item in agentes_snapshot:
                try:
                    # V4.0-4: antes se pasaba siempre 0, así que la evaluación
                    # del agente no se podía atribuir a NINGÚN agente concreto
                    # y la auto-crítica acababa reescribiendo el prompt del
                    # último LLM de la ejecución. Ahora se resuelve la fila real.
                    agente_ejecucion_id = _id_agente_ejecucion(
                        db_path, ejecucion_id, item.get("agente_id")
                    )
                    engine.registrar_resultado_agente(
                        ejecucion_id=ejecucion_id,
                        agente_ejecucion_id=agente_ejecucion_id,
                        agente=item["proxy"], tarea=item["descripcion"] or item["nombre"],
                        resultado_texto=item["resultado_texto"], estado_real=item["estado_real"])
                except Exception as e:
                    logger.debug(f"Aprendizaje falló para {item['nombre']}: {e}")

            # ── Aprendizaje del plan (ya existía) ──
            if plan_snap is not None and problema_snap:
                try:
                    resultado_plan = resumen_plan_snap
                    engine.registrar_resultado_plan(
                        ejecucion_id=ejecucion_id, plan=plan_snap,
                        tarea=problema_snap, resultado_texto=resultado_plan
                    )

                    # ✅ FASE 4c: propaga el score al A/B y decide.
                    try:
                        from learning.prompt_ab_evaluator import PromptABEvaluator
                        ab = PromptABEvaluator(db_path)

                        # Score del plan completo (o del último agente si no hay plan)
                        score_plan = None
                        # obtener_learning_engine ya guardó la evaluación en
                        # `evaluaciones_llm`. Recuperamos la última de este plan.
                        
                        with closing(sqlite3.connect(db_path, timeout=5)) as conn:
                            conn.row_factory = sqlite3.Row
                            row = conn.execute(
                                """SELECT score FROM evaluaciones_llm
                                   WHERE ejecucion_id = ? AND alcance = 'plan'
                                   ORDER BY id DESC LIMIT 1""",
                                (ejecucion_id,),
                            ).fetchone()
                            if row:
                                score_plan = float(row["score"])

                        if score_plan is not None:
                            ab.actualizar_score(ejecucion_id, score_plan)

                            # Evaluar candidatos de las firmas que se usaron.
                            # V4.0-AB: también las firmas de las ejecuciones que
                            # fueron brazo de CONTROL (prompt original); así la
                            # decisión se toma en cuanto hay baseline suficiente,
                            # sin esperar a que vuelva a tocar explorar.
                            firmas_vistas = set()
                            for item in agentes_snapshot:
                                pid = getattr(item["proxy"], "prompt_reescrito_id", 0)
                                firma_directa = getattr(item["proxy"], "prompt_firma", "") or ""
                                if firma_directa:
                                    firmas_vistas.add(firma_directa)
                                    continue
                                if not pid:
                                    continue
                                # Recuperar la firma del prompt reescrito
                                with closing(sqlite3.connect(db_path, timeout=5)) as conn:
                                    conn.row_factory = sqlite3.Row
                                    r = conn.execute(
                                        "SELECT firma FROM prompts_reescritos WHERE id = ?",
                                        (pid,),
                                    ).fetchone()
                                    if r and r["firma"]:
                                        firmas_vistas.add(r["firma"])
                            for firma in firmas_vistas:
                                decision = ab.evaluar_candidato(firma)
                                if decision in ("promovido", "descartado"):
                                    logger.info(f"AB: firma {firma[:8]} → {decision}")
                    except Exception as e:
                        logger.debug(f"AB evaluar_candidato falló: {e}")

                except Exception as e:
                    logger.debug(f"Aprendizaje del plan falló: {e}")

            # ── V4.0-4: auto-crítica del LLM → reescritura de prompt ──
            # Cierra el ciclo: la evaluación del LLM (no solo los errores duros)
            # puede disparar una reescritura. Se guarda como 'candidato' y el
            # A/B (ya arreglado en V4.0-AB) decide si se promueve.
            #
            # Best-effort y acotado: umbral de score, tope por ejecución,
            # deduplicación por firma y respeto del presupuesto de la ejecución.
            try:
                from learning.self_critique import obtener_self_critic

                self_critic = obtener_self_critic(db_path, presupuesto=presupuesto_snap)
                if self_critic is not None:
                    critica = self_critic.revisar_ejecucion(ejecucion_id)
                    if critica.reescribio:
                        logger.info(
                            "🧠 Auto-crítica aplicada a la ejecución %s: "
                            "prompts %s", ejecucion_id, critica.reescrituras,
                        )
                    elif critica.saltadas:
                        logger.debug(
                            "Auto-crítica sin reescritura (%s): %s",
                            ejecucion_id, critica.saltadas,
                        )
            except Exception as e:
                logger.debug(f"Auto-crítica no disponible: {e}")

            logger.info(f"🧠 Aprendizaje procesado para ejecución {ejecucion_id}")
        except Exception as e:
            logger.debug(f"Aprendizaje en background falló: {e}")

    threading.Thread(target=_worker, name="learning-recorder", daemon=True).start()
    return ejecucion_id

def _id_agente_ejecucion(db_path: str, ejecucion_id: int, agente_id: str | None) -> int:
    """Fila de ``agentes_ejecucion`` de un agente, o 0 si no se puede saber.

    V4.0-4: sin esto la evaluación del agente se guardaba con
    ``agente_ejecucion_id = 0`` y era imposible atribuirla al prompt que la
    produjo, así que la auto-crítica reescribía el prompt equivocado.
    """
    if not agente_id:
        return 0
    try:
        with closing(sqlite3.connect(db_path, timeout=5)) as conn:
            fila = conn.execute(
                """SELECT id FROM agentes_ejecucion
                   WHERE ejecucion_id = ? AND agente_id = ?
                   ORDER BY orden ASC LIMIT 1""",
                (ejecucion_id, str(agente_id)),
            ).fetchone()
        return int(fila[0]) if fila else 0
    except Exception as e:
        logger.debug(f"No se pudo resolver agente_ejecucion_id: {e}")
        return 0


def _snapshot_de_agente(a) -> dict[str, Any] | None:
    try:
        deps_ids = list(getattr(a, "dependencias_ids", []) or [])
        deps_nombres = list(getattr(a, "dependencias_nombres", []) or [])
        if not deps_nombres and deps_ids: deps_nombres = list(deps_ids)
        proxy = SimpleNamespace(
            id=a.id, nombre=a.nombre, descripcion=getattr(a, "descripcion", "") or "",
            tipo=a.tipo, estado=a.estado, dependencias_ids=deps_ids, dependencias_nombres=deps_nombres,
            codigo_python=getattr(a, "codigo_python", "") or "", codigo=getattr(a, "codigo_python", "") or "",
            comando_shell=getattr(a, "comando_shell", "") or "", comando=getattr(a, "comando_shell", "") or "",
            prompt_llm=getattr(a, "prompt_llm", "") or "", prompt=getattr(a, "prompt_llm", "") or "",
            modelo_llm=getattr(a, "modelo_llm", "desconocido"), modelo=getattr(a, "modelo_llm", "desconocido"),
            temperatura_llm=getattr(a, "temperatura_llm", 0.7), max_tokens_llm=getattr(a, "max_tokens_llm", 4000),
            reasoning_effort_llm=getattr(a, "reasoning_effort_llm", "low"),
            thinking_enabled_llm=bool(getattr(a, "thinking_enabled_llm", False)),
            prompt_reescrito_id=getattr(a, "prompt_reescrito_id", 0),  # ✅ FASE 4c
            prompt_reescrito_motivo=getattr(a, "prompt_reescrito_motivo", ""),  # ✅ H2
            prompt_firma=getattr(a, "prompt_firma", ""),  # ✅ V4.0-AB
            url_http=getattr(a, "url_http", "") or "", url=getattr(a, "url_http", "") or "",
            metodo=getattr(a, "metodo_http", "GET"), operacion=getattr(a, "operacion_file", "") or "",
            archivo_origen=getattr(a, "archivo_origen", "") or "", archivo_destino=getattr(a, "archivo_destino", "") or "",
            fuente_items=getattr(a, "fuente_items", "") or "", codigo_por_item=getattr(a, "codigo_por_item", "") or "",
            continuar_en_error=bool(getattr(a, "continuar_en_error", False)),
            timeout_python=getattr(a, "timeout_python", 30), timeout_shell=getattr(a, "timeout_shell", 30),
            timeout_http=getattr(a, "timeout_http", 30), timeout_loop=getattr(a, "timeout_loop", 300),
            max_reintentos=getattr(a, "max_reintentos", 0), reintentos=getattr(a, "reintentos", 0),
            resultado={"__preview__": str(a.resultado or "")[:2000], "__tipo__": type(a.resultado).__name__ if a.resultado is not None else "None"},
        )
        return {"proxy": proxy, "nombre": a.nombre, "descripcion": getattr(a, "descripcion", "") or a.nombre,
                "resultado_texto": str(a.resultado or "")[:4000],
                "estado_real": a.estado.value if hasattr(a.estado, "value") else str(a.estado),
                "agente_id": a.id, "_id": a.id}
    except Exception as e:
        logger.debug(f"Snapshot aprendizaje falló para un agente: {e}")
        return None

def _construir_snapshot(scheduler) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    vistos: set = set()
    for a in getattr(scheduler, "_agentes_plan_original", []):
        aid = getattr(a, "id", None)
        if aid is not None and aid in vistos: continue
        item = _snapshot_de_agente(a)
        if item: vistos.add(item.pop("_id")); snapshot.append(item)
    for a in list(scheduler.agentes.values()):
        aid = getattr(a, "id", None)
        if aid is not None and aid in vistos: continue
        item = _snapshot_de_agente(a)
        if item: vistos.add(item.pop("_id")); snapshot.append(item)
    return snapshot

def _construir_resumen_plan(scheduler, plan) -> str:
    try:
        if plan and hasattr(plan, "agentes_generados"):
            ids_con_dependientes = set()
            for a in plan.agentes_generados:
                for dep_id in (a.dependencias_ids or []): ids_con_dependientes.add(dep_id)
            agentes_finales = [a for a in plan.agentes_generados if a.id not in ids_con_dependientes]
            if agentes_finales:
                agente_final = agentes_finales[-1]
                if agente_final.resultado: return str(agente_final.resultado)[:2000]
                return f"(agente final '{agente_final.nombre}' sin resultado)"
    except Exception as e: logger.debug(f"No se pudo construir resumen del plan: {e}")
    try:
        agentes = list(scheduler.agentes.values())
        if agentes:
            ultimo = agentes[-1]
            if ultimo.resultado: return str(ultimo.resultado)[:2000]
    except Exception: pass
    return "(sin resultado del plan)"
