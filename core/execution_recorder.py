#core/execution_recorder.py — FIX Bug #7
from __future__ import annotations
import logging
import threading
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
logger = logging.getLogger(__name__)

def registrar_ejecucion_en_aprendizaje(scheduler, db, plan, problema: str, duracion_total: float) -> Optional[int]:
    # 1. Guardar en DB - incluir plan original si hubo Plan B, deduplicando por id
    try:
        vistos_db: set = set()
        agentes_data = []
        for a in getattr(scheduler, "_agentes_plan_original", []):
            aid = getattr(a, "id", None)
            if aid is not None and aid in vistos_db: continue
            if aid is not None: vistos_db.add(aid)
            try: agentes_data.append(a.to_dict())
            except Exception: continue
        for a in scheduler.agentes.values():
            aid = getattr(a, "id", None)
            if aid is not None and aid in vistos_db: continue
            if aid is not None: vistos_db.add(aid)
            try: agentes_data.append(a.to_dict())
            except Exception: continue
        ejecucion_id = db.guardar_ejecucion(agentes_data, duracion_total)
    except Exception as e:
        logger.warning(f"No se pudo guardar la ejecución: {e}")
        return None
    agentes_snapshot = _construir_snapshot(scheduler)
    try: stats_snap = dict(scheduler.obtener_estadisticas())
    except Exception: stats_snap = {}
    db_path = db.db_path
    problema_snap = problema or ""
    plan_snap = plan
    
    def _worker():
        try:
            from learning import obtener_learning_engine
            from core.llm_client import obtener_llm_client_compartido
            engine = obtener_learning_engine(db_path=db_path, llm_client=obtener_llm_client_compartido())
            if engine is None: return

            # ✅ FASE 4c: registrar usos de prompts reescritos.
            # Cada agente LLM que usó una versión reescrita (activa o
            # candidata) deja constancia para poder atribuir el score.
            try:
                from learning.prompt_ab_evaluator import PromptABEvaluator
                ab = PromptABEvaluator(db_path)
                for item in agentes_snapshot:
                    pid = getattr(item["proxy"], "prompt_reescrito_id", 0)
                    if pid:
                        ab.registrar_uso(pid, ejecucion_id)
            except Exception as e:
                logger.debug(f"AB registrar_uso falló: {e}")

            # ── Aprendizaje por agente (ya existía) ──
            for item in agentes_snapshot:
                try:
                    engine.registrar_resultado_agente(
                        ejecucion_id=ejecucion_id, agente_ejecucion_id=0,
                        agente=item["proxy"], tarea=item["descripcion"] or item["nombre"],
                        resultado_texto=item["resultado_texto"], estado_real=item["estado_real"])
                except Exception as e:
                    logger.debug(f"Aprendizaje falló para {item['nombre']}: {e}")

            # ── Aprendizaje del plan (ya existía) ──
            if plan_snap is not None and problema_snap:
                try:
                    resultado_plan = _construir_resumen_plan(scheduler, plan_snap)
                    engine.registrar_resultado_plan(
                        ejecucion_id=ejecucion_id, plan=plan_snap,
                        tarea=problema_snap, resultado_texto=resultado_plan
                    )

                    # ✅ FASE 4c: propaga el score al A/B y decide.
                    try:
                        from learning.prompt_ab_evaluator import PromptABEvaluator
                        from learning.feedback_processor import FeedbackProcessor
                        ab = PromptABEvaluator(db_path)

                        # Score del plan completo (o del último agente si no hay plan)
                        score_plan = None
                        # obtener_learning_engine ya guardó la evaluación en
                        # `evaluaciones_llm`. Recuperamos la última de este plan.
                        import sqlite3
                        with sqlite3.connect(db_path, timeout=5) as conn:
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

                            # Evaluar candidatos de las firmas que se usaron
                            firmas_vistas = set()
                            for item in agentes_snapshot:
                                pid = getattr(item["proxy"], "prompt_reescrito_id", 0)
                                if not pid:
                                    continue
                                # Recuperar la firma del prompt reescrito
                                with sqlite3.connect(db_path, timeout=5) as conn:
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
            logger.info(f"🧠 Aprendizaje procesado para ejecución {ejecucion_id}")
        except Exception as e:
            logger.debug(f"Aprendizaje en background falló: {e}")

def _snapshot_de_agente(a) -> Optional[Dict[str, Any]]:
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
                "estado_real": a.estado.value if hasattr(a.estado, "value") else str(a.estado), "_id": a.id}
    except Exception as e:
        logger.debug(f"Snapshot aprendizaje falló para un agente: {e}")
        return None

def _construir_snapshot(scheduler) -> List[Dict[str, Any]]:
    snapshot: List[Dict[str, Any]] = []
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