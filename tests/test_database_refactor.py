# test_database_refactor.py
from storage.database import Database
from storage.database.models import Ejecucion, AgenteEjecucion

db = Database("test_refactor.db")

# Guardar
eid = db.guardar_ejecucion(
    agentes=[
        {"id": "a1", "nombre": "Test", "tipo": "Python", "estado": "completado",
         "duracion": 1.5, "dependencias": [], "resultado": {"ok": True}},
    ],
    duracion_total=1.5,
    estado="completada",
)
print(f"Guardado ID: {eid}")

# Consultar
hist = db.obtener_historial(limit=5)
print(f"Historial: {len(hist)} registros")

detalle = db.obtener_detalle_ejecucion(eid)
print(f"Detalle: {len(detalle)} agentes")

stats = db.obtener_estadisticas()
print(f"Stats: {stats.get('total_ejecuciones')} ejecuciones")

info = db.obtener_info_db()
print(f"Info DB: {info.get('tamaño_mb'):.3f} MB")

diag = db.diagnostico()
print(f"Diagnóstico: integridad={diag['integridad']}, faltantes={diag['columnas_faltantes']}")

# Mantenimiento
print(f"Integridad: {db.verificar_integridad()}")

db.close()