# HARNESS REPORT — agentes_visuales v3.2.1

- **Fecha**: 2026-09-20
- **Rama**: `harness/fix-v3.2.1` (base `v3.2.0`, commit `4f2ad0e`)
- **Alcance**: hotfix de una sola regla del validador. 1 archivo de
  producción (`core/problem_solver/validator.py`) + 1 archivo de tests.
- **Commits**: 3 (test rojo, fix verde, release).

## Resumen ejecutivo

El bug **N2** queda cerrado. La primera versión de la regla se anclaba al
envoltorio `__...__` de los ejemplos (`__FARSI_JSON__`), y eso era
sobreajuste: `__FARSI_JSON__` no es un placeholder del sistema, es un
nombre que el LLM **se inventó** para el caso concreto (traducir noticias
en farsi). El anti-patrón real es «el LLM se inventa un nombre para el
contenido y lo usa como literal en vez de construirlo». La regla final
detecta esa **forma de nombre** (MAYÚSCULAS_CON_GUIONES_BAJOS, con o sin
envoltura `__...__`), sin lista de nombres conocidos y sin depender del
ejemplo.

## 1. Qué era N2 (análisis corregido)

Cuando el LLM escribía:

```python
farsi = json.loads("__FARSI_JSON__")
```

no estaba emitiendo «un placeholder con doble underscore». Estaba
inventándose un nombre para el contenido que esperaba recibir y usándolo
como literal, esperando que «algo» lo sustituyera. El mismo fallo se
produce con cualquier otro nombre inventado:

```python
texto = json.loads("__UCRAINIAN_JSON__")
html  = json.loads('__NEWS_HTML__')
cuento = "__CUENTO_DRAGON__"
script = "__CALCULADORA_JS__"
datos  = "FARSI_JSON"
```

`__FARSI_JSON__` **no es especial**. Por eso la regla NO puede ser «casar
`__XXX__`» ni ninguna lista de identificadores: tiene que detectar la
forma del nombre inventado. Ese es el cambio respecto al primer intento de
esta misma release.

## 2. Cómo se reprodujo (evidencia)

> El snippet del encargo llama a `PlanValidator.validarcodigopythonast(...)`,
> que **no existe**. El método real es el estático
> `PlanValidator._validar_codigo_python_ast(codigo=..., nombre=..., nombres_agentes=...)`.

```python
from core.problem_solver.validator import PlanValidator

codigo = 'import json\nfarsi = json.loads("__FARSI_JSON__")\n'
errores = PlanValidator._validar_codigo_python_ast(
    codigo=codigo, nombre="Test", nombres_agentes={"X"},
)
# antes del fix: []
# después del fix: ["BLOQUEANTE: Test: placeholder literal '__FARSI_JSON__' sin sustituir (no existe en el sandbox)"]
```

| Caso | Antes | Después |
|---|---|---|
| `json.loads("__FARSI_JSON__")` | ❌ 0 | ✅ 1 BLOQUEANTE |
| `json.loads('__FARSI_JSON__')` | ❌ 0 | ✅ 1 BLOQUEANTE |
| `farsi = "__FARSI_JSON__"` | ❌ 0 | ✅ 1 BLOQUEANTE |
| `json.loads("""__FARSI_JSON__""")` | ❌ 0 | ✅ 1 BLOQUEANTE |
| `json.loads("__UCRAINIAN_JSON__")` (otro nombre) | ❌ 0 | ✅ 1 BLOQUEANTE |
| `datos = "FARSI_JSON"` (sin envoltura) | ❌ 0 | ✅ 1 BLOQUEANTE |
| `json.loads("""{farsi_json}""")` (regresión) | ✅ 1 | ✅ 1 |
| `print(__name__)` | ✅ 0 | ✅ 0 |
| `logging.info(f"file={__file__}")` | ✅ 0 | ✅ 0 |
| `if __name__ == "__main__":` | ✅ 0 | ✅ 0 |
| `metodo = "GET"` | ✅ 0 | ✅ 0 |
| `formato = "CSV"` | ✅ 0 | ✅ 0 |

## 3. El fix (generalizado, sin hardcodeo)

Regla nº 5 de `_validar_codigo_python_ast`, tras las cuatro existentes:

```python
objetivos_asignacion = {
    id(nodo.slice)
    for nodo in ast.walk(arbol)
    if isinstance(nodo, ast.Subscript)
    and isinstance(nodo.ctx, (ast.Store, ast.Del))
}
patron_nombre_inventado = re.compile(r"[A-Z][A-Z0-9_]*")
for nodo in ast.walk(arbol):
    if not isinstance(nodo, ast.Constant):
        continue
    if not isinstance(nodo.value, str):
        continue
    if "_" not in nodo.value:
        continue
    if not patron_nombre_inventado.fullmatch(nodo.value.strip("_")):
        continue
    if id(nodo) in objetivos_asignacion:
        continue
    errores.append(
        f"BLOQUEANTE: {nombre}: placeholder literal '{nodo.value}' "
        f"sin sustituir (no existe en el sandbox)"
    )
```

**Por qué esto no es hardcodeo:**

- No hay lista de nombres. El criterio es la forma del nombre inventado
  (mayúsculas con guiones bajos), no `__FARSI_JSON__` ni ningún otro valor.
- No se exige el envoltorio `__...__`: se acepta cualquier nombre de esa
  forma, con o sin él. `FARSI_JSON`, `__UCRAINIAN_JSON__` y
  `__CALCULADORA_JS__` caen por la misma regla.
- Se exige al menos un guion bajo para no marcar constantes de una sola
  palabra legítimas en código generado (`"GET"`, `"POST"`, `"CSV"`,
  `"OK"`, `"PATH"`).
- Los dunders legítimos (`__name__`, `__file__`, `__main__`, `__all__`)
  van en minúsculas y no casan, así que no hacen falta listas de
  excepciones.

**Matriz de decisión** (verificada empíricamente):

| Se marca (1 BLOQUEANTE) | No se marca (0) |
|---|---|
| `__FARSI_JSON__`, `FARSI_JSON`, `__FARSI__`, `_NOTICIAS_ES` | `GET`, `POST`, `CSV`, `OK`, `PATH`, `JSON` |
| `__UCRAINIAN_JSON__`, `__NEWS_HTML__` | `UTF-8`, `application/json`, `Hola mundo` |
| `__CUENTO_DRAGON__`, `__CALCULADORA_JS__` | `__key__`, `__main__`, `__all__`, `__file__` |
| `API_KEY`, `HTTP_PROXY` *(coste conocido, §5.2)* | `contexto["__X__"] = ...` (LHS) |

## 4. Tests añadidos

`tests/test_validador.py`, función parametrizada
`test_n2_nombre_inventado_usado_como_literal` (commit rojo `54c7c5a`, que
falló contra el validador original). 20 casos:

- **Los 4 del bug**: `json.loads("__FARSI_JSON__")`, con comilla simple,
  asignación directa y triple comilla.
- **Generalidad (6)**: `__UCRAINIAN_JSON__`, `__NEWS_HTML__`,
  `__CUENTO_DRAGON__`, `__CALCULADORA_JS__`, `FARSI_JSON` (sin envoltura)
  y `_NOTICIAS_ES` (un solo guion). Demuestran que `__FARSI_JSON__` no es
  un caso especial.
- **Regresión (1)**: `json.loads("""{farsi_json}""")`.
- **Falsos positivos (8)**: `__name__`, `f-string` con `__file__`,
  `"__main__"`, `"__key__"`, `"GET"`, `"CSV"`, cabecera HTTP normal,
  `"UTF-8"`.
- **LHS (1)**: `contexto["__FARSI_JSON__"] = 1` → 0.

Total del archivo: 9 → **29 tests**. Suite completa: **684 → 704 passed**
(+20), sin regresiones.

## 5. Decisiones de diseño

### 5.1 Se generaliza el criterio: forma del nombre, no el ejemplo

El primer intento de esta release exigía el envoltorio `__...__`
(`__[A-Z][A-Z0-9_]*__`). Se descarta por sobreajuste al ejemplo: el bug no
es «placeholder con doble underscore», es «nombre inventado usado como
literal». Cualquier nombre en mayúsculas con guiones bajos cuenta.

### 5.2 Se exige al menos un guion bajo (coste conocido)

Sin ese requisito, la regla marcaría constantes de una sola palabra
legítimas y muy frecuentes en código generado: `"GET"`, `"POST"`,
`"CSV"`, `"OK"`, `"PATH"`, `"JSON"`. Inventario real en el repo: `"GET"`
(12 apariciones), `"POST"` (7), `"PUT"`, `"PATCH"`, `"OPTIONS"`, `"HEAD"`,
`"DELETE"`, `"CSV"`, `"OK"`, `"URL"`, `"PATH"`.

**Coste que queda**: un literal con forma de variable de entorno
(`"API_KEY"`, `"DEEPSEEK_API_KEY"`, `"HTTP_PROXY"`, `"NO_PROXY"`) sí casa
con la forma y se marcaría. En la práctica esos nombres aparecen en código
de infraestructura (configuración y `env_checker`), no en el código de
pasos que valida `ProblemSolver`; el sandbox solo expone `contexto`. Se
asume y se documenta. Si se quisiera refinar, el sitio es v3.3.

### 5.3 El LHS de una asignación no se marca

`contexto["__X__"] = 1` (y `+=`, `AnnAssign`, `del`) **no** bloquea: ahí
la constante es el nombre de una clave elegida por el código, no un
placeholder que se consuma. Se excluyen los `ast.Constant` que son el
`slice` de un `ast.Subscript` con `ctx` `Store`/`Del`. Un literal de
cadena nunca puede ser LHS directo de una asignación.

### 5.4 `_nombres_ligados` no aplica a constantes

El encargo sugiere usarlo «si aplica». No aplica: `_nombres_ligados`
devuelve identificadores ligados (`ast.Name` en `Store`, argumentos,
imports…), no literales de cadena. Excluir además los literales cuyo texto
coincida con un nombre ligado abriría un falso negativo
(`__X__ = 1` seguido de `json.loads("__X__")` seguiría siendo código roto).

### 5.5 El regex del mensaje de commit original

El encargo proponía en el mensaje de commit `/^[A-Z][A-Z0-9_]*$/`. Se
interpretó al principio como un error y luego se vio que apuntaba a la
dirección correcta (forma genérica en mayúsculas). La regla final equivale
a ese patrón aplicado al valor **sin** los guiones bajos de los extremos
(`value.strip("_")`), más el requisito de contener al menos un guion bajo.

## 6. Verificaciones

### 6.1 Suite completa — `python run_all_tests.py` → exit 0

```
✅ TODAS LAS PRUEBAS PASARON
🔒 BD de producción intacta (agent_history.db)
```

- `100 passed, 1 skipped` (A_ui_datos) · `70 passed` (B_scheduler) ·
  `18 passed` (C_integration) · `62 passed` (D_sandbox) ·
  `45 passed` (E_loops) · `409 passed, 5 deselected` (F_ligeros)
- **Total: 704 passed, 1 skipped, 0 failed**, exit 0.
- Base v3.2.0: 684 → **+20** tests nuevos. Sin regresiones.

### 6.2 Integridad de la BD

`sha256sum agent_history.db` antes y después de todo el trabajo:

```
d6b7d586062e01cefc2e069a034cdce79491f5f975dc45d5407f6e881f0cfdd3  agent_history.db
```

Idéntico ✅. El smoke test E2E propio (§6.4) se ejecutó además sobre una
**copia** de la BD en `tmp/smoke_v3.2.1/`, para no tocar la de producción.

### 6.3 `python main.py --check-env` → exit 0

```
✅ API key encontrada: sk-54c46…b6dc   (Origen: archivo)
✅ Respuesta 200 en 5450 ms — https://api.deepseek.com/v1/models
RESULTADO: ✅ Todo OK
```

### 6.4 Smoke test E2E con `ProblemSolver` → exit 0

```
Plan: Generar archivo saludo.txt con Hola Mundo | Pasos: 2 | Agentes: 2
  1. [Python] CrearContenidoSaludo
  2. [File] EscribirArchivoSaludo
```

2 pasos (≥ 2) y 2 agentes generados; sin tocar `agent_history.db`.

### 6.5 Lint

`ruff check` + `pyflakes` sobre los dos archivos tocados: limpio
(`All checks passed!`, exit 0). `pre_tag_check.sh` repite el lint acotado a
todo el código del proyecto: también limpio.

### 6.6 `./tools/pre_tag_check.sh` → exit 0

Ejecutado sobre el árbol limpio (los 3 commits hechos):

```
1. Working tree limpio                     ✅
6. Todos los .py compilan                  ✅
7. ruff limpio                             ✅
10. CHANGELOG.md menciona v3.2.1           ✅
11. Smoke test de ProblemSolver            ✅ (plan con 2 pasos)
12. Suite de tests completa                ✅ (704 passed, 1 skipped)
════════════════════════════════════════════
✅ LISTO PARA TAGGEAR (revisa los ⚠️  manualmente)
```

Avisos ⚠️ no bloqueantes, todos preexistentes y ajenos a este hotfix:
rama distinta de `main` (esperado: rama de fix), tags `v3.x` previos
(esperado), «sin upstream» (el repo no tiene remoto) y `print()` de los
módulos de demo/CLI (`core/llm_client.py`, `core/env_checker.py`) y del
protocolo interno del sandbox (`__RESULT__`/`__ERROR__`). Ninguno toca
`validator.py`. Hash de la BD verificado también después de esta puerta:
sin cambios.

## 7. Hallazgos NO arreglados (fuera del alcance de N2)

1. **`json.loads("{x}")` con comilla simple no se detecta.** La regla 3 usa
   `(['\"]{2,3})`, es decir, exige 2-3 comillas: solo cubre la comilla
   triple. Evidencia:
   - `json.loads("{x}")` → **0 errores** (hueco)
   - `json.loads("""{x}""")` → 1 error ✅
2. **Las reglas 3 y 4 son regex sobre el texto fuente, no sobre el AST**, y
   también marcan dentro de comentarios o docstrings. Evidencia:
   `# json.loads("""{x}""")` → 1 error (falso positivo). La regla 5 no lo
   sufre: trabaja sobre `ast.Constant`.
3. **Un nombre inventado en mayúsculas usado como *variable***
   (`__FARSI_JSON__ = 1`) no se detecta: la regla 5 solo mira literales.

## 8. Discrepancias del encargo

| Encargo | Realidad |
|---|---|
| `PlanValidator.validarcodigopythonast` | `PlanValidator._validar_codigo_python_ast` |
| `python runalltests.py` | `python run_all_tests.py` |
| `HARNESSREPORTv3.2.1.md` | `HARNESS_REPORT_v3.2.1.md` (convención de v3.0.1/v3.1.0/v3.2.0) |
| «684 tests» | 684 en la base → 704 con los 20 nuevos |
| `json.loads("FARSI_JSON")` (snippet) | literal real: `__FARSI_JSON__` |
| Límite «solo `validator.py` y tests» vs. Fase 4 | La Fase 4 (versión, CHANGELOG e informe) es instrucción explícita y criterio de éxito; se ejecutó. El **código** de producción tocado se limita a `validator.py`. |
| Regla `__XXX__` del primer intento | Generalizada a «nombre inventado» a petición del usuario: `__FARSI_JSON__` no es especial |

## 9. Recomendaciones v3.3

1. **Cerrar el hueco de la comilla simple** en la regla 3
   (`json.loads("{x}")`), hoy no detectado.
2. **Reescribir las reglas 3 y 4 sobre el AST** en vez de sobre el texto,
   para eliminar los falsos positivos en comentarios/docstrings.
3. **Sustituir la detección por forma por una semántica** donde se pueda:
   un literal pasado a `json.loads` que no parsea como JSON **siempre**
   revienta en el sandbox, así que se puede marcar sin mirar el nombre. Eso
   cubriría cualquier placeholder inventado (no solo mayúsculas con
   guiones bajos) con cero falsos positivos, y haría innecesario el
   requisito del guion bajo. Requiere deduplicar con la regla 3.
4. **Decidir sobre el coste conocido de `"API_KEY"`/`"HTTP_PROXY"`**
   (§5.2) si aparecen en código generado real.
5. Mantener el patrón «test rojo → fix verde → release»: el rojo
   (`54c7c5a`) reprodujo los casos con varios nombres inventados.

## Anexo — Reproducibilidad

```bash
source .venv/bin/activate
python -m pytest tests/test_validador.py -q     # 29 passed
python run_all_tests.py                          # 704 passed, exit 0
sha256sum agent_history.db                       # d6b7d586…cfdd3
./tools/pre_tag_check.sh                         # exit 0
python main.py --check-env                       # exit 0
ruff check core/problem_solver/validator.py tests/test_validador.py
python -m pyflakes core/problem_solver/validator.py tests/test_validador.py
```
