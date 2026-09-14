from pathlib import Path
import re

p = Path("learning/lessons.py")
src = p.read_text(encoding="utf-8")

# Backup
backup = p.with_suffix(".py.bak2")
backup.write_text(src, encoding="utf-8")
print(f"📦 Backup: {backup}")

# ──────────────────────────────────────────────────────────────
# FIX 1: Reemplazar el método _lecciones_por_error_recurrente completo
# ──────────────────────────────────────────────────────────────

# Detectar el método actual (que tiene el bug del return mal ubicado)
patron_metodo = re.compile(
    r'(    def _lecciones_por_error_recurrente\(\s*\n'
    r'        self, conn: sqlite3\.Connection\s*\n'
    r'    \) -> List\[Leccion\]:.*?)\n'
    r'    def _categorizar_error',
    re.DOTALL,
)

match = patron_metodo.search(src)
if not match:
    print("❌ No encontré _lecciones_por_error_recurrente")
    raise SystemExit(1)

metodo_corregido = '''    def _lecciones_por_error_recurrente(
        self, conn: sqlite3.Connection
    ) -> List[Leccion]:
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

    def _categorizar_error'''

src = src[:match.start()] + metodo_corregido + src[match.end() + len("    def _categorizar_error"):]
print("✅ _lecciones_por_error_recurrente restaurado")

# ──────────────────────────────────────────────────────────────
# FIX 2: Verificar que _texto_leccion_error tiene los bloques nuevos
# ──────────────────────────────────────────────────────────────

if 'if patron == "http_auth_blocked":' not in src or 'if patron == "ruta_invalida":' not in src:
    print("⚠️ _texto_leccion_error NO tiene los bloques nuevos. Los añado...")
    
    # Buscar el cierre de _texto_leccion_error
    ancla_fin = '''        if patron == "dependencia_faltante":
            return (
                "Evita importar módulos externos que puedan no estar "
                "instalados. Prefiere solo librería estándar de Python.",
                f"{n} errores por módulo no disponible"
            )
        return None, ""'''
    
    nuevo_fin = '''        if patron == "dependencia_faltante":
            return (
                "Evita importar módulos externos que puedan no estar "
                "instalados. Prefiere solo librería estándar de Python.",
                f"{n} errores por módulo no disponible"
            )
        if patron == "http_auth_blocked":
            return (
                "Cuando uses APIs externas (Reddit, Twitter, etc.), añade un "
                "User-Agent realista en headers. Muchas APIs bloquean peticiones "
                "sin User-Agent o con valores genéricos. También considera "
                "rate limiting (no más de 1 req/s).",
                f"{n} bloqueos HTTP 401/403"
            )
        if patron == "ruta_invalida":
            return (
                "No uses rutas con '..' ni rutas absolutas (que empiecen con '/'). "
                "Usa rutas relativas simples como 'datos.txt' o 'output/resultado.json'.",
                f"{n} rutas inválidas"
            )
        return None, ""'''
    
    if ancla_fin not in src:
        print("❌ No encontré el ancla del final de _texto_leccion_error")
        raise SystemExit(1)
    src = src.replace(ancla_fin, nuevo_fin, 1)
    print("✅ Bloques nuevos añadidos a _texto_leccion_error")
else:
    print("✅ _texto_leccion_error ya tiene los bloques nuevos")

# Guardar
p.write_text(src, encoding="utf-8")
print("💾 Archivo guardado")
