# Changelog

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