# HARNESS_REPORT — v3.7.0 · H1–H12

Rama `harness/v3.7.0` · Evolución de «orquestador que ejecuta» a **sistema que
garantiza resultados verificables**.

Base del análisis: `HARNESS_ANALISIS_v3.7.0.md`. Orden ejecutado:
`H1 → H2 → H3 → H5 → H6 → H10 → H7 → H8 → H4 → H9 → H11 → H12`.

> Regla de oro que gobierna todo el trabajo: *cuando agentes_visuales dice que
> una tarea está resuelta, existe evidencia verificable de que el resultado
> cumple el contrato solicitado.*

---

## IMPLEMENTADO

| Fase | Estado | Resumen |
|------|--------|---------|
| **H1** Config API/modelo | ✅ | `core/ia_config.py`, diálogo `⚙ Configuración`, `DEEPSEEK_MODEL`, `reset_llm_client_compartido()`, modelo respetado por el builder |
| **H2** Matcher A/B | ✅ | Umbral 0.85 + compatibilidad de intención (firma/solape) + margen de duda + motivo registrado |
| **H3** Dependencias/estabilidad | ✅ | `sentence-transformers`/`torch`/`pypdf`/`ddgs` declarados; verificador de deps; suite aislada `--forked` documentada |
| **H5** Persistir problema | ✅ | Migración 10→11: `problema`, `plan_json`, `resultado`, `aceptada`, `motivo_fallo`, embeddings del problema |
| **H6** VerificationEngine | ✅ | `core/verification.py` + `VerificationResult` + criterios (incl. directorio y contenedor) + gate real + evidencia en la API |
| **H10** Cliente de tareas | ✅ | Informe por tarea con verificación/artefactos y `--continue-on-error` |
| **H7** Plan B adaptativo | ✅ | Límites configurables, escalera de estrategias, anti-repetición por firma, reutilización de agentes válidos, `reparaciones_plan` en uso (migración 11→12) |
| **H8** Retrieval de casos | ✅ | `obtener_casos_similares` sobre ejecuciones con `aceptada=1`, umbral 0.85, 1–3 casos, inyección con aviso de no copiar |
| **H4** Log bus | ✅ | `core/log_bus.py` (deque acotado + handler + cursor), GUI por QTimer, `GET /api/logs` y `GET /logs` |
| **H9** API de jobs | ✅ | `POST/GET /api/jobs`, SSE `/api/jobs/<id>/logs`, cancel; `/run` intacto; paridad en `serve` (polling) |
| **H11** Visión | ⚠️ | **Fase 1**: cliente multimodal + decisión validada por allowlist + kill switch. **No** hay bucle autónomo de acciones ni agente de escritorio (por diseño) |
| **H12** VLM local | ⚠️ | **Preparación**: `DEEPSEEK_BASE_URL` ya soportado, cliente multimodal compatible y herramienta de comprobación. **No** hay entrenamiento (fuera del núcleo, por diseño) |

---

## ARCHIVOS NUEVOS

- `core/ia_config.py` — archivo de secretos 700/600, modelos, prueba de conexión.
- `core/verification.py` — verificador determinista + `VerificationEngine`.
- `core/vision.py` — captura → decisión multimodal validada (H11 fase 1).
- `core/log_bus.py` — bus de logs con cursor.
- `web/jobs.py` — gestor de trabajos no bloqueante.
- `ui/config_dialog.py` — diálogo de configuración.
- `tools/verificar_dependencias.py`, `tools/run_tests.sh`,
  `tools/comprobar_servidor_local.py`.
- `HARNESS_ANALISIS_v3.7.0.md`, `HARNESS_VLM_LOCAL.md`, este informe.
- Tests: `test_verification.py`, `test_aceptacion_plan.py`,
  `test_scheduler_aceptacion.py`, `test_flujo_e2e_aceptacion.py`,
  `test_ia_config.py`, `test_ab_matcher.py`, `test_dependencias.py`,
  `test_tarea_serie.py`, `test_plan_b_adaptativo.py`, `test_retrieval_casos.py`,
  `test_log_bus.py`, `test_jobs_api.py`, `test_vision.py`.

## ARCHIVOS MODIFICADOS (principales)

`core/llm_client.py`, `core/problem_solver/{models,builder,validator,prompt_builder,solver}.py`,
`core/agent.py`, `core/scheduler.py`, `core/plan_recovery.py`,
`core/execution_recorder.py`, `core/env_checker.py`, `learning/{embedding_matcher,engine,prompt_ab_evaluator,schema}.py`,
`storage/database.py`, `main.py`, `web/app.py`, `ui/simple_main_window.py`,
`tools/enviar_tarea_subproceso.py`, `requirements.txt`, `pytest.ini`.

## MIGRACIONES BD (idempotentes, compatibles hacia atrás)

| Versión | Cambio |
|---------|--------|
| 8→9 | Backfill H6: `completada` con `errores>0` → `fallida` (la ejecución 469) |
| 9→10 | H2: columna `motivo` en `prompt_reescrito_usos` |
| 10→11 | H5: `problema`, `plan_json`, `resultado`, `aceptada`, `motivo_fallo`, `problema_embedding`, `problema_embedding_model` en `ejecuciones` |
| 11→12 | H7: `ejecucion_id`, `intento`, `agente`, `error`, `estrategia`, `plan_firma`, `resultado`, `exito` en `reparaciones_plan` |

`DB_VERSION = 12`. Ninguna migración borra datos.

## DEPENDENCIAS NUEVAS DECLARADAS

`sentence-transformers>=3.0.0`, `torch>=2.0.0`, `pypdf>=4.0.0` y `ddgs>=9.0`
(sustituye al nombre antiguo `duckduckgo-search`, que el executor aún tolera).
Todas eran dependencias REALES ya instaladas o necesarias para H2/H6/H8.

## TESTS NUEVOS / EJECUTADOS

- **Suite completa aislada (`tools/run_tests.sh`): 935 passed, 1 skipped.**
  - Sin proceso aislado, la suite hereda un SIGSEGV **pre-existente** de
    CPython 3.13 (fork+exec del sandbox desde hilos). `--forked` lo convierte
    en fallo de un test concreto, no en caída de la suite. No se desactivó
    ninguna prueba.
- Tests obligatorios de H6: `.docx` sin imagen → fallida con motivo; `.docx`
  con imagen → completada; JSON no parseable → fallida; `errores=1` → fallida.
- E2E determinista (`test_flujo_e2e_aceptacion.py`): fallo de aceptación →
  Plan B → artefacto correcto → aceptación con evidencia, con ejecutores reales
  (sandbox + File) y sin LLM.

## VALIDACIÓN FINAL

1. **Unitarios/integración/BD/scheduler/API**: suite completa ✅ (935/1 skip).
2. **Prueba completa real (plan→ejecución→verificación→registro)**:
   determinista vía `test_flujo_e2e_aceptacion.py` y `test_docx_e2e_sin_llm.py`
   (artefacto real en disco). La variante con LLM real no se ejecuta en tests
   (política: no llamar APIs reales en la suite).
3. **Prueba de fallo → Plan B → nueva verificación**: ✅ E2E determinista.
4. **Prueba de aprendizaje/retrieval**: ✅ `test_retrieval_casos.py` (solo casos
   con `aceptada=1`, umbral conservador, 1–3 casos).

## FALLOS CONOCIDOS

- **SIGSEGV de CPython 3.13** en el sandbox (fork+exec desde hilos) al correr
  muchos tests en un proceso. Mitigado con `tools/run_tests.sh` (`--forked`);
  documentado en `pytest.ini`.
- H11/H12 quedan en fase 1/preparación (ver tabla): no hay agente de escritorio
  ni entrenamiento, deliberadamente.
- La cancelación de un job **en ejecución** es best-effort: el pipeline corre en
  el hilo de Qt y no se interrumpe desde HTTP (los encolados sí se cancelan).

## DEUDA TÉCNICA

- `core/scheduler.py` (~1950 líneas), `storage/database.py` (~2200) y
  `ui/simple_main_window.py` (~1220) superan la guía de 500 líneas. La lógica
  nueva se extrajo a módulos nuevos (`plan_recovery`, `verification`,
  `log_bus`, `jobs`, `ia_config`, `vision`), pero el orquestador sigue siendo
  grande: extraer Plan B/reintentos a un colaborador es el siguiente paso
  natural (requiere tocar concurrencia con cuidado).
- `reparaciones_plan` registra el intento y si generó plan; no actualiza el
  resultado final de la ejecución reparada (se puede enlazar por
  `ejecucion_id` en una fase futura).
- El retrieval depende de que el embedding del problema se haya calculado
  (background). Ejecuciones antiguas sin embedding no aparecen hasta
  reindexarlas.
- La visión (H11) no se cablea todavía como acción declarativa del Browser
  (`screenshot → decidir → ejecutar`); las piezas están y probadas, falta el
  bucle opt-in en el flujo de agentes.

---

## ARQUITECTURA FINAL

```
USUARIO
  │  prompt (GUI / CLI / POST /run / POST /api/jobs)
  ▼
PROBLEMA ──► se persiste en ejecuciones.problema (H5)
  │
  ▼
PLANIFICADOR (ProblemSolver)
  │   ├─ lecciones del historial
  │   └─ casos similares EXITOSOS (H8, umbral 0.85, 1–3)
  ▼
PLAN (ExecutionPlan + contratos de aceptación por paso)
  │   validación estática: sintaxis, contratos imposibles, invariantes (H6)
  ▼
SCHEDULER (DAG, Qt-safe)
  │   └─ agente es_critico ⇒ su fallo tumba la ejecución (H6 Nivel 1)
  ▼
AGENTES (Python/Shell/HTTP/LLM/File/Loop/Browser/Search)
  │   ├─ modelo LLM del usuario (H1)
  │   ├─ variante de prompt solo si pasa intención (H2, motivo registrado)
  │   └─ logs al bus unificado (H4)
  ▼
ARTEFACTOS (disco/bytes)
  │
  ▼
VERIFICATION ENGINE (H6, core/verification.py)
  │   existe · tamaño · formato real · JSON · claves · imágenes incrustadas ·
  │   contenedor (.docx/.pdf…) · directorio · items/errores
  │   → VerificationResult: ok, estado, motivos, advertencias, evidencias,
  │     criterios_comprobados / criterios_fallidos
  │
  ├─ PASA ──► estado = completada · aceptada = 1
  │              └─► APRENDIZAJE: evaluación, embedding del problema,
  │                  casos recuperables, feedback
  │
  └─ FALLA ─► estado = fallida · motivo_fallo estructurado
                 └─► PLAN B ADAPTATIVO (H7)
                        ├─ firma del plan fallido (anti-repetición)
                        ├─ escalera de estrategias (corrección → config →
                        │   tipo → reestructurar → fallback)
                        ├─ reutiliza agentes completados y verificados
                        ├─ traza en reparaciones_plan
                        └─► NUEVO PLAN ──► SCHEDULER ──► … (máx. intentos y
                                              presupuesto de tiempo)
  ▼
RESULTADO FINAL
  ok = true solo si TODOS los agentes terminaron bien Y la aceptación pasó
  estado = completada | fallida   (misma verdad para GUI, web, CLI y API)
```

**Por qué esto cambia el nivel:** la fuente de verdad es el **contrato de
aceptación comprobado en disco**, no el estado de los agentes. El `ok` de la
API, la etiqueta del historial, el motivo del Plan B y las etiquetas del
aprendizaje parten todos del mismo veredicto verificado.
