# ROADMAP — agentes_visuales

- **Versión del documento**: 1.0
- **Fecha**: 2026-09-21
- **Rama**: `harness/v3.8.0`
- **Origen**: acta de cierre + backlog priorizado de la sesión del 21/09, **auditada contra el código real** antes de versionarla.
- **Estado del código en esta fecha**: `1220 passed, 1 skipped` (85 s) con `tools/run_tests.sh tests/ -m "not network"`; los 4 tests de red (API real de DeepSeek) quedan fuera porque son intermitentes. La referencia previa a las correcciones de §0.1 era `1180 passed, 1 skipped`.

> **Cómo leer este documento.** El material de origen describía 18 fallos en el
> Bloque 1 como si estuvieran todos vivos. La auditoría muestra que **casi la
> mitad ya no se reproduce**: el proyecto avanzó a V4.0 mientras el documento
> seguía describiendo el estado anterior. Cada punto lleva por eso un
> **veredicto** y su **evidencia reproducible**. No se ha borrado nada del
> backlog original: lo obsoleto queda marcado, no eliminado.

## Leyenda de veredictos

| Símbolo | Significado |
|---|---|
| ✅ **YA RESUELTO** | El defecto no existe hoy en el código. No invertir tiempo. |
| ⚪ **NO REPRODUCE** | Se intentó reproducir y no ocurre. Puede ser un falso positivo del acta. |
| 📝 **MAL DESCRITO** | Hay un defecto real, pero la causa descrita no es la causa real. |
| 🟠 **PARCIAL** | Parte de la afirmación es cierta y parte no. Requiere precisión antes de tocar. |
| 🔴 **CONFIRMADO** | Reproducido o leído en el código. Es trabajo real. |
| ❓ **NO VERIFICABLE** | Requiere red, credenciales o datos que esta auditoría no tiene. |

---

## 0. Veredicto de la auditoría (resumen ejecutivo)

**Bloque 1 (18 puntos):**

| Veredicto | Puntos | IDs |
|---|---|---|
| ✅ Ya resuelto / ya implementado | 3 | 1.6, 1.11, 1.15 |
| ⚪ No reproduce | 1 | 1.1 |
| 📝 Mal descrito (defecto real distinto) | 1 | 1.10 |
| 🟠 Parcial (afirmación mezclada) | 5 | 1.3, 1.5, 1.9, 1.12, 1.17 |
| 🔴 Confirmado | 7 | 1.2, 1.7, 1.8, 1.13, 1.14, 1.16, 1.18 |
| ❓ No verificable (auditoría de repo limpia) | 1 | 1.4 |

**Bloque 2 (5 puntos):**

| Veredicto | Puntos | IDs |
|---|---|---|
| ✅ Ya resuelto / ya no existe | 3 | 2.1, 2.2, 2.3 |
| 🔴 Confirmado | 2 | 2.4, 2.5 |

**Consecuencia directa sobre el plan del día.** La «hora 1 — estabilidad» tal
como estaba planteada (1.1–1.6) apunta a **un solo defecto vivo** (1.2). El
orden sugerido de las notas finales —«2.1 y 2.3 primero, luego 1.1–1.3 y
1.12»— queda **invalidado**: 2.1, 2.3 y 1.1 ya están cerrados, y 1.3/1.12 son
parciales. El plan corregido está en la §13.

**Los tres hallazgos que sí justifican la jornada**, por orden de valor:

1. **1.14** — `EvaluadorLLM(None)` en tres rutas reales. Degrada el aprendizaje
   por refuerzo **en silencio**: es exactamente la clase de fallo que el
   documento quiere erradicar, y no estaba en ninguna lista de prioridades.
2. **1.2 / 1.10** — el plan de respaldo **nunca produce el artefacto pedido**,
   y devuelve `status: ok`. Es el «fallback que simula que hizo algo».
3. **1.7 / 1.8, causa raíz** — el placeholder «Descripción no disponible» no
   viene de `content_extractor`: lo **genera el propio LLM** dentro del código
   Python del plan, como fallback silencioso ante un JSON de forma inesperada.

---

## 0.1 Correcciones ya aplicadas el 2026-09-21

| Item | Qué se hizo | Test de regresión |
|---|---|---|
| **3.2 (1.14)** | `EvaluadorLLM` resuelve el cliente compartido de forma perezosa (`_cliente()`), expone `set_llm_client()` y, si no hay cliente, emite un **ERROR accionable** en vez de degradar el aprendizaje en silencio con el críptico `'NoneType' object has no attribute 'chat'`. `LearningEngine.__init__` resuelve el cliente cuando recibe `None`. | `tests/test_regression_001_evaluador_sin_cliente.py` (10 casos) |
| **3.6 (1.16)** | `"weasyprint"` añadido a los loggers silenciados de `main._configurar_logging`. | `tests/test_regression_002_weasyprint_silenciado.py` (4 casos) |
| **3.5 (1.18)** | `BrokenPipeError` neutralizado en tres puntos: `_StreamHandlerTolerante` (el handler de consola ya no propaga el EPIPE), `_manejar_broken_pipe()` (redirige stdout/stderr a `os.devnull`) y `main()`, que devuelve `EXIT_OK` en vez de un código de error engañoso. | `tests/test_regression_003_broken_pipe.py` (7 casos, incluido uno end-to-end con proceso real y pipe cerrado) |
| **3.11 (1.17)** | `--check-env --timeout N` ahora acota el comando de verdad. El `timeout` de `requests` solo limita operaciones de socket y **la resolución DNS queda fuera** (se midió: 17-22 s con `--timeout 2`). Un `signal.setitimer` **tampoco** lo resuelve (medido: 17 s), porque el handler de Python no corre mientras `getaddrinfo` está bloqueado en C. Solución: el ping se ejecuta en un hilo del que se desiste al agotar el presupuesto. Medición tras el fix: el ping tarda **exactamente 2,00 s** con `--timeout 2` (el total del comando, ~4 s, incluye ~2 s de arranque e imports). | `tests/test_regression_004_check_env_timeout.py` (10 casos) |
| **3.8 (1.3)** | Gate de sintaxis en `PythonCodeCorrector._validar_o_revertir`: el código corregido se compila (`compile()`, el equivalente en proceso de `py_compile`) y, si no compila, se devuelve el **original** (rollback). La corrupción `contexto contexto...` no se reproducía, pero el corrector aceptaba cualquier reescritura sin comprobarla. | `tests/test_regression_005_code_corrector_gate.py` (12 casos) |
| **3.9 (1.9)** | Reintento correctivo ante respuesta «solo razonamiento»: `_llamar_con_reintento_correctivo` reintenta **una vez** subiendo `max_tokens`, desactivando `thinking` y añadiendo una instrucción explícita de responder ya. Antes solo había `logger.warning` y el paso fallaba en cascada. | `tests/test_regression_006_reintento_razonamiento.py` (7 casos) |
| **3.12** | El presupuesto compartido de `resolve` ya no se reinicia en cada intento: `set_contexto_plan_b` acepta `reiniciar_presupuesto` y `_ejecutar_plan` pasa `False` cuando recibe un presupuesto compartido (`run` no cambia). | `tests/test_regression_008_presupuesto_compartido.py` (5 casos) |
| **3.7 (1.5)** | `agente.duracion` es **acumulativa** entre reintentos: se suma en `_duracion_acumulada` en vez de sobrescribirse. Se usa un atributo propio para no arrastrar el `0.1` de relleno de `Agente.__post_init__`. | `tests/test_regression_007_duracion_acumulativa.py` (3 casos, comparados contra un control de un solo intento) |

Efecto en la suite: **1166 → 1220 passed, 1 skipped**, sin regresiones (58 tests de regresión nuevos: 10 + 4 + 7 + 10 + 12 + 7 + 5 + 3).

Los conteos de §0 y las tablas de §1 reflejan el estado **en la auditoría**;
los puntos de esta tabla ya están cerrados.

---

## 0.2 Hallazgo de la reconstrucción del modo `resolve` (2026-09-21)

El cableado de `resolve` en `main.py` (V4.0) estaba **sin commitear**. Al
versionar el ROADMAP se perdió y hubo que **reconstruirlo** a partir de su
especificación, que sí sobrevivía:

- `tests/test_main_resolve.py` (12 casos) — el contrato exacto del subcomando.
- `core/goal_resolver.py` (497 líneas, intacto) — el bucle, con su API real.
- `HARNESS_REPORT_v4.0.0-resolver.md` y el diff de `docs/MANUAL_CLI.md` — el
  diseño y las banderas documentadas.

Reconstruido y verificado: **12/12** en `test_main_resolve.py`, **43/43** en
`test_main_cli.py` (el contrato de `run` no cambia) y **21/21** en
`test_goal_resolver.py`.

### 🔴 Defecto descubierto al reconstruir: el presupuesto compartido no se comparte → ✅ arreglado (3.12)

El informe promete que «un único presupuesto cubre la planificación y **todos**
los intentos». En el cableado real **no ocurre**:

- `core/scheduler.py:232-233` — `set_contexto_plan_b()` llama a
  `self.presupuesto.reset()`.
- `core/budget_manager.py:162-164` — `reset()` es un **alias de `iniciar()`**:
  hace `self.consumo = Consumo()` y reinicia el reloj (`self._inicio = momento`).
- `_ejecutar_plan` llama a `set_contexto_plan_b()` en **cada** intento (cuando
  `aprender=True`), y `GoalResolver` comprueba `motivo_agotado()` **antes** de
  planificar cada intento leyendo ese mismo reloj.

Efecto: el tiempo, las llamadas y los tokens se **ponen a cero en cada
intento**, así que el límite no acota el total de la resolución. La intención
del diseño está escrita y es correcta (`core/scheduler.py:1489-1496`: «el
presupuesto es del conjunto» y `iniciar()` **no** lo reinicia); el reinicio
entra por `set_contexto_plan_b`.

Por qué no se detectó: los tests del bucle (`test_goal_resolver.py`) inyectan un
ejecutor falso que registra gasto directamente en el `BudgetManager`, sin pasar
por el `Scheduler`. La interacción solo existe en el cableado real.

**Acción aplicada el 2026-09-21** (punto 3.12 de §3): `set_contexto_plan_b`
acepta `reiniciar_presupuesto: bool = True`, y `_ejecutar_plan` pasa
`presupuesto is None` — es decir, `False` cuando el presupuesto es compartido
(modo `resolve`) y `True` en `run`, que no cambia de comportamiento.
`tests/test_regression_008_presupuesto_compartido.py` (5 casos). Verificado que
el test **discrimina**: con el código antiguo fallan 3 de 5; con el arreglo,
5 de 5.

---

## 1. Estado de verificación — Bloque 1

| ID | Afirmación del acta | Veredicto | Evidencia |
|---|---|---|---|
| 1.1 | `solver.py` con `SyntaxError` tras parche | ⚪ NO REPRODUCE | `python -m compileall -q core main.py` → `exit=0`. Sin `SyntaxError`. |
| 1.2 | Problem Solver → agente Python no ejecuta la acción | 🔴 CONFIRMADO | `core/problem_solver/solver.py:416-448` (`_crear_plan_fallback`) devuelve solo un dict con `status: ok`; nunca abre ni escribe el fichero pedido. |
| 1.3 | `PythonCodeCorrector` corrompe código (`contexto contexto...`) | 🟠 PARCIAL | La corrupción **no está** en el repo: las 3 ocurrencias de `data = contexto if contexto else {}` (`solver.py:437`, `builder.py:112`, `validator.py:205`) están intactas. Pero `code_corrector.py:82` no llama a `py_compile` ni tiene rollback: **el gate pedido no existía**. → ✅ **arreglado el 2026-09-21** (§0.1). |
| 1.4 | 401 con clave antigua; auditar claves en el repo | ❓ NO VERIFICABLE / auditoría limpia | `git ls-files \| xargs grep -nI "sk-[A-Za-z0-9]\{16,\}"` → solo fixtures enmascarados (`tests/test_env_checker.py:19`, `tests/test_ia_config.py:97`). `.env` es un **venv** (no un fichero de secretos) y está en `.gitignore`. No hay fuga en el repo; el 401 es de configuración en runtime. |
| 1.5 | `Scheduler._ejecutar_agente` no actualiza `duracion` | 🟠 PARCIAL | **Sí la actualiza**: `core/scheduler.py:605` (`agente.duracion = duracion`). El defecto real es el segundo: `duracion` se calcula por intento (`:594`, `tiempo_fin - tiempo_inicio`) y se **sobrescribe** en cada reintento (`:726-751`), en vez de acumularse. → ✅ **arreglado el 2026-09-21** (§0.1). |
| 1.6 | Verificar flujo `Plan → Validación → BLOQUEANTE → refinar → máx N` | ✅ YA IMPLEMENTADO | `core/problem_solver/solver.py:191` `MAX_INTENTOS_VALIDACION = 2`; bucle de reintento `:193-224`; `core/problem_solver/validator.py` emite `BLOQUEANTE` en 13 ramas (p. ej. `:539` SyntaxError, `:672` identificador sin definir, `:717` clave inexistente). |
| 1.7 | `GenerarHTML` muestra «Descripción no disponible» | 🔴 CONFIRMADO (causa raíz distinta) | El string no está en el código: lo **genera el LLM** en el paso 2 del plan. `logs/llm_response_20260913_091705_deepseek-v4-flash.txt:33` contiene el fallback `ideas.append({'titulo': f'Idea {i}', 'descripcion': 'Descripción no disponible'})`. `SeleccionarTop10`/`GenerarDescripciones`/`CombinarDatos` **no existen** en el código: son nombres de agente generados por el LLM. |
| 1.8 | Extracción vacía en plan de 4 agentes | 🔴 CONFIRMADO (misma raíz que 1.7) | Contrato del executor LLM: `core/executors/llm_executor.py:580-582` expone `respuesta` **y** `json` (`json_auto`, puede ser `None`). El código generado hace `respuesta_llm.get('json', {})`: si el JSON tiene otra forma, degrada a `[]`/`''` **sin fallar**. Sin test de regresión. |
| 1.9 | LLM «solo razonamiento» tras 3 reintentos | 🟠 PARCIAL | La detección **existe** pero solo avisa: `core/executors/llm_executor.py:566-576` → `logger.warning`. No hay reintento correctivo ni cambio de `max_tokens`. → ✅ **arreglado el 2026-09-21** (§0.1). |
| 1.10 | Fallback con `SyntaxError` hardcodeado | 📝 MAL DESCRITO | El código es **sintácticamente válido** (`data = contexto if contexto else {}` es correcto; `contexto` está en el scope del `exec`). El defecto real es el de 1.2: el plan de respaldo **nunca escribe el fichero**. `solver.py:437`, `builder.py:112`. |
| 1.11 | `export/exporters.py` — 2 bugs | ✅ YA RESUELTO | Columna inferida de una **ventana** de `STREAM_SNIFF_ROWS = 1000` (`export/exporters.py:44`, `:1049`), con aviso y `extrasaction="ignore"` para claves nuevas (`:1080-1085`). Y `exportar_streaming` devuelve `exito=not errores and filas_exportadas > 0` (`:1004`). |
| 1.12 | LLM genera nombres de agentes como variables globales | 🟠 PARCIAL | El detector **existe**: `validator.py:665-680` (`identificador in nombres_agentes`) y `code_corrector.py:82` reescribe vía AST (`:156`, `:185`). Falta **medir cobertura** sobre el corpus de `logs/`, no solo el caso probado a mano. |
| 1.13 | Segfault en `core/sandbox.py:853` | 🔴 CONFIRMADO | Reproducido: `python -m pytest -q` muere con SIGSEGV (`tests/conftest.py:49` → `tests/test_flujo_e2e_aceptacion.py:79`). El código ya mitiga por otra vía (`sandbox.py:853-870`: `cwd` explícito para evitar `vfork`, `TemporaryFile` en vez de `PIPE`; `:872-879` con `_SPAWN_LOCK` y `stdin=DEVNULL`). El fix propuesto en el acta (`NamedTemporaryFile(delete=False)`) **no** está aplicado; la mitigación operativa es `tools/run_tests.sh --forked`. |
| 1.14 | `Evaluador no disponible: 'NoneType' object has no attribute 'chat'` | 🔴 CONFIRMADO | `learning/reward_llm.py:63` hace `self.llm_client.chat(...)`; el `except` de `:75` produce **literalmente** ese mensaje. `EvaluadorLLM` se construye con el valor recibido (`learning/engine.py:125`) y `obtener_learning_engine()` tiene `llm_client=None` por defecto (`learning/__init__.py:22`) — **pero 3 llamadas no pasan cliente**: `core/problem_solver/solver.py:125`, `:137`, `core/scheduler.py:832`. Solo `core/execution_recorder.py:150` lo pasa bien. Al ser singleton perezoso, se salva o se rompe **según el orden de arranque**. → ✅ **arreglado el 2026-09-21** (§0.1). |
| 1.15 | Colisión `--stdin` vs agentes que leen stdin | ✅ YA RESUELTO | `core/sandbox.py:879` pasa `stdin=subprocess.DEVNULL` a todo subproceso del sandbox. `main.py:448-455` consume stdin en el proceso padre; el hijo ya no lo hereda. |
| 1.16 | `weasyprint` contamina logs | 🔴 CONFIRMADO | `main.py:343-345` silencia `urllib3, openai, matplotlib, PIL, httpx, httpx2, httpcore, charset_normalizer`. **`weasyprint` no está en la lista**, y `core/executors/file_executor.py:18` lo importa. → ✅ **arreglado el 2026-09-21** (§0.1). |
| 1.17 | `--check-env` con latencia alta (10949 ms con `--timeout 5`) | 🟠 PARCIAL → ✅ **arreglado el 2026-09-21** (§0.1) | Cierto que el `timeout` no acotaba el comando: se pasa a `requests` (`core/env_checker.py:239`), que es por operación. **La causa real resultó ser la resolución DNS**, que `requests` no acota y que tampoco se puede interrumpir con `signal.setitimer`. |
| 1.18 | `BrokenPipeError` en `run --json \| jq` | 🔴 CONFIRMADO → ✅ **arreglado el 2026-09-21** (§0.1) | `grep -rn BrokenPipeError` no devolvía **ninguna** coincidencia en el código del proyecto (solo en `.venv`). |

---

## 2. Estado de verificación — Bloque 2 (deuda técnica)

| ID | Afirmación del acta | Veredicto | Evidencia |
|---|---|---|---|
| 2.1 | `LLMClient` sin singleton en `core/executors.py` («candidato #1 a corrupción de heap») | ✅ YA RESUELTO | `core/executors/llm_executor.py:366-371` usa `obtener_llm_client_compartido()`, con comentario explícito del fix (`✅ B1`). |
| 2.2 | Residuos de refactor: `export/core/scheduler/`, `export/logs/` | ✅ YA NO EXISTEN | `ls export/` → solo `exporters.py`, `__init__.py`, `utils.py`, `__pycache__`. |
| 2.3 | Módulos migrados sin verificar tests (`core/executors/`, `storage/database/`) | ✅ RESUELTO | Ambos directorios existen y la suite completa pasa: `1166 passed, 1 skipped` vía `tools/run_tests.sh`. |
| 2.4 | `_ejecutar_agente` del `Scheduler` sin refactorizar | 🔴 CONFIRMADO | Deuda **ya declarada** en `HARNESS_STATUS.md:186-187`: el refactor V4.0-5 se limitó a `_intentar_plan_b`. |
| 2.5 | `_ultima_ejecucion_id` con parche `SELECT MAX(id)` | 🔴 CONFIRMADO (+ bug menor) | `ui/simple_main_window.py:1085` ejecuta `SELECT MAX(id) AS ultimo FROM ejecuciones` en `_obtener_ultimo_ejecucion_id_fallback`. La causa raíz de que `registrar_ejecucion_en_aprendizaje` falle en silencio **sigue sin diagnosticar**. Extra: `logger.info` duplicado en `:1088-1093`. |

---

## 3. Bloque 1 — Bugs que bloquean estabilidad (backlog vivo)

Solo los que siguen abiertos. Cada uno listo para convertirse en issue
(plantilla en `.github/ISSUE_TEMPLATE/`).

### 3.1 · 🔴 `[bug]` El plan de fallback nunca produce el artefacto pedido (1.2 + 1.10)

- **Archivos**: `core/problem_solver/solver.py:416-448`, `core/problem_solver/builder.py:112`
- **Síntoma**: ante fallo de API key, «crear `saludo.txt`» termina en `status: ok` sin que exista el fichero.
- **Causa**: `_crear_plan_fallback` devuelve un dict de resultado; la escritura la haría un agente `File` que el plan de respaldo no incluye.
- **Acción**: que el fallback declare un paso `File`/`Shell` que materialice el artefacto, o que devuelva `status: error` honesto. Prohibido simular éxito.
- **Criterio de cierre**: test que ejecuta el plan de fallback y comprueba `os.path.exists(saludo.txt)`.

### 3.2 · ✅ CERRADO (2026-09-21) · `[bug]` `EvaluadorLLM` recibe `None` y degrada el aprendizaje en silencio (1.14)

- **Archivos**: `learning/__init__.py:22`, `learning/engine.py:125`, `learning/reward_llm.py:63,75`, `core/problem_solver/solver.py:125,137`, `core/scheduler.py:832`
- **Causa**: 3 de 4 rutas llaman a `obtener_learning_engine()` sin `llm_client`; el singleton perezoso fija el valor de la **primera** llamada.
- **Acción**: `set_llm_client(llm)` tras `obtener_llm_client_compartido()`, o resolver el cliente dentro de `EvaluadorLLM` de forma perezosa. Que la ausencia de cliente sea **error visible**, no score neutro 0.5.
- **Criterio de cierre**: test que crea el engine desde `solver.py` y verifica que el evaluador tiene cliente real.
- **Resuelto**: resolución perezosa en `EvaluadorLLM._cliente()` + `set_llm_client()` + resolución en `LearningEngine.__init__`; sin cliente, ERROR accionable en lugar de score neutro silencioso. `tests/test_regression_001_evaluador_sin_cliente.py` (10 casos, en verde).

### 3.3 · 🔴 `[bug]` Placeholder fabricado por código generado por el LLM (1.7 + 1.8)

- **Evidencia**: `logs/llm_response_20260913_091705_deepseek-v4-flash.txt:33`; contrato en `core/executors/llm_executor.py:580-582`
- **Causa**: el plan asume `respuesta_llm.get('json', {})` con forma `{"ideas": [...]}`; si no encaja, el código **generado** rellena datos falsos en vez de fallar.
- **Acción**: prohibir en el prompt del planificador los fallbacks que inventan contenido; que un resultado vacío sea fallo del paso y dispare Plan B.
- **Criterio de cierre**: el caso «5 ideas» falla de forma visible o se recupera; nunca muestra «Descripción no disponible».

### 3.4 · 🔴 `[bug]` `SIGSEGV` intermitente en la suite / sandbox (1.13)

- **Archivos**: `core/sandbox.py:853-879`, `tools/run_tests.sh`
- **Estado**: confirmado y **con mitigación operativa** (`--forked`). El fix de código propuesto no está aplicado.
- **Acción**: decidir entre asumir la mitigación como definitiva (documentándolo) o aplicar el ciclo de vida explícito de temporales.
- **Criterio de cierre**: `python -m pytest -q` sin `--forked` completa en verde, o decisión escrita de no perseguirlo.

### 3.5 · ✅ CERRADO (2026-09-21) · `[bug]` `BrokenPipeError` al cerrar el pipe (`run --json | jq`) (1.18)

- **Archivos**: `main.py` (`_configurar_logging`, `print` final)
- **Acción**: capturar `BrokenPipeError`, redirigir stdout a `os.devnull` y salir con `EXIT_OK`.
- **Coste**: minutos. **Criterio de cierre**: `python main.py run --json ... | head -1` no imprime traceback.
- **Resuelto**: `_StreamHandlerTolerante` (el handler de consola no propaga el EPIPE), `_manejar_broken_pipe()` (dup2 a `os.devnull`, que evita además el traceback del volcado final del intérprete) y captura en `main()` — ahora delega en `_despachar`. `tests/test_regression_003_broken_pipe.py` (7 casos, incluido uno end-to-end con proceso real y pipe cerrado a propósito).

### 3.6 · ✅ CERRADO (2026-09-21) · `[bug]` `weasyprint` no está silenciado (1.16)

- **Archivo**: `main.py:343-345`
- **Acción**: añadir `"weasyprint"` a la tupla de loggers ruidosos.
- **Coste**: 2 minutos. **Impacto**: alto en legibilidad de logs.
- **Resuelto**: `"weasyprint"` añadido a la tupla de `main.py:343`. El test comprueba el nivel **efectivo** del logger hijo real (`weasyprint.progress`), no solo el nombre del padre. `tests/test_regression_002_weasyprint_silenciado.py` (4 casos, en verde).

### 3.7 · ✅ CERRADO (2026-09-21) · `duracion` no acumulativa en reintentos (1.5)

- **Archivo**: `core/scheduler.py:594, 605, 726-751`
- **Acción**: acumular por agente (`agente.duracion += delta`) en vez de sobrescribir; decidir si se expone el último intento por separado.
- **Criterio de cierre**: test con 3 intentos de 5/7/4 s → `duracion == 16`.
- **Resuelto**: se acumula en `agente._duracion_acumulada` (atributo propio, para no arrastrar el `0.1` de relleno de `Agente.__post_init__`) y `agente.duracion` refleja la suma. El test compara contra un **control de un solo intento** con calentamiento previo, en vez de fijar segundos absolutos, para no depender de lo que tarde el sandbox en arrancar. Verificado que **discrimina**: con el código antiguo fallan 3 de 3; con el arreglo, 3 de 3 pasan. `tests/test_regression_007_duracion_acumulativa.py`.

### 3.8 · ✅ CERRADO (2026-09-21) · Gate `py_compile` + rollback en `PythonCodeCorrector` (1.3)

- **Archivo**: `core/problem_solver/code_corrector.py:82`
- **Acción**: pipeline `generar → py_compile → test → aceptar; si falla → rollback`.
- **Nota**: la corrupción `contexto contexto...` **no se reproduce**; el valor está en blindar el corrector y en tener el gate, no en arreglar un string roto.
- **Resuelto**: `_validar_o_revertir(original, corregido)` compila el resultado y revierte al original si falla, con `logger.warning`. Se usa `compile()` en vez de `py_compile` porque el corrector trabaja en memoria (no escribe fichero): es la misma comprobación de sintaxis sin E/S. La parte de «tests» del pipeline no aplica aquí: un corrector no puede ejecutar los tests del paso, y el validador ya cubre el contrato. `tests/test_regression_005_code_corrector_gate.py` (12 casos).

### 3.9 · ✅ CERRADO (2026-09-21) · Reintento correctivo ante respuesta «solo razonamiento» (1.9)

- **Archivo**: `core/executors/llm_executor.py:566-576`
- **Acción**: hoy solo `logger.warning`; añadir reintento con instrucción correctiva / subida de `max_tokens` antes de bloquear dependientes.
- **Resuelto**: `_llamar_con_reintento_correctivo` (envoltorio de `_llamar_una_vez`): ante `error == 'only_reasoning'` reintenta **una vez** con `thinking_enabled=False`, `reasoning_effort='low'`, `max_tokens` al menos el doble y una instrucción explícita de responder directamente. Si el reintento también falla, se devuelve su error sin bucles. `tests/test_regression_006_reintento_razonamiento.py` (8 casos).

### 3.10 · 🟠 Cobertura del validador de Fase 6 sobre corpus real (1.12)

- **Archivos**: `core/problem_solver/validator.py:665-680`, `core/problem_solver/code_corrector.py:82`
- **Acción**: medir cuántos planes de `logs/` con nombres de agente como variable global detecta, en vez de fiarse del caso probado a mano.

### 3.11 · ✅ CERRADO (2026-09-21) · `--timeout` no acota el comando completo (1.17)

- **Archivo**: `core/env_checker.py`
- **Acción original propuesta**: endpoint más ligero o `signal.alarm` global. **La propuesta era insuficiente**: ver abajo.
- **Resuelto**: el ping corre en un hilo del que se desiste al agotar el presupuesto (`_ping_http_deepseek` envuelve a `_ping_http_deepseek_interno`). Además se dejó de seguir redirecciones (cada salto es otra petición) y cada petición recibe solo el tiempo restante.
- **Dos hipótesis descartadas con medición**: (a) el `timeout` de `requests` no basta porque la **resolución DNS** queda fuera — `--timeout 2` tardaba 17-22 s; (b) `signal.setitimer` tampoco lo resuelve porque el handler de Python no corre mientras `getaddrinfo` bloquea en C — medido: 17 s. Tras el arreglo, el ping tarda **exactamente 2,00 s** con `--timeout 2`.
- **Test**: `tests/test_regression_004_check_env_timeout.py` (10 casos).

### 3.12 · ✅ CERRADO (2026-09-21) · `[bug]` El presupuesto compartido de `resolve` se reinicia en cada intento

- **Archivos**: `core/scheduler.py:232-233`, `core/budget_manager.py:162-164`, `main.py` (`_ejecutar_plan`)
- **Síntoma**: el límite de tiempo/llamadas/tokens de `resolve` no acota el total; cada intento arranca el presupuesto de cero.
- **Causa**: `set_contexto_plan_b()` (llamado una vez por intento) ejecuta `presupuesto.reset()`, que es alias de `iniciar()` y reinicia el reloj y el consumo.
- **Acción**: `reiniciar_presupuesto: bool = True` en `set_contexto_plan_b`; `_ejecutar_plan` pasa `False` con presupuesto compartido.
- **Criterio de cierre**: test que ejecuta dos intentos y verifica que el consumo se **suma**.
- **Detalle y evidencia**: ver §0.2.
- **Resuelto**: `reiniciar_presupuesto` en `set_contexto_plan_b` + `_ejecutar_plan` pasando `presupuesto is None`. Verificado que el test **discrimina**: con el código antiguo fallan 3 de 5; con el arreglo, 5 de 5. `tests/test_regression_008_presupuesto_compartido.py`.

---

## 4. Bloque 2 — Deuda técnica (backlog vivo)

### 4.1 · 🔴 Refactorizar `Scheduler._ejecutar_agente`

Bloque más grande sin dividir; ya declarado en `HARNESS_STATUS.md:186-187`.
Método: tests de caracterización **antes** de tocar (patrón ya usado en V4.0-5,
`tests/test_scheduler_plan_b_caracterizacion.py`).

### 4.2 · 🔴 Causa raíz de `registrar_ejecucion_en_aprendizaje`

`ui/simple_main_window.py:1085` rodea el síntoma con `SELECT MAX(id)`. Entender
**por qué** falla la función, no solo rodearla. Incluye limpiar el `logger.info`
duplicado de `:1088-1093`.

---

## 5. Bloque 3 — Problem Solver

- **3.1 Contrato de entrada/salida** — `StepPlan` no tiene `contrato_salida`; usar
  `configuracion["contrato_salida"]` = `{"tipo": "archivo", "obligatorio": true, "validacion": "exists"}`.
  Nota: la infraestructura de contratos de aceptación **ya existe**
  (`core/verification.py`, `validator.py:431-451`); el trabajo es extenderla al
  plan de fallback y a los pasos generados.
- **3.2 Validación real de resultados** — parcialmente cubierta por
  `core/verification.py` y la «FASE 4b» del Scheduler (`scheduler.py:558-593`),
  que comprueba el artefacto en disco, no lo que dice el ejecutor. El hueco es
  el plan de respaldo (3.1).
- **3.3 Mejorar el replanning** — base existente: `core/plan_recovery.py`,
  Plan B (`_intentar_plan_b`) y `core/recovery_manager.py` (7 códigos de parada
  dura). Falta Plan B → Plan C con análisis de causa.
- **3.4 Dependencias explícitas con paralelismo seguro** — base existente:
  `core/dependency_manager.py`. Falta expresar el grafo en abanico
  (`A ─┬→ C`, `B ─┘`).

---

## 6. Bloque 4 — Arquitectura

- **4.1 Separar Agent de Tool** — `AGENTE decide` / `TOOL hace`. Hoy
  `TipoAgente` + `Executor` mezclan ambas.
- **4.2 Tool Registry** — familias `filesystem`, `shell`, `git`, `browser`, `OCR`,
  `mouse`, `keyboard`, `HTTP`; extensible sin tocar el core.
- **4.3 Event Bus** — **base ya existente**: `core/event_bus.py` (con
  `LOG_ADVERTENCIA` y familia de eventos) y `core/log_bus.py`. El trabajo es
  ampliar el catálogo (`agent.*`, `tool.*`, `plan.*`, `session.*`), no crearlo.
- **4.4 Sesiones persistentes** — recuperar objetivo, plan, agentes, resultados,
  errores, reintentos y archivos modificados. Base: `core/execution_recorder.py`
  y las tablas `ejecuciones` / `agentes_ejecucion`.
- **4.5 Replay de ejecuciones** — depende de 4.3 + 4.4.

---

## 7. Bloque 5 — Quick wins de alto valor

| ID | Item | Estado |
|---|---|---|
| 5.1 | Exponer `resolve` en GUI o API web | **Abierto y confirmado** (`HARNESS_STATUS.md:179-180`): el bucle es puro y el ejecutor es una función, es trabajo de cableado. |
| 5.2 | Aplicar fix del fallback | **Vivo** → es 3.1 de este roadmap. |
| 5.3 | Revisar `_ultima_ejecucion_id` | **Vivo** → es 4.2. |
| 5.4 | Autenticación web por token | Abierto. `web`/`serve` sin auth: cualquiera en la red consume la API key y escribe ficheros. Fix: `--token` / `AGENTES_TOKEN`. |
| 5.5 | `--cancelar-al-timeout` | Parcialmente cubierto: existe `core/job_cancellation.py` (V3.8-3) para trabajos HTTP. Falta conectarlo al `504`. |
| 5.6 | `--timeout` cubre también la planificación | Abierto: hoy mide desde `scheduler.iniciar()`, no desde `solver.resolver_problema()`. |

---

## 8. Bloque 6 — Seguridad

- **6.1 Permisos** (`ALLOW`/`DENY`/`ASK`) antes de `rm`, `git reset`, `git push`,
  `sudo`, modificar archivos. Base: `core/executors/security.py` y el aviso del
  Shell; hoy **avisa**, no decide. `DECISIONES.md` ya fija que esto es condición
  para reabrir el agente de escritorio.
- **6.2 Sandbox real** — separar `workspace real` de `workspace de pruebas`
  (`core/sandbox.py`, `core/sandbox_contract.py`).

---

## 9. Bloque 7 — Tests

- **7.1 Unitarios**: `Database`, `Scheduler`, `Executors`, `Validator`, Problem
  Solver, `dependencies`, `retry`, `loops`. Cobertura base alta (1166 tests).
- **7.2 Integración**: `Problem Solver → Scheduler → FileExecutor → Validator`.
- **7.3 E2E**: crear/leer/copiar/mover/eliminar archivo, Python, Shell, HTTP,
  dependencias, loop, retry.
- **7.4 Regression suite**: `test_regression_001`, `002`, … **Prioridad**: los
  puntos 3.1–3.6 de este roadmap deben nacer cada uno con su test de regresión.

---

## 10. Bloque 8 — Observabilidad

Base ya existente: `core/budget_manager.py` (tiempo + llamadas + tokens + coste,
V3.8-2) y `core/execution_log.py`.
- **8.1 Execution Trace** por agente: inicio, fin, duración, tools, resultado, validación.
- **8.2 Cost tracking**: modelo, tokens in/out, tiempo, nº de llamadas, coste estimado.
- **8.3 Métricas/dashboard**: éxito %, fallos %, reintentos, tiempo medio, coste, tokens.

---

## 11. Bloque 9 — Nuevos agentes

- **9.1 Git Agent** — `status`, `diff`, `log`, `branch`, `commit`, `tag`, `checkout`.
  Protección: `commit → ALLOW`, `push → ASK`, `reset --hard → ASK`.
- **9.2 Browser Agent (Playwright)** — `abrir`, `buscar`, `click`, `fill`, `scroll`,
  `screenshot`, `descargar`, leer DOM, extraer texto. **Base ya existente**:
  `core/executors/browser_executor.py`, `core/browser_vision_loop.py`.
- **9.3 Desktop Agent** — `pantalla → visión/OCR → decisión → mouse → keyboard`.
  **Decisión tomada en `DECISIONES.md`: no ahora**, con 5 condiciones explícitas
  para reabrirlo y alternativa concreta (ampliar `BrowserVisionLoop`).
- **9.4 Vision/OCR Tool** — separar Vision Agent de OCR Tool. Base:
  `core/vision.py`.

---

## 12. Bloque 10 — Mantenimiento

- **10.1 Máximo ~500 líneas por archivo**, una responsabilidad principal por
  módulo. `core/scheduler.py` está en 1792 líneas y `main.py` en ~1450.
- **10.2 Tipado fuerte**: `TypedDict`, `dataclass`, `Enum`, `Protocol` alrededor de
  `Agent`, `Executor`, `Tool`, `Result`, `Plan`, `Step`, `Validation`, `Event`.
- **10.3 Eliminar duplicación**: `ruff` limpio en lo tocado; quedan 11 avisos
  **preexistentes** en `core/sandbox.py`, `tests/repro/*`,
  `tests/test_sandbox_contract.py` (`HARNESS_STATUS.md:158-159`).

---

## 13. Bloque 11 — Preparación para DeepSeek Harness

**No migrar todavía.** Frontera objetivo:

```
agentes_visuales/
├── core/{agents,tools,events,sessions,problem_solver}/
├── plugins/{browser,git,desktop,ocr,...}/
└── ui/
```

Ventaja competitiva a preservar (el resto conviene alinearlo con Harness):

- **Computer-use / Desktop Agent** (visión + OCR + mouse + teclado).
- **Problem Solver con validación real.**
- **Aprendizaje por refuerzo con `agent_history.db`.**

---

## 14. Plan de 7 horas **corregido**

El plan original gastaba la hora 1 en 6 puntos de los que **5 no están vivos**.
Reparto propuesto, por valor real:

| Hora | Foco | Items | Por qué |
|---|---|---|---|
| **1** | Aprendizaje + fallback | ~~3.2 (1.14)~~ ✅, 3.1 (1.2/1.10) | La degradación silenciosa del refuerzo ya está cerrada (§0.1); queda el fallback que miente. |
| **2** | Causa raíz del pipeline | 3.3 (1.7/1.8), 3.10 (1.12) | El placeholder y la extracción vacía comparten raíz. Prohibir fallbacks que inventan datos. |
| **3** | Quick wins visibles | ~~3.6 (1.16)~~ ✅, 3.5 (1.18), 5.1 (`resolve` en web) | El logger ya está silenciado (§0.1); quedan `BrokenPipeError` y exponer `resolve`, la pieza más visible que el usuario nunca ve. |
| **4** | Robustez | ~~3.7 (1.5)~~ ✅, ~~3.8 (1.3)~~ ✅, ~~3.9 (1.9)~~ ✅ | Hora completa: duración acumulativa, gate del corrector y reintento correctivo cerrados (§0.1). |
| **5** | Tools + Event Bus | 4.1, 4.2, 4.3 | Separar Agent/Tool, registry, ampliar el bus existente. |
| **6** | Sesiones + seguridad | 4.4, 4.5, 6.1, 6.2 | Sesiones persistentes, replay, permisos, sandbox. |
| **7** | Futuro | 10.1, 10.2, 11 | Dividir `scheduler.py`/`main.py`, tipado, mapa de plugin DSH. |

**Fuera del día** (anotado para no perderlo): agentes por texto libre,
clasificador de riesgo con datos reales (bloqueado por volumen de
`problema`/`exito_real`), corrector de código con IA, calibración de umbrales de
auto-crítica (0.4, tope 1), 1.13 (SIGSEGV) y 1.17.

---

## 15. Top 10 **corregido**

1. `py_compile` + tests verdes — **ya está**: 1180 passed. Mantenerlo como puerta.
2. ~~`EvaluadorLLM` con cliente real en las 3 rutas~~ ✅ **cerrado** (§0.1).
3. Problem Solver → acción real, no simulada (3.1).
4. Prohibir fallbacks que fabrican datos (3.3).
5. ~~`PythonCodeCorrector` con compile/test/rollback~~ ✅ **cerrado** (§0.1).
6. ~~`Scheduler` + duración acumulada en reintentos (3.7)~~ ✅ **cerrado** (§0.1).
7. Separar Agent de Tool + `ToolRegistry` (4.1, 4.2).
8. Ampliar el Event Bus y cerrar sesiones persistentes + replay (4.3–4.5).
9. Permisos + sandbox real (6.1, 6.2).
10. Preparar `agentes_visuales` como plugin/Tools de DSH (11).

---

## 16. Meta real de la semana

**No** que `agentes_visuales` tenga más agentes. **Sí** que su núcleo sea tan
limpio y modular que pueda evolucionar a ecosistema de agentes y conectarse a
DeepSeek Harness **sin reescribirlo entero**. DSH ya resuelve sessions, tools,
agent loop, subagents, jobs, workflow, sandbox, browser-use, computer-use y
plugins: conviene delegar o alinearse ahí, y concentrar el esfuerzo propio en
las tres ventajas diferenciales del §13.

---

## 17. Método de verificación reproducible

```bash
# 1. Sintaxis de todo el proyecto
python -m compileall -q core main.py ; echo "exit=$?"

# 2. Suite completa AISLADA (obligatorio: sin --forked hay SIGSEGV conocido)
tools/run_tests.sh

# 3. Auditoría de credenciales en ficheros rastreados
git ls-files | xargs grep -nI "sk-[A-Za-z0-9]\{16,\}"

# 4. Puntos concretos de este documento
grep -rn "contexto if contexto else" core/problem_solver/   # 1.3 / 1.10
grep -n "ruidoso" -A3 main.py                               # 1.16
grep -rn "EvaluadorLLM(\|obtener_learning_engine()" learning/ core/  # 1.14
grep -rn "BrokenPipeError" --include=*.py core/ main.py     # 1.18
```

**Regla de oro**: ningún punto de este roadmap se marca cerrado sin su
comando de verificación y, si es un bug, su `test_regression_NNN`.
