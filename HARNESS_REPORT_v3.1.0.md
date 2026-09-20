# HARNESS REPORT — agentes_visuales v3.1.0

**Fecha:** 2026-09-20
**Workspace:** `/home/christian/proyectosPython/agentes_visuales`
**Rama:** `harness/fix-v3.1` · **Último commit de código:** `a764c9a` · **Tag de partida:** `v3.0.1` (`b86b4f3`)
**Entorno:** Python 3.13.5 (`.venv`), pytest 9.1.1 + pytest-forked, ruff 0.16.8, pyflakes 3.4.0

---

## Resumen ejecutivo

| Verificación | Resultado |
|---|---|
| `python run_all_tests.py` | ✅ **6/6 grupos OK** — 668 passed, 1 skipped, 5 deselected |
| `sha256(agent_history.db)` vs inicio | ✅ **idéntico** (`d6b7d586…`) |
| `./tools/pre_tag_check.sh` | ✅ **exit 0** (incluye suite completa) |
| `python main.py --check-env` | ✅ **exit 0** |
| Smoke test E2E (`ProblemSolver`) | ✅ **2 pasos / 2 agentes** |
| `ruff check` + `pyflakes` | ✅ sin hallazgos |
| Árbol de trabajo | ✅ limpio |

Se corrigieron **7 hallazgos de la auditoría previa (B1–B7)**, **6 bugs nuevos
(X1–X6)** encontrados al restaurar la suite y auditar el código, y **4 bugs más
(H1, H3, H5, H6)** de una segunda pasada delegada sobre `scheduler.py`,
`event_bus.py` y `database.py`. Quedan **2 hallazgos de diseño (H2, H4)**
documentados y **no arreglados** de forma deliberada (§2.4 y §6). Se descartaron
explícitamente 4 falsos positivos (ver §5).

---

## 1. Estado de partida (verificado, no asumido)

| Afirmación de la auditoría v3.0.1 | Verificación |
|---|---|
| B1 `run_all_tests.py:153` referencia un test borrado | ✅ confirmado |
| B2 `tests/conftest.py` reducido a 6 líneas | ⚠️ **ya estaba restaurado** por `472b2d5` (previo a esta sesión); verificado con 120 líneas y 4 fixtures |
| B3 `tests/test_ab_sintetico.py` escribe en la BD real | ✅ confirmado (ruta hardcodeada + `DELETE`/`INSERT`) |
| B4 `pre_tag_check.sh` corre `pyflakes .` sin acotar | ✅ confirmado |
| B5 versión inconsistente | ✅ confirmado (`main.py` → `1.1.0`, tag `v3.0.1`) |
| B6 `.bak` trackeados, `.gitignore` duplicado, `./~/` | ✅ confirmado (16 backups trackeados, bloque duplicado, `./~/reporte_actualizacion.txt`) |
| B7 7 tests sin clasificar | ✅ confirmado (`Z_sin_clasificar`) |
| `agregar_callback` no comprueba token ya cancelado | ✅ confirmado (y con impacto real en `shell_executor`) |
| `_put_in_cache` no comprueba clave existente | ✅ confirmado |
| ~24 `raise` sin `from e` | ✅ confirmado (24 exactos, ruff B904) |
| `import re` local que sombrea el de cabecera | ✅ confirmado (F401 + F811) |

**Nota importante sobre `/tmp/db_antes_fix.txt`:** el fichero **no existía** al empezar
(se indica en el encargo que ya estaba). Además, `/tmp` es efímero en este entorno
(se vacía entre comandos). Por eso la huella de referencia se guardó en el propio
workspace (`tmp/harness_v3.1/db_baseline.txt`) y la comparación final se hizo
recreando `/tmp/db_antes_fix.txt` y ejecutando `sha256sum -c` en el mismo comando.

**Estado de `agent_history.db`:** la huella de referencia de esta sesión es
`d6b7d586…`. La auditoría anterior ya la había modificado (`6962f1c1…` →
`d6b7d586…`) al ejecutar la suite rota. Se comprobó en solo-lectura que **no queda
residuo lógico** (`prompts_reescritos` con `razon='sintético'` → 0 filas,
`PRAGMA integrity_check` → `ok`) y que el único backup disponible
(`agent_history.db.before_harness_20260920_094430`) **también** tiene la huella
post-contaminación, por lo que no había una copia limpia a la que restaurar. **No
se modificó la BD en ningún momento de esta sesión.**

---

## 2. Bugs encontrados y estado

### 2.1 De la lista de pistas (B1–B7)

| ID | Severidad | Archivo | Estado | Commit |
|---|---|---|---|---|
| B1 | Crítico | `run_all_tests.py:153` | ✅ Arreglado | `a664335` |
| B2 | Crítico | `tests/conftest.py` | ✅ Ya arreglado antes (`472b2d5`); verificado | — |
| B3 | Crítico | `tests/test_ab_sintetico.py:34` | ✅ Arreglado | `311fcdc` |
| B4 | Menor | `tools/pre_tag_check.sh:91` | ✅ Arreglado | `629a1ba` |
| B5 | Menor | `main.py:17` | ✅ Arreglado | `ad283eb` |
| B6 | Menor | `.gitignore`, `.bak`, `./~/` | ✅ Arreglado | `2029eab` |
| B7 | Menor | `run_all_tests.py` (`GRUPOS_TESTS`) | ✅ Arreglado | `8e5c014` |

### 2.2 Bugs nuevos encontrados (X1–X6)

| ID | Severidad | Archivo | Estado | Commit |
|---|---|---|---|---|
| X1 | Alto | `tests/test_ab_sintetico.py` (4 tests) | ✅ Arreglado | `311fcdc` |
| X2 | Alto | `core/executors/file_executor.py:297` | ✅ Arreglado | `5567f79` |
| X3 | Alto | `core/cancellation.py:94` | ✅ Arreglado | `053734f` |
| X4 | Medio | `core/executors/http_executor.py:293` | ✅ Arreglado | `0d54122` |
| X5 | Medio | `storage/config_manager.py:454` | ✅ Arreglado | `e18dc69` |
| X6 | Bajo | 5 archivos + `pyproject.toml` | ✅ Arreglado | `cb3e815`, `e84ec4b` |

**X1 — Los tests de A/B sintético no podían fallar.**
Los 4 tests devolvían `bool` en lugar de usar `assert`. En pytest 9 eso solo
emite `PytestReturnNotNoneWarning` y el test se marca **PASSED** aunque la lógica
falle (verificado empíricamente). Es decir: 4 tests con cobertura aparente que no
detectaban nada. Se convirtieron a `assert` reales y, además, se añadió
`error::pytest.PytestReturnNotNoneWarning` a `pytest.ini` para que cualquier test
futuro con ese patrón falle en lugar de pasar en silencio (`e1aec63`). Un barrido
AST de toda la suite confirma que no quedan más casos.

**X2 — Escribir un `dict` en `.txt`/`.log` perdía el resultado (regresión).**
Desde `903f94a`, para las extensiones de `EXTENSIONES_TEXTO_PLANO` el executor
intentaba desenvolver el dict buscando una clave conocida y, si no la encontraba,
**fallaba el agente** con `error='no_known_text_keys'`. Antes de ese commit los
dicts se serializaban a JSON para cualquier extensión. Esto rompía la cadena
natural HTTP → Python (devuelve dict) → File `.txt` y bloqueaba a los
dependientes. Estaba oculto porque `tests/conftest.py` fue vaciado en `a988b46`
justo antes, así que `test_integration.py` ni se ejecutaba.
Arreglo: `EXTENSIONES_FALLBACK_JSON = {".txt", ".log"}` — si no hay texto útil y
la extensión es de texto libre, se vuelca el JSON con un *warning*. En formatos
con estructura (html/xml/css/js/yaml/toml/ini/csv) se conserva el error explícito.

**X3 — `agregar_callback` no dispara el callback si el token ya está cancelado.**
Ventana de carrera real: `core/executors/shell_executor.py` registra el callback
**fuera** de su `try` y **no sondea el token**; depende por completo de él para
interrumpir `proceso.communicate()`. Un comando cancelado en esa ventana seguía
corriendo hasta agotar el timeout (verificado: el test nuevo falla con
`Timeout (30s)` sin el arreglo). Arreglo: si el token ya está `CANCELLED`, el
callback se ejecuta de inmediato fuera del lock; si está `COMPLETED`, no se
registra (nunca habrá cancelación).

**X4 — La sesión HTTP no se cerraba en la ruta de éxito (fuga de fds).**
`HTTPExecutor.ejecutar` creaba un `requests.Session` por petición y solo lo
cerraba en cancelación/timeout. En éxito y al propagarse una excepción de red
(ej. `ConnectionError`) la sesión quedaba viva, con su pool de conexiones, hasta
que el GC la recogía. Arreglo: cierre en el `finally` principal (la respuesta ya
viene descargada en memoria, no se usa `stream=True`).

**X5 — La caché LRU expulsaba una entrada viva al actualizar otra.**
`_put_in_cache` comprobaba `len(cache) >= max_cache` **antes** de mirar si la
clave ya existía. Con la caché llena, actualizar una entrada expulsaba al LRU sin
necesidad y la caché encogía. Arreglo: solo se expulsa si la clave es nueva.

**X6 — Lint sucio y encadenamiento de excepciones.**
* 24 `raise` dentro de `except ... as e` sin `from e`: perdían la causa original en
  el traceback. Detectados con ruff **B904** (que el proyecto tenía desactivado) y
  corregidos; se retiró `B904` de los `ignore` para que no vuelva a pasar.
* `import re` local en `_crear_plan_fallback` que sombreaba el de cabecera (F401 +
  F811).
* Imports sin usar, expresión suelta, orden de imports y falta de newline final en
  16 sitios. Ahora **ruff y pyflakes salen limpios**.

### 2.3 Segunda pasada: auditoría profunda de scheduler / event_bus / database (H1–H6)

Se delegó una auditoría estática adicional (solo lectura) sobre
`core/scheduler.py`, `core/event_bus.py` y `storage/database.py`. Sus hallazgos
se verificaron leyendo el flujo exacto **antes** de tocar nada; los 4 que
resultaron reales y con arreglo contenido se corrigieron con test que falla sin
el arreglo.

| ID | Severidad | Archivo | Estado | Commit |
|---|---|---|---|---|
| H1a | Alto | `core/scheduler.py:743` | ✅ Arreglado | `d6fcb4e` |
| H1b | Alto | `core/scheduler.py:611` | ✅ Arreglado | `d6fcb4e` |
| H3 | Alto | `storage/database.py:1901` | ✅ Arreglado | `a764c9a` |
| H5 | Medio | `core/scheduler.py:1223` | ✅ Arreglado | `d6fcb4e` |
| H6 | Bajo | `core/event_bus.py:414` | ✅ Arreglado | `af4da4a` |
| H2 | Alto | `core/scheduler.py:588` | ⚠️ **No arreglado** (requiere decisión) | — |
| H4 | Medio | `storage/database.py:296` | ⚠️ **No arreglado** (requiere decisión) | — |

**H1a — El worker saliente borraba el token de un reintento posterior.**
`_ejecutar_agente` hacía `self._tokens_activos.pop(agente.id, None)` en su
`finally` sin comprobar que el token fuera el suyo. La ruta de reintento lanza
un segundo worker para el mismo agente **antes** de que el primero llegue a su
`finally`, así que el saliente podía borrar el token del nuevo: ese worker
quedaba fuera del mapa de cancelación y `detener()` ya no podía pararlo (seguía
ejecutando shell/HTTP/escritura de ficheros tras "Detener"). Arreglo: borrar
solo si `self._tokens_activos.get(agente.id) is token`.

**H1b — `UnboundLocalError` silencioso en FASE 5.**
`razon` solo se asignaba dentro de `if token.esta_cancelado()`. Si el agente
estaba en `_cancelados` sin que su token estuviera cancelado (justo la carrera
de H1a), FASE 5 usaba `razon` sin asignar → `UnboundLocalError` dentro del hilo
worker. Como nadie consulta el `Future` del `ThreadPoolExecutor`, el fallo era
**totalmente mudo** y además saltaba el cierre de FASE 5/6/7. El test reproduce
el error literal. Arreglo: inicializar `razon` desde la metadata del token.

**H3 — `restore_backup` reaplicaba el WAL de la base anterior (integridad de datos).**
`restore_backup` cerraba solo la conexión del hilo llamante y copiaba el fichero
con `shutil.copy2`. Si quedaba un `-wal` con frames sin volcar (p. ej. otra
conexión con una lectura abierta impide el checkpoint del cierre), ese `-wal`
sobrevivía a la copia y SQLite reaplicaba sus frames sobre la BD restaurada al
reabrir: **reaparecían filas borradas justo antes de restaurar** o la BD quedaba
inconsistente. Arreglo: restaurar con la API de backup de SQLite
(`origen.backup(destino)`), igual que ya hacía `backup()`. El test reproduce la
resurrección de la fila borrada.

**H5 — El reintento despachado por señal encolada nunca se ejecutaba.**
`_on_reintentar_agente` hacía `submit` sin pasar el agente a `LISTO` ni sumarlo
a `running`. Si el lanzamiento inmediato no había podido ejecutarse (scheduler
pausado o pool saturado), el agente seguía en `EN_COLA` y `_ejecutar_agente`
abortaba con "No se puede ejecutar" porque `EN_COLA → EJECUTANDO` **no es una
arista válida** de la máquina de estados. El reintento se perdía pese a haber
consumido el contador, y al no contar en `running` podía superar
`max_concurrent`. Arreglo: pasar a `LISTO`, sumar a `running` y descartar el
duplicado si el agente ya está en `running`.

**H6 — `obtener_historial(0)` devolvía todo el historial.**
`-0 == 0`, así que `self._historial[-0:]` es `[0:]` = la lista completa. Con
`limit` negativo devolvía la cola a partir de `abs(limit)`. Arreglo:
`limit <= 0` → `[]`, y semántica explícita de `_max_historial <= 0` = sin límite.

**H2 (NO arreglado) — El Plan B ejecuta la llamada al LLM con el RLock del
scheduler tomado.** Verificado: FASE 5 llama a `_bloquear_dependientes`
(`core/scheduler.py:633/677/728`) dentro de `with self._lock:` (línea 588) y este
llama a `_intentar_plan_b`, que hace `llm_client.chat` (red, ~15-20 s) sin
soltar el lock. Consecuencia: la UI se congela (`obtener_estadisticas()` bloquea
en el mismo lock) y los demás workers se quedan parados al llegar a FASE 5.
**No lo he arreglado**: sacar la llamada de red fuera del crítico obliga a
reestructurar el bloque de finalización del scheduler, que es exactamente el
tipo de cambio de arquitectura que el encargo pide consultar antes de hacer.
Ver §6 y §7.

**H4 (NO arreglado) — Lecturas con `BEGIN IMMEDIATE` y errores silenciados.**
`_transaction` usa siempre `BEGIN IMMEDIATE` (lock de escritura), también para
los SELECT de `obtener_historial`, `obtener_ejecucion`, `diagnostico`, etc., y
varios de esos métodos capturan `sqlite3.Error` y devuelven `[]`/`{}` sin
registrar nada, de modo que un "database is locked" se ve en la UI como
"historial vacío". **No lo he arreglado**: separar transacciones de lectura y
escritura toca la capa de persistencia completa y su modelo de concurrencia;
prefiero acordarlo antes que cambiarlo en un release de estabilización.
Ver §6 y §7.

### 2.4 Mejora de proceso (sin bug asociado)

* **Guardarraíl de datos en el runner** (`a664335`): `run_all_tests.py` hashea
  `agent_history.db` antes y después de cada grupo y marca la ejecución como
  fallida si cambia. Es lo que habría detectado B3 automáticamente.
* **Detección de "0 tests recolectados"** (`rc=5`) como fallo explícito, en vez de
  un `rc != 0` genérico.

---

## 3. Lista de commits (17 desde `v3.0.1`)

| Hash | Resumen |
|---|---|
| `472b2d5` | fix(tests): restaurar `conftest.py` desde `4112a0e` *(previo a esta sesión)* |
| `311fcdc` | fix(tests): aislar `test_ab_sintetico` de la BD de producción (B3, X1) |
| `5567f79` | fix(file_executor): no perder el resultado al escribir dicts en `.txt`/`.log` (X2) |
| `a664335` | fix(runner): validar `GRUPOS_TESTS`, detectar 0 tests y proteger `agent_history.db` (B1) |
| `053734f` | fix(cancellation): disparar callbacks sobre un token ya cancelado (X3) |
| `e18dc69` | fix(config_manager): no expulsar el LRU al actualizar una entrada existente (X5) |
| `cb3e815` | fix(errores): encadenar la causa original en los 24 `raise` dentro de `except` (X6) |
| `e84ec4b` | chore(lint): dejar ruff y pyflakes limpios en todo el proyecto (X6) |
| `8e5c014` | chore(tests): clasificar los 7 tests de `Z_sin_clasificar` (B7) |
| `2029eab` | chore(repo): higiene de `.gitignore`, desversionar backups y quitar `./~` (B6) |
| `ad283eb` | fix(version): unificar la versión en `pyproject.toml` (B5) |
| `629a1ba` | fix(tools): acotar el lint y convertir el `pre_tag_check` en puerta real (B4) |
| `0d54122` | fix(http_executor): cerrar la sesión HTTP en todas las rutas (X4) |
| `e1aec63` | test(pytest): tratar `Return-not-None` como fallo (endurecer X1) |
| `d6fcb4e` | fix(scheduler): no perder el token del reintento ni el reintento encolado (H1, H5) |
| `af4da4a` | fix(event_bus): `obtener_historial(limit<=0)` ya no devuelve todo (H6) |
| `a764c9a` | fix(database): `restore_backup` deja de reaplicar el WAL anterior (H3) |

*(16 commits de esta sesión + 1 preexistente. `54 files changed,
~1600 insertions(+), ~13000 deletions(-)` respecto a `v3.0.1`; los borrados
corresponden a los backups y duplicados desversionados.)*

---

## 4. Verificaciones finales (evidencia)

### 4.1 Suite completa — `python run_all_tests.py` → exit 0

```
📋 Resumen por grupo:
   ✅ OK  A_ui_datos              3.6s    115 líneas    100 passed, 1 skipped
   ✅ OK  B_scheduler            29.2s    103 líneas     67 passed
   ✅ OK  C_integration          27.0s     69 líneas     18 passed
   ✅ OK  D_sandbox              11.7s    132 líneas     62 passed
   ✅ OK  E_loops                13.7s    107 líneas     45 passed
   ✅ OK  F_ligeros               6.0s    489 líneas    383 passed, 5 deselected
🔒 BD de producción intacta (agent_history.db)
✅ TODAS LAS PRUEBAS PASARON
```

Total: **675 tests passed, 1 skipped** (frente a 668 antes de los arreglos de la
segunda pasada). Comparativa con el punto de partida (auditoría v3.0.1):
**19 errores de setup + 1 grupo con 0 tests** → **0 fallos**. `Z_sin_clasificar`
desaparece al quedar sus 7 tests clasificados.

### 4.2 Integridad de la BD

```
$ sha256sum -c /tmp/db_antes_fix.txt
agent_history.db: La suma coincide        # d6b7d586… == d6b7d586…
```

### 4.3 `./tools/pre_tag_check.sh` → exit 0

```
✅ Working tree limpio
✅ ruff limpio
✅ CHANGELOG.md menciona v3.1.0
✅ Smoke test completado (plan con >= 2 pasos)
✅ Suite de tests: todos los grupos OK
✅ LISTO PARA TAGGEAR
```

### 4.4 `python main.py --check-env` → exit 0

```
✅ API key encontrada: sk-54c46…b6dc
✅ Respuesta 200 en 5485 ms
RESULTADO: ✅ Todo OK
```

### 4.5 Smoke test E2E (`ProblemSolver`)

```
Titulo del plan : Generar archivo saludo.txt con Hola Mundo
Pasos           : 2
Agentes         : 2
  1. [Python] GenerarContenido
  2. [File] EscribirArchivo
SMOKE_E2E_OK
```

### 4.6 Lint

```
$ ruff check core learning ui storage export tools main.py run_all_tests.py tests
All checks passed!
$ pyflakes core learning ui storage export tools main.py run_all_tests.py tests
(0 hallazgos)
```

---

## 5. Falsos positivos descartados (con justificación)

1. **`http_executor.py:175` "redefinition of `cancelar_peticion`"** (pyflakes):
   **no es un bug**. El `cancelar_peticion = None` de la línea 160 es necesario para
   que el `finally` pueda desregistrar el callback incluso si el fallo ocurre antes
   de definir la función; sin él habría `UnboundLocalError` enmascarando la
   excepción original. Se renombró la función anidada a `_cancelar_peticion` como
   limpieza, manteniendo el comportamiento (verificado).
2. **`nonlocal proceso` / `nonlocal session` "unused"** (`sandbox.py:880`,
   `shell_executor.py:147`, `http_executor.py:176`): **no son bugs**. Las variables
   solo se leen desde el closure; el `nonlocal` es innecesario pero inocuo. Se
   eliminaron como limpieza.
3. **Ficheros temporales del sandbox sin cerrar** (sospecha inicial): **no es un
   bug**. `PythonSandbox.ejecutar` ya los cierra en el `finally` interno del bloque
   principal. Se descartó el cambio de código; solo se añadió una prueba de
   invariante que mantiene la garantía bajo vigilancia.
4. **`--check-env` declara timeout 5 s y reporta latencias mayores**: la
   observación original (15 713 ms) **no se reprodujo** (5 485 ms en la medición
   final). `requests` aplica el timeout por fase (conexión y lectura), no al total,
   y la resolución DNS no está cubierta de forma estricta; con DNS lento el total
   puede superar el valor declarado sin que el código esté mal. **No se tocó**;
   ver recomendaciones.

---

## 6. Cosas que decidí NO tocar (y por qué)

* **Restaurar `agent_history.db`**: no hay copia limpia (el único backup ya está
  contaminado) y no quedan filas sintéticas; restaurar habría sido inventar datos.
  Se documenta en §1.
* **`sqlite3` (binario de 204 KB en la raíz)**: está *ignorado* y no trackeado; no
  ensucia el repo. Borrarlo es decisión del usuario.
* **Directorio `.env/`** (virtualenv viejo): el encargo pide ignorarlo. Sigue en
  disco, excluido de git y del lint.
* **Backups `.backups_fix_*`, `*.bak`**: se **desversionaron** pero se conservan en
  disco (son copias del usuario). Borrarlos no me corresponde.
* **Los ⚠️ de la sección 8 de `pre_tag_check.sh`** (prints en
  `llm_client.run_prueba`, `sandbox.__main__`, `env_checker`): son salida legítima
  de CLIs y de un protocolo (`__RESULT__`), no debug olvidado. Se dejaron como
  advertencia manual (ahora se filtra `run_all_tests.py`, que sí era ruido).
* **Aislamiento del test de integración respecto al cwd**: `test_flujo_completo_con_http_mock`
  escribe un `.txt` relativo, pero se verificó que **no deja artefactos** en el
  árbol (probablemente por la resolución de variables del executor). No se cambió.
* **Rediseños grandes**: no fue necesario ninguno. Los arreglos son locales; no se
  tocó la arquitectura del scheduler, el sandbox ni el sistema de aprendizaje.
* **H2 — llamada al LLM de Plan B con el lock del scheduler tomado** (§2.3):
  requiere reestructurar el bloque de finalización (FASE 5) y el alcance del
  `RLock` del scheduler. Es un cambio de concurrencia en el núcleo, con riesgo de
  regresión y sin cobertura del comportamiento de bloqueo; prefiero acordarlo
  contigo antes de hacerlo (Regla 7).
* **H4 — lecturas con `BEGIN IMMEDIATE` y `sqlite3.Error` silenciado** (§2.3):
  separar transacciones de lectura/escritura toca `_transaction` y los ~8 métodos
  de lectura, y cambiar el silenciamiento puede alterar contratos existentes
  (`[]` vs excepción) que la UI ya asume. Igualmente, lo dejo para v3.2 con tu OK.

---

## 7. Recomendaciones para v3.2

1. **CI/pre-commit**: ejecutar `ruff check` + `run_all_tests.py` en cada push. Hoy
   todo depende de que alguien se acuerde de correr `pre_tag_check.sh`.
2. **Guardarraíl en `conftest.py`**: además del hash en el runner, envolver
   `sqlite3.connect` durante los tests para fallar si la ruta apunta a
   `agent_history.db`. Cierra la clase de bug B3/X3 de raíz.
3. **Auditar el resto de tests**: buscar más rutas hardcodeadas y escrituras en el
   cwd; migrarlas sistemáticamente a `tmp_path`.
4. **Cobertura**: el proyecto ya genera `.coverage`; fijar un umbral mínimo en CI
   evitaría que un grupo entero deje de recolectarse sin que nadie lo note (el
   guardarraíl de `rc=5` ayuda, pero no detecta tests que no existen).
5. **`requests` y timeouts**: documentar que el timeout es por fase; si se quiere un
   límite total, usar un `deadline` explícito o `httpx` con `timeout` global.
6. **Grupos de tests**: `F_ligeros` ya tiene 26 archivos. Si sigue creciendo,
   separar un grupo de *executors* ligeros.
7. **Empaquetado**: `pyproject.toml` ya tiene `[project]`; considerar `pip install -e .`
   y consumir la versión con `importlib.metadata` en lugar de leer el TOML.
8. **Limpiar la raíz**: mover outputs de ejemplo (`*.html`, `*.docx`, `calculadora*`,
   etc.) a `examples/` o borrarlos; hoy están ignorados por git pero ensucian el
   directorio.
9. **[H2] Sacar el Plan B (y en general cualquier I/O de red) de dentro del lock
   del scheduler.** Recolectar el estado bajo lock y ejecutar
   `recovery.generar_plan_b` fuera, re-adquiriendo el lock solo para mutar
   estado/plan. Añadir un test de no-bloqueo (con un `generar_plan_b` que duerme)
   antes de tocar nada.
10. **[H4] Separar transacciones de lectura y escritura en `storage/database.py`**
    (`BEGIN DEFERRED` para SELECT) y dejar de devolver `[]`/`{}` sin registrar el
    error; distinguir "vacío" de "error" en la UI.
11. **Guardarraíl de datos en `restore_backup`**: comprobar explícitamente la
    ausencia de `-wal`/`-shm` (o usar siempre la API SQLite, ya aplicado) y añadir
    un test con una segunda conexión, que es el escenario que falló.

---

## Anexo — Reproducibilidad

Logs de esta sesión en `tmp/harness_v3.1/` (ignorado por git):

| Archivo | Contenido |
|---|---|
| `db_baseline.txt` | sha256 de `agent_history.db` al inicio (`d6b7d586…`) |
| `db_pre_final.txt` | sha256 antes de la verificación final |
| `tests_run_1.log` | Suite de diagnóstico (solo fallaba `C_integration`) |
| `tests_run_2.log` | Suite tras el arreglo de `file_executor` |
| `tests_run_final.log` | Suite final (6/6 grupos OK) |
| `tests_run_final2.log` | Suite final tras endurecer `pytest.ini` (6/6 grupos OK) |
| `tests_run_audit.log` | Suite tras los arreglos H1/H3/H5/H6 (6/6 grupos OK, 675 passed) |
| `pretag_final.log` | `pre_tag_check.sh` completo |

Secuencia de verificación final:

```bash
python run_all_tests.py            # exit 0, 668 passed
sha256sum -c /tmp/db_antes_fix.txt # La suma coincide
./tools/pre_tag_check.sh           # exit 0
python main.py --check-env         # exit 0
# smoke E2E: ProblemSolver → plan de 2 pasos
ruff check ... && pyflakes ...     # sin hallazgos
```
