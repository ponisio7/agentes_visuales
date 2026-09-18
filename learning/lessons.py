"""
learning/lessons.py
Extrae "lecciones" legibles desde las evaluaciones del LLM y el
historial de ejecuciones.

Objetivo: convertir datos crudos (scores bajos, errores recurrentes,
justificaciones del evaluador) en reglas cortas que se puedan inyectar
en el system prompt del ProblemSolver para que el LLM genere mejores
planes.

NO usa el LLM para nada: es pura minería sobre la BD.
"""
from __future__ import annotations

import logging
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass

from core.plan_repairs import GENERADOR_URLS_NOMBRE

logger = logging.getLogger(__name__)


# Mínimos para que una lección se considere "fiable"
MIN_EJEMPLOS_PARA_LECCION = 2
SCORE_BAJO = 0.5
MAX_LECCIONES = 6


@dataclass
class Leccion:
    """Una regla extraída de los datos, lista para el prompt."""
    regla: str                    # Texto corto, imperativo
    evidencia: str                # Ej: "5 fallos en 30 ejecuciones"
    confianza: float = 0.5        # 0.0-1.0
    categoria: str = "general"    # 'error_recurrente' | 'parametro' | 'estructura'


class ExtractorLecciones:
    """
    Extrae lecciones de la BD. Uso típico:

        extractor = ExtractorLecciones("agent_history.db")
        lecciones = extractor.extraer(max_lecciones=5)
        texto = extractor.formatear_para_prompt(lecciones)
    """

    def __init__(self, db_path: str):
        self.db_path = str(db_path)

    def _lecciones_por_reparaciones(self, conn: sqlite3.Connection) -> list[Leccion]:
        """
        Si un tipo de reparación se repite N veces, genera una lección
        que se inyectará en el prompt para que el LLM aprenda.
        """
        try:
            cursor = conn.execute(
                """
                SELECT tipo, COUNT(*) as n
                FROM reparaciones_plan
                GROUP BY tipo
                HAVING n >= ?
                """,
                (MIN_EJEMPLOS_PARA_LECCION,),
            )
        except sqlite3.OperationalError:
            # Tabla no existe (BD antigua). Ignorar.
            return []

        lecciones = []
        for fila in cursor.fetchall():
            tipo = fila["tipo"]
            n = fila["n"]
            regla = self._texto_leccion_reparacion(tipo, n)
            if regla:
                confianza = min(0.95, 0.5 + n / 20)
                lecciones.append(Leccion(
                    regla=regla,
                    evidencia=f"{n} planes reparados automáticamente",
                    confianza=confianza,
                    categoria="reparacion_recurrente",
                ))
        return lecciones

    def _texto_leccion_reparacion(self, tipo: str, n: int) -> str | None:
        if tipo == GENERADOR_URLS_NOMBRE:
            return (
                "Cuando el usuario pida un documento CON IMÁGENES, DEBES "
                "incluir un paso que genere las URLs de las imágenes "
                "(usa `picsum.photos` u otra API pública sin auth). "
                "El agente File de .docx descargará e insertará esas imágenes. "
                "NO generes solo el texto del documento."
            )
        return None

    # ------------------------------------------------------------------
    # API pública
    # ------------------------------------------------------------------
    def extraer(self, max_lecciones: int = MAX_LECCIONES) -> list[Leccion]:
        """Devuelve una lista de lecciones ordenadas por confianza."""
        try:
            with closing(sqlite3.connect(self.db_path, timeout=10)) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA busy_timeout=10000")
                lecciones = []
                lecciones.extend(self._lecciones_por_error_recurrente(conn))
                lecciones.extend(self._lecciones_por_score_bajo(conn))
                lecciones.extend(self._lecciones_por_estructura(conn))
                lecciones.extend(self._lecciones_por_reparaciones(conn))   # ← AÑADIR
        except Exception as e:
            logger.debug(f"ExtractorLecciones falló: {e}")
            return []

        # Ordenar por confianza descendente y limitar
        lecciones.sort(key=lambda l: l.confianza, reverse=True)
        return lecciones[:max_lecciones]

    def formatear_para_prompt(self, lecciones: list[Leccion]) -> str:
        """Convierte la lista en un bloque de texto para el system prompt."""
        if not lecciones:
            return ""
        lineas = [
            "## LECCIONES APRENDIDAS DE EJECUCIONES PREVIAS",
            "",
            "Basado en el historial de este sistema, ten en cuenta:",
            "",
        ]
        for i, lec in enumerate(lecciones, 1):
            lineas.append(f"{i}. {lec.regla} ({lec.evidencia})")
        lineas.append("")
        lineas.append(
            "Aplica estas lecciones al generar el plan. Si una lección "
            "es sobre parámetros, ajusta los valores por defecto que ibas "
            "a usar. Si es sobre estructura, evita el patrón mencionado."
        )
        return "\n".join(lineas)

    # ------------------------------------------------------------------
    # Lecciones por error recurrente
    # ------------------------------------------------------------------
    def _lecciones_por_error_recurrente(
        self, conn: sqlite3.Connection
    ) -> list[Leccion]:
        """
        Detecta errores que se repiten en agentes_ejecucion y los
        convierte en reglas del tipo "cuando X, haz Y".
        """
        cursor = conn.execute(
            """
            SELECT error, tipo, nombre
            FROM agentes_ejecucion
            WHERE estado IN ('Error', 'Timeout')
              AND error IS NOT NULL
              AND error != ''
            """
        )
        filas = cursor.fetchall()
        if not filas:
            return []

        # Agrupar por "patrón de error" (categorizado)
        patrones = defaultdict(list)
        for fila in filas:
            error = (fila["error"] or "").lower()
            tipo = fila["tipo"] or "?"
            patron = self._categorizar_error(error, tipo)
            if patron:
                patrones[patron].append(fila)

        lecciones = []
        for patron, ejemplos in patrones.items():
            if len(ejemplos) < MIN_EJEMPLOS_PARA_LECCION:
                continue
            n = len(ejemplos)
            confianza = min(0.95, 0.5 + (n / 20))
            regla, evidencia = self._texto_leccion_error(patron, n)
            if regla:
                lecciones.append(Leccion(
                    regla=regla,
                    evidencia=evidencia,
                    confianza=confianza,
                    categoria="error_recurrente",
                ))
        return lecciones

    def _categorizar_error(self, error: str, tipo: str) -> str | None:
        """Clasifica un mensaje de error en una categoría."""
        if "path traversal" in error or "ruta contiene" in error:
            return "ruta_invalida"
        if "status: 403" in error:
            return "http_forbidden"
        if "status: 401" in error:
            return "http_unauthorized"
        if "status: 404" in error or "status_code: 404" in error:
            return "endpoint_no_existe"
        if "json" in error and ("truncad" in error or "truncat" in error):
            return "json_truncado_llm"
        if "max_tokens" in error or "max tokens" in error:
            return "max_tokens_bajo"
        if "filenotfound" in error or "no such file" in error:
            return "archivo_no_existe"
        if "permission" in error or "root" in error or "permiso" in error:
            return "permisos_root"

        if "indent" in error:
            return "indentacion_incorrecta"
        if "connection" in error or "network" in error:
            return "problema_red"
        if "no module named 'fpdf'" in error or "module named 'fpdf'" in error:
            return "fpdf_no_instalada"
        if "content_is_pdf" in error:
            return "pdf_a_mano"
        if "modulenotfound" in error or "no module named" in error:
            return "dependencia_faltante"
        # ── NUEVOS PATRONES ──
        if "401 client error" in error and "unauthorized" in error:
            return "http_401_api_key"
        if "timeout" in error and tipo == "Loop":
            return "loop_timeout_sandbox"
        if "tu_access_key" in error.lower() or "tu_api_key" in error.lower():
            return "api_key_placeholder"

        # ⬇ NUEVAS CATEGORÍAS (parche 2026-09)
        # El caso más común en el log: 'str' object has no attribute 'get'
        # cuando el LLM asume que una API devuelve dicts anidados y
        # devuelve listas de strings, o cuando un JSON viene mal formado.
        if ("attributeerror" in error
                or "object has no attribute" in error
                or "'str' object has no attribute" in error):
            return "estructura_datos_inesperada"
        if "keyerror" in error:
            return "clave_faltante_json"
        if "typeerror" in error or "unexpected type" in error:
            return "tipo_datos_inesperado"
        if "indexerror" in error or "list index out of range" in error:
            return "indice_fuera_rango"
        if "valueerror" in error and ("invalid" in error or "could not convert" in error):
            return "valor_invalido"
        if "timeout" in error:
            return "timeout"

        return None

    def _texto_leccion_error(self, patron: str, n: int) -> tuple[str | None, str]:
        if patron == "json_truncado_llm":
            return (
                "Cuando generes JSON grande, divide la salida en pasos más "
                "pequeños o aumenta max_tokens. El LLM está truncando JSON a mitad.",
                f"{n} JSON truncados por el LLM"
            )
        if patron == "max_tokens_bajo":
            return (
                "Aumenta el parámetro max_tokens. Las respuestas se están "
                "cortando por límite de tokens.",
                f"{n} cortes por max_tokens"
            )
        if patron == "archivo_no_existe":
            return (
                "Antes de leer un archivo con open(), "
                "asegúrate de que el agente anterior lo crea con el nombre exacto "
                "y añade una verificación previa con os.path.exists().",
                f"{n} fallos por FileNotFoundError"
            )
        if patron == "permisos_root":
            return (
                "Los comandos que requieren root (apt, systemctl, mount...) "
                "se ejecutarán con pkexec automáticamente. No añadas 'sudo' "
                "explícito.",
                f"{n} comandos con permisos elevados"
            )
        if patron == "timeout":
            return (
                "Aumenta los timeouts si el paso hace operaciones lentas "
                "(descargas HTTP grandes, bucles extensos).",
                f"{n} timeouts"
            )
        if patron == "indentacion_incorrecta":
            return (
                "Al generar código Python, usa SIEMPRE indentación de 4 "
                "espacios por nivel. No mezcles tabuladores con espacios.",
                f"{n} casos de indentación incorrecta"
            )
        if patron == "problema_red":
            return (
                "Añade manejo de errores de red en agentes HTTP: reintentos "
                "y validación de status_code.",
                f"{n} errores de red"
            )
        if patron == "dependencia_faltante":
            return (
                "Evita importar módulos externos que puedan no estar "
                "instalados. Prefiere solo librería estándar de Python.",
                f"{n} errores por módulo no disponible"
            )
        if patron == "fpdf_no_instalada":
            return (
                "La librería 'fpdf' NO está instalada en este entorno. Para "
                "generar PDFs, usa un agente File con operacion='escribir' y "
                "archivo_destino terminado en .pdf (el sistema convierte el "
                "texto plano automáticamente). Si necesitas generar el PDF "
                "directamente en Python, usa 'reportlab' en su lugar.",
                f"{n} errores por fpdf no instalada"
            )
        if patron == "pdf_a_mano":
            return (
                "NO generes PDFs (ni otros formatos binarios como .docx/.xlsx) "
                "concatenando bytes o strings 'a mano' en un agente Python: los "
                "offsets de xref y longitudes de stream casi nunca quedan "
                "correctos. Genera el contenido como texto plano y deja que el "
                "agente File lo convierta al formato indicado por la extensión.",
                f"{n} intentos de generar un PDF a mano"
            )
        if patron == "http_forbidden":
            return (
                "Cuando uses APIs externas (Reddit, Twitter, etc.), añade un "
                "User-Agent realista en headers. Muchas APIs bloquean peticiones "
                "sin User-Agent o con valores genéricos, o aplican rate limiting "
                "(status 403). Considera espaciar las peticiones (no más de 1 req/s).",
                f"{n} bloqueos HTTP 403 (forbidden)"
            )
        if patron == "http_unauthorized":
            return (
                "Un status 401 indica que falta la API key o es inválida. "
                "Verifica que el header de autenticación (Authorization, "
                "X-API-Key, etc.) esté presente y con el formato exacto que "
                "exige la API antes de incluir la llamada en el plan.",
                f"{n} bloqueos HTTP 401 (unauthorized)"
            )
        if patron == "endpoint_no_existe":
            return (
                "Cuando uses APIs externas, VERIFICA que la URL existe antes "
                "de incluirla en el plan. Los endpoints siguen la sintaxis "
                "exacta de la documentación oficial; NO inventes rutas. "
                "Ejemplo: bible-api.com espera '/juan+3:16', no '?verse=325'. "
                "Si no estás seguro de la URL, usa un agente Python con datos "
                "sintéticos en lugar de inventarte una API.",
                f"{n} errores HTTP 404 (endpoint no existe)"
            )
        if patron == "ruta_invalida":
            return (
                "No uses rutas con '..' ni rutas absolutas (que empiecen con '/'). "
                "Usa rutas relativas simples como 'datos.txt' o 'output/resultado.json'.",
                f"{n} rutas inválidas"
            )
        if patron == "estructura_datos_inesperada":
            return (
                "Cuando proceses datos de APIs externas (REST, JSON, etc.), "
                "NUNCA asumas que son dicts anidados. Valida el tipo antes "
                "de llamar a .get(): usa 'if isinstance(x, dict) else ...' "
                "o 'x.get(...) if isinstance(x, dict) else default'. "
                "Esta clase de error aparece cuando una API devuelve una "
                "lista de strings donde el LLM esperaba objetos con claves.",
                f"{n} errores de estructura de datos"
            )
        if patron == "clave_faltante_json":
            return (
                "Usa .get('clave', default) en lugar de ['clave'] al leer "
                "JSON de APIs externas. Las APIs a veces omiten campos, y "
                "un KeyError detiene toda la cadena de agentes.",
                f"{n} KeyError en lectura de JSON"
            )
        if patron == "tipo_datos_inesperado":
            return (
                "Verifica el tipo de los datos antes de operar con ellos. "
                "Si un paso anterior puede devolver un tipo inesperado, "
                "añade una comprobación explícita (isinstance, type()) "
                "y un fallback. No confíes en que el LLM anterior generó "
                "el formato correcto.",
                f"{n} errores de tipo"
            )
        if patron == "indice_fuera_rango":
            return (
                "Antes de acceder a una posición de una lista, comprueba "
                "que la longitud es suficiente. Usa 'if len(lista) > i:' "
                "o slicing seguro 'lista[i:i+1]'. Los datos de APIs "
                "pueden venir vacíos aunque la llamada sea exitosa.",
                f"{n} accesos fuera de rango"
            )
        if patron == "valor_invalido":
            return (
                "Valida los valores antes de convertirlos (int(), float(), "
                "json.loads()). Los strings vacíos, 'null' o formatos "
                "inesperados rompen la conversión. Usa try/except con "
                "valor por defecto.",
                f"{n} conversiones inválidas"
            )

        if patron == "http_401_api_key":
            return (
                "No uses APIs que requieren autenticación (Unsplash, Twitter, etc.) "
                "si no tienes API key configurada. Usa APIs públicas sin auth: "
                "picsum.photos, source.unsplash.com, o similares.",
                f"{n} errores 401 por API key inventada"
            )
        if patron == "loop_timeout_sandbox":
            return (
                "El sandbox de Python NO tiene acceso a red fiable. Los loops que "
                "hacen peticiones HTTP con requests.get() fallan por timeout. "
                "El HTTP debe hacerse con el agente HTTP, no dentro del sandbox.",
                f"{n} timeouts de sandbox en loops con HTTP"
            )
        if patron == "api_key_placeholder":
            return (
                "NUNCA uses valores como 'TU_ACCESS_KEY' o 'TU_API_KEY' en el código. "
                "Si una API requiere autenticación y no tienes la key, elige otra API "
                "pública o cambia de estrategia.",
                f"{n} usos de API key placeholder"
            )
        return None, ""

    # ------------------------------------------------------------------
    # Lecciones por score bajo del LLM evaluador
    # ------------------------------------------------------------------
    def _lecciones_por_score_bajo(
        self, conn: sqlite3.Connection
    ) -> list[Leccion]:
        """
        Agrupa evaluaciones con score bajo por tipo de agente y detecta
        tendencias: "los agentes tipo X están puntuando bajo".
        """
        cursor = conn.execute(
            """
            SELECT e.score, e.justificacion, ae.tipo, ae.nombre
            FROM evaluaciones_llm e
            LEFT JOIN agentes_ejecucion ae ON ae.id = e.agente_ejecucion_id
            WHERE e.alcance = 'agente' AND e.score < ?
            """,
            (SCORE_BAJO,),
        )
        filas = cursor.fetchall()
        if not filas:
            return []

        # ⬇ PARCHE 3: detectar patrón "resultado vacío" en las
        # justificaciones del evaluador. Es la causa raíz más común
        # de score 0.0 y no está en el campo `error` de la BD.
        patrones_justificacion = defaultdict(list)
        for fila in filas:
            just = (fila["justificacion"] or "").lower()
            if not just:
                continue
            if ("está vacío" in just or "está vacia" in just
                    or "está vacía" in just or "no contiene" in just
                    or "no incluye" in just or "sin información" in just
                    or "solo devolvió un error" in just):
                patrones_justificacion["resultado_vacio"].append(fila)

        # Contar por tipo de agente
        por_tipo = Counter()
        justificaciones = defaultdict(list)
        for fila in filas:
            tipo = fila["tipo"]
            # ⬇️ PARCHE: descartar filas sin tipo real. El LEFT JOIN
            # devuelve NULL cuando agente_ejecucion_id=0 (que es siempre
            # en la integración actual). Sin tipo no se puede generar
            # una lección útil, así que se ignora.
            if not tipo or tipo == "?":
                continue
            por_tipo[tipo] += 1
            if fila["justificacion"]:
                justificaciones[tipo].append(fila["justificacion"])

        lecciones = []
        for tipo, n in por_tipo.most_common():
            if n < MIN_EJEMPLOS_PARA_LECCION:
                continue
            confianza = min(0.9, 0.4 + (n / 30))
            justs = justificaciones.get(tipo, [])
            ejemplo = justs[0][:100] if justs else ""
            regla = (
                f"Los pasos de tipo '{tipo}' suelen puntuar bajo. "
                f"Sé especialmente cuidadoso al configurarlos "
                f"({ejemplo})"
            )
            lecciones.append(Leccion(
                regla=regla,
                evidencia=f"{n} evaluaciones con score < {SCORE_BAJO}",
                confianza=confianza,
                categoria="parametro",
            ))

        # Generar la lección de "resultado vacío" si hay suficientes ejemplos.
        ejemplos_vacio = patrones_justificacion.get("resultado_vacio", [])
        if len(ejemplos_vacio) >= MIN_EJEMPLOS_PARA_LECCION:
            n = len(ejemplos_vacio)
            confianza = min(0.9, 0.5 + (n / 20))

            regla = (
                "MUCHOS pasos devuelven resultados VACÍOS sin lanzar error "
                "(dict vacío, lista vacía, string vacío). Esto es un fallo "
                "silencioso. Añade SIEMPRE validación post-ejecución: "
                "'if not resultado: raise ValueError(...)' o "
                "'if not datos: return dict(error=\"sin datos\")'. "
                "Verifica que la API devolvió datos antes de procesarlos."
            )
            lecciones.append(Leccion(
                regla=regla,
                evidencia=f"{n} evaluaciones con score 0.0 por resultado vacío",
                confianza=confianza,
                categoria="error_silencioso",
            ))

        return lecciones

    # ------------------------------------------------------------------
    # Lecciones por estructura de plan
    # ------------------------------------------------------------------
    def _lecciones_por_estructura(
        self, conn: sqlite3.Connection
    ) -> list[Leccion]:
        """
        Compara planes exitosos vs fallidos y extrae reglas estructurales
        (ej: "planes con 6+ agentes fallan más").
        """
        cursor = conn.execute(
            """
            SELECT agentes_total, errores, completados, estado
            FROM ejecuciones
            WHERE agentes_total > 0
            """
        )
        filas = cursor.fetchall()
        if len(filas) < 10:
            return []

        # Agrupar por tramos de tamaño
        tramos = {
            "corto (1-3 agentes)": [0, 0],      # [exitos, total]
            "medio (4-6 agentes)": [0, 0],
            "largo (7+ agentes)": [0, 0],
        }
        for fila in filas:
            n = fila["agentes_total"] or 0
            errores = fila["errores"] or 0
            if n <= 3:
                clave = "corto (1-3 agentes)"
            elif n <= 6:
                clave = "medio (4-6 agentes)"
            else:
                clave = "largo (7+ agentes)"
            tramos[clave][1] += 1
            if errores == 0:
                tramos[clave][0] += 1

        lecciones = []
        for clave, (exitos, total) in tramos.items():
            if total < 5:
                continue
            tasa = exitos / total
            if tasa < 0.5:
                confianza = min(0.85, 0.5 + (total / 40))
                lecciones.append(Leccion(
                    regla=(
                        f"Los planes {clave} tienen tasa de éxito baja "
                        f"({tasa:.0%}). Sé conciso: si puedes resolver el "
                        f"problema con menos pasos, hazlo."
                    ),
                    evidencia=f"{exitos}/{total} con éxito",
                    confianza=confianza,
                    categoria="estructura",
                ))
        return lecciones
