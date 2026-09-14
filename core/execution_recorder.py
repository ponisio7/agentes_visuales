#core/execution_recorder.py
"""
Registra una ejecución en la BD y alimenta al LearningEngine.
"""
from __future__ import annotations
import logging
import threading
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
logger = logging.getLogger(__name__)
def registrar_ejecucion_en_aprendizaje(
    scheduler,
    db,
    plan,
    problema: str,
    duracion_total: float,
) -> Optional[int]:
    """Guarda la ejecución y dispara el aprendizaje en background."""
    # 1. Guardar en DB
    try:
        agentes_data = [a.to_dict() for a in scheduler.agentes.values()]
        ejecucion_id = db.guardar_ejecucion(agentes_data, duracion_total)
    except Exception as e:
        logger.warning(f"No se pudo guardar la ejecución: {e}")
        return None
    # 2. Snapshot de agentes en el hilo principal
    agentes_snapshot = _construir_snapshot(scheduler)
    # 3. Snapshot de stats
    try:
        stats_snap = dict(scheduler.obtener_estadisticas())
    except Exception:
        stats_snap = {}
    db_path = db.db_path
    problema_snap = problema or ""
    plan_snap = plan
    # 4. Worker en background
    def _worker():
        try:
            from learning import obtener_learning_engine
            from core.llm_client import obtener_llm_client_compartido
            engine = obtener_learning_engine(
                db_path=db_path,
                llm_client=obtener_llm_client_compartido(),
            )
            if engine is None:
                logger.debug("LearningEngine no disponible, saltando")
                return
            for item in agentes_snapshot:
                try:
                    engine.registrar_resultado_agente(
                        ejecucion_id=ejecucion_id,
                        agente_ejecucion_id=0,
                        agente=item["proxy"],
                        tarea=item["descripcion"] or item["nombre"],
                        resultado_texto=item["resultado_texto"],
                        estado_real=item["estado_real"],
                    )
                except Exception as e:
                    logger.debug(f"Aprendizaje falló para {item['nombre']}: {e}")
            if plan_snap is not None and problema_snap:
                try:
                    resultado_plan = _construir_resumen_plan(scheduler, plan_snap)
                    engine.registrar_resultado_plan(
                        ejecucion_id=ejecucion_id,
                        plan=plan_snap,
                        tarea=problema_snap,
                        resultado_texto=resultado_plan,
                    )
                except Exception as e:
                    logger.debug(f"Aprendizaje del plan falló: {e}")
            logger.info(f"🧠 Aprendizaje procesado para ejecución {ejecucion_id}")
        except Exception as e:
            logger.debug(f"Aprendizaje en background falló: {e}")
    hilo = threading.Thread(target=_worker, name=f"learning-{ejecucion_id}", daemon=True)
    hilo.start()
    return ejecucion_id
def _construir_snapshot(scheduler) -> List[Dict[str, Any]]:
    """Snapshot inmutable de los agentes, tomado en el hilo principal."""
    snapshot = []
    for a in list(scheduler.agentes.values()):
        try:
            deps_ids = list(getattr(a, "dependencias_ids", []) or [])
            deps_nombres = list(getattr(a, "dependencias_nombres", []) or [])
            if not deps_nombres and deps_ids:
                deps_nombres = list(deps_ids)
            proxy = SimpleNamespace(
                id=a.id,
                nombre=a.nombre,
                descripcion=getattr(a, "descripcion", "") or "",
                tipo=a.tipo,
                estado=a.estado,
                dependencias_ids=deps_ids,
                dependencias_nombres=deps_nombres,
                codigo_python=getattr(a, "codigo_python", "") or "",
                codigo=getattr(a, "codigo_python", "") or "",
                comando_shell=getattr(a, "comando_shell", "") or "",
                comando=getattr(a, "comando_shell", "") or "",
                prompt_llm=getattr(a, "prompt_llm", "") or "",
                prompt=getattr(a, "prompt_llm", "") or "",
                modelo_llm=getattr(a, "modelo_llm", "desconocido"),
                modelo=getattr(a, "modelo_llm", "desconocido"),
                temperatura_llm=getattr(a, "temperatura_llm", 0.7),
                max_tokens_llm=getattr(a, "max_tokens_llm", 4000),
                reasoning_effort_llm=getattr(a, "reasoning_effort_llm", "low"),
                thinking_enabled_llm=bool(getattr(a, "thinking_enabled_llm", False)),
                url_http=getattr(a, "url_http", "") or "",
                url=getattr(a, "url_http", "") or "",
                metodo=getattr(a, "metodo_http", "GET"),
                operacion=getattr(a, "operacion_file", "") or "",
                archivo_origen=getattr(a, "archivo_origen", "") or "",
                archivo_destino=getattr(a, "archivo_destino", "") or "",
                fuente_items=getattr(a, "fuente_items", "") or "",
                codigo_por_item=getattr(a, "codigo_por_item", "") or "",
                continuar_en_error=bool(getattr(a, "continuar_en_error", False)),
                timeout_python=getattr(a, "timeout_python", 30),
                timeout_shell=getattr(a, "timeout_shell", 30),
                timeout_http=getattr(a, "timeout_http", 30),
                timeout_loop=getattr(a, "timeout_loop", 300),
                max_reintentos=getattr(a, "max_reintentos", 0),
                reintentos=getattr(a, "reintentos", 0),
                resultado={
                    "__preview__": str(a.resultado or "")[:2000],
                    "__tipo__": type(a.resultado).__name__ if a.resultado is not None else "None",
                },
            )
            snapshot.append({
                "proxy": proxy,
                "nombre": a.nombre,
                "descripcion": getattr(a, "descripcion", "") or a.nombre,
                "resultado_texto": str(a.resultado or "")[:4000],
                "estado_real": a.estado.value if hasattr(a.estado, "value") else str(a.estado),
            })
        except Exception as e:
            logger.debug(f"Snapshot aprendizaje falló para un agente: {e}")
    return snapshot
def _construir_resumen_plan(scheduler, plan) -> str:
    """Extrae un resultado_texto representativo del plan (salida del agente final)."""
    try:
        # Buscar el agente "final" del plan: el que no tiene dependientes
        if plan and hasattr(plan, "agentes_generados"):
            ids_con_dependientes = set()
            for a in plan.agentes_generados:
                for dep_id in (a.dependencias_ids or []):
                    ids_con_dependientes.add(dep_id)
            # El agente final es el que no aparece como dependencia de nadie
            agentes_finales = [
                a for a in plan.agentes_generados
                if a.id not in ids_con_dependientes
            ]
            # Tomar el último en ejecutarse (si hay varios)
            if agentes_finales:
                agente_final = agentes_finales[-1]
                if agente_final.resultado:
                    return str(agente_final.resultado)[:2000]
                return f"(agente final '{agente_final.nombre}' sin resultado)"
    except Exception as e:
        logger.debug(f"No se pudo construir resumen del plan: {e}")
    # Fallback: usar el resultado del último agente del scheduler
    try:
        agentes = list(scheduler.agentes.values())
        if agentes:
            ultimo = agentes[-1]
            if ultimo.resultado:
                return str(ultimo.resultado)[:2000]
    except Exception:
        pass
    return "(sin resultado del plan)"