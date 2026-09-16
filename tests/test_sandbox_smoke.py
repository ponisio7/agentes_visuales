from core.sandbox import PythonSandbox
sb = PythonSandbox()

# Caso 1: diccionario con llaves
ok, msg, meta = sb.ejecutar("""
datos = {'a': 1, 'b': 2}
resultado = sum(datos.values())
""", {})
print("Caso 1:", ok, msg, meta)

# Caso 2: f-string del usuario con llaves
ok, msg, meta = sb.ejecutar("""
nombre = "pepe"
resultado = f"Hola {nombre}, tienes {2+3} mensajes"
""", {})
print("Caso 2:", ok, msg, meta)

# Caso 3: docstring con triple comilla
ok, msg, meta = sb.ejecutar('''
def foo():
    """Docstring normal"""
    return 42
resultado = foo()
''', {})
print("Caso 3:", ok, msg, meta)