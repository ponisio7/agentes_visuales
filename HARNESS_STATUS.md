# HARNESS_STATUS

- Rama: harness/v3.7.0
- Inicio: 2026-09-20
- Plan: dudas_sobre_el_programa.md §13
- Informe de la evolución H1–H12: `HARNESS_REPORT_v3.7.0-H1-H12.md`
- Análisis previo: `HARNESS_ANALISIS_v3.7.0.md`

## En curso
- (ninguna)

## Completadas

### H6 — Capa de verificación/aceptación de la salida (§4, §11) ✅
- `e78529a` verificador determinista + `ContratoAceptacion`.
- `a462b0b` invariantes en `builder`/`prompt_builder` + rechazo estático.
- `6a1ff4f` gate en el scheduler y `es_critico` como fallo real.
- `1080be8` `ok`/`estado` honestos + etiqueta real en el historial.
- `5e07eeb` estado inicial de H6.
- `c8b4e45` `VerificationEngine`/`VerificationResult` con evidencia y criterios
  (directorio y contenedor). Migración 8→9 reclasifica la ejecución 469.

### H1 — Configuración de API y modelo ✅
- `0502d9e` `core/ia_config.py` (archivo 700/600), `DEEPSEEK_MODEL`,
  `reset_llm_client_compartido()`, diálogo `⚙ Configuración`, modelo respetado
  por el builder. `17a8178` análisis arquitectónico previo.

### H2 — Matcher A/B ✅
- `f6cd82e` umbral 0.85 + compatibilidad de intención + margen de duda +
  motivo registrado (migración 9→10).

### H3 — Dependencias y estabilidad ✅
- `b909a22` deps reales declaradas, verificador de dependencias, runner
  aislado `tools/run_tests.sh` (`--forked`) documentado en `pytest.ini`.

### H5 — Persistir el problema original ✅
- `c0ddfd9` migración 10→11 (problema, plan, resultado, aceptación, embedding).

### H10 — Cliente de tareas en serie ✅
- `8389668` informe por tarea con verificación/artefactos y
  `--continue-on-error`.

### H7 — Plan B adaptativo ✅
- `20d7706` límites configurables, escalera de estrategias, anti-repetición,
  reutilización de agentes válidos y `reparaciones_plan` (migración 11→12).

### H8 — Retrieval de casos similares ✅
- `d4f44b5` solo casos con `aceptada=1`, umbral 0.85, 1–3 casos, aviso de no
  copiar ciegamente.

### H4 — Log bus unificado ✅
- `ef34f88` `core/log_bus.py`, GUI por cursor, `GET /api/logs` y `GET /logs`.

### H9 — API de trabajos ✅
- `57205f8` `POST/GET /api/jobs`, SSE de logs, cancel; `/run` intacto; paridad
  en `serve`.

### H11 — Visión (fase 1) ⚠️
- `129e6fe` cliente multimodal + decisión validada por allowlist + kill switch.
  Sin bucle autónomo de acciones ni agente de escritorio (deliberado).

### H12 — VLM local (preparación) ⚠️
- `974e4d2` `DEEPSEEK_BASE_URL` local, herramienta de comprobación y documento
  de arquitectura. Sin entrenamiento en el núcleo (deliberado).

### Validación
- `a9bc6fa` E2E determinista: fallo de aceptación → Plan B → artefacto
  correcto → aceptación con evidencia.
- Suite completa aislada: **935 passed, 1 skipped**.

## Bloqueadas
- (ninguna)

## Notas
- La suite completa hereda un `SIGSEGV` intermitente de CPython 3.13 en el
  sandbox (fork + hilos). No es de las fases: se ejecuta con
  `tools/run_tests.sh` (aislamiento por proceso, sin desactivar pruebas).
- H11/H12 quedan en fase 1/preparación por decisión de diseño (el encargo pide
  no meter entrenamiento pesado en el núcleo y no hay control de escritorio).
