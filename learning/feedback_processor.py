# learning/feedback_processor.py
"""
FeedbackProcessor: traduce el feedback del usuario en reescrituras
concretas de prompts de agentes LLM.

Se invoca cuando el usuario deja un comentario en el diálogo de
feedback. Solo actúa si:
  · score < 0 (feedback negativo), o
  · score == 0 con comentario (feedback tibio con matiz)

Con score positivo NO reescribe: el prompt funciona, no lo toques.

Diseño:
  1. Lee el feedback de `feedback_usuario`.
  2. Encuentra el agente LLM relevante (por agente_ejecucion_id o, si no
     lo hay, el último agente LLM de la ejecución).
  3. Pide a la LLM que reescriba el prompt del agente a la luz del
     comentario del usuario.
  4. Guarda la reescritura en `prompts_reescritos` con la firma del
     prompt original, para que futuras ejecuciones con el mismo prompt
     usen la versión mejorada.

La consulta en runtime (`consultar_reescritura`) es estática, rápida
y no lanza excepciones: si algo falla, devuelve None y el llamador
sigue con el prompt original.
"""
from __future__ import annotations
import hashlib
import logging
import re
import sqlite3
import unicodedata
from datetime import datetime
from typing import Dict, Optional

logger = logging.getLogger(__name__)


# ============================================================
# META-PROMPT: cómo pedirle a la LLM que reescriba
# ============================================================
META_PROMPT = """Eres un ingeniero de prompts. Un usuario acaba de dar \
feedback sobre una ejecución de agentes. Tu tarea es MEJORAR el prompt \
del agente que causó el problema.

PROMPT ACTUAL DEL AGENTE:
{prompt_original}

COMENTARIO DEL USUARIO:
{comentario}

CONTEXTO DE LA EJECUCIÓN:
- Agente: {nombre_agente} (tipo {tipo_agente})
- Descripción del agente: {descripcion}
- Resultado obtenido (resumido): {resultado}

INSTRUCCIONES:
1. Identifica qué parte del prompt causó el problema.
2. Reescribe el prompt para corregirlo.
3. Mantén el objetivo original, no lo cambies.
4. NO añadas texto explicativo dentro del prompt.
5. Si el comentario del usuario indica que el prompt ya funciona bien
   y solo sugiere mejoras opcionales, devuelve `{{"skip": true}}`.
6. Si el comentario es ambiguo o no aporta información útil para
   reescribir, devuelve `{{"skip": true}}`.
7. Responde SOLO con JSON válido:
{{
  "prompt_nuevo": "texto completo del prompt mejorado",
  "razon": "qué cambiaste y por qué (1-2 frases)"
}}
   O bien:
{{"skip": true, "razon": "por qué no reescribo"}}

NO uses markdown. NO añadas texto antes o después del JSON."""


SYSTEM_PROMPT_LLM = (
    "Eres un ingeniero de prompts. Respondes únicamente con JSON válido, "
    "sin markdown ni texto adicional."
)


# ============================================================
# CLASE PRINCIPAL
# ============================================================
class FeedbackProcessor:
    """
    Procesa un feedback del usuario y produce una reescritura de prompt.

    Uso típico (asíncrono, desde la UI):
        proc = FeedbackProcessor(db_path, llm_client)
        resultado = proc.procesar_feedback(feedback_id)
        # {'procesado': bool, 'prompt_id': int|None, 'razon': str}

    Uso típico (runtime, en _kwargs_llm):
        prompt = FeedbackProcessor.consultar_reescritura(
            db_path, prompt_original
        ) or prompt_original
    """

    def __init__(self, db_path: str, llm_client):
        self.db_path = str(db_path)
        self.llm = llm_client
        self._asegurar_tabla()

    # ------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------
    def procesar_feedback(self, feedback_id: int) -> Dict:
        """
        Procesa un feedback y devuelve un dict con el resultado.

        No lanza excepciones: si algo falla, devuelve
        {'procesado': False, 'error'|'razon': ...}.

        ✅ FASE 4d: antes de calcular la firma, se quita el endurecimiento
        del prompt_usado (si lo tiene). Esto es imprescindible para que
        la firma coincida con la que calcula el builder sobre el prompt
        crudo. Sin esto, todos los prompts de agentes LLM comparten la
        misma firma (el endurecimiento ocupa más de 200 chars) y el A/B
        no puede distinguir entre tareas.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")

                fb = conn.execute(
                    "SELECT * FROM feedback_usuario WHERE id = ?",
                    (feedback_id,),
                ).fetchone()
                if not fb:
                    return {"procesado": False, "error": "feedback no existe"}

                # Solo reescribimos si hay señal negativa o comentario.
                if fb["score"] > 0:
                    return {"procesado": False, "razon": "feedback positivo"}
                if not (fb["comentario"] or "").strip():
                    return {"procesado": False, "razon": "sin comentario"}

                agente = self._agente_relevante(conn, fb)
                if not agente:
                    return {"procesado": False, "razon": "sin agente LLM relevante"}

                prompt_original = (agente["prompt_usado"] or "").strip()
                if not prompt_original:
                    return {
                        "procesado": False,
                        "razon": "agente sin prompt_usado guardado",
                    }

                # ✅ FASE 4d: quitar endurecimiento antes de firmar.
                # El prompt_usado es el endurecido (con INSTRUCCIONES CRÍTICAS).
                # El builder calcula la firma sobre el prompt CRUDO.
                # Para que coincidan, aquí también firmamos el crudo.
                PREFIJO_ENDURECIDO = "INSTRUCCIONES CRÍTICAS:"
                if prompt_original.startswith(PREFIJO_ENDURECIDO):
                    idx = prompt_original.find("TAREA:\n")
                    if idx > 0:
                        prompt_original = prompt_original[
                            idx + len("TAREA:\n"):
                        ].lstrip()
        except Exception as e:
            logger.warning(f"FeedbackProcessor: lectura falló: {e}")
            return {"procesado": False, "error": str(e)}

        # Fuera de la transacción: llamar al LLM.
        reescritura = self._reescribir_con_llm(
            prompt_original=prompt_original,
            comentario=fb["comentario"],
            agente_row=agente,
        )
        if not reescritura:
            return {"procesado": False, "razon": "LLM no respondió"}

        if reescritura.get("skip"):
            logger.info(
                f"FeedbackProcessor: LLM decidió no reescribir "
                f"({reescritura.get('razon', 'sin razon')})"
            )
            return {
                "procesado": False,
                "razon": reescritura.get("razon", "skip"),
            }

        prompt_nuevo = reescritura.get("prompt_nuevo", "").strip()
        if not prompt_nuevo:
            return {"procesado": False, "razon": "prompt_nuevo vacío"}

        prompt_id = self._guardar_reescritura(
            prompt_original=prompt_original,
            prompt_nuevo=prompt_nuevo,
            razon=reescritura.get("razon", ""),
            feedback_id=feedback_id,
        )
        if not prompt_id:
            return {"procesado": False, "razon": "no se pudo guardar"}

        logger.info(
            f"✨ Prompt reescrito desde feedback {feedback_id} → id {prompt_id}"
        )
        return {
            "procesado": True,
            "prompt_id": prompt_id,
            "razon": reescritura.get("razon", ""),
        }

    @classmethod
    def consultar_reescritura(
        cls,
        db_path: str,
        prompt_original: str,
    ) -> Optional[str]:
        """
        Runtime. Devuelve el prompt reescrito si existe, o None.

        Método de clase, sin instancia: no necesita LLM y se llama en el
        camino crítico de _kwargs_llm, así que debe ser lo más liviano
        posible. Nunca lanza.
        """
        if not prompt_original or not prompt_original.strip():
            return None
        try:
            firma = cls._firmar(prompt_original)
            with sqlite3.connect(db_path, timeout=5) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    """SELECT prompt_nuevo FROM prompts_reescritos
                       WHERE firma = ? AND activo = 1
                       ORDER BY fecha DESC LIMIT 1""",
                    (firma,),
                ).fetchone()
            return row["prompt_nuevo"] if row else None
        except Exception as e:
            logger.debug(f"consultar_reescritura falló: {e}")
            return None

    # ------------------------------------------------------------
    # Helpers internos
    # ------------------------------------------------------------
    def _agente_relevante(self, conn, fb):
        """
        Encuentra el agente LLM al que dirigir la reescritura.
        Prioridad:
        1. Si el feedback tiene agente_ejecucion_id, ese (si es LLM).
        2. El último agente LLM de la ejecución (por orden DESC).
        3. None.

        NOTA: `agentes_ejecucion` no tiene columna 'descripcion'
        (Agente.to_dict() no la persiste). El meta-prompt usará
        string vacío en su lugar.
        """
        if fb["agente_ejecucion_id"]:
            agente = conn.execute(
                """SELECT id, nombre, tipo, prompt_usado,
                        resultado, estado
                FROM agentes_ejecucion
                WHERE id = ? AND tipo = 'LLM'""",
                (fb["agente_ejecucion_id"],),
            ).fetchone()
            if agente:
                return agente

        return conn.execute(
            """SELECT id, nombre, tipo, prompt_usado,
                    resultado, estado
            FROM agentes_ejecucion
            WHERE ejecucion_id = ? AND tipo = 'LLM'
            ORDER BY orden DESC, id DESC LIMIT 1""",
            (fb["ejecucion_id"],),
        ).fetchone()

    def _reescribir_con_llm(self, prompt_original, comentario, agente_row):
        # Convertir Row a dict para permitir .get() con default
        agente = dict(agente_row) if agente_row else {}
        try:
            respuesta = self.llm.chat(
                prompt=META_PROMPT.format(
                    prompt_original=prompt_original[:4000],
                    comentario=(comentario or "")[:2000],
                    nombre_agente=agente.get("nombre", "?"),
                    tipo_agente=agente.get("tipo", "?"),
                    descripcion=(agente.get("descripcion") or "")[:500],
                    resultado=str(agente.get("resultado") or "")[:1000],
                ),
                system_prompt=SYSTEM_PROMPT_LLM,
                temperature=0.3,
                max_tokens=2000,
                reasoning_effort="low",
                thinking_enabled=False,
            )

            from core.utils import extraer_json_de_llm
            data = extraer_json_de_llm(respuesta)
            if not data or not isinstance(data, dict):
                return None
            return data
        except Exception as e:
            logger.warning(f"Reescritura LLM falló: {e}")
            return None

    def _guardar_reescritura(
    self,
    prompt_original: str,
    prompt_nuevo: str,
    razon: str,
    feedback_id: int,
) -> Optional[int]:
        try:
            firma = self._firmar(prompt_original)
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                conn.execute("PRAGMA busy_timeout=10000")
                # Desactivar versiones anteriores de la misma firma.
                # Mantenemos `activo` por compatibilidad y añadimos `estado`.
                conn.execute(
                    "UPDATE prompts_reescritos "
                    "SET activo = 0, estado = 'descartado' "
                    "WHERE firma = ?",
                    (firma,),
                )
                cur = conn.execute(
                    """INSERT INTO prompts_reescritos
                    (firma, prompt_original, prompt_nuevo, feedback_id,
                        razon, fecha, activo, estado, n_usos)
                    VALUES (?, ?, ?, ?, ?, ?, 1, 'candidato', 0)""",
                    (firma, prompt_original, prompt_nuevo, feedback_id,
                    razon[:500], datetime.now().isoformat()),
                )
                conn.commit()
                return cur.lastrowid
        except Exception as e:
            logger.warning(f"Guardar reescritura falló: {e}")
            return None

    @staticmethod
    def _firmar(prompt: str) -> str:
        """
        Firma semántica agresiva: normaliza, quita stopwords, toma las
        primeras N palabras significativas, y hashea.

        Motivación: el LLM genera prompts ligeramente distintos para la
        misma tarea en cada ejecución ('Escribe un cuento corto original
        sobre un dragón' vs 'Escribe un cuento corto en español sobre un
        dragón'). Con una firma de hash exacto, esos prompts nunca
        coinciden y el A/B no acumula usos.

        Con esta firma, ambos colisionan porque comparten las primeras
        palabras significativas (escribe, cuento, corto, ...).

        ⚠️ Invalida todas las firmas existentes. Requiere re-ejecutar
        el recálculo de firmas sobre prompts_reescritos tras el cambio.
        """
        if not prompt:
            return ""
        import unicodedata
        import re

        # Normalizar: minúsculas, sin acentos
        t = unicodedata.normalize("NFKD", prompt.lower())
        t = "".join(c for c in t if not unicodedata.combining(c))

        # Extraer palabras alfanuméricas
        palabras = re.findall(r"[a-z0-9]+", t)

        # Quitar stopwords y palabras muy cortas
        stopwords = frozenset({
            # artículos, preposiciones, conjunciones
            "de", "la", "el", "los", "las", "un", "una", "unos", "unas",
            "y", "o", "u", "e", "a", "en", "con", "por", "para",
            "que", "es", "son", "al", "del", "se", "su", "sus",
            "lo", "le", "les", "tu", "tus", "mi", "mis", "si", "no",
            "the", "of", "and", "or", "to", "in", "on", "at", "by",
            "for", "with", "from", "as", "is", "are", "be", "been",
            # verbos genéricos de instrucción (los quitamos porque
            # varían entre ejecuciones)
            "escribe", "genera", "crea", "redacta", "elabora",
            "produce", "construye", "haz",
            # instrucciones meta comunes
            "devuelve", "responde", "usa", "utiliza", "incluye",
            "asegurate", "verifica", "no", "solo", "unicamente",
        })

        significativas = [
            p for p in palabras
            if p not in stopwords and len(p) > 2
        ][:1]  # primera palabra significativas

        if not significativas:
            # Fallback: si todo son stopwords, usar las primeras palabras
            significativas = palabras[:6]

        clave = " ".join(significativas)
        return hashlib.sha256(clave.encode("utf-8")).hexdigest()

    def _asegurar_tabla(self):
        """
        Crea la tabla `prompts_reescritos` si no existe.
        Idempotente. No debería hacer falta si `learning/schema.py` ya
        la define, pero garantiza que el processor funcione aunque se
        instancie en un contexto donde el schema no se haya aplicado.
        """
        try:
            with sqlite3.connect(self.db_path, timeout=10) as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS prompts_reescritos (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        firma TEXT NOT NULL,
                        prompt_original TEXT NOT NULL,
                        prompt_nuevo TEXT NOT NULL,
                        feedback_id INTEGER NOT NULL,
                        razon TEXT DEFAULT '',
                        fecha TEXT NOT NULL,
                        activo INTEGER DEFAULT 1
                    )
                """)
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_prompts_reescritos_firma
                    ON prompts_reescritos(firma, activo)
                """)
                conn.commit()
        except Exception as e:
            logger.debug(f"_asegurar_tabla falló: {e}")

    