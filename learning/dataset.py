"""
learning/dataset.py
Minería de datos: junta el historial de ejecuciones (agentes_ejecucion,
ya persistido por storage/database.py) con las evaluaciones del LLM
(evaluaciones_llm, nueva tabla de este módulo) para construir un dataset
de entrenamiento.
"""
from __future__ import annotations
from typing import List, Tuple, Dict, Any
import sqlite3

from .feature_extraction import (
    extraer_features_registro_historico,
    etiqueta_exito,
)


def minar_dataset(
    conn: sqlite3.Connection,
    minimo_muestras: int = 1,
) -> Tuple[List[Dict[str, Any]], List[int], List[float]]:
    """
    Devuelve (features_por_fila, etiquetas_exito, recompensas_llm).

    - etiquetas_exito: 1/0 según el 'estado' real de la ejecución (siempre
      disponible, no depende de que exista evaluación LLM).
    - recompensas_llm: score 0.0-1.0 de evaluaciones_llm si existe una
      evaluación para ese agente_ejecucion_id; si no hay evaluación
      todavía, se usa la etiqueta de éxito como proxy (0.0 o 1.0) para
      no perder la fila del entrenamiento.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT ae.*,
               (SELECT score FROM evaluaciones_llm ev
                WHERE ev.agente_ejecucion_id = ae.id
                ORDER BY ev.id DESC LIMIT 1) AS score_llm
        FROM agentes_ejecucion ae
        """
    )
    columnas = [d[0] for d in cursor.description]
    filas = [dict(zip(columnas, fila)) for fila in cursor.fetchall()]

    features, y_exito, y_reward = [], [], []
    for fila in filas:
        features.append(extraer_features_registro_historico(fila))
        exito = etiqueta_exito(fila)
        y_exito.append(exito)
        score_llm = fila.get("score_llm")
        y_reward.append(float(score_llm) if score_llm is not None else float(exito))

    if len(features) < minimo_muestras:
        return [], [], []
    return features, y_exito, y_reward


def minar_dataset_planes(
    conn: sqlite3.Connection,
) -> Tuple[List[int], List[float]]:
    """
    Devuelve los ejecucion_id que tienen evaluación de PLAN (alcance='plan')
    junto a su score, para poder reconstruir sus features si se guardó
    también el plan.
    """
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT ejecucion_id, score FROM evaluaciones_llm
        WHERE alcance = 'plan'
        ORDER BY id DESC
        """
    )
    filas = cursor.fetchall()
    return [f[0] for f in filas], [float(f[1]) for f in filas]