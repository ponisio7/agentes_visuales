# HARNESS_STATUS

- Rama: harness/v3.8.0 (base harness/v3.7.0)
- Inicio V3.8: 2026-09-20
- Informe del sprint V3.8: `HARNESS_REPORT_v3.8.0.md`
- Informe V3.7 (H1–H12): `HARNESS_REPORT_v3.7.0-H1-H12.md`
- Análisis previo: `HARNESS_ANALISIS_v3.7.0.md`

## En curso
- (ninguna)

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
- Pendiente deliberado: `_intentar_plan_b`/reintentos (lo más acoplado).
- Tests: `tests/test_scheduler_refactor.py`.

### Validación
- Suite completa aislada: **en verde** tras cada entregable (base 935).
- `ruff` limpio en todo lo tocado.

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
- Siguiente hito previsto: **V4.0 — modo «Resolver tarea»**.
