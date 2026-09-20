import ast
from core.problem_solver.code_corrector import PythonCodeCorrector as C

casos = [
    ("respuesta con dep", "texto = respuesta", ["GenerarCuento"]),
    ("respuesta SIN dep", "texto = respuesta", []),
    ("data = respuesta", "data = respuesta", ["GenerarCuento"]),
    ("result legitimo", "result = json.loads(respuesta)\nprint(result)", ["GenerarCuento"]),
    ("ya usa contexto.get + respuesta", "texto = respuesta\nx = contexto.get('A', {})", []),
    ("param respuesta", "def f(respuesta):\n    return respuesta", []),
    ("dict key respuesta", "d = {'respuesta': 1}\nprint(d['respuesta'])", []),
]
for nombre, codigo, deps in casos:
    out = C.corregir(codigo, deps)
    print(f"--- {nombre} (deps={deps}) ---")
    print("IN :", codigo.replace("\n", " / "))
    print("OUT:", out.replace("\n", " / "))
    try:
        ast.parse(out); print("syntax: OK")
    except SyntaxError as e:
        print("syntax: SyntaxError ->", e.msg, "linea", e.lineno)
    print()
