from pathlib import Path

p = Path("learning/lessons.py")
src = p.read_text(encoding="utf-8")

# Bloque viejo (como está en tu archivo actual)
viejo = '''        cursor = conn.execute(
            """
            SELECT total_agents, errores, completados, estado
            FROM ejecuciones
            WHERE total_agents > 0
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
            n = fila["total_agents"] or 0
            errores = fila["errores"] or 0'''

# Bloque nuevo (con el nombre correcto de columna)
nuevo = '''        cursor = conn.execute(
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
            errores = fila["errores"] or 0'''

if "agentes_total, errores, completados, estado" in src:
    print("✅ Ya estaba arreglado, no toco nada.")
elif viejo not in src:
    print("❌ No encontré el bloque exacto.")
    print("Buscando variantes...")
    # Fallback: reemplazo simple de strings
    if "total_agents, errores, completados, estado" in src:
        src = src.replace("total_agents, errores, completados, estado",
                          "agentes_total, errores, completados, estado")
    if "WHERE total_agents > 0" in src:
        src = src.replace("WHERE total_agents > 0", "WHERE agentes_total > 0")
    if 'fila["total_agents"]' in src:
        src = src.replace('fila["total_agents"]', 'fila["agentes_total"]')
    p.write_text(src, encoding="utf-8")
    print("✅ Aplicado con reemplazo simple")
else:
    src = src.replace(viejo, nuevo, 1)
    p.write_text(src, encoding="utf-8")
    print("✅ Bloque arreglado")
