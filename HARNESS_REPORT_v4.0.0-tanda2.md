# HARNESS_REPORT v4.0.0 — Segunda tanda: puntos 5, 3, 4 y 6

- Fecha: 2026-09-20
- Base: v3.8.0 + V4.0-AB + V4.0 «Resolver tarea»
- Alcance: los cuatro puntos restantes del roadmap, **en el orden 5 → 3 → 4 → 6**
  (primero estabilizar el Scheduler, porque `GoalResolver` multiplica sus
  ejecuciones; luego construir encima).
- Estado: **entregados**, con 71 tests nuevos entre los cuatro.

---

## 5. Refactor de `Scheduler._intentar_plan_b` (primero)

### Método

Tests de **caracterización antes de tocar nada**
(`tests/test_scheduler_plan_b_caracterizacion.py`, 9 casos), en verde contra el
código original. Fijan lo que los tests previos no cubrían:

- guard de `_detenido` (no llama al LLM ni consume intento);
- camino «sin plan»: bloqueo de **todos** los no terminales, respetando los que
  están corriendo, y notificación de término;
- limpieza completa de estado en el éxito (agentes, completed, running,
  cancelados, loops, verificaciones, aceptación, flag de notificado);
- política de reutilización: **solo** artefactos verificados y aceptados (un
  rechazado o sin resultado NO se reutiliza);
- excepción del recovery: libera el turno, consume el intento y devuelve False;
- contabilidad exacta: estrategia escalonada, traza del intento y firma del plan
  fallido en el conjunto anti-repetición;
- qué recibe `recovery.generar_plan_b` (kwarg a kwarg).

Después, extracción mecánica **sin cambiar comportamiento**:

| | antes | después |
|---|---|---|
| `_intentar_plan_b` | 222 líneas | **73** (orquestador) |
| helpers `_pb_*` | — | 10 métodos, 209 líneas con docstring |
| `core/scheduler.py` | 1738 | 1792 |

El fichero crece porque se documenta cada fase; el método pasa a leerse de un
vistazo. **Los 9 tests de caracterización no se modificaron ni una línea** entre
antes y después, y los 123 tests de scheduler/Plan B siguen verdes.

---

## 3. Clasificador de fallo de plan (`learning/plan_failure_classifier.py`)

Predice la probabilidad de que un plan **falle antes de ejecutarlo**, con
scikit-learn (regresión logística + escalado), entrenado sobre el histórico real
(`ejecuciones` + `agentes_ejecucion`).

- **Features solo pre-ejecución**: nº de pasos, reparto por tipo, raíces, hojas,
  fan-in/fan-out, profundidad del DAG, tipos distintos, ratio con dependencias.
  `estado`, `duracion`, `resultado` y `error` de los agentes **no** se usan: son
  posteriores y filtrarían la etiqueta (hay un test que lo fija).
- **La misma extracción** sirve para un `ExecutionPlan` nuevo y para el
  histórico, así que train y serve no pueden desalinearse.
- **Etiqueta honesta y explícita**: `exito_real` si está calculado; si no, una
  etiqueta *operativa* derivada de las mismas columnas duras que usa
  `HistoricalIndexer.calcular_exito_real` **sin exigir `problema`**.
- Sin datos suficientes **no entrena** y lo dice; la predicción es neutra con
  `confianza='sin_datos'`.
- Métricas **fuera de muestra** (`cross_val_predict`), no de ajuste.

### Medición sobre el histórico real (473 ejecuciones)

| Métrica | Valor |
|---|---|
| Muestras etiquetadas | 473 (397 éxito / 76 fallo) |
| AUC | **0.645** |
| Acierto con umbral 0.5 | 0.841 (línea base mayoritaria: 0.839) |
| Precisión / cobertura de fallo | 0.52 / 0.14 |
| **Lift del decil de mayor riesgo** | **2.65×** |

**Lectura honesta**: como *clasificador con umbral* es malo (apenas iguala al
trivial), pero como **score de riesgo que ordena** es útil: el 10 % de planes
con mayor riesgo concentra 2.65 veces más fallos que la media. Se usa por tanto
como **observabilidad** (se registra en la traza de cada intento de
`GoalResolver` y en el log), **no** como puerta ni disparador de re-planificación.

### Un error mío que conviene dejar escrito

La primera medición dio un lift de 0.96 («no accionable»). Era **falso**: estaba
tomando `predict_proba[:, 1]` como P(fallo) cuando la clase 1 era «éxito». Lo
detecté con un dataset sintético separable (`n_loop` → fallo) en el que el
modelo ordenaba **al revés**. Corregido el sentido de la clase —y con él todas
las métricas, que ahora hablan del mismo suceso— el lift real es 2.65. Sin el
test sintético habría reportado una conclusión falsa y negativa.

### Prerrequisito de datos, dicho claro

`exito_real` está a **NULL en las 473 ejecuciones** porque `calcular_exito_real`
exige `problema` y solo **4** ejecuciones lo tienen guardado (la columna llegó en
H5; antes no se persistía). Por eso hoy la etiqueta es la operativa. El
clasificador mejorará cuando el histórico acumule `problema` + `plan_json`
(ya se guardan en cada ejecución nueva) y se reindexe. Está a un
`python -m learning.plan_failure_classifier entrenar` de distancia.

### CLI

```bash
python -m learning.plan_failure_classifier entrenar --db agent_history.db --json
python -m learning.plan_failure_classifier estado --json
```

---

## 4. Ciclo de auto-crítica del LLM (`learning/self_critique.py`)

El hueco: el `EvaluadorLLM` ya puntuaba cada resultado después de ejecutarlo y
ahí se acababa todo; la única vía para reescribir un prompt era el feedback del
usuario. Ahora la evaluación del LLM **dispara** reescrituras.

- **Un solo camino de reescritura**: `FeedbackProcessor.procesar_critica(...)`
  es el destino común del feedback humano y de la auto-crítica.
  `procesar_feedback` pasa a ser una puerta sobre él.
- **`SelfCritic.revisar_ejecucion(id)`**: lee `evaluaciones_llm`, selecciona las
  que merecen crítica (score < 0.4 y con justificación) y crea el candidato con
  la justificación del evaluador como comentario. Cubre **agente y plan**.
- **Cuatro frenos** (porque reescribir sin control degrada en silencio):
  umbral de score, presupuesto (`BudgetManager` agotado = no se gasta la
  llamada), tope por ejecución (1) y **deduplicación**: si esa firma ya tiene un
  candidato esperando al A/B, no se gasta otra llamada ni se pisa el que se está
  midiendo.
- Interruptor `AGENTES_AUTOCRITICA=0` para desactivarlo.
- Se engancha en el hilo de aprendizaje de `execution_recorder`, best-effort.

### Dos arreglos que hicieron falta para que esto fuera correcto

1. **Atribución**: `execution_recorder` guardaba **siempre**
   `agente_ejecucion_id=0`, así que la evaluación de un agente no se podía
   atribuir a ningún agente concreto y la crítica acababa reescribiendo el
   prompt del *último* LLM de la ejecución. Ahora se resuelve la fila real.
2. **Contaminación de `exito_real`**: las críticas se auditan en
   `feedback_usuario` (es el destino de la FK de `prompts_reescritos`) con el
   marcador `alcance='auto_critica'`, que el `HistoricalIndexer` **ignora**
   porque solo lee `alcance='plan'`. El aislamiento no es cosmético y hay un
   test que lo demuestra: el evaluador puntúa 0–1 (0.6 = mediocre) y el usuario
   −1–1 (0.6 = claramente positivo), así que una crítica colada como feedback
   humano convertiría una ejecución **fallida** en «éxito» del corpus.

---

## 6. Agente de escritorio V4.1/V4.2 — decisión tomada

Documentada en **`DECISIONES.md`**: **no se construye ahora**, con condiciones
explícitas para reabrirlo. El razonamiento se apoya en el código real, no en
generalidades:

- El executor Shell **solo avisa** de comandos peligrosos
  (`DANGEROUS_SHELL_COMMANDS` → `logger.warning`, no bloquea) y el sandbox
  protege código Python. Un agente con ratón y teclado **no pasa por ninguno de
  los dos**: puede abrir un terminal y teclear el comando.
- El allowlist de `core/vision.py` es de **navegador**; el mismo `click` que
  dentro de una pestaña es inocuo puede ser «Aceptar» en un diálogo de sudo.
- No hay confirmación humana en el bucle y el pipeline es autónomo por diseño.

Cinco condiciones para reabrirlo (allowlist de apps con denylist dura,
confirmación por acción irreversible, kill switch de sesión con límites duros,
auditoría con captura antes/después, y modo de solo lectura primero) y una
alternativa concreta mientras tanto: ampliar `BrowserVisionLoop`, que ya tiene
allowlist, límites y kill switch.

---

## Ficheros

| Fichero | Cambio |
|---|---|
| `core/scheduler.py` | `_intentar_plan_b` extraído en orquestador + 10 `_pb_*` |
| `tests/test_scheduler_plan_b_caracterizacion.py` | **nuevo**: 9 tests de caracterización |
| `learning/plan_failure_classifier.py` | **nuevo**: features, etiqueta, modelo, métricas, CLI |
| `tests/test_plan_failure_classifier.py` | **nuevo**: 25 tests |
| `learning/self_critique.py` | **nuevo**: `SelfCritic`, frenos, auditoría |
| `tests/test_self_critique.py` | **nuevo**: 14 tests |
| `learning/feedback_processor.py` | `procesar_critica` unificado + `_prompt_crudo` |
| `core/execution_recorder.py` | atribución real del agente + enganche de la auto-crítica |
| `core/goal_resolver.py` | `predictor` opcional y `riesgo_fallo` en la traza |
| `main.py` | `_cargar_predictor_riesgo` conectado a `resolve` |
| `DECISIONES.md` | decisión del agente de escritorio |
| `run_all_tests.py` | los 3 ficheros nuevos entran en los grupos |

## Validación

- **Suite completa aislada: 1166 passed, 1 skipped, exit 0**
  (1115 antes de esta tanda + 51 tests nuevos).
- `ruff check` limpio en **todos los ficheros tocados**. Los 11 avisos que
  quedan en el árbol son preexistentes y están en ficheros que no se han tocado
  (`core/sandbox.py`, `tests/repro/*`, `tests/test_sandbox_contract.py`).
- Los tests nuevos no tocan red ni descargan modelos (hay un fixture que anula
  el matcher de embeddings).

## Límites honestos

- **El clasificador no gatea nada.** Su precisión/cobertura con umbral son malas;
  usarlo para decidir re-planificar gastaría presupuesto con poco criterio. Es
  observabilidad hasta que el histórico tenga `problema`/`exito_real`.
- **La auto-crítica tiene un tope de 1 reescritura por ejecución** y umbral 0.4:
  valores elegidos a ojo, no calibrados con datos. Son constantes fáciles de
  ajustar, pero hoy no están justificadas empíricamente.
- **La auto-crítica sigue atribuyendo por `orden DESC`** cuando la evaluación es
  de alcance `plan`: es la limitación que ya existía con el feedback de usuario.
- **El refactor del Scheduler no reduce líneas**, reduce acoplamiento. El
  fichero sigue teniendo 1792 líneas: quedan piezas grandes (`_ejecutar_agente`)
  que no se han tocado.
- **La decisión del punto 6 es un aplazamiento, no un cierre.** Las condiciones
  están escritas para que la próxima vez la respuesta no dependa de quién
  pregunte.
