# learning/embedding_matcher.py
"""
Matching semántico de prompts por embeddings.

Reemplaza el matching por firma exacta (que nunca coincidía porque
el LLM varía el texto entre ejecuciones) por similitud coseno entre
embeddings multilingües.

Modelo: paraphrase-multilingual-MiniLM-L12-v2
  · 384 dimensiones
  · 100+ idiomas (incluye español)
  · ~470 MB de modelo (descarga única, cacheado en ~/.cache/huggingface/)
  · ~20-50ms por embedding en CPU

Uso típico:
    matcher = EmbeddingMatcher()              # singleton, thread-safe
    vector = matcher.calcular("texto cualquiera")
    best = matcher.buscar_match(db_path, "texto", umbral=0.80)
    # → {'id': 4, 'similitud': 0.94, 'estado': 'activo', 'prompt': '...'} o None
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import closing

import numpy as np

logger = logging.getLogger(__name__)

# Silenciar el ruido de HuggingFace al cargar el modelo
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("huggingface_hub").setLevel(logging.WARNING)
logging.getLogger("sentence_transformers").setLevel(logging.WARNING)
logging.getLogger("transformers").setLevel(logging.WARNING)


# ============================================================
# CONSTANTES
# ============================================================
MODELO_DEFAULT = "paraphrase-multilingual-MiniLM-L12-v2"
UMBRAL_DEFAULT = 0.68       # similitud mínima para considerar match
MAX_CANDIDATOS = 500         # límite de filas a cargar en cada búsqueda


# ============================================================
# SINGLETON THREAD-SAFE
# ============================================================
_matcher_instance: EmbeddingMatcher | None = None
_matcher_lock = threading.RLock()


def obtener_matcher(modelo: str = MODELO_DEFAULT) -> EmbeddingMatcher:
    """Devuelve el EmbeddingMatcher singleton (crea si no existe)."""
    global _matcher_instance
    with _matcher_lock:
        if _matcher_instance is None:
            _matcher_instance = EmbeddingMatcher(modelo=modelo)
        return _matcher_instance


def reset_matcher():
    """Fuerza la recreación del singleton (útil en tests)."""
    global _matcher_instance
    with _matcher_lock:
        _matcher_instance = None


# ============================================================
# CLASE PRINCIPAL
# ============================================================
class EmbeddingMatcher:
    """
    Calcula embeddings y busca el prompt más similar en la BD.

    El modelo se carga LAZY: solo al primer uso. La primera vez que
    se llama a `calcular()` o `buscar_match()`, se descarga el modelo
    (~470 MB) si no está cacheado.
    """

    def __init__(self, modelo: str = MODELO_DEFAULT):
        self.modelo = modelo
        self._model = None
        self._dim = None
        self._load_lock = threading.RLock()

    # ------------------------------------------------------------
    # Carga perezosa del modelo
    # ------------------------------------------------------------
    def _cargar_modelo(self):
        """Carga el modelo la primera vez que se necesita."""
        if self._model is not None:
            return
        with self._load_lock:
            if self._model is not None:
                return
            try:
                from sentence_transformers import SentenceTransformer
                logger.info(f"🧠 Cargando modelo de embeddings: {self.modelo}")
                self._model = SentenceTransformer(self.modelo)
                if hasattr(self._model, "get_embedding_dimension"):
                    self._dim = self._model.get_embedding_dimension()
                else:
                    self._dim = self._model.get_sentence_embedding_dimension()
                logger.info(f"✅ Modelo cargado (dim={self._dim})")
            except Exception as e:
                logger.error(f"❌ No se pudo cargar el modelo: {e}")
                raise

    # ------------------------------------------------------------
    # Cálculo de embedding
    # ------------------------------------------------------------
    def calcular(self, texto: str) -> bytes | None:
        """
        Calcula el embedding de un texto y lo serializa a bytes
        (float32). Devuelve None si algo falla.
        """
        if not texto or not texto.strip():
            return None
        try:
            self._cargar_modelo()
            vec = self._model.encode(
                texto, convert_to_numpy=True, normalize_embeddings=True
            )
            return vec.astype(np.float32).tobytes()
        except Exception as e:
            logger.warning(f"Error calculando embedding: {e}")
            return None

    # ------------------------------------------------------------
    # Similitud coseno
    # ------------------------------------------------------------
    @staticmethod
    def _coseno(a: np.ndarray, b: np.ndarray) -> float:
        """Similitud coseno entre dos arrays. Asume normalizados."""
        if a.shape != b.shape:
            return 0.0
        return float(np.dot(a, b))

    # ------------------------------------------------------------
    # Búsqueda de match
    # ------------------------------------------------------------
    def buscar_match(
        self,
        db_path: str,
        texto: str,
        umbral: float = UMBRAL_DEFAULT,
        estados_validos: tuple = ("activo", "candidato"),
    ) -> dict | None:
        """
        Busca la reescritura más similar al texto dado.

        Args:
            db_path: ruta a la BD.
            texto: prompt crudo a comparar.
            umbral: similitud mínima (0..1). Por debajo, no hay match.
            estados_validos: estados que se consideran candidatos al match.

        Returns:
            Dict con {'id', 'similitud', 'estado', 'prompt', 'n_usos'}
            o None si no hay match por encima del umbral.
        """
        if not texto or not texto.strip():
            return None

        # 1. Calcular el embedding del texto de consulta
        vector_bytes = self.calcular(texto)
        if vector_bytes is None:
            return None
        vec_consulta = np.frombuffer(vector_bytes, dtype=np.float32)

        # 2. Cargar candidatos de la BD
        candidatos = self._cargar_candidatos(db_path, estados_validos)
        if not candidatos:
            return None

        # 3. Comparar contra todos y quedarse con el mejor
        mejor = None
        mejor_sim = -1.0
        for cand in candidatos:
            vec_cand = np.frombuffer(cand["embedding"], dtype=np.float32)
            sim = self._coseno(vec_consulta, vec_cand)
            if sim > mejor_sim:
                mejor_sim = sim
                mejor = cand

        # 4. ¿Supera el umbral?
        if mejor is None or mejor_sim < umbral:
            logger.debug(
                f"Sin match (mejor similitud={mejor_sim:.3f}, "
                f"umbral={umbral})"
            )
            return None

        logger.info(
            f"🎯 Match encontrado: id={mejor['id']} "
            f"(similitud={mejor_sim:.3f}, estado={mejor['estado']})"
        )
        return {
            "id": mejor["id"],
            "similitud": mejor_sim,
            "estado": mejor["estado"],
            "prompt": mejor["prompt_nuevo"],
            "n_usos": mejor["n_usos"],
        }

    # ------------------------------------------------------------
    # Carga de candidatos desde la BD
    # ------------------------------------------------------------
    def _cargar_candidatos(
        self,
        db_path: str,
        estados_validos: tuple,
    ) -> list[dict]:
        """Carga las filas con embedding no nulo de la BD."""
        try:
            with closing(sqlite3.connect(db_path, timeout=5)) as conn:
                conn.row_factory = sqlite3.Row
                placeholders = ",".join("?" * len(estados_validos))
                cursor = conn.execute(
                    f"""SELECT id, embedding, embedding_model, estado,
                               n_usos, prompt_nuevo
                        FROM prompts_reescritos
                        WHERE embedding IS NOT NULL
                          AND embedding_model = ?
                          AND estado IN ({placeholders})
                        ORDER BY id DESC
                        LIMIT ?""",
                    (self.modelo, *estados_validos, MAX_CANDIDATOS),
                )
                return [dict(r) for r in cursor.fetchall()]
        except Exception as e:
            logger.warning(f"Error cargando candidatos: {e}")
            return []

    # ------------------------------------------------------------
    # Utilidad: recodificar embeddings de toda la tabla
    # ------------------------------------------------------------
    def recodificar_todos(self, db_path: str) -> dict[str, int]:
        """
        Calcula y guarda el embedding de todas las filas que no lo
        tengan (o que lo tengan con otro modelo).

        Devuelve: {'actualizados': N, 'errores': M, 'saltados': K}.
        """
        actualizados = 0
        errores = 0
        saltados = 0

        try:
            with closing(sqlite3.connect(db_path, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")

                filas = conn.execute(
                    """SELECT id, prompt_original, embedding, embedding_model
                       FROM prompts_reescritos"""
                ).fetchall()

                for f in filas:
                    ya_tiene = (
                        f["embedding"] is not None
                        and f["embedding_model"] == self.modelo
                    )
                    if ya_tiene:
                        saltados += 1
                        continue

                    vec = self.calcular(f["prompt_original"] or "")
                    if vec is None:
                        errores += 1
                        continue

                    conn.execute(
                        """UPDATE prompts_reescritos
                           SET embedding = ?, embedding_model = ?
                           WHERE id = ?""",
                        (vec, self.modelo, f["id"]),
                    )
                    actualizados += 1
                    logger.debug(f"  embedding actualizado: id={f['id']}")

                conn.commit()

            logger.info(
                f"✅ Recodificación: {actualizados} actualizados, "
                f"{saltados} saltados, {errores} errores"
            )
        except Exception as e:
            logger.error(f"Error en recodificación: {e}")

        return {
            "actualizados": actualizados,
            "errores": errores,
            "saltados": saltados,
        }
