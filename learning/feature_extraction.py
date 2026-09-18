"""
learning/feature_extraction.py
Convierte un Agente (antes de ejecutar), un plan (ExecutionPlan) o un
registro histórico de agentes_ejecucion (después de ejecutar) en un
diccionario de features homogéneo, para que el mismo vectorizador
(DictVectorizer) sirva tanto en entrenamiento como en predicción en vivo.
"""
from __future__ import annotations

import json
from typing import Any

CAMPOS_TEXTO_POR_TIPO = {
    "Python": ["codigo"],
    "Shell": ["comando"],
    "HTTP": ["url", "metodo"],
    "LLM": ["prompt", "modelo"],
    "File": ["operacion", "archivo_origen", "archivo_destino"],
    "Loop": ["fuente_items", "codigo_por_item"],
}


def _longitud_segura(valor: Any) -> int:
    if not valor:
        return 0
    try:
        return len(str(valor))
    except Exception:
        return 0


def extraer_features_agente(agente: Any) -> dict[str, Any]:
    """
    Features de un objeto Agente REAL, previo a la ejecución.
    Usa getattr con default para tolerar atributos ausentes según el tipo
    (un agente Shell no tiene 'prompt', uno LLM no tiene 'comando', etc.).
    """
    tipo = getattr(agente, "tipo", None)
    tipo_str = getattr(tipo, "value", tipo) or "Desconocido"

    deps = getattr(agente, "dependencias_nombres", []) or []
    campos_texto = CAMPOS_TEXTO_POR_TIPO.get(tipo_str, [])
    longitud_config = sum(
        _longitud_segura(getattr(agente, c, None)) for c in campos_texto
    )

    features: dict[str, Any] = {
        "tipo": tipo_str,
        "num_dependencias": len(deps),
        "longitud_nombre": _longitud_segura(getattr(agente, "nombre", "")),
        "longitud_config": longitud_config,
        "tiene_descripcion": bool(getattr(agente, "descripcion", "")),
        "continuar_en_error": bool(getattr(agente, "continuar_en_error", False)),
    }

    # Timeout según el tipo (cada tipo tiene un nombre distinto)
    timeouts_por_tipo = {
        "Python": "timeout_python",
        "Shell": "timeout_shell",
        "HTTP": "timeout_http",
        "LLM": None,
        "File": None,
        "Loop": "timeout_loop",
    }
    campo_timeout = timeouts_por_tipo.get(tipo_str)
    if campo_timeout:
        features["timeout"] = float(getattr(agente, campo_timeout, 30) or 30)
    else:
        features["timeout"] = 30.0

    if tipo_str == "LLM":
        features["modelo_llm"] = getattr(agente, "modelo_llm", "desconocido")
        features["temperatura"] = float(
            getattr(agente, "temperatura_llm", 0.7) or 0.7
        )
        features["max_tokens"] = int(
            getattr(agente, "max_tokens_llm", 4000) or 4000
        )
        features["reasoning_effort"] = getattr(
            agente, "reasoning_effort_llm", "low"
        )
        features["thinking_enabled"] = bool(
            getattr(agente, "thinking_enabled_llm", False)
        )

    return features


def extraer_features_registro_historico(fila: dict[str, Any]) -> dict[str, Any]:
    """
    Features equivalentes a partir de una fila de agentes_ejecucion (para
    entrenar). No todos los campos del Agente original están disponibles
    aquí (el historial no guarda 'codigo'/'prompt' completos), así que se
    aproxima con lo que sí persiste la tabla actual.
    """
    dependencias = fila.get("dependencias") or "[]"
    try:
        num_dependencias = len(json.loads(dependencias))
    except Exception:
        num_dependencias = 0

    return {
        "tipo": fila.get("tipo", "Desconocido"),
        "num_dependencias": num_dependencias,
        "longitud_nombre": _longitud_segura(fila.get("nombre")),
        "longitud_config": 0,
        "tiene_descripcion": False,
        "timeout": 30.0,
        "continuar_en_error": False,
        "orden": fila.get("orden", 0),
    }


def extraer_features_plan(plan: Any) -> dict[str, Any]:
    """Features agregadas de un ExecutionPlan completo (para el PlanScorer)."""
    pasos = getattr(plan, "pasos", []) or []
    tipos = [getattr(p, "tipo_agente", "Desconocido") for p in pasos]

    conteo_tipos: dict[str, Any] = {}
    for t in tipos:
        clave = f"tipo_{t}"
        conteo_tipos[clave] = conteo_tipos.get(clave, 0) + 1

    features: dict[str, Any] = {
        "num_pasos": len(pasos),
        "num_criticos": sum(
            1 for p in pasos if getattr(p, "es_critico", False)
        ),
        "max_dependencias": max(
            (len(getattr(p, "dependencia_ids", []) or []) for p in pasos),
            default=0,
        ),
        "tiene_llm": any(t == "LLM" for t in tipos),
        "tiene_loop": any(t == "Loop" for t in tipos),
        "estimacion_tiempo": float(
            getattr(plan, "estimacion_tiempo", 0.0) or 0.0
        ),
    }
    features.update(conteo_tipos)
    return features


def etiqueta_exito(fila: dict[str, Any]) -> int:
    """1 = éxito, 0 = fallo, según el campo 'estado' del historial."""
    estado = (fila.get("estado") or "").lower()
    return 1 if estado in (
        "completado", "completada", "exito", "éxito", "ok"
    ) else 0
