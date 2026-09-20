# ANÁLISIS ARQUITECTÓNICO — evolución a sistema que garantiza resultados

Rama: `harness/v3.7.0` · Fecha: 2026-09-20 · Base: lectura completa del código real.

> Fuente de verdad: el código. Donde el encargo y el código difieren, manda el código.

---

## ARQUITECTURA ACTUAL

### Puntos de entrada (`main.py`, 1055 líneas)
`main.py` es el entrypoint y la CLI: `GUI` (por defecto), `run`, `serve` (HTTP stdlib,
`POST /run` bloqueante), `web` (Flask, `POST /api/run` bloqueante vía `ColaTrabajos`),
`list-agents`, `--check-env`, `--version`. La versión sale de `pyproject.toml` (3.3.0).
El logging se configura en `_configurar_logging()` (basicConfig + RotatingFileHandler a
`logs/agentes_visuales.log`). Quedan `print()` directos en varios sitios.

### Planificación (`core/problem_solver/`)
`ProblemSolver.resolver_problema()` orquesta:
`PromptBuilder` (system/user prompt + contratos de salida) → parser JSON →
`FileNameNormalizer` → `PlanBuilder.construir_plan/generar_agentes` →
`PlanValidator.validar_plan` (BLOQUEANTE ⇒ regenera hasta 2 veces).
Modelos: `StepPlan`, `ExecutionPlan`, `ContratoAceptacion`.
`PlanValidator` ya rechaza estáticamente contratos de aceptación imposibles.

### Ejecución (`core/scheduler.py`, 1809 líneas · `core/executors/`)
`Scheduler` (QObject, DAG, `ThreadPoolExecutor`, máquina de estados `EstadoAgente`).
Tipos: `Python, Shell, HTTP, LLM, File, Loop, Browser, Search`. `core/sandbox.py`
**no es un sandbox real** (docstring propio); `core/sandbox_contract.py` define el
contrato de datos e imágenes (`formato_imagen_real`, `preparar_imagen`).

### Verificación (H6 — ya implementado en esta rama)
`core/verification.py` (determinista, disco/bytes) + `ContratoAceptacion` +
gate en `Scheduler._ejecutar_agente` + `Scheduler.obtener_resultado_aceptacion()` +
etiqueta honesta en `core/execution_recorder.py` + migración 8→9 (`estado='fallida'`
cuando `errores>0`). `main._ejecutar_pipeline` devuelve `ok/estado/aceptacion`.

### Recuperación (`core/plan_recovery.py`, 749 líneas)
`PlanRecovery.generar_plan_b()` construye `PROMPT_PLAN_B` (con lecciones), pide JSON al
LLM, construye/valida el plan y reintenta UNA vez con corrección. Límite rígido
`Scheduler._max_intentos_plan_b = 2`. No hay anti-repetición ni escalera de estrategias.
`reparaciones_plan` existe en `learning/schema.py` pero está **vacía y sin uso**.

### Aprendizaje (`learning/`)
`engine.py` (singleton, 275 líneas): `reentrenar_desde_historial`, `predecir_riesgo_agente`,
`registrar_resultado_agente/plan`, `obtener_lecciones_para_prompt`. `reward_llm.EvaluadorLLM`
puntúa en background. `embedding_matcher` (MiniLM multilingüe, umbral **0.68**).
`prompt_ab_evaluator` (estados candidato/activo, `firma` semántica agresiva).
`feedback_processor` reescribe prompts. **No existe** retrieval de casos similares.

### Persistencia (`storage/database.py`, 2090 líneas · `storage/config_manager.py`)
`SCHEMA_DEFINITION` + `_migrar_db()` con versión explícita (**DB_VERSION = 9**).
`ejecuciones` NO tiene columna `problema`: el problema no se persiste como entidad.
`learning/schema.py` añade tablas de aprendizaje (`evaluaciones_llm`, `feedback_usuario`,
`prompts_reescritos`, `prompt_reescrito_usos`, `reparaciones_plan`, `modelos_entrenados`).

### API / GUI / herramientas
`web/app.py` (Flask + `ColaTrabajos`, Qt-safe) y `main._ejecutar_serve` (stdlib).
`ui/simple_main_window.py` (1168 líneas, terminal verde) + `ui/admin_panel.py`.
`tools/enviar_tarea_subproceso.py`: serie de tareas por subproceso, **sin
`--continue-on-error`** y sin reportar verificación/artefactos.

### Configuración de IA (H1)
`core/llm_client.py`: key desde arg > `DEEPSEEK_API_KEY` > archivo
(`~/.config/agentes_visuales/env`, `~/.config/deepseek.env`, `~/.deepseek_key`).
`_VARIABLES_SOPORTADAS` **no incluye `DEEPSEEK_MODEL`**; el modelo por defecto es
constante (`deepseek-v4-pro`). No hay escritor de config ni `reset_llm_client_compartido()`.

---

## CAMBIOS NECESARIOS

| Fase | Cambio | Archivos |
|------|--------|----------|
| H1 | `DEEPSEEK_MODEL` en el env; escritor de config 700/600; `reset_llm_client_compartido()`; diálogo GUI `⚙ Configuración`; respetar el modelo en `builder._kwargs_llm` | `core/llm_client.py`, `ui/config_dialog.py` (nuevo), `ui/simple_main_window.py`, `core/problem_solver/builder.py`, `core/problem_solver/constants.py` |
| H2 | Umbral conservador configurable + compatibilidad de intención + no aplicar en duda + motivo registrado | `learning/embedding_matcher.py`, `core/problem_solver/builder.py` |
| H3 | Declarar `sentence-transformers`/`torch`; documentar/aislar el SIGSEGV; verificar `ddgs` | `requirements.txt`, `pytest.ini`, docs |
| H5 | Migración 9→10: `ejecuciones.problema`, `aceptada`, `motivo_fallo`; persistir problema/plan/resultado/estado | `storage/database.py`, `core/execution_recorder.py`, `main.py` |
| H6 | Refinar: `VerificationEngine`/`VerificationResult` con advertencias y evidencias; exponer en API | `core/verification.py`, `main.py` |
| H10 | `--continue-on-error`, resumen por tarea (estado/artefactos/verificación) | `tools/enviar_tarea_subproceso.py` |
| H7 | Plan B configurable (intentos/tiempo), firma de plan anti-repetición, escalera de estrategias, reutilizar agentes válidos, usar `reparaciones_plan` | `core/plan_recovery.py`, `core/scheduler.py`, `learning/*` |
| H8 | `obtener_casos_similares` con `EmbeddingMatcher` (umbral 0.85, 1-3 casos) sobre ejecuciones exitosas; inyectar en prompt | `learning/engine.py`, `core/problem_solver/solver.py`, `learning/schema.py` |
| H4 | `core/log_bus.py` (handler + deque acotado); terminal/GUI/web/archivo; `GET /api/logs` | `core/log_bus.py` (nuevo), `main.py`, `ui/simple_main_window.py`, `web/app.py` |
| H9 | `POST /jobs`, `GET /jobs/<id>`, `GET /jobs/<id>/logs` (+cancel) sin romper `/run` | `web/app.py`, `main.py` |
| H11 | Fase 1: visión de página con Playwright + mensajes multimodales en `LLMClient`; escritorio opt-in con allowlist/kill switch | `core/llm_client.py`, `core/executors/browser_executor.py` |
| H12 | Arquitectura para `DEEPSEEK_BASE_URL` local; separar inferencia/dataset/runtime; **sin entrenamiento en el núcleo** | docs + `tools/` |

---

## RIESGOS

1. **Scheduler**: concurrencia y Plan B ya delicados (locks, `_detener`, señales Qt).
   Toda mutación debe respetar la máquina de estados y no bloquear la UI.
2. **GUI**: PyQt6 solo en runtime; los tests no deben instanciar diálogos reales.
3. **SQLite**: migraciones idempotentes y compatibles; nunca borrar histórico.
4. **A/B matcher**: un umbral mal calibrado desactiva el aprendizaje en silencio
   (fallback obligatorio al prompt original).
5. **SIGSEGV** intermitente de CPython 3.13 (fork+hilos en `core/sandbox.py`):
   pre-existente. Aislar sin ocultar fallos (p. ej. `--forked` documentado).
6. **Tamaño**: `scheduler.py` (1809), `database.py` (2090), `simple_main_window.py`
   (1168) superan 500 líneas. La lógica nueva va a módulos nuevos; no se divide
   artificialmente lo existente.

---

## DEPENDENCIAS ENTRE FASES

- H8 ← H5 (problema+éxito persistidos) y H6 (éxito fiable).
- H10 ← H6 (verificación en el JSON) y H9/H5 para `ejecucion_id`.
- H7 ← H6 (motivo estructurado) y `reparaciones_plan`.
- H9 ← H4 (log bus) para `GET /jobs/<id>/logs`.
- H1 condiciona el modelo usado por `builder` (H2/H8 no dependen de H1).

## ORDEN DE IMPLEMENTACIÓN

`H1 → H2 → H3 → H5 → H6(refinar) → H10 → H7 → H8 → H4 → H9 → H11(fase 1) → H12(prep)`

Tras cada fase: tests, regresiones, commit atómico y repositorio funcionando.
