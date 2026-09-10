# run_all_tests.py
#!/usr/bin/env python3
import sys
import os
import subprocess
import importlib.util

def verificar_dependencias():
    """Verifica que todas las dependencias estén instaladas"""
    dependencias = ['pytest', 'PyQt6', 'matplotlib', 'requests', 'openai', 'plyer', 'numpy', 'pandas']
    faltantes = []
    
    for dep in dependencias:
        if importlib.util.find_spec(dep) is None:
            faltantes.append(dep)
    
    if faltantes:
        print(f"❌ Faltan dependencias: {', '.join(faltantes)}")
        print(f"   Instalar con: pip install {' '.join(faltantes)}")
        return False
    return True

def main():
    print("=" * 70)
    print("🧪 EJECUTANDO PRUEBAS UNITARIAS - Agentes Visuales")
    print("=" * 70)
    
    # Verificar dependencias
    if not verificar_dependencias():
        return 1
    
    # Cambiar al directorio del proyecto
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    
    # Ejecutar pytest
    cmd = [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short"]
    
    result = subprocess.run(cmd, capture_output=False)
    
    print("=" * 70)
    if result.returncode == 0:
        print("✅ TODAS LAS PRUEBAS PASARON")
    else:
        print("❌ ALGUNAS PRUEBAS FALLARON")
    
    return result.returncode

if __name__ == "__main__":
    sys.exit(main())