# Changelog

## [v3.1.0] — 2026-09-20

Release de estabilización: cierra los 7 hallazgos de la auditoría de
v3.0.1 más 3 bugs nuevos encontrados al restaurar la suite (ver
`HARNESS_REPORT_v3.1.0.md`).

### Corregido
- **Datos**: la suite escribía en la BD de producción (`agent_history.db`)
  desde `tests/test_ab_sintetico.py`; ahora usa una BD temporal y el runner
  aborta si detecta cambios en la BD real.
- **Cobertura**: `run_all_tests.py` referenciaba un test borrado, hacía que
  pytest abortara `F_ligeros` con rc=4 y ningún test se ejecutara; ahora
  valida los paths, distingue "0 tests recolectados" y clasifica los 7
  tests que caían en `Z_sin_clasificar`.
- **Fixtures**: restauradas las fixtures de `tests/conftest.py`
  (`qapp`, `esperar`, `scheduler_rapido`, `scheduler_basico`) que dejaban
  19 tests sin ejecutarse.
- **File executor**: escribir un dict en `.txt`/`.log` fallaba y perdía el
  resultado (regresión de 903f94a); ahora serializa a JSON como fallback.
- **Cancelación**: `agregar_callback` sobre un token ya cancelado no
  disparaba el callback; un comando shell cancelado seguía corriendo hasta
  agotar el timeout.
- **Caché LRU**: actualizar una entrada existente expulsaba al LRU sin
  necesidad.
- **Errores**: 24 `raise` dentro de `except` encadenan ahora la causa
  original (`from e`).
- **Versión**: unificada en `[project].version` de `pyproject.toml`
  (antes `main.py` decía 1.1.0 y el CHANGELOG v3.0).

### Cambiado
- `.gitignore` deduplicado; los backups (`.bak`, `.backups_fix_*`) y las
  copias de la BD dejan de versionarse.
- `ruff` y `pyflakes` limpios en todo el proyecto (B904 activado).
- `pre_tag_check.sh` acota el lint, ejecuta la suite y trata el smoke test
  como puerta real.

## [v3.0] — 2026-09-20

### Añadido
- Integración con DeepSeek Harness como orquestador de agentes.
- Sistema de aprendizaje online (FailurePredictor, PlanScorer).
- A/B testing de prompts por embeddings semánticos (sentence-transformers).
- Extractor de lecciones desde el historial (error recurrente, score bajo, estructura).
- Agentes Browser (Playwright) y Search (DuckDuckGo).
- Exportadores CSV, JSON, HTML, Excel, Markdown, PDF.
- Panel de administración del sistema de aprendizaje.
- Script `tools/pre_tag_check.sh` para verificación pre-release.

### Cambiado
- ProblemSolver refactorizado en paquete modular (parser, validator, builder, etc.).
- Sandbox con cancelación token-based y `_SPAWN_LOCK` para evitar SIGSEGV en py3.13.
- Scheduler con máquina de estados de transiciones validadas.

### Corregido
- Múltiples bugs de cancelación, race conditions en EventBus, y eviction en cachés LRU.

## [v2.x] — Anterior
- (completar con tu historial)