# learning/plan_failure_classifier.py
"""
PlanFailureClassifier — probabilidad de FALLO de un plan antes de ejecutarlo.

Por qué existe (V4.0, punto 3):

  El histórico ya era fiable para *retrieval* (V3.8-4: `HistoricalIndexer` +
  `exito_real`), pero solo se estaba usando por similitud de embeddings. Este
  módulo convierte ese corpus en un clasificador: dado un plan **todavía no
  ejecutado**, estima la probabilidad de que la ejecución falle, con qué
  confianza y qué rasgos del plan empujan el riesgo.

  No sustituye a nada:
    - `FailurePredictor` (learning/models.py) es por TIPO DE AGENTE.
    - `PlanScorer` (learning/models.py) es un regresor de CALIDAD esperada.
    - Este es un clasificador de FALLO a nivel de PLAN/EJECUCIÓN COMPLETA.

Etiqueta: honesta y explícita.

  1. Si `ejecuciones.exito_real` está calculado (lo rellena el
     `HistoricalIndexer`), se usa tal cual: es el veredicto compuesto y además
     incorpora el feedback del usuario.
  2. Si no, se deriva una etiqueta **operativa** de las mismas columnas duras
     que `HistoricalIndexer.calcular_exito_real` (aceptada, errores,
     cancelados, estado, completados/agentes_total) **sin exigir `problema`**.

  ¿Por qué la alternativa? Porque exigir `problema` deja el histórico entero
  sin etiquetar (a fecha de hoy solo 4 de 473 ejecuciones lo tienen, y todas
  del mismo lado), y entonces no se puede entrenar nada. La etiqueta operativa
  es más débil —no ve el feedback del usuario— y por eso se documenta aquí y
  se reporta su balance de clases junto a las métricas.

Features: SOLO lo conocido antes de ejecutar.

  Estructura del plan (nº de pasos, reparto por tipo, raíces, hojas, fan-in y
  fan-out máximos, profundidad del DAG, si declara críticos y contratos). Se
  extraen igual desde un `ExecutionPlan` (plan nuevo) y desde las filas de
  `agentes_ejecucion` (histórico), para que entrenamiento y uso coincidan.

  **No** se usa `estado`, `duracion`, `resultado` ni `error` de los agentes:
  son posteriores a la ejecución y filtrarían la etiqueta (leakage).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

NOMBRE_MODELO = "plan_failure"
# Con menos de esto no se entrena: preferimos decir «no sé» a publicar un
# clasificador que solo ha visto un puñado de casos.
MIN_MUESTRAS = 30
MIN_POR_CLASE = 5

# Confianza de la predicción según las muestras vistas.
UMBRAL_CONFIANZA_MEDIA = 100
UMBRAL_CONFIANZA_ALTA = 400

TIPOS_CONOCIDOS = {
    "python": "n_python",
    "llm": "n_llm",
    "http": "n_http",
    "browser": "n_browser",
    "search": "n_search",
    "file": "n_file",
    "shell": "n_shell",
    "loop": "n_loop",
}


# ============================================================
# Resultado
# ============================================================
@dataclass
class PrediccionFallo:
    """Riesgo estimado de que un plan falle antes de ejecutarlo."""
    probabilidad_fallo: float
    confianza: str                 # 'sin_datos' | 'baja' | 'media' | 'alta'
    n_muestras: int = 0
    factores: list[str] = field(default_factory=list)

    @property
    def disponible(self) -> bool:
        return self.confianza != "sin_datos"

    def to_dict(self) -> dict[str, Any]:
        return {
            "probabilidad_fallo": round(float(self.probabilidad_fallo), 4),
            "confianza": self.confianza,
            "n_muestras": int(self.n_muestras),
            "factores": list(self.factores),
            "disponible": self.disponible,
        }


# ============================================================
# Features
# ============================================================
def _normalizar_tipo(tipo: Any) -> str:
    return str(tipo or "").strip().lower().replace(" script", "")


def _nodos_desde_plan(plan: Any) -> list[tuple[str, str, list[str]]]:
    """``[(clave, tipo, refs)]`` de un ``ExecutionPlan``.

    En un ``StepPlan`` las dependencias referencian NOMBRES de paso.
    """
    nodos: list[tuple[str, str, list[str]]] = []
    for paso in getattr(plan, "pasos", None) or []:
        clave = str(getattr(paso, "nombre", "") or getattr(paso, "id", ""))
        refs = [str(r) for r in (getattr(paso, "dependencia_ids", None) or [])]
        nodos.append((clave, _normalizar_tipo(getattr(paso, "tipo_agente", "")), refs))
    return nodos


def _nodos_desde_historico(filas: list[dict]) -> list[tuple[str, str, list[str]]]:
    """``[(clave, tipo, refs)]`` de filas de ``agentes_ejecucion``.

    Ahí las dependencias referencian ``agente_id``.
    """
    nodos: list[tuple[str, str, list[str]]] = []
    for fila in filas:
        clave = str(fila.get("agente_id") or fila.get("nombre") or "")
        crudo = fila.get("dependencias")
        refs: list[str] = []
        if isinstance(crudo, str) and crudo.strip():
            try:
                datos = json.loads(crudo)
                if isinstance(datos, list):
                    refs = [str(r) for r in datos]
            except (json.JSONDecodeError, ValueError):
                refs = []
        elif isinstance(crudo, list):
            refs = [str(r) for r in crudo]
        nodos.append((clave, _normalizar_tipo(fila.get("tipo")), refs))
    return nodos


def _profundidad_max(nodos: list[tuple[str, str, list[str]]]) -> int:
    """Longitud, en aristas, del camino más largo del DAG (tolerante a ciclos)."""
    refs_por_clave = {clave: refs for clave, _tipo, refs in nodos}
    memo: dict[str, int] = {}

    def profundidad(clave: str, visitando: frozenset[str]) -> int:
        if clave in memo:
            return memo[clave]
        if clave in visitando:            # ciclo: no colgarse
            return 0
        refs = [r for r in refs_por_clave.get(clave, []) if r in refs_por_clave]
        valor = 0
        if refs:
            valor = 1 + max(
                profundidad(r, visitando | {clave}) for r in refs
            )
        memo[clave] = valor
        return valor

    if not nodos:
        return 0
    return max(profundidad(clave, frozenset()) for clave, _t, _r in nodos)


def extraer_features(nodos: list[tuple[str, str, list[str]]]) -> dict[str, float]:
    """Features canónicas de la estructura de un plan."""
    n = len(nodos)
    claves = {clave for clave, _t, _r in nodos}
    features: dict[str, float] = {
        "n_pasos": float(n),
        "n_raices": 0.0,
        "n_hojas": 0.0,
        "max_fan_in": 0.0,
        "max_fan_out": 0.0,
        "profundidad_max": 0.0,
        "n_tipos_distintos": 0.0,
        "ratio_con_dependencias": 0.0,
    }
    for clave_destino in TIPOS_CONOCIDOS.values():
        features[clave_destino] = 0.0
    features["n_otros"] = 0.0

    if n == 0:
        return features

    fan_out: dict[str, int] = {}
    con_dependencias = 0
    tipos: set[str] = set()

    for _clave, tipo, refs in nodos:
        tipos.add(tipo)
        bucket = TIPOS_CONOCIDOS.get(tipo)
        features[bucket if bucket else "n_otros"] += 1.0

        refs_validas = [r for r in refs if r in claves]
        if not refs_validas:
            features["n_raices"] += 1.0
        else:
            con_dependencias += 1

        features["max_fan_in"] = max(features["max_fan_in"], float(len(refs_validas)))
        for r in refs_validas:
            fan_out[r] = fan_out.get(r, 0) + 1

    if fan_out:
        features["max_fan_out"] = float(max(fan_out.values()))
    features["n_hojas"] = float(sum(1 for c in claves if c not in fan_out))
    features["profundidad_max"] = float(_profundidad_max(nodos))
    features["n_tipos_distintos"] = float(len(tipos))
    features["ratio_con_dependencias"] = con_dependencias / n
    return features


def extraer_features_de_plan(plan: Any) -> dict[str, float]:
    """Features de un plan nuevo, antes de ejecutarlo."""
    return extraer_features(_nodos_desde_plan(plan))


def extraer_features_de_agentes(filas: list[dict]) -> dict[str, float]:
    """Features de una ejecución histórica, desde ``agentes_ejecucion``."""
    return extraer_features(_nodos_desde_historico(filas))


# ============================================================
# Etiqueta
# ============================================================
def _a_bool(valor: Any) -> bool | None:
    if valor is None:
        return None
    if isinstance(valor, bool):
        return valor
    try:
        return bool(int(valor))
    except (TypeError, ValueError):
        return None


def etiqueta_operativa(fila: dict) -> int | None:
    """1 = éxito, 0 = fallo, ``None`` = indeterminado (se descarta).

    Prefiere ``exito_real`` (veredicto compuesto del ``HistoricalIndexer``,
    que incorpora el feedback del usuario) y, si no está, deriva la etiqueta
    de las columnas duras sin exigir ``problema``. Ver el docstring del módulo.
    """
    exito_real = fila.get("exito_real")
    if exito_real is not None:
        valor = _a_bool(exito_real)
        return None if valor is None else (1 if valor else 0)

    aceptada = _a_bool(fila.get("aceptada"))
    if aceptada is False:
        return 0

    try:
        errores = int(fila.get("errores") or 0)
        cancelados = int(fila.get("cancelados") or 0)
        completados = int(fila.get("completados") or 0)
        agentes_total = int(fila.get("agentes_total") or 0)
    except (TypeError, ValueError):
        return None

    estado = str(fila.get("estado") or "").strip().lower()
    if errores > 0 or cancelados > 0:
        return 0
    if estado and estado not in ("completada", "completado", "ok", "success"):
        return 0
    if agentes_total and completados < agentes_total:
        return 0
    if aceptada is True:
        return 1
    return None


# ============================================================
# Dataset
# ============================================================
def construir_dataset(db_path: str) -> tuple[list[dict], list[int], dict]:
    """``(features, etiquetas, resumen)`` a partir del histórico real."""
    features: list[dict] = []
    etiquetas: list[int] = []
    resumen = {
        "ejecuciones": 0,
        "con_etiqueta": 0,
        "descartadas_sin_etiqueta": 0,
        "descartadas_sin_agentes": 0,
        "exitos": 0,
        "fallos": 0,
        "fuente_exito_real": 0,
    }
    try:
        with closing(sqlite3.connect(db_path, timeout=10)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            tablas = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "ejecuciones" not in tablas or "agentes_ejecucion" not in tablas:
                return [], [], resumen

            filas = [dict(r) for r in conn.execute("SELECT * FROM ejecuciones")]
            resumen["ejecuciones"] = len(filas)
            for fila in filas:
                etiqueta = etiqueta_operativa(fila)
                if etiqueta is None:
                    resumen["descartadas_sin_etiqueta"] += 1
                    continue
                agentes = [
                    dict(r) for r in conn.execute(
                        """SELECT agente_id, nombre, tipo, dependencias
                           FROM agentes_ejecucion WHERE ejecucion_id = ?""",
                        (fila.get("id"),),
                    )
                ]
                if not agentes:
                    resumen["descartadas_sin_agentes"] += 1
                    continue
                features.append(extraer_features_de_agentes(agentes))
                etiquetas.append(etiqueta)
                resumen["con_etiqueta"] += 1
                if etiqueta == 1:
                    resumen["exitos"] += 1
                else:
                    resumen["fallos"] += 1
                if fila.get("exito_real") is not None:
                    resumen["fuente_exito_real"] += 1
    except Exception as e:
        logger.warning(f"No se pudo construir el dataset de fallos: {e}")
    return features, etiquetas, resumen


# ============================================================
# Clasificador
# ============================================================
class PlanFailureClassifier:
    """Clasificador logístico (scikit-learn) de fallo de plan.

    Se mantiene deliberadamente ligero e interpretable: escala + regresión
    logística. Con los datos disponibles hoy un modelo más expresivo solo
    sobreajustaría.
    """

    def __init__(
        self,
        ruta_modelos: str | Path = "learning_models",
        *,
        min_muestras: int = MIN_MUESTRAS,
        min_por_clase: int = MIN_POR_CLASE,
    ):
        self.ruta_modelos = Path(ruta_modelos)
        self.ruta_modelos.mkdir(parents=True, exist_ok=True)
        self.min_muestras = int(min_muestras)
        self.min_por_clase = int(min_por_clase)
        self.pipeline = None
        self.entrenado = False
        self.n_muestras = 0
        self.metricas: dict[str, Any] = {}
        # Orden canónico de las features. Se persiste con el modelo: si se
        # reconstruyera al vuelo tras cargar de disco, el vector podría salir
        # en otro orden y la predicción sería silenciosamente incorrecta.
        self._claves: list[str] = []
        self.cargar()

    # ------------------------------------------------------------
    # Persistencia
    # ------------------------------------------------------------
    def _ruta(self) -> Path:
        return self.ruta_modelos / f"{NOMBRE_MODELO}.joblib"

    def cargar(self) -> bool:
        ruta = self._ruta()
        if not ruta.exists():
            return False
        try:
            import joblib

            datos = joblib.load(ruta)
            self.pipeline = datos.get("pipeline")
            self._claves = list(datos.get("claves") or [])
            self.entrenado = bool(datos.get("entrenado"))
            self.n_muestras = int(datos.get("n_muestras") or 0)
            self.metricas = dict(datos.get("metricas") or {})
            logger.debug(
                f"PlanFailureClassifier cargado ({self.n_muestras} muestras)"
            )
            return self.entrenado
        except Exception as e:
            logger.warning(f"No se pudo cargar el clasificador de fallos: {e}")
            return False

    def guardar(self) -> None:
        try:
            import joblib

            joblib.dump(
                {
                    "pipeline": self.pipeline,
                    "claves": list(self._claves),
                    "entrenado": self.entrenado,
                    "n_muestras": self.n_muestras,
                    "metricas": self.metricas,
                },
                self._ruta(),
            )
        except Exception as e:
            logger.warning(f"No se pudo guardar el clasificador de fallos: {e}")

    # ------------------------------------------------------------
    # Entrenamiento
    # ------------------------------------------------------------
    def entrenar(
        self, db_path: str = "agent_history.db", *, registrar: bool = True
    ) -> dict[str, Any]:
        """Entrena desde el histórico. Devuelve un informe honesto.

        Nunca lanza: si no hay datos suficientes deja el modelo sin entrenar y
        lo dice en ``metricas['motivo']``.
        """
        features, etiquetas, resumen = construir_dataset(db_path)
        n = len(etiquetas)
        positivos = sum(etiquetas)
        negativos = n - positivos
        self.metricas = {
            "dataset": resumen,
            "n_muestras": n,
            "exitos": positivos,
            "fallos": negativos,
        }

        if n < self.min_muestras:
            self.entrenado = False
            self.metricas["motivo"] = (
                f"datos insuficientes: {n} muestras etiquetadas "
                f"(mínimo {self.min_muestras})"
            )
            self.metricas["entrenado"] = False
            logger.warning("PlanFailureClassifier: %s", self.metricas["motivo"])
            return self.metricas
        if min(positivos, negativos) < self.min_por_clase:
            self.entrenado = False
            self.metricas["motivo"] = (
                f"una sola clase o casi: {positivos} éxitos / {negativos} fallos "
                f"(mínimo {self.min_por_clase} por clase)"
            )
            self.metricas["entrenado"] = False
            logger.warning("PlanFailureClassifier: %s", self.metricas["motivo"])
            return self.metricas

        try:
            import numpy as np
            from sklearn.linear_model import LogisticRegression
            from sklearn.metrics import (
                accuracy_score,
                precision_score,
                recall_score,
                roc_auc_score,
            )
            from sklearn.model_selection import StratifiedKFold, cross_val_predict
            from sklearn.pipeline import Pipeline
            from sklearn.preprocessing import StandardScaler

            self._claves = sorted({k for f in features for k in f})
            X = [self._vector(f) for f in features]
            # ⚠️ La clase positiva del modelo es el FALLO (y=1). Así
            # `predict_proba[:, 1]` es P(fallo) y todas las métricas de abajo
            # (AUC, precisión/cobertura de fallo y lift del decil de riesgo)
            # hablan del mismo suceso. Tomar[:, 1] con y=éxito invertía el
            # sentido del score sin que nada fallara.
            y = [1 - e for e in etiquetas]

            # Sin `class_weight`: para un SCORE de riesgo lo que importa es el
            # ranking (AUC), que no cambia, y sin ponderar la probabilidad está
            # mejor calibrada. Además, ponderar hacía que el acierto con umbral
            # 0.5 cayera por debajo de la línea base trivial (0.69 vs 0.84):
            # un mal negocio para algo que se lee, no se umbraliza.
            self.pipeline = Pipeline([
                ("escala", StandardScaler()),
                ("modelo", LogisticRegression(max_iter=2000, random_state=42)),
            ])

            # Métricas fuera de muestra (out-of-fold) para no autoengañarnos.
            pliegues = max(2, min(5, min(positivos, negativos)))
            cv = StratifiedKFold(n_splits=pliegues, shuffle=True, random_state=42)
            probabilidades = cross_val_predict(
                self.pipeline, X, y, cv=cv, method="predict_proba"
            )
            proba_fallo = np.asarray([float(p[1]) for p in probabilidades])
            auc = float(roc_auc_score(y, proba_fallo))
            predicho = (proba_fallo >= 0.5).astype(int)
            acierto = float(accuracy_score(y, predicho))
            # Línea base: acertar siempre la clase mayoritaria del problema
            # (predecir «no falla»), que es contra lo que hay que compararse.
            linea_base = max(positivos, negativos) / n
            tasa_base_fallo = negativos / n

            # Lift del decil de mayor riesgo: la métrica que de verdad dice si
            # el score es ACCIONABLE. Un AUC bueno con lift ≈ 1 significa que
            # ordenar no concentra fallos donde importa (la cola).
            k = max(1, n // 10)
            orden = np.argsort(-proba_fallo)[:k]
            tasa_top = float(sum(y[i] for i in orden) / k)
            lift_decil = tasa_top / tasa_base_fallo if tasa_base_fallo else 0.0

            self.pipeline.fit(X, y)
            self.entrenado = True
            self.n_muestras = n
            self.metricas.update({
                "entrenado": True,
                "auc": round(auc, 4),
                "acierto": round(acierto, 4),
                "linea_base_mayoritaria": round(linea_base, 4),
                "precision_fallo": round(
                    float(precision_score(y, predicho, zero_division=0)), 4
                ),
                "recall_fallo": round(
                    float(recall_score(y, predicho, zero_division=0)), 4
                ),
                "lift_decil_superior": round(float(lift_decil), 4),
                "accionable": bool(lift_decil >= 1.5),
                "pliegues_cv": pliegues,
                "entrenado_en": datetime.now().isoformat(),
            })
            self.metricas.pop("motivo", None)
            self.guardar()
            if registrar:
                self._registrar_en_bd(db_path)

            logger.info(
                "PlanFailureClassifier entrenado: n=%d, AUC=%.3f, lift_decil=%.2f "
                "(accionable=%s)",
                n, auc, lift_decil, self.metricas["accionable"],
            )
            return self.metricas
        except Exception as e:
            self.entrenado = False
            self.metricas["motivo"] = f"entrenamiento falló: {e}"
            logger.warning("PlanFailureClassifier: %s", self.metricas["motivo"])
            return self.metricas

    def _vector(self, features: dict) -> list[float]:
        claves = getattr(self, "_claves", None) or sorted(features)
        return [float(features.get(k, 0.0) or 0.0) for k in claves]

    def _registrar_en_bd(self, db_path: str) -> None:
        """Anota el modelo en ``modelos_entrenados`` (best-effort)."""
        try:
            with closing(sqlite3.connect(db_path, timeout=10)) as conn:
                conn.execute(
                    """INSERT INTO modelos_entrenados
                       (nombre, version, n_muestras_entrenamiento, metricas,
                        ruta_archivo, fecha, activo)
                       VALUES (?, ?, ?, ?, ?, ?, 1)""",
                    (
                        NOMBRE_MODELO,
                        int(self.metricas.get("pliegues_cv") or 1),
                        self.n_muestras,
                        json.dumps(self.metricas, ensure_ascii=False, default=str),
                        str(self._ruta()),
                        datetime.now().isoformat(),
                    ),
                )
                conn.commit()
        except Exception as e:
            logger.debug(f"No se pudo registrar el modelo en la BD: {e}")

    # ------------------------------------------------------------
    # Predicción
    # ------------------------------------------------------------
    def _confianza(self) -> str:
        if self.n_muestras >= UMBRAL_CONFIANZA_ALTA:
            return "alta"
        if self.n_muestras >= UMBRAL_CONFIANZA_MEDIA:
            return "media"
        return "baja"

    def _factores(self, features: dict, limite: int = 3) -> list[str]:
        """Rasgos que más empujan el riesgo (interpretabilidad)."""
        try:
            modelo = self.pipeline.named_steps["modelo"]
            escala = self.pipeline.named_steps["escala"]
            claves = self._claves
            vector = self._vector(features)
            z = escala.transform([vector])[0]
            aportes = modelo.coef_[0] * z
            orden = sorted(range(len(claves)), key=lambda i: -abs(aportes[i]))
            factores = []
            for i in orden[:limite]:
                if abs(aportes[i]) < 1e-9:
                    continue
                sentido = "sube" if aportes[i] > 0 else "baja"
                factores.append(
                    f"{claves[i]}={features.get(claves[i], 0):.2f} {sentido} el riesgo"
                )
            return factores
        except Exception as e:
            logger.debug(f"No se pudieron calcular factores: {e}")
            return []

    def predecir(self, plan: Any) -> PrediccionFallo:
        """Probabilidad de fallo del plan, con confianza y factores."""
        if not self.entrenado or self.pipeline is None:
            return PrediccionFallo(0.5, "sin_datos", self.n_muestras, [])
        try:
            features = extraer_features_de_plan(plan)
            probabilidad = float(
                self.pipeline.predict_proba([self._vector(features)])[0][1]
            )
        except Exception as e:
            logger.debug(f"predicción de fallo no disponible: {e}")
            return PrediccionFallo(0.5, "sin_datos", self.n_muestras, [])
        return PrediccionFallo(
            probabilidad_fallo=max(0.0, min(1.0, probabilidad)),
            confianza=self._confianza(),
            n_muestras=self.n_muestras,
            factores=self._factores(features),
        )

    # ------------------------------------------------------------
    def estadisticas(self) -> dict[str, Any]:
        return {
            "entrenado": self.entrenado,
            "n_muestras": self.n_muestras,
            "ruta": str(self._ruta()),
            "metricas": self.metricas,
        }


# ============================================================
# CLI
# ============================================================
def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m learning.plan_failure_classifier",
        description="Clasificador de fallo de plan entrenado con el histórico.",
    )
    parser.add_argument("accion", choices=["entrenar", "estado"])
    parser.add_argument("--db", default="agent_history.db")
    parser.add_argument("--modelos", default="learning_models")
    parser.add_argument("--min-muestras", type=int, default=MIN_MUESTRAS)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    clasificador = PlanFailureClassifier(
        args.modelos, min_muestras=args.min_muestras
    )

    if args.accion == "estado":
        datos = clasificador.estadisticas()
    else:
        datos = clasificador.entrenar(args.db)

    if args.json:
        print(json.dumps(datos, ensure_ascii=False, default=str))
    else:
        for clave, valor in datos.items():
            print(f"{clave}: {valor}")
    return 0 if datos.get("entrenado") or args.accion == "estado" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
