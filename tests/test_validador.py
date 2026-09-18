import sys
sys.path.insert(0, '.')
from core.problem_solver.validator import PlanValidator

nombres = {"GenerarCuento", "CrearImagenes"}

casos = [
    # (codigo, esperado_errores, descripcion)
    (
        "datos = GenerarCuento\ntexto = datos['cuento']",
        1,  # debe detectar 1 NameError
        "Nombre de agente como variable",
    ),
    (
        "datos = {{CrearImagenes}}['imagenes']",
        1,  # debe detectar 1 {{X}}
        "Sintaxis de plantilla",
    ),
    (
        "import json\ndatos = json.loads('''{datos_llm}''')",
        1,  # debe detectar 1 placeholder
        "json.loads con placeholder",
    ),
    (
        "x = 1 / 0",
        0,  # código válido (falla en runtime, no en validación)
        "ZeroDivisionError (no detectado por AST)",
    ),
    (
        "datos = contexto.get('GenerarCuento', {})",
        0,  # OK
        "Código correcto",
    ),
]

for codigo, esperado, desc in casos:
    errs = PlanValidator._validar_codigo_python_ast(
        codigo=codigo,
        nombre="TestPaso",
        nombres_agentes=nombres,
    )
    ok = "✅" if len(errs) == esperado else "❌"
    print(f"{ok} {desc}: {len(errs)} error(es) (esperado {esperado})")
    for e in errs:
        print(f"     {e}")