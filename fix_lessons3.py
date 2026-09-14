from pathlib import Path

p = Path("learning/lessons.py")
src = p.read_text(encoding="utf-8")

# Backup por si acaso
(p.with_suffix(".py.bak3")).write_text(src, encoding="utf-8")
print("📦 Backup: learning/lessons.py.bak3")

# ─────────────────────────────────────────────────────────────
# FIX 1: Eliminar los bloques mal ubicados en _lecciones_por_error_recurrente
# ─────────────────────────────────────────────────────────────

bloque_mal_ubicado = '''            patron = self._categorizar_error(error, tipo)
            if patron == "http_auth_blocked":
                return (
                    "Cuando uses APIs externas (Reddit, Twitter, etc.), añade un "
                    "User-Agent realista en headers. Muchas APIs bloquean peticiones "
                    "sin User-Agent o con valores genéricos. También considera rate limiting.",
                    f"{n} bloqueos HTTP 401/403"
                )
            elif patron == "ruta_invalida":
                return (
                    "No uses rutas con '..' ni rutas absolutas (que empiecen con '/'). "
                    "Usa rutas relativas simples como 'datos.txt' o 'output/resultado.json'.",
                    f"{n} rutas inválidas"
                )
            elif patron:
                patrones[patron].append(fila)
'''

bloque_correcto = '''            patron = self._categorizar_error(error, tipo)
            if patron:
                patrones[patron].append(fila)
'''

if bloque_mal_ubicado in src:
    src = src.replace(bloque_mal_ubicado, bloque_correcto, 1)
    print("✅ Bloque mal ubicado eliminado de _lecciones_por_error_recurrente")
elif bloque_correcto in src:
    print("⚠️ El bloque mal ubicado ya estaba eliminado")
else:
    print("❌ No encontré el bloque mal ubicado. Abortando.")
    raise SystemExit(1)

# ─────────────────────────────────────────────────────────────
# FIX 2: Añadir los 2 bloques nuevos al final de _texto_leccion_error
# ─────────────────────────────────────────────────────────────

ancla_final = '''        if patron == "dependencia_faltante":
            return (
                "Evita importar módulos externos que puedan no estar "
                "instalados. Prefiere solo librería estándar de Python.",
                f"{n} errores por módulo no disponible"
            )
        return None, ""
'''

bloque_nuevo = '''        if patron == "dependencia_faltante":
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
        return None, ""
'''

if 'if patron == "http_auth_blocked":' in src and 'if patron == "ruta_invalida":' in src and 'bloqueos HTTP 401/403' in src:
    # Verificar que están DENTRO de _texto_leccion_error (después del if de dependencia_faltante)
    idx = src.find('if patron == "dependencia_faltante":')
    idx_auth = src.find('if patron == "http_auth_blocked":')
    if idx != -1 and idx_auth != -1 and idx_auth > idx:
        print("⚠️ Los bloques nuevos ya están en el sitio correcto, no toco nada.")
    else:
        print("❌ Los bloques existen pero en sitio incorrecto. Abortando.")
        raise SystemExit(1)
elif ancla_final in src:
    src = src.replace(ancla_final, bloque_nuevo, 1)
    print("✅ Bloques nuevos añadidos a _texto_leccion_error")
else:
    print("❌ No encontré el ancla final de _texto_leccion_error. Abortando.")
    raise SystemExit(1)

# ─────────────────────────────────────────────────────────────
# Guardar
# ─────────────────────────────────────────────────────────────
p.write_text(src, encoding="utf-8")
print("💾 Archivo guardado")
