# tests/test_plan_failure_classifier.py
"""
Tests del clasificador de fallo de plan (V4.0, punto 3).

Lo que se fija aquí:

  - las features son SOLO pre-ejecución (nada de `estado`/`resultado`/`error`
    de los agentes: sería leakage de la etiqueta);
  - las features de un `ExecutionPlan` y las del histórico coinciden, para que
    entrenamiento y uso no se desalineen;
  - la etiqueta prefiere `exito_real` y, si no está, se deriva de las columnas
    duras sin exigir `problema` (documentado en el módulo);
  - sin datos suficientes NO se entrena y la predicción es neutra y explícita;
  - con datos suficientes entrena, reporta métricas fuera de muestra y ordena
    correctamente planes de riesgo distinto;
  - el orden de features se persiste con el modelo (si no, la predicción tras
    cargar de disco sería silenciosamente incorrecta).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from core.problem_solver.models import ExecutionPlan, StepPlan
from learning.plan_failure_classifier import (
    PlanFailureClassifier,
    construir_dataset,
    etiqueta_operativa,
    extraer_features_de_agentes,
    extraer_features_de_plan,
)


# ------------------------------------------------------------
# Helpers
# ------------------------------------------------------------
def _plan(pasos: list[tuple[str, str, list[str]]]) -> ExecutionPlan:
    return ExecutionPlan(
        id="p", titulo="p",
        pasos=[
            StepPlan(nombre=n, tipo_agente=t, dependencia_ids=list(d))
            for n, t, d in pasos
        ],
    )


def _base_datos(tmp_path, filas: list[dict], *, nombre="hist.db") -> str:
    """Crea una BD con esquema real y las ejecuciones indicadas.

    Cada fila: ``{"exito": bool, "agentes": [(agente_id, tipo, [refs])],
    "exito_real": int|None}``.
    """
    from storage.database import Database

    ruta = tmp_path / nombre
    Database(str(ruta)).close()

    with sqlite3.connect(str(ruta), timeout=10) as conn:
        for i, fila in enumerate(filas, start=1):
            exito = bool(fila["exito"])
            agentes = fila["agentes"]
            conn.execute(
                """INSERT INTO ejecuciones
                   (id, fecha, duracion_total, agentes_total, completados,
                    errores, cancelados, estado, aceptada, exito_real, problema)
                   VALUES (?, ?, 1.0, ?, ?, ?, 0, ?, ?, ?, ?)""",
                (
                    i, "2026-01-01T00:00:00", len(agentes),
                    len(agentes) if exito else 0,
                    0 if exito else 1,
                    "completada" if exito else "fallida",
                    1 if exito else 0,
                    fila.get("exito_real"),
                    fila.get("problema", ""),
                ),
            )
            for aid, tipo, refs in agentes:
                conn.execute(
                    """INSERT INTO agentes_ejecucion
                       (ejecucion_id, agente_id, nombre, tipo, estado, dependencias, orden)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        i, aid, aid, tipo,
                        "Completado" if exito else "Error",
                        json.dumps(refs), 0,
                    ),
                )
        conn.commit()
    return str(ruta)


def _agentes_dag() -> list[tuple[str, str, list[str]]]:
    return [
        ("a1", "HTTP", []),
        ("a2", "LLM", ["a1"]),
        ("a3", "File", ["a2"]),
    ]


# ------------------------------------------------------------
# 1. Features
# ------------------------------------------------------------
def test_features_del_plan_describen_el_dag():
    plan = _plan([
        ("A", "HTTP", []),
        ("B", "LLM", ["A"]),
        ("C", "File", ["B"]),
        ("D", "Python", []),
    ])
    f = extraer_features_de_plan(plan)
    assert f["n_pasos"] == 4
    assert f["n_raices"] == 2          # A y D
    assert f["n_hojas"] == 2           # C y D
    assert f["profundidad_max"] == 2   # A→B→C
    assert f["max_fan_in"] == 1
    assert f["max_fan_out"] == 1
    assert f["n_http"] == 1 and f["n_llm"] == 1
    assert f["n_file"] == 1 and f["n_python"] == 1
    assert f["n_tipos_distintos"] == 4
    assert f["ratio_con_dependencias"] == pytest.approx(0.5)


def test_features_de_plan_y_de_historico_coinciden():
    """Entrenar y predecir deben ver exactamente lo mismo."""
    plan = _plan([("A", "HTTP", []), ("B", "LLM", ["A"]), ("C", "File", ["B"])])
    filas = [
        {"agente_id": aid, "tipo": tipo, "dependencias": json.dumps(refs)}
        for aid, tipo, refs in _agentes_dag()
    ]
    assert extraer_features_de_plan(plan) == extraer_features_de_agentes(filas)


def test_dependencias_a_nodos_inexistentes_se_ignoran():
    f = extraer_features_de_agentes([
        {"agente_id": "a1", "tipo": "HTTP", "dependencias": '["no_existe"]'},
    ])
    assert f["n_raices"] == 1
    assert f["max_fan_in"] == 0


def test_dependencias_corruptas_no_rompen():
    f = extraer_features_de_agentes([
        {"agente_id": "a1", "tipo": "HTTP", "dependencias": "{no es json"},
    ])
    assert f["n_pasos"] == 1


def test_un_ciclo_no_cuelga_el_calculo_de_profundidad():
    f = extraer_features_de_agentes([
        {"agente_id": "a1", "tipo": "Python", "dependencias": '["a2"]'},
        {"agente_id": "a2", "tipo": "Python", "dependencias": '["a1"]'},
    ])
    assert f["n_pasos"] == 2
    assert f["profundidad_max"] >= 0


def test_plan_vacio_no_rompe():
    f = extraer_features_de_plan(ExecutionPlan(id="v", titulo="v"))
    assert f["n_pasos"] == 0
    assert f["n_raices"] == 0


def test_no_hay_leakage_con_estado_ni_resultado():
    """Columnas posteriores a la ejecución no deben influir en las features."""
    base = [{"agente_id": "a1", "tipo": "HTTP", "dependencias": "[]"}]
    con_leakage = [dict(base[0], estado="Error", resultado="boom", error="x", duracion=99)]
    assert extraer_features_de_agentes(base) == extraer_features_de_agentes(con_leakage)


# ------------------------------------------------------------
# 2. Etiqueta
# ------------------------------------------------------------
def test_etiqueta_prefiere_exito_real():
    fila = {"exito_real": 0, "aceptada": 1, "errores": 0, "estado": "completada"}
    assert etiqueta_operativa(fila) == 0
    assert etiqueta_operativa({"exito_real": 1, "aceptada": 0}) == 1


@pytest.mark.parametrize(
    "fila, esperado",
    [
        ({"aceptada": 0, "errores": 0, "estado": "completada"}, 0),
        ({"aceptada": 1, "errores": 2, "estado": "completada"}, 0),
        ({"aceptada": 1, "errores": 0, "cancelados": 1, "estado": "completada"}, 0),
        ({"aceptada": 1, "errores": 0, "estado": "fallida"}, 0),
        ({"aceptada": 1, "errores": 0, "estado": "completada",
          "agentes_total": 3, "completados": 2}, 0),
        ({"aceptada": 1, "errores": 0, "estado": "completada",
          "agentes_total": 3, "completados": 3}, 1),
        ({"aceptada": None, "errores": 0, "estado": "completada"}, None),
    ],
)
def test_etiqueta_operativa_desde_columnas_duras(fila, esperado):
    assert etiqueta_operativa(fila) == esperado


# ------------------------------------------------------------
# 3. Sin datos suficientes: no se inventa nada
# ------------------------------------------------------------
def test_sin_datos_suficientes_no_entrena(tmp_path):
    db = _base_datos(tmp_path, [
        {"exito": True, "agentes": _agentes_dag()},
        {"exito": False, "agentes": _agentes_dag()},
    ])
    clasificador = PlanFailureClassifier(tmp_path / "modelos", min_muestras=30)
    informe = clasificador.entrenar(db)

    assert informe["entrenado"] is False
    assert "datos insuficientes" in informe["motivo"]

    prediccion = clasificador.predecir(_plan([("A", "HTTP", [])]))
    assert prediccion.confianza == "sin_datos"
    assert prediccion.probabilidad_fallo == 0.5
    assert prediccion.disponible is False


def test_una_sola_clase_no_entrena(tmp_path):
    filas = [{"exito": True, "agentes": _agentes_dag()} for _ in range(50)]
    db = _base_datos(tmp_path, filas)
    clasificador = PlanFailureClassifier(tmp_path / "modelos", min_muestras=10)
    informe = clasificador.entrenar(db)

    assert informe["entrenado"] is False
    assert "una sola clase" in informe["motivo"]


def test_dataset_descarta_las_filas_indeterminadas(tmp_path):
    db = _base_datos(tmp_path, [
        {"exito": True, "agentes": _agentes_dag()},
        {"exito": True, "agentes": [], "exito_real": None},   # sin agentes
    ])
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE ejecuciones SET aceptada = NULL, estado = '' WHERE id = 1"
        )
        conn.commit()
    _features, etiquetas, resumen = construir_dataset(db)
    assert resumen["descartadas_sin_etiqueta"] >= 1
    assert len(etiquetas) == 0


# ------------------------------------------------------------
# 4. Con datos suficientes: entrena de verdad y con métricas honestas
# ------------------------------------------------------------
def _dataset_con_senal() -> list[dict]:
    """Fallan los planes que llevan un agente `Loop`; el resto, éxito."""
    filas = []
    for _ in range(100):
        filas.append({
            "exito": False,
            "agentes": [("b1", "Loop", []), ("b2", "Python", ["b1"])],
        })
    for _ in range(100):
        filas.append({
            "exito": True,
            "agentes": [("c1", "Python", []), ("c2", "File", ["c1"])],
        })
    return filas


def test_entrena_y_reporta_metricas_fuera_de_muestra(tmp_path):
    db = _base_datos(tmp_path, _dataset_con_senal())
    clasificador = PlanFailureClassifier(tmp_path / "modelos", min_muestras=30)
    informe = clasificador.entrenar(db)

    assert informe["entrenado"] is True
    assert informe["n_muestras"] == 200
    assert informe["exitos"] == 100 and informe["fallos"] == 100
    # Métricas presentes y fuera de muestra (cross_val_predict).
    for clave in ("auc", "acierto", "linea_base_mayoritaria",
                  "precision_fallo", "recall_fallo", "lift_decil_superior"):
        assert clave in informe
    # La señal es fuerte y separable: el clasificador debe verla.
    assert informe["auc"] > 0.9
    assert informe["lift_decil_superior"] > 1.5
    assert informe["accionable"] is True


def test_predice_mas_riesgo_al_plan_que_falla(tmp_path):
    db = _base_datos(tmp_path, _dataset_con_senal())
    clasificador = PlanFailureClassifier(tmp_path / "modelos", min_muestras=30)
    clasificador.entrenar(db)

    con_loop = _plan([("A", "Loop", []), ("B", "Python", ["A"])])
    sin_loop = _plan([("A", "Python", []), ("B", "File", ["A"])])

    riesgo_malo = clasificador.predecir(con_loop)
    riesgo_bueno = clasificador.predecir(sin_loop)

    assert riesgo_malo.disponible is True
    assert riesgo_malo.probabilidad_fallo > riesgo_bueno.probabilidad_fallo
    assert riesgo_malo.factores          # interpretabilidad


def test_registra_el_modelo_en_la_tabla_modelos_entrenados(tmp_path):
    db = _base_datos(tmp_path, _dataset_con_senal())
    clasificador = PlanFailureClassifier(tmp_path / "modelos", min_muestras=30)
    clasificador.entrenar(db)

    with sqlite3.connect(db) as conn:
        filas = conn.execute(
            "SELECT nombre, n_muestras_entrenamiento, metricas FROM modelos_entrenados"
        ).fetchall()
    assert len(filas) == 1
    assert filas[0][0] == "plan_failure"
    assert filas[0][1] == 200
    assert "auc" in json.loads(filas[0][2])


def test_la_recarga_conserva_el_orden_de_features(tmp_path):
    """Sin persistir el orden, el vector cambiaría y la predicción también."""
    db = _base_datos(tmp_path, _dataset_con_senal())
    ruta = tmp_path / "modelos"
    primero = PlanFailureClassifier(ruta, min_muestras=30)
    primero.entrenar(db)
    plan = _plan([("A", "Loop", []), ("B", "Python", ["A"])])
    antes = primero.predecir(plan).probabilidad_fallo

    segundo = PlanFailureClassifier(ruta, min_muestras=30)
    assert segundo.entrenado is True
    assert segundo._claves == primero._claves
    assert segundo.predecir(plan).probabilidad_fallo == pytest.approx(antes)


def test_estadisticas_reflejan_el_estado(tmp_path):
    db = _base_datos(tmp_path, _dataset_con_senal())
    clasificador = PlanFailureClassifier(tmp_path / "modelos", min_muestras=30)
    assert clasificador.estadisticas()["entrenado"] is False
    clasificador.entrenar(db)
    datos = clasificador.estadisticas()
    assert datos["entrenado"] is True
    assert datos["n_muestras"] == 200


# ------------------------------------------------------------
# 5. CLI
# ------------------------------------------------------------
def test_cli_entrenar_y_estado(tmp_path, capsys):
    from learning.plan_failure_classifier import main

    db = _base_datos(tmp_path, _dataset_con_senal())
    modelos = str(tmp_path / "modelos")

    assert main(["entrenar", "--db", db, "--modelos", modelos, "--json"]) == 0
    datos = json.loads(capsys.readouterr().out)
    assert datos["entrenado"] is True

    assert main(["estado", "--modelos", modelos, "--json"]) == 0
    estado = json.loads(capsys.readouterr().out)
    assert estado["entrenado"] is True


def test_cli_devuelve_1_si_no_puede_entrenar(tmp_path, capsys):
    from learning.plan_failure_classifier import main

    db = _base_datos(tmp_path, [{"exito": True, "agentes": _agentes_dag()}])
    rc = main(["entrenar", "--db", db, "--modelos", str(tmp_path / "m"), "--json"])
    assert rc == 1
    datos = json.loads(capsys.readouterr().out)
    assert datos["entrenado"] is False
