(.venv) christian@ponisio7:~/proyectosPython/agentes_visuales$ python -c "
from core.executors.content_extractor import extraer_contenido_relevante
# Caso 1: dict con 'cuento' e 'imagenes' → debe devolver el dict entero
caso1 = {'titulo': 'X', 'cuento': 'Había una vez...', 'imagenes': [{'url': 'u1'}]}
r1 = extraer_contenido_relevante(caso1)
assert r1 is caso1, f'Caso 1 falla: {type(r1)}'
print('Caso 1 OK: devuelve el dict entero')

# Caso 2: dict con solo 'contenido' → debe extraer 'contenido'
caso2 = {'status': 'ok', 'contenido': 'Hola mundo'}
r2 = extraer_contenido_relevante(caso2)
assert r2 == 'Hola mundo', f'Caso 2 falla: {r2}'
print('Caso 2 OK: extrae contenido')

# Caso 3: dict solo con imagenes → debe devolver el dict entero
caso3 = {'imagenes': [{'url': 'u1'}]}
r3 = extraer_contenido_relevante(caso3)
assert r3 is caso3, f'Caso 3 falla: {type(r3)}'
print('Caso 3 OK: devuelve el dict entero')

# Caso 4: dict con 2+ claves prioritarias clásicas → debe devolver el dict entero
caso4 = {'html': '<p>x</p>', 'markdown': '# x'}
r4 = extraer_contenido_relevante(caso4)
"rint('Caso 4 OK: devuelve el dict entero')4)}'
Caso 1 OK: devuelve el dict entero
Traceback (most recent call last):
  File "<string>", line 12, in <module>
    assert r2 == 'Hola mundo', f'Caso 2 falla: {r2}'
           ^^^^^^^^^^^^^^^^^^
AssertionError: Caso 2 falla: {'status': 'ok', 'contenido': 'Hola mundo'}
(.venv) christian@ponisio7:~/proyectosPython/agentes_visuales$ python -c "
import zipfile
with zipfile.ZipFile('pingu.docx') as z:
    media = [n for n in z.namelist() if 'media' in n]
    print(f'Imágenes: {len(media)}')
    for m in media:
        print(f'  {m}: {z.getinfo(m).file_size} bytes')
"
Imágenes: 0
(.venv) christian@ponisio7:~/proyectosPython/agentes_visuales$





