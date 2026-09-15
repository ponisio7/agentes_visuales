# tests/test_export_rapido.py
import sys
from pathlib import Path

# Añadir la raíz del proyecto al sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Ahora sí, los imports del proyecto
from export import ResultExporter, exportar_resultados

# ... resto del test
agentes = [
    {"id": 1, "nombre": "Agente A", "estado": "Completado", "duracion": 1.23,
     "config": {"tipo": "python", "timeout": 30}},
    {"id": 2, "nombre": "Agente B", "estado": "Error", "duracion": 0.5,
     "config": {"tipo": "shell", "timeout": 10}},
]

for fmt in ["csv", "json", "html", "markdown", "txt"]:
    result = exportar_resultados(agentes, formato=fmt)
    print(f"{fmt:10} → exito={result.exito}  filas={result.filas_exportadas}  ruta={result.ruta}")