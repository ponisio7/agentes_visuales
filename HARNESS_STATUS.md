# HARNESS_STATUS

- Rama: harness/v3.8.0 (base harness/v3.7.0)
- Inicio V3.8: 2026-09-20
- Informe del sprint V3.8: `HARNESS_REPORT_v3.8.0.md`
- Informe V3.7 (H1–H12): `HARNESS_REPORT_v3.7.0-H1-H12.md`
- Análisis previo: `HARNESS_ANALISIS_v3.7.0.md`
- Informe V4.0-AB: `HARNESS_REPORT_v4.0.0-AB.md`
- Informe V4.0 «Resolver tarea»: `HARNESS_REPORT_v4.0.0-resolver.md`
- Informe V4.0 segunda tanda (5, 3, 4, 6): `HARNESS_REPORT_v4.0.0-tanda2.md`

## En curso
- (ninguna)

## Completadas — V4.0 segunda tanda: puntos 5, 3, 4 y 6 ✅

Orden aplicado: **5 → 3 → 4 → 6** (estabilizar el Scheduler antes de que
`GoalResolver` multiplique sus ejecuciones).

### V4.0-5 — Refactor de `_intentar_plan_b` ✅
- Tests de **caracterización ANTES** de tocar: `tests/test_scheduler_plan_b_caracterizacion.py`
  (9 casos) en verde contra el código original, sin modificarlos después.
- `_intentar_plan_b`: 222 → **73 líneas** (orquestador) + 10 helpers `_pb_*`.
  `core/scheduler.py`: 1738 → 1792 (crece por la documentación de cada fase).
- 123 tests de scheduler/Plan B en verde.

### V4.0-3 — Clasificador de fallo de plan ✅
- `learning/plan_failure_classifier.py`: regresión logística sobre el histórico
  (`ejecuciones` + `agentes_ejecucion`), features **solo pre-ejecución**,
  métricas fuera de muestra y gating honesto si faltan datos.
- Medición real (473 ejecuciones): AUC **0.645**, **lift 2.65×** en el decil de
  mayor riesgo, precisión/cobertura con umbral 0.52/0.14.
- Se usa como **observabilidad** (traza de `GoalResolver`), **no** como puerta:
  con umbral apenas iguala al trivial.
- CLI: `python -m learning.plan_failure_classifier entrenar|estado`.
- Prerrequisito pendiente: `exito_real` está a NULL en las 473 filas porque solo
  4 tienen `problema`; la etiqueta actual es la operativa (documentado).

### V4.0-4 — Ciclo de auto-crítica ✅
- `learning/self_critique.py`: la evaluación del LLM dispara reescrituras.
- Un solo camino: `FeedbackProcessor.procesar_critica` (feedback humano y
  auto-crítica). Cubre agente y plan.
- Cuatro frenos: umbral 0.4, presupuesto, tope 1 por ejecución y deduplicación
  por firma con candidato pendiente. Opt-out `AGENTES_AUTOCRITICA=0`.
- Arreglos necesarios: atribución real de `agente_ejecucion_id` (antes siempre
  0) y marcador `alcance='auto_critica'` que impide que las críticas contaminen
  `exito_real` (test que demuestra la mezcla de escalas 0–1 vs −1–1).

### V4.0-6 — Agente de escritorio: decisión tomada ✅
- En `DECISIONES.md`: **no ahora**, con 5 condiciones explícitas para reabrirlo
  y alternativa concreta (ampliar `BrowserVisionLoop`).
- Base: el Shell solo **avisa** de comandos peligrosos y el allowlist de visión
  es de navegador; el escritorio cambia la naturaleza de la superficie.

## Completadas — V4.0 «Resolver tarea» ✅

Punto 2 del roadmap V4.0: el sistema toma un **objetivo de alto nivel** y
persigue el resultado, en vez de un único tiro.

- `core/goal_resolver.py`: bucle
  `planificar → ¿verificable? → ejecutar → verificar → re-planificar con el
  motivo`. Módulo **puro** (sin Qt): planificador y ejecutor se inyectan.
- **El sistema decide la verificación**: si el paso final/crítico no declara
  `aceptacion`, el plan **no se ejecuta** y vuelve al planificador con el
  motivo. `--sin-exigir-verificacion` lo desactiva.
- Reutiliza lo cerrado en v3.7–v3.8: verificación + gate (H6/V3.8-6), Plan B
  (H7), presupuesto compartido por intentos (V3.8-2) y retrieval de casos
  exitosos + lecciones (H8), que ya inyecta `ProblemSolver`.
- Para de forma honesta: `verificado` | `intentos_agotados` |
  `presupuesto_agotado`, con motivo y consumo.
- `main.py`: `_ejecutar_plan` extraído de `_ejecutar_pipeline` (mismo pipeline,
  mismo Plan B, mismo gate) y nuevo subcomando **`resolve OBJETIVO`**.
  `run`, `serve` y `web` no cambian de contrato.
- Tests: `tests/test_goal_resolver.py` (21) y `tests/test_main_resolve.py` (12).
- `ruff` limpio en todo lo tocado.

## Completadas — V4.0-AB «Cerrar el bug de promoción A/B» ✅

Punto 1 del roadmap V4.0: la capa de auto-mejora **no promovía nunca**
(`activo=0` en la BD). Cuatro defectos encadenados, todos cerrados:

- **Brazo de control inexistente**: `elegir_variante` usaba el candidato al
  100 % cuando no había `activo`. Ahora el incumbente es el `activo` o, si no
  lo hay, el prompt **original** (exploración 20 %).
- **Referencia inválida**: se comparaba contra `_score_global()` (media de todo
  lo registrado, incluido huérfano). Ahora: `activo` → **baseline de la firma**
  (tabla nueva `prompt_reescrito_baseline`) → `espera`. Nunca sin control.
- **El `activo` se destruía** al crear un candidato (`_guardar_reescritura`).
  Ahora solo se superan los `candidato` previos.
- **Huérfanas**: 2679/2691 usos apuntaban a versiones inexistentes (FK OFF en
  conexiones auxiliares). Ahora `JOIN` obligatorio, `foreign_keys=ON` y
  `PromptABEvaluator.limpiar_huerfanos()`.

Efecto medible (copia de la BD real, firma `73f260b440`): candidato 223 con
media 0.65 vs original 0.438. Regla antigua (global 0.616) ⇒ delta +0.034 ⇒
espera eterna; regla nueva ⇒ delta +0.212 ⇒ **promovido** (primera fila
`activo` de la BD).

- Tests: `tests/test_ab_promocion_baseline.py` (13 casos) + A/B existentes sin
  cambios de expectativa.
- `ruff` limpio en todo lo tocado.

## Completadas — V3.8 «Orquestador autónomo controlado»

### V3.8-1 — Recovery inteligente y paradas duras ✅
- `core/recovery_manager.py`: `RecoveryManager`, `RecoveryDecision`,
  `clasificar_fallo` y los 7 códigos de parada dura (`API_KEY_MISSING`,
  `DEPENDENCY_MISSING`, `INVALID_PROBLEM`, `SECURITY_BLOCK`,
  `INVALID_CONTRACT`, `BUDGET_EXCEEDED`, `TIMEOUT_GLOBAL`).
- El Scheduler decide **antes** de gastar Plan B; el fallo de aceptación en
  runtime nunca es parada dura; la parada se expone en la aceptación y se
  audita en `reparaciones_plan`.
- Tests: `tests/test_recovery_manager.py`, `tests/test_scheduler_paradas_duras.py`.

### V3.8-2 — BudgetManager (tiempo + llamadas + tokens + coste) ✅
- `core/budget_manager.py`: límites opt-in por entorno, contabilidad vía el
  punto único de salida del LLM (`agregar_observador_llamada`).
- Presupuesto agotado ⇒ parada dura `BUDGET_EXCEEDED`. El consumo se expone en
  la aceptación y se persiste en `ejecuciones` (llamadas/tokens/coste).
- Tests: `tests/test_budget_manager.py`.

### V3.8-3 — Cancelación real de jobs ✅
- `core/job_cancellation.py`: registro thread-safe; el hilo HTTP deja una
  solicitud y el latido del pipeline (hilo de Qt) llama a `detener()`.
- `ColaTrabajos` pasa el `job_id`, marca `cancelled` y el pipeline devuelve
  `cancelado`.
- Tests: `tests/test_job_cancellation.py`.

### V3.8-4 — Reindexado histórico y éxito real ✅
- `learning/historical_indexer.py`: `HistoricalIndexer`, `CaseRecord`, CLI
  `python -m learning.historical_indexer` (embeddings + éxito real compuesto).
- Migración 12→13: `exito_real`, `exito_real_motivo`, `indexado_fecha`,
  `llamadas_llm`, `tokens_total`, `coste`.
- El retrieval prefiere `exito_real`; las antiguas «aceptadas por defecto» con
  errores dejan de colarse como éxitos.
- Tests: `tests/test_historical_indexer.py`.

### V3.8-5 — BrowserVisionLoop ✅
- `core/browser_vision_loop.py`: ciclo captura → VLM → acción → nueva captura
  con límites (20 pasos / 120 s / 30 capturas / 3 fallos) y traza
  `{accion, objetivo, razon, evidencia}`.
- `core/vision.py` pide `objetivo`/`evidencia`; el bucle es una acción opt-in
  del Browser (`{"tipo": "vision"}`), fuera del vocabulario del VLM.
- Tests: `tests/test_browser_vision_loop.py`.

### V3.8-6 — Refactor del Scheduler ✅
- Extraídos `core/acceptance_manager.py`, `core/dependency_manager.py` y
  `core/execution_log.py`; el Scheduler delega manteniendo su API pública.
- `core/scheduler.py`: 2094 → **1738 líneas**.
- Pendiente deliberado: `_intentar_plan_b`/reintentos (lo más acoplado) →
  **cerrado en V4.0-5**.
- Tests: `tests/test_scheduler_refactor.py`.

### Validación
- Suite completa aislada: **en verde** tras cada entregable. Base 935 en V3.8;
  **1166 passed, 1 skipped** tras la segunda tanda de V4.0 (+51 tests de los
  puntos 5, 3, 4 y 6).
- `ruff` limpio en todo lo tocado. Quedan 11 avisos **preexistentes** en ficheros
  no tocados (`core/sandbox.py`, `tests/repro/*`, `tests/test_sandbox_contract.py`).

## Histórico — V3.7.0 (H1–H12)
Completadas H1–H12 (config, matcher A/B, deps, log bus, persistencia,
verificación, Plan B adaptativo, retrieval, API de trabajos, visión fase 1 y
preparación del VLM local). Detalle en `HARNESS_REPORT_v3.7.0-H1-H12.md`.

## Bloqueadas
- (ninguna)

## Notas
- La suite completa hereda un `SIGSEGV` intermitente de CPython 3.13 en el
  sandbox (fork + hilos). No es de las fases: se ejecuta con
  `tools/run_tests.sh` (aislamiento por proceso, sin desactivar pruebas).
- Sin cambios en códigos de salida ni en el contrato de `run`.
- V4.0-AB no requiere migración numerada: `prompt_reescrito_baseline` se crea
  con `CREATE TABLE IF NOT EXISTS` al abrir la BD (sigue en versión 13).
- La BD de producción **no** se ha limpiado: quedan 2679 usos huérfanos. El
  código ya los ignora; borrarlos es una decisión aparte
  (`PromptABEvaluator.limpiar_huerfanos`).
- `resolve` no está expuesto todavía en web/API ni en la GUI (el bucle es puro y
  el ejecutor es una función, así que es trabajo de cableado).
- **Datos pendientes para exprimir el clasificador**: `exito_real` está a NULL en
  las 473 ejecuciones porque solo 4 tienen `problema` (la columna llegó en H5).
  Las ejecuciones nuevas ya guardan `problema` + `plan_json`; cuando haya
  volumen, `python -m learning.plan_failure_classifier entrenar` dará métricas
  mejores y se podrá plantear usarlo como puerta (hoy es solo observabilidad).
- **Deuda declarada que sigue abierta**: `_ejecutar_agente` (Scheduler) no se ha
  tocado; el refactor V4.0-5 se limitó a `_intentar_plan_b`.
- Roadmap V4.0: **puntos 1–6 cerrados** (1 y 2 en los informes previos, 3–6 en
  `HARNESS_REPORT_v4.0.0-tanda2.md`).
- Siguiente hito posible: exponer `resolve` en web/API, o volver a la GUI/CLI
  para el modo «Resolver tarea».
