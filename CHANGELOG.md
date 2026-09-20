# Changelog

## [v3.2.1] — 2026-09-20

Hotfix de una sola regla del validador. Ver `HARNESS_REPORT_v3.2.1.md`.

### Corregido
- **Validador (N2)**: `_validar_codigo_python_ast` detectaba los
  placeholders con llaves (`json.loads('{X}')`) pero no el anti-patrón
  real de N2: el LLM se inventa un nombre para el contenido y lo usa como
  literal en vez de construir el valor
  (`farsi = json.loads("__FARSI_JSON__")`). Ahora todo `ast.Constant` de
  tipo `str` cuyo valor sea MAYÚSCULAS_CON_GUIONES_BAJOS — con o sin
  envoltura `__...__` — se marca como `BLOQUEANTE`. El criterio es la
  FORMA del nombre, no una lista de nombres conocidos ni el caso concreto
  `__FARSI_JSON__`: cubre igual `__UCRAINIAN_JSON__`, `__NEWS_HTML__`,
  `__CUENTO_DRAGON__` o `FARSI_JSON`.

### Notas
- Sin cambios de contrato: la función sigue devolviendo `list[str]` con
  prefijo `BLOQUEANTE:`. Solo se añade una regla de detección.
- Falsos positivos evitados sin listas de excepciones hardcodeadas: los
  dunders legítimos (`__name__`, `__file__`, `__main__`, `__all__`) van en
  minúsculas y no casan; las constantes de una sola palabra (`"GET"`,
  `"POST"`, `"CSV"`, `"OK"`) no llevan guion bajo y tampoco se marcan; y el
  LHS de una asignación (`contexto["__X__"] = ...`) es un nombre de clave
  elegido por el código, no un placeholder consumido.
- Coste conocido: un literal tipo variable de entorno (`"API_KEY"`,
  `"HTTP_PROXY"`) sí casa con la forma. Documentado en el informe.

## [v3.2.0] — 2026-09-20

Release de estabilización: cierra los tres bugs que quedaban abiertos en
el informe v3.1.0 y en la auditoría posterior (H2, H4, N1). Ver
`HARNESS_REPORT_v3.2.0.md`.

### Corregido
- **Scheduler (H2)**: el Plan B ejecutaba `llm_client.chat(...)` con el
  `RLock` del scheduler tomado, así que durante ~15-20 s la UI se congelaba
  (`obtener_estadisticas()` usa el mismo lock) y los demás workers se
  paraban al llegar a FASE 5. Ahora FASE 5 decide el bloqueo bajo lock y lo
  ejecuta fuera; el turno de Plan B se reserva de forma atómica
  (`_reclamar_plan_b`) antes de soltar el lock.
- **Base de datos (H4)**: `_transaction` usaba siempre `BEGIN IMMEDIATE`,
  también para los SELECT, de modo que una lectura con otro escritor activo
  agotaba los reintentos y devolvía `[]`/`{}` ("historial vacío" en la UI).
  Los 9 métodos de solo lectura usan ahora `BEGIN DEFERRED`, y los fallos de
  lectura dejan de ser silenciosos: se registran en el log y se consultan
  con `Database.ultimo_error_lectura()`. `verificar_integridad` también los
  registra (antes se los tragaba sin log).
- **Validador (N1)**: `_validar_codigo_python_ast` no detectaba el alias
  `dependencias` que el LLM inventa a menudo (`dependencias.get(...)`); el
  sandbox solo define `contexto`, así que el paso fallaba con `NameError`.
  Ahora se marca como bloqueante (salvo si el código lo liga antes de
  usarlo).

### Notas
- El cuarto bug del encargo no llegó (el mensaje se truncó al empezar N1):
  se documenta en `HARNESS_REPORT_v3.2.0.md` y no se inventó un arreglo.
- Sin cambios de contrato de retorno en `storage/database.py`: los métodos
  de lectura siguen devolviendo `[]`/`{}`/`None`, pero el error es
  observable.

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