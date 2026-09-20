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
import os
import re
import sqlite3
import threading
import unicodedata
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

# ── Umbrales (H2) ──
# Antes 0.68: demasiado permisivo. Una reescritura de OTRA tarea entraba por
# similitud semántica y sustituía el prompt pedido (el caso albóndigas →
# cuento de terror). Ahora el umbral es conservador y, además, hay que pasar
# la comprobación de intención.
UMBRAL_DEFAULT = 0.85
# Si el mejor y el segundo mejor candidato (de otra tarea) están a menos de
# este margen, hay duda → no se aplica ninguna reescritura.
MARGEN_DUDA_DEFAULT = 0.03
# Solape léxico mínimo (Jaccard) entre la consulta y la tarea de la
# reescritura cuando la firma NO coincide.
MIN_SOLAPE_DEFAULT = 0.5

MAX_CANDIDATOS = 500         # límite de filas a cargar en cada búsqueda


def _env_float(nombre: str, defecto: float) -> float:
    """Lee un umbral de una variable de entorno, con fallback seguro."""
    try:
        valor = float(os.environ.get(nombre, ""))
    except (TypeError, ValueError):
        return defecto
    return valor if 0.0 < valor <= 1.0 else defecto


def configuracion() -> dict:
    """Umbrales efectivos del matcher (configurables por entorno)."""
    return {
        "umbral": _env_float("AGENTES_AB_UMBRAL", UMBRAL_DEFAULT),
        "margen_duda": _env_float("AGENTES_AB_MARGEN_DUDA", MARGEN_DUDA_DEFAULT),
        "min_solape": _env_float("AGENTES_AB_MIN_SOLAPE", MIN_SOLAPE_DEFAULT),
    }


# ============================================================
# COMPATIBILIDAD DE INTENCIÓN
# ============================================================

_STOPWORDS = frozenset({
    "de", "la", "el", "los", "las", "un", "una", "unos", "unas",
    "y", "o", "u", "e", "a", "en", "con", "por", "para", "que", "es",
    "son", "al", "del", "se", "su", "sus", "lo", "le", "les", "tu",
    "tus", "mi", "mis", "si", "no", "the", "of", "and", "or", "to",
    "in", "on", "at", "by", "for", "with", "from", "as", "is", "are",
    "be", "been", "escribe", "genera", "crea", "redacta", "elabora",
    "produce", "construye", "haz",
})


def tokens_significativos(texto: str) -> set[str]:
    """Tokens normalizados (sin acentos, sin stopwords) de un texto."""
    if not texto:
        return set()
    normalizado = unicodedata.normalize("NFKD", str(texto).lower())
    normalizado = "".join(c for c in normalizado if not unicodedata.combining(c))
    return {
        p for p in re.findall(r"[a-z0-9]+", normalizado)
        if p not in _STOPWORDS and len(p) > 2
    }


def solape_lexico(a: str, b: str) -> float:
    """Jaccard de tokens significativos entre dos textos (0..1)."""
    ta, tb = tokens_significativos(a), tokens_significativos(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def compatible_por_intencion(
    consulta: str,
    candidato: dict,
    similitud: float,
    cfg: dict | None = None,
) -> tuple[bool, str]:
    """¿La reescritura del candidato pertenece a la misma intención?

    Solo hay dos vías de compatibilidad, y ninguna permite el salto entre
    tareas distintas (la regla de H2: un prompt de la tarea A NUNCA debe
    recibir la reescritura de la tarea B):

    1. **Misma firma** (mismo conjunto de palabras significativas) → sí.
    2. **Solape léxico suficiente** con la tarea original del candidato → sí.

    La similitud semántica por sí sola NO basta: es justo la señal que
    producía el falso positivo entre tareas. Si ninguna vía se cumple, la
    reescritura NO se aplica (fallback al prompt original).
    """
    cfg = cfg or configuracion()
    firma_consulta = _firmar(consulta)
    firma_cand = candidato.get("firma") or ""

    if firma_consulta and firma_cand and firma_consulta == firma_cand:
        return True, "firma coincidente"

    solape = solape_lexico(consulta, candidato.get("prompt_original") or "")
    if solape >= cfg["min_solape"]:
        return True, f"solape léxico {solape:.2f} >= {cfg['min_solape']:.2f}"

    return False, (
        f"intención incompatible (solape {solape:.2f} < {cfg['min_solape']:.2f}, "
        f"firma distinta; similitud {similitud:.3f} insuficiente por sí sola)"
    )


def _firmar(prompt: str) -> str:
    """Firma semántica del FeedbackProcessor (import perezoso, sin ciclos)."""
    try:
        from learning.feedback_processor import FeedbackProcessor

        return FeedbackProcessor._firmar(prompt)
    except Exception as e:
        logger.debug(f"No se pudo calcular la firma: {e}")
        return ""



# ============================================================
# SINGLETON THREAD-SAFE
# ============================================================
_matchers: dict[str, EmbeddingMatcher] = {}
_matcher_lock = threading.RLock()


def obtener_matcher(modelo: str = MODELO_DEFAULT) -> EmbeddingMatcher:
    """Devuelve el EmbeddingMatcher del modelo pedido (lo crea si no existe).

    Se cachea por nombre de modelo: antes se ignoraba el argumento en
    llamadas posteriores y se devolvía el primer modelo creado, lo que
    hacía que la búsqueda SQL (filtrada por ``embedding_model``) no
    encontrara coincidencias.
    """
    with _matcher_lock:
        matcher = _matchers.get(modelo)
        if matcher is None:
            matcher = EmbeddingMatcher(modelo=modelo)
            _matchers[modelo] = matcher
        return matcher


def reset_matcher():
    """Fuerza la recreación de los matchers cacheados (útil en tests)."""
    with _matcher_lock:
        _matchers.clear()


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
        umbral: float | None = None,
        estados_validos: tuple = ("activo", "candidato"),
    ) -> dict | None:
        """
        Busca la reescritura más similar al texto dado, con guardas de
        intención (H2).

        Una reescritura solo se devuelve si:

        1. su similitud supera el umbral (conservador por defecto);
        2. supera la comprobación de intención (firma o solape léxico)
           contra la tarea que la originó;
        3. no hay un segundo candidato de OTRA tarea a menos de
           ``margen_duda`` (en caso de duda, no se aplica nada).

        Si algo falla, devuelve ``None`` y el builder conserva el prompt
        original.

        Returns:
            Dict con ``id``, ``similitud``, ``estado``, ``prompt``,
            ``n_usos``, ``prompt_original``, ``firma`` y ``motivo``;
            o ``None`` si no hay match seguro.
        """
        if not texto or not texto.strip():
            return None

        cfg = configuracion()
        umbral = cfg["umbral"] if umbral is None else umbral

        # 1. Calcular el embedding del texto de consulta
        vector_bytes = self.calcular(texto)
        if vector_bytes is None:
            return None
        vec_consulta = np.frombuffer(vector_bytes, dtype=np.float32)

        # 2. Cargar candidatos de la BD
        candidatos = self._cargar_candidatos(db_path, estados_validos)
        if not candidatos:
            return None

        # 3. Puntuar todos y ordenar por similitud descendente
        puntuados = []
        for cand in candidatos:
            vec_cand = np.frombuffer(cand["embedding"], dtype=np.float32)
            puntuados.append((self._coseno(vec_consulta, vec_cand), cand))
        puntuados.sort(key=lambda par: par[0], reverse=True)

        mejor_sim, mejor = puntuados[0]

        # 4. ¿Supera el umbral?
        if mejor_sim < umbral:
            logger.debug(
                f"Sin match (mejor similitud={mejor_sim:.3f}, umbral={umbral})"
            )
            return None

        # 5. Duda: ¿hay otro candidato de una tarea distinta casi igual?
        for sim, cand in puntuados[1:]:
            if sim < mejor_sim - cfg["margen_duda"]:
                break
            if (cand.get("prompt_original") or "") != (mejor.get("prompt_original") or ""):
                logger.info(
                    f"AB: match ambiguo para '{texto[:40]}…' "
                    f"(mejor={mejor_sim:.3f}, segundo={sim:.3f} de otra tarea) "
                    f"→ se conserva el prompt original"
                )
                return None

        # 6. Compatibilidad de intención
        compatible, motivo = compatible_por_intencion(texto, mejor, mejor_sim, cfg)
        if not compatible:
            logger.info(
                f"AB: reescritura id={mejor['id']} descartada por intención "
                f"({motivo}) → se conserva el prompt original"
            )
            return None

        logger.info(
            f"🎯 Match seguro: id={mejor['id']} "
            f"(similitud={mejor_sim:.3f}, estado={mejor['estado']}, "
            f"motivo={motivo})"
        )
        return {
            "id": mejor["id"],
            "similitud": mejor_sim,
            "estado": mejor["estado"],
            "prompt": mejor["prompt_nuevo"],
            "n_usos": mejor["n_usos"],
            "prompt_original": mejor.get("prompt_original") or "",
            "firma": mejor.get("firma") or "",
            "motivo": motivo,
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
                               n_usos, prompt_nuevo, prompt_original, firma
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
