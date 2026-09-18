"""
learning/models.py
Modelos entrenables (scikit-learn) para:
FailurePredictor: probabilidad de éxito de un agente, antes de
ejecutarlo.
PlanScorer: puntuación de calidad esperada de un plan completo.
Ambos usan aprendizaje ONLINE (partial_fit) para poder actualizarse con
cada ejecución nueva sin reentrenar desde cero.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import joblib
from sklearn.feature_extraction import DictVectorizer
from sklearn.linear_model import SGDRegressor

logger = logging.getLogger(__name__)
MUESTRAS_MINIMAS_ENTRENAMIENTO_INICIAL = 20

@dataclass
class ResultadoPrediccion:
    probabilidad_exito: float
    confianza: str      # 'baja' | 'media' | 'alta'
    n_muestras_vistas: int

class ModeloOnlineBase:
    """Wrapper común: vectorización + modelo con partial_fit + persistencia."""
    nombre_modelo = "base"

    def __init__(self, ruta_modelos: Path):
        self.ruta_modelos = Path(ruta_modelos)
        self.ruta_modelos.mkdir(parents=True, exist_ok=True)
        self.vectorizador = DictVectorizer(sparse=False)
        self.modelo = self._crear_modelo()
        self._entrenado = False
        self._n_muestras = 0
        self._cargar_si_existe()

    def _crear_modelo(self):
        raise NotImplementedError

    def _ruta_archivo(self) -> Path:
        return self.ruta_modelos / f"{self.nombre_modelo}.joblib"

    def _cargar_si_existe(self):
        ruta = self._ruta_archivo()
        if ruta.exists():
            try:
                data = joblib.load(ruta)
                self.vectorizador = data["vectorizador"]
                self.modelo = data["modelo"]
                self._entrenado = data.get("entrenado", False)
                self._n_muestras = data.get("n_muestras", 0)
                logger.debug(f"Modelo '{self.nombre_modelo}' cargado")
            except Exception as e:
                logger.warning(f"No se pudo cargar '{self.nombre_modelo}': {e}")

    def guardar(self):
        try:
            joblib.dump(
                {
                    "vectorizador": self.vectorizador,
                    "modelo": self.modelo,
                    "entrenado": self._entrenado,
                    "n_muestras": self._n_muestras,
                },
                self._ruta_archivo(),
            )
        except Exception as e:
            logger.warning(f"No se pudo guardar '{self.nombre_modelo}': {e}")

    @property
    def n_muestras(self) -> int:
        return self._n_muestras


class FailurePredictor:
    """
    Predictor bayesiano por tipo de agente.

    En lugar de entrenar un SGDClassifier sobre features que no tienen
    señal suficiente (leakage, constantes, poco varianza), este predictor
    calcula la tasa de éxito empírica por tipo de agente y predice
    directamente esa tasa.

    Ventajas:
    - Honesto: predice según datos reales, no sobreajusta.
    - Interpretable: "HTTP tiene 92% de éxito, LLM 78%".
    - Valores intermedios: no satura a 0.0/1.0.
    - Funciona desde la primera ejecución.
    - Aprendizaje online: cada actualización ajusta el prior del tipo.

    Limitaciones:
    - Solo usa 'tipo' como feature discriminante.
    - No distingue entre dos agentes del mismo tipo.
    """

    nombre_modelo = "predictor_fallos"

    def __init__(self, ruta_modelos: Path):
        # No llamamos a super().__init__() porque no queremos DictVectorizer
        # ni SGDClassifier. Solo necesitamos el path y cargar priors.
        self.ruta_modelos = Path(ruta_modelos)
        self.ruta_modelos.mkdir(parents=True, exist_ok=True)
        self._priors_por_tipo: dict[str, list[int]] = {}  # tipo -> [exitos, total]
        self._entrenado = False
        self._n_muestras = 0
        self._cargar_si_existe()

    def _crear_modelo(self):
        # No hay modelo sklearn. Devolvemos None para compatibilidad.
        return None

    def _ruta_archivo(self) -> Path:
        return self.ruta_modelos / f"{self.nombre_modelo}.joblib"

    def _cargar_si_existe(self):
        ruta = self._ruta_archivo()
        if ruta.exists():
            try:
                data = joblib.load(ruta)
                self._priors_por_tipo = data.get("priors_por_tipo", {})
                self._entrenado = data.get("entrenado", False)
                self._n_muestras = data.get("n_muestras", 0)
                logger.debug(
                    f"Prior cargado: {len(self._priors_por_tipo)} tipos, "
                    f"{self._n_muestras} muestras"
                )
            except Exception as e:
                logger.warning(f"No se pudo cargar '{self.nombre_modelo}': {e}")

    def guardar(self):
        try:
            joblib.dump(
                {
                    "priors_por_tipo": self._priors_por_tipo,
                    "entrenado": self._entrenado,
                    "n_muestras": self._n_muestras,
                },
                self._ruta_archivo(),
            )
        except Exception as e:
            logger.warning(f"No se pudo guardar '{self.nombre_modelo}': {e}")

    # ------------------------------------------------------------------
    # ENTRENAMIENTO
    # ------------------------------------------------------------------
    def entrenar_inicial(self, features: list[dict], etiquetas: list[int]):
        """
        Calcula priors por tipo desde cero. Reemplaza los priors
        existentes (no acumula) porque se llama con el historial completo.
        """
        if not features:
            return
        from collections import defaultdict

        stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for f, y in zip(features, etiquetas, strict=False):
            tipo = f.get("tipo", "Desconocido")
            stats[tipo][1] += 1
            if y == 1:
                stats[tipo][0] += 1

        self._priors_por_tipo = dict(stats)
        self._entrenado = True
        self._n_muestras = len(features)
        self.guardar()

        # Log bonito para ver los priors
        logger.info(
            f"✅ FailurePredictor: priors calculados para "
            f"{len(stats)} tipos ({self._n_muestras} muestras)"
        )
        for tipo, (exitos, total) in sorted(stats.items()):
            tasa = exitos / total if total > 0 else 0.5
            logger.info(f"   {tipo:12s}: {tasa:.0%} ({exitos}/{total})")

    def actualizar(self, features: dict, etiqueta: int):
        """Aprendizaje online: ajusta el prior del tipo con una muestra."""
        if not self._entrenado:
            return
        tipo = features.get("tipo", "Desconocido")
        if tipo not in self._priors_por_tipo:
            self._priors_por_tipo[tipo] = [0, 0]
        self._priors_por_tipo[tipo][1] += 1
        if etiqueta == 1:
            self._priors_por_tipo[tipo][0] += 1
        self._n_muestras += 1
        # Guardar solo cada 50 actualizaciones (misma lógica que antes)
        if self._n_muestras % 50 == 0:
            self.guardar()

    # ------------------------------------------------------------------
    # PREDICCIÓN
    # ------------------------------------------------------------------
    def predecir(self, features: dict) -> ResultadoPrediccion:
        if not self._entrenado:
            return ResultadoPrediccion(0.5, "baja", 0)

        tipo = features.get("tipo", "Desconocido")
        exitos, total = self._priors_por_tipo.get(tipo, [0, 0])

        if total == 0:
            # Tipo nunca visto: usar prior global (todos los tipos)
            exitos_global = sum(e for e, _ in self._priors_por_tipo.values())
            total_global = sum(t for _, t in self._priors_por_tipo.values())
            p = exitos_global / total_global if total_global > 0 else 0.5
            confianza = "baja"
        else:
            p = exitos / total
            # Confianza según cuántos ejemplos del tipo hay
            if total >= 50:
                confianza = "alta"
            elif total >= 10:
                confianza = "media"
            else:
                confianza = "baja"

        return ResultadoPrediccion(p, confianza, total)


class PlanScorer(ModeloOnlineBase):
    """Regresor: puntúa la calidad esperada (0.0-1.0) de un plan completo."""
    nombre_modelo = "scorer_planes"

    def _crear_modelo(self):
        return SGDRegressor(random_state=42)

    def entrenar_inicial(self, features: list[dict], recompensas: list[float]):
        if len(features) < 1:
            return
        try:
            X = self.vectorizador.fit_transform(features)
            self.modelo.partial_fit(X, recompensas)
            self._entrenado = True
            # fit_transform parte de cero: el contador se fija, no se suma
            # (antes se inflaba al reentrenar o cargar desde disco).
            self._n_muestras = len(features)
            self.guardar()
        except Exception as e:
            logger.warning(f"Error entrenando PlanScorer: {e}")

    def actualizar(self, features: dict, recompensa: float):
        if not self._entrenado:
            return
        try:
            X = self.vectorizador.transform([features])
            self.modelo.partial_fit(X, [recompensa])
            self._n_muestras += 1
            # ⬇️ PARCHE 4 (PlanScorer): misma razón que en
            # FailurePredictor — no guardar en cada llamada.
            if self._n_muestras % 50 == 0:
                self.guardar()
        except Exception as e:
            logger.debug(f"partial_fit de PlanScorer ignorado: {e}")

    def puntuar(self, features: dict) -> float:
        if not self._entrenado:
            return 0.5
        try:
            X = self.vectorizador.transform([features])
            pred = float(self.modelo.predict(X)[0])
            return max(0.0, min(1.0, pred))
        except Exception as e:
            logger.debug(f"puntuar falló, devolviendo neutro: {e}")
            return 0.5
