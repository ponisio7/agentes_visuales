"""
Módulo de aprendizaje de agentes_visuales.

Se integra sobre la MISMA base de datos que usa storage/database.py:
solo añade tablas nuevas (evaluaciones_llm, feedback_usuario,
modelos_entrenados), sin tocar el esquema existente.

Uso:
    from learning import obtener_learning_engine
    engine = obtener_learning_engine()       # singleton perezoso
    
    # O explícitamente (rara vez necesario):
    engine = obtener_learning_engine(db_path="...", llm_client=...)
"""
import logging

logger = logging.getLogger(__name__)

_engine_instance = None


def obtener_learning_engine(
    db_path: str | None = None,
    llm_client=None,
    ruta_modelos: str | None = None,
    modelo_evaluador: str | None = None,
):
    """
    Devuelve el LearningEngine singleton.
    
    La primera llamada crea la instancia. Llamadas posteriores devuelven
    la misma instancia (ignoran argumentos nuevos para evitar sorpresas).
    
    Si algo falla al crearlo (falta scikit-learn, falta API key, etc.),
    devuelve None silenciosamente. Esto es intencional: la app principal
    NO debe romperse por un fallo del subsistema de aprendizaje.
    """
    global _engine_instance
    
    if _engine_instance is not None:
        return _engine_instance
    
    try:
        from .engine import LearningEngine
        
        if db_path is None:
            # Fallback: misma ruta por defecto que storage/database.py
            db_path = "agent_history.db"
        
        _engine_instance = LearningEngine(
            db_path=db_path,
            llm_client=llm_client,
            ruta_modelos=ruta_modelos,
            modelo_evaluador=modelo_evaluador,
        )
        logger.info(f"🧠 LearningEngine inicializado (db={db_path})")
        return _engine_instance
    
    except ImportError as e:
        logger.warning(
            f"LearningEngine no disponible (¿falta scikit-learn?): {e}"
        )
        return None
    except Exception as e:
        logger.warning(f"LearningEngine falló al inicializar: {e}")
        return None


def reset_learning_engine():
    """Fuerza la recreación del singleton (útil en tests)."""
    global _engine_instance
    _engine_instance = None


__all__ = ["obtener_learning_engine", "reset_learning_engine"]
