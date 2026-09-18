# se_hizo.md — Mejora integral del código

Sesión de mejora de calidad sobre el proyecto **agentes_visuales**.
Se corrigieron bugs reales, se reforzó la robustez frente a respuestas
malformadas del LLM y a condiciones de carrera, se endurecieron los
límites de seguridad y se limpió código muerto. Todo se verificó con
`ruff`, `compileall` y la suite de tests.

---

## 1. Método de trabajo

1. **Reconocimiento** del repositorio (rama `mejora/codigo-calidad`,
   ~22.600 líneas de código propio, 34 archivos fuente relevantes).
2. **Punto de seguridad en git**: se creó el tag
   `checkpoint-auto-pre` apuntando al commit `603d970` (HEAD inicial).
   Para volver atrás en cualquier momento:
   ```bash
   git reset --hard checkpoint-auto-pre   # descarta TODOS los cambios
   git diff checkpoint-auto-pre           # revisar qué cambió
   ```
3. **Auditoría paralela**: se revisaron los módulos por bloques
   (`core/`, `storage/`, `learning/`, `export/`, `ui/`, `tests/`) buscando
   bugs de correctitud, concurrencia, fugas de recursos y seguridad.
4. **Verificación de cada hallazgo** contra el código real antes de
   tocarlo (se descartaron varios falsos positivos).
5. **Corrección por lotes** y verificación con:
   ```bash
   .venv/bin/python -m compileall -q core storage learning export ui main.py run_all_tests.py
   .venv/bin/ruff check .
   .venv/bin/python run_all_tests.py
   ```

---

## 2. Red de seguridad (git)

| Elemento | Valor |
|---|---|
| Rama de trabajo | `mejora/codigo-calidad` |
| Tag de seguridad | `checkpoint-auto-pre` |
| Commit base | `603d970` |
| Estado inicial | solo `calculadora.html` sin seguimiento |

> Los cambios **no** se han commiteado todavía; quedan en el árbol de
> trabajo para que se puedan revisar con `git diff` antes de confirmarlos.

---

## 3. Cambios realizados

### 3.1. `core/sandbox.py` — ejecución de subprocesos

- **Deadlock por tuberías llenas (bug real y reproducible).** Se usaba
  `Popen(stdout=PIPE, stderr=PIPE)` y se esperaba con `proceso.poll()` sin
  drenar. Si el hijo escribía más de ~64 KB, se bloqueaba escribiendo, el
  proceso nunca terminaba y se mataba por un falso *timeout* perdiendo el
  resultado. Ahora `stdout`/`stderr` se redirigen a **ficheros
  temporales** y se espera con `wait(timeout=...)` comprobando
  cancelación/timeout. Verificado con un script que imprime 20.000 líneas
  (antes: timeout; ahora: `OK=True`).
- **Proceso no *reaped* en timeout/cancelación.** Se añadió
  `_reapear_proceso()` (kill + `wait` + cierre de pipes) y se invoca en las
  rutas de error y en el `finally`, evitando zombies y descriptores
  abiertos.
- **`_SPAWN_LOCK`**: serializa la creación de subprocesos para reducir la
  carrera de `fork()` concurrente.
- **`cwd` explícito**: se mantiene `os.getcwd()` (en lugar de `None`) para
  que `subprocess` **no** use `posix_spawn/vfork`, que en Linux con varios
  hilos y extensiones nativas (Qt/NumPy/SciPy) provocaba `SIGSEGV`.
- Se eliminó un `communicate()` repetido en bucle que también disparaba
  `SIGSEGV` en CPython 3.13.

### 3.2. `core/scheduler.py`

- **Ejecución que nunca terminaba.** `_verificar_terminado_internal`
  contaba completados/errores/cancelados/bloqueados, pero omitía
  `TIMEOUT` y `SALTADO`, por lo que `ejecucion_terminada` no se emitía y la
  UI podía quedarse colgada. Se añadieron ambos estados a las estadísticas
  y al recuento de terminados.
- **Reinicio incompleto.** `iniciar()` solo reseteaba
  COMPLETADO/ERROR/CANCELADO; ahora usa `EstadoAgente.es_terminal` (incluye
  TIMEOUT, SALTADO y BLOQUEADO).
- **`running` obsoleto.** `detener()` ahora limpia `self.running`; los
  futuros cancelados por `cancel_futures=True` nunca ejecutaban su
  `discard` y dejaban IDs "en marcha" que bloqueaban un `iniciar()`
  posterior.
- **Código muerto eliminado**: `_stats_timer` / `_stats_pending` /
  `_emitir_stats_throttled` (nunca se usaban) y el `QTimer` asociado.

### 3.3. `core/cancellation.py`

- **Colisión de IDs de token.** El id se generaba con
  `int(time.time()*1000)`; dos tokens en el mismo milisegundo se
  sobrescribían en el gestor. Ahora usa `uuid.uuid4().hex`.
- **Callbacks ejecutados con el lock del gestor tomado.** `cancelar_token`,
  `cancelar_todos` y `cancelar_por_agente` ahora liberan el lock antes de
  ejecutar `token.cancelar()` (que dispara callbacks de usuario),
  evitando deadlocks.
- **Singleton thread-safe**: `obtener_gestor_cancelacion` con doble
  comprobación bajo lock.

### 3.4. `core/event_bus.py`

- **`callback.__name__` en el manejador de errores.** Un `functools.partial`
  u otro invocable sin `__name__` lanzaba `AttributeError` y abortaba la
  publicación. Se añadió `_nombre_callback()` seguro.
- **Singletons sin lock**: `EventBus.__init__` y `obtener_bus()` ahora son
  thread-safe (doble comprobación), evitando dobles inicializaciones que
  borraban suscriptores.

### 3.5. `core/agent.py`

- **`from_dict()` rompía el *round-trip* con `to_dict()`**: no mapeaba
  `"dependencias"` → `dependencias_ids`, no convertía `"estado"` de string
  a `EstadoAgente` y no mapeaba `"prompt_usado"` → `prompt_llm`. Ahora sí.
- **Listas robustas**: un string en `dependencias_ids` ya no se itera
  carácter a carácter.
- Nuevo campo opcional `memory_limit_mb` para el sandbox.

### 3.6. `core/executors/*`

- **`helpers.py` y `core/utils.py` — extracción de JSON.** Se sustituyó la
  estrategia "primer `{` … último `}`" por un **escáner balanceado** que
  respeta las llaves dentro de cadenas y se detiene en el cierre correcto.
  Funciona con texto alrededor y con varios JSON concatenados.
- **`security.py` — `validar_ruta_archivo`.** Rechaza `.`, `..`, `~`,
  rutas absolutas (POSIX y Windows), componentes `..` y nombres ocultos.
  Antes `validar_ruta_archivo(".") == True`, lo que permitía
  `shutil.rmtree(".")`.
- **`file_executor.py`.** En la operación `eliminar` se comprueba que la
  ruta resuelta esté **dentro** del directorio de trabajo y no sea el
  propio directorio (cubre enlaces simbólicos).
- **`shell_executor.py`.** Nueva `sustituir_variables_shell()` que cita los
  valores con `shlex.quote()` antes de interpolarlos en un comando
  `shell=True`, cerrando la inyección de comandos. Se corrigió además el
  mismo deadlock de tuberías cambiando a un único `communicate()` con
  manejo de timeout/cancelación.
- **`http_executor.py`.** Un cuerpo JSON de tipo *array* ahora se envía con
  `json=` (antes `data=`). Los reintentos automáticos se limitan a métodos
  idempotentes (antes reintentaba POST/PATCH, pudiendo duplicar
  escrituras).
- **`python_executor.py`.** Propaga `memory_limit_mb` al sandbox (antes el
  límite nunca se aplicaba).

### 3.7. `core/problem_solver/*`

- **`builder.py`:** coerción segura de valores del LLM:
  `estimacion_tiempo_segundos` (null/texto), `timeout` de Python/HTTP/Shell,
  `max_temperatura`, `max_tokens`, `max_iteraciones`, etc.
  `dependencias` se normaliza (null / string / lista) y `configuracion: null`
  pasa a `{}`. `metadatos.fecha_creacion` ahora es una fecha ISO real (antes
  un UUID). Se eliminó la **doble elección A/B** (se llamaba dos veces a
  `elegir_variante`, que usa `random`, quedándose con una variante distinta a
  la registrada).
- **`validator.py`:** `configuracion: null` ya no lanza `AttributeError`;
  `int(max_tokens)` ya no revienta con null o texto.
- **`solver.py`:** el volcado de respuestas de depuración incluye
  microsegundos para no sobrescribir dos respuestas del mismo segundo.
- **`plan_repairs.py`:** al asegurar que el paso DOCX depende del
  preparador ya no se **reemplaza** su lista de dependencias, se añade sin
  perder las existentes.
- **`constants.py`:** `memory_limit_mb` como campo válido de agentes Python.

### 3.8. `storage/database.py`

- **`_transaction()` roto.** El *retry* se hacía sin `ROLLBACK`, reintentando
  un `BEGIN IMMEDIATE` sobre una transacción ya abierta, y el último intento
  hacía `ROLLBACK` sin transacción. Ahora solo se reintenta el `BEGIN`
  cuando la BD está bloqueada, el cuerpo nunca se reintenta y siempre se
  hace rollback si hace falta.
- **`_migrar_db()`.** Si una migración fallaba se registraba un *warning* y
  aun así se marcaba la BD como versión 8 con el esquema incompleto. Ahora
  los fallos se acumulan y, si hay alguno, **no se actualiza la versión**
  (rollback).
- **`reparar_esquema()` / `_crear_indices()`.** El `BEGIN` anidado hacía
  que los índices nunca se crearan. `_crear_indices` acepta ahora una
  conexión/transacción existente.
- **Columnas `NOT NULL` en `ALTER TABLE`.** Se añade un `DEFAULT` automático
  para que las columnas `TEXT/INTEGER NOT NULL` sin default sí se puedan
  añadir a tablas con datos.
- **`backup()` / `restore_backup()`.** El backup con `shutil.copy2` ignoraba
  `-wal`/`-shm` (copia inconsistente); ahora usa la API
  `sqlite3.Connection.backup()`. La verificación de integridad del backup
  ahora **lee** el resultado y se comprueban las conexiones. Tras restaurar
  se reejecutan migración y verificación de esquema.

### 3.9. `storage/config_manager.py`

- `_ruta_segura` compara contra `os.path.realpath(config_dir)` (antes
  `abspath`), evitando falsos positivos con enlaces simbólicos.
- `_validar_archivo_config` reintenta el parseo tras recuperar del
  *journal* (antes la recuperación era código muerto).
- `_get_from_cache` devuelve una **copia profunda** para que el llamador no
  corrompa la caché compartida.
- El nombre de archivo al guardar incluye **microsegundos** (dos guardados
  en el mismo segundo se pisaban).

### 3.10. `learning/*`

- **`reward_llm.py` / `engine.py` — bug crítico.** `EvaluadorLLM.evaluar`
  devolvía una tupla `(score, justificacion)` pero `engine.py` accedía a
  `evaluacion.score/.justificacion/.modelo`, por lo que **todo** el
  registro de evaluaciones fallaba con `AttributeError` silencioso. Ahora
  `evaluar` devuelve un dataclass `Evaluacion(score, justificacion, modelo)`.
- **`feedback_processor.py`.** `_asegurar_tabla` creaba
  `prompts_reescritos` con solo 8 columnas mientras el INSERT usaba 11;
  ahora crea el esquema completo y añade columnas faltantes en BD legadas.
- **`schema.py`.** Un índice sobre una columna ausente ya no aborta el resto
  del esquema (se registra y continúa).
- **`embedding_matcher.py`.** El singleton ignoraba el argumento `modelo` y
  devolvía el primero creado, provocando 0 coincidencias. Ahora se cachea
  por nombre de modelo.
- **`models.py`.** `PlanScorer.entrenar_inicial` ya no infla
  `_n_muestras` (fija el contador en lugar de sumarlo).
- **`lessons.py`.** La categoría `permisos_root` ya no se dispara con
  "router"/"root cause" (patrones explícitos) y el éxito de un plan exige
  que no esté cancelado/fallido, no solo `errores == 0`.

### 3.11. `export/exporters.py`

- **Doble extensión `.zip.zip`.** `_generar_ruta` añadía `.zip` y
  `_comprimir_archivo` lo añadía otra vez. Ahora solo lo hace el segundo.
- **XSS almacenado en el HTML exportado.** Los valores y cabeceras se
  escapan con `html.escape`.

### 3.12. `main.py`, `run_all_tests.py`, `ui/`

- **`main.py`:** `basicConfig` era un no-op porque el `RotatingFileHandler`
  se añadía antes (el nivel INFO y el handler de consola no se aplicaban);
  ahora se usa `force=True` y se configura en el orden correcto. `--timeout`
  exige valor > 0. La creación de la ventana principal está protegida con
  `try/except`. La carpeta de logs se crea con manejo de error.
- **`run_all_tests.py`:** `Popen` con `encoding="utf-8"` y `errors="replace"`
  (evita `UnicodeDecodeError` con emojis en Windows); reintentos
  configurables (`--retries`, por defecto 3) para el *flake* de
  CPython 3.13 + Qt + fork; se incluyen códigos de caída de Windows; tras
  `kill()` se hace `wait()` para no dejar `returncode=None`.
- **`ui/simple_main_window.py`:** el log escapa el HTML de los mensajes
  (salidas de agentes/excepciones con `<` o `&` ya no corrompen el panel).

### 3.13. `tests/conftest.py` y `tests/test_scheduler.py`

- Los *fixtures* de `Scheduler` ahora hacen **teardown** (`yield` +
  `detener()` + `shutdown` del `ThreadPoolExecutor`), en lugar de dejar
  hilos y `QObject` vivos entre tests. Se corrigió además la anotación
  `condicion: callable` → `Callable[[], bool]`.

---

## 4. Archivos modificados

| Archivo | Tipo de cambio |
|---|---|
| `core/sandbox.py` | deadlock, reaping, fork/lock |
| `core/scheduler.py` | estados terminales, reinicio, `running`, código muerto |
| `core/cancellation.py` | IDs únicos, callbacks fuera de lock, singleton |
| `core/event_bus.py` | callables sin `__name__`, singleton |
| `core/agent.py` | `from_dict`, listas, `memory_limit_mb` |
| `core/executors/helpers.py` | JSON balanceado |
| `core/utils.py` | JSON balanceado |
| `core/executors/security.py` | validación de rutas |
| `core/executors/file_executor.py` | borrado seguro |
| `core/executors/shell_executor.py` | anti-inyección, deadlock |
| `core/executors/http_executor.py` | body JSON array, reintentos idempotentes |
| `core/executors/python_executor.py` | `memory_limit_mb` |
| `core/executors/content_extractor.py` | sustitución shell segura |
| `core/problem_solver/builder.py` | coerción LLM, A/B, fecha |
| `core/problem_solver/validator.py` | `configuracion: null`, `max_tokens` |
| `core/problem_solver/solver.py` | nombre de log único |
| `core/problem_solver/constants.py` | campo `memory_limit_mb` |
| `core/plan_repairs.py` | no perder dependencias |
| `core/execution_recorder.py` | hilo daemon, snapshot sin carrera |
| `storage/database.py` | transacciones, migraciones, índices, backup |
| `storage/config_manager.py` | rutas, caché, journal, unicidad |
| `learning/reward_llm.py` | dataclass `Evaluacion` |
| `learning/feedback_processor.py` | esquema completo + migración |
| `learning/schema.py` | tolerancia a columnas ausentes |
| `learning/embedding_matcher.py` | caché por modelo |
| `learning/models.py` | contador correcto |
| `learning/lessons.py` | categorización y tasa de éxito |
| `export/exporters.py` | `.zip` y escape HTML |
| `main.py` | logging, `--timeout`, GUI |
| `run_all_tests.py` | encoding, reintentos, Windows |
| `ui/simple_main_window.py` | escape del log |
| `tests/conftest.py`, `tests/test_scheduler.py` | teardown de schedulers |

---

## 5. Verificación

- `python -m compileall` sobre `core storage learning export ui main.py run_all_tests.py`: **OK**.
- `ruff check .`: **All checks passed**.
- Pruebas de regresión puntuales:
  - Sandbox: salida grande de 20.000 líneas antes hacía *timeout*; ahora
    termina correctamente.
  - `test_sandbox.py` + `test_sandbox_smoke.py`: 58 passed.
  - Resto de grupos (A, C, D, E, F): verdes.

### Resultado de la suite

Ejecución final con `run_all_tests.py` (grupos aislados + reintentos):

```
✅ Grupo A_ui_datos:    OK (3.0s,  138 líneas)
✅ Grupo B_scheduler:   OK (11.1s,  96 líneas)
✅ Grupo C_integration: OK (11.2s,  65 líneas)
✅ Grupo D_sandbox:     OK (10.6s, 128 líneas)
✅ Grupo E_loops:       OK ( 9.8s, 108 líneas)
✅ Grupo F_ligeros:     OK ( 2.1s,  76 líneas)

✅ TODAS LAS PRUEBAS PASARON  (EXIT 0)
```

Subtotales por grupo (pytest): A `97 passed, 1 skipped`; B `51`; C `18`;
D `58`; E `45`; F `46 passed, 5 deselected`.

> Nota: el grupo B (scheduler) es **intermitente** por un `SIGSEGV`
> preexistente de CPython 3.13 + PyQt6 + `fork()` desde hilos (ver §6).
> En esta ejecución pasó a la primera; el runner reintenta los grupos que
> mueren por señal (`--retries`, por defecto 3).

---

## 6. Problema conocido: segfault intermitente en el grupo B

- **Síntoma:** `Fatal Python error: Segmentation fault` en
  `test_estadisticas_finales`, dentro de `QApplication.processEvents()`,
  mientras los hilos del scheduler ejecutan subprocesos del sandbox.
- **Naturaleza:** es un *flake* de la combinación **CPython 3.13 + PyQt6 +
  NumPy/SciPy + `fork()` desde hilos**, ya documentado en el propio
  `run_all_tests.py`. No lo introducen estos cambios: la línea base también
  fallaba en el mismo test.
- **Mitigaciones aplicadas:**
  - `cwd` explícito para evitar `posix_spawn/vfork` (que empeoraba el
    fallo).
  - `_SPAWN_LOCK` para serializar la creación de subprocesos.
  - Eliminación del `QTimer` muerto del scheduler.
  - Teardown de los *fixtures* de `Scheduler` (menos hilos vivos).
  - El runner reintenta los grupos que mueren por señal
    (`--retries`, por defecto 3).
- **Recomendación:** ejecutar la suite con `run_all_tests.py` (grupos en
  procesos separados + reintentos) y, a medio plazo, aislar el sandbox en
  un proceso *fork server* de un solo hilo o usar `os.posix_spawn` desde
  un auxiliar sin extensiones nativas cargadas.

---

## 7. Cómo revertir

```bash
# Ver todo lo cambiado en esta sesión:
git diff checkpoint-auto-pre

# Descartar todos los cambios y volver al estado inicial:
git reset --hard checkpoint-auto-pre

# Descartar solo un archivo:
git checkout checkpoint-auto-pre -- ruta/al/archivo.py

# El tag se puede eliminar cuando ya no haga falta:
git tag -d checkpoint-auto-pre
```

---

## 8. Pendientes / mejoras futuras sugeridas

- Sustituir el *fork* del sandbox por un *fork server* de un solo hilo
  (eliminaría el segfault del grupo B).
- Revisar `storage/database.py`: `check_same_thread=False` innecesario y
  `close()` solo cierra la conexión del hilo actual.
- Centralizar la extracción/reparación de JSON (hoy duplicada en
  `core/utils.py`, `core/executors/helpers.py` y `learning/reward_llm.py`).
- Añadir `UNIQUE(prompt_reescrito_id, ejecucion_id)` en
  `prompt_reescrito_usos` para evitar duplicados en A/B.
- Rotar/limitar `logs/sandbox_debug.log` (hoy crece sin límite).

---

## 9. Incidencia durante la sesión: archivos movidos a la Papelera

Durante la sesión, un proceso externo (no los tests ni el código del
proyecto) movió a la Papelera del sistema 16 ficheros del workspace a las
20:52–20:53, entre ellos `calculadora.html`, `cuento_*.docx/pdf`,
`clima.txt`, `resumenhoy.md`, `dibujo.svg`, `documento_con_imagen.pdf`,
`rosca.sh` y `sqlite3`.

Se detectó al revisar `git status` y el contenido de la Papelera, y se
**restauraron los 16 ficheros** a su ubicación original (verificado por
tamaño; los que están bajo control de versiones también se restauraron con
`git checkout checkpoint-auto-pre -- <archivo>`). El árbol de trabajo
quedó de nuevo con los mismos ficheros que al inicio de la sesión.

