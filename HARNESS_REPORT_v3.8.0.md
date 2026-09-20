# HARNESS_REPORT v3.8.0 — Orquestador autónomo controlado

Rama sugerida: `harness/v3.8.0` · Base: `harness/v3.7.0` (935 passed, 1 skipped).

Objetivo del sprint (visión del usuario): que `agentes_visuales` pueda
**planificar, ejecutar, comprobar objetivamente, corregirse, aprender y
exponer el proceso de forma controlada**, empezando por cerrar recovery +
presupuesto + cancelación + memoria + Browser/Visión **antes** de añadir más
capacidades.

Los 6 entregables de V3.8 están implementados, con tests y sin regresiones.

| # | Entregable | Estado | Módulo principal |
|---|------------|--------|------------------|
| 1 | RecoveryManager + paradas duras | ✅ | `core/recovery_manager.py` |
| 2 | BudgetManager (tiempo/llamadas/tokens/coste) | ✅ | `core/budget_manager.py` |
| 3 | Cancelación real de jobs | ✅ | `core/job_cancellation.py` |
| 4 | HistoricalIndexer + éxito real | ✅ | `learning/historical_indexer.py` |
| 5 | BrowserVisionLoop | ✅ | `core/browser_vision_loop.py` |
| 6 | Refactor del Scheduler | ✅ | `core/acceptance_manager.py`, `core/dependency_manager.py`, `core/execution_log.py` |

---

## 1. Recovery inteligente: paradas duras

**Problema.** Cualquier fallo terminal gastaba un intento de Plan B (llamada
al LLM, ~15-20 s y dinero) aunque el fallo no pudiera arreglarse
reescribiendo el plan.

**Solución.** `RecoveryManager` clasifica el fallo ANTES de reclamar el turno
de Plan B y devuelve una `RecoveryDecision` explícita:

```python
@dataclass
class RecoveryDecision:
    permitir_reintento: bool
    motivo: str
    estrategia: str
    coste_estimado: float
    prioridad: int
    codigo: str | None = None
    parada_dura: bool = False
    detalles: dict = field(default_factory=dict)
```

Códigos de parada dura (no gastan intento ni llaman al LLM):

`API_KEY_MISSING`, `DEPENDENCY_MISSING`, `INVALID_PROBLEM`,
`SECURITY_BLOCK`, `INVALID_CONTRACT`, `BUDGET_EXCEEDED`, `TIMEOUT_GLOBAL`.

Guardarraíles del clasificador:

- Patrones **específicos** (nada de «error» o «fallo» genéricos): un falso
  positivo impediría un Plan B legítimo, que es peor que un falso negativo.
- `401` solo cuenta como auth si va pegado a HTTP/unauthorized.
- **El fallo de aceptación en runtime NUNCA es parada dura**: es justo lo que
  el Plan B debe arreglar (early return explícito).
- Si el propio `RecoveryManager` falla, se permite el reintento (comportamiento
  anterior a V3.8).

Integración: `Scheduler._decidir_recuperacion()` consulta al manager en
`_bloquear_dependientes`; una parada dura se registra en `_parada_dura`, se
anuncia y se **expone en `obtener_resultado_aceptacion()`** y en el resumen
del job (`parada_dura`). Los errores de importación del executor se registran
como `DEPENDENCY_MISSING`. Cada parada se audita en `reparaciones_plan`.

## 2. BudgetManager: tiempo + llamadas + tokens + coste

`BudgetManager` limita y contabiliza cuatro dimensiones (todas opt-in;
sin límites nada cambia):

- `AGENTES_BUDGET_MAX_SEGUNDOS`
- `AGENTES_BUDGET_MAX_LLAMADAS`
- `AGENTES_BUDGET_MAX_TOKENS`
- `AGENTES_BUDGET_MAX_COSTE` (con `AGENTES_PRECIO_1K_PROMPT`,
  `AGENTES_PRECIO_1K_COMPLETION`, `AGENTES_MONEDA`; por defecto 0 — **no se
  inventa dinero** si no hay precios)

La contabilidad de tokens no acopla módulos: se suscribe al **punto único de
salida** del LLM (`core/llm_client.agregar_observador_llamada`), de modo que
`completar()` notifica a los observadores sin exigir un cliente previo.

El presupuesto alimenta al `RecoveryManager`: si se agota, la recuperación es
una parada dura `BUDGET_EXCEEDED`. El Scheduler lo arranca una sola vez por
ejecución (no se reinicia al relanzar un Plan B, porque el presupuesto es del
conjunto) y lo expone en la aceptación (`presupuesto`).

## 3. Cancelación real de jobs

**Antes:** `POST /api/jobs/<id>/cancel` solo marcaba la petición; el pipeline
en marcha seguía hasta el final y el job terminaba como `failed`.

**Ahora:** `JobCancellationRegistry` (thread-safe). El hilo HTTP **solo deja
una solicitud**; el latido del pipeline, que corre en el hilo de Qt dueño del
`Scheduler`, la consume (~200 ms) y llama a `Scheduler.detener()` — que
cancela tokens y workers de verdad. Se respeta el modelo de hilos de Qt.

- `ColaTrabajos.ejecutar_pendientes` pasa el `job_id` al pipeline y marca el
  estado `cancelled` (no `failed`).
- `_ejecutar_pipeline(...)` acepta `job_id`, atiende una cancelación previa al
  arranque y devuelve `cancelado: True` (nunca como éxito).
- El `CancellationToken` existente se consulta también en el bucle de visión.

## 4. HistoricalIndexer: embeddings históricos + éxito real

**Problema doble.** (a) Las ejecuciones antiguas no tienen
`problema_embedding`, así que H8 no las recupera. (b) `aceptada` se añadió con
`DEFAULT 1`, así que **todas** las filas antiguas quedaron «aceptadas» aunque
tuvieran errores: el «100 % de éxito» histórico era falso.

**Solución.** `HistoricalIndexer` (con CLI `python -m learning.historical_indexer`):

```
ejecuciones antiguas → embedding → ÉXITO REAL compuesto → guardar → retrieval
```

Éxito real conservador (orden de decisión): feedback de plan negativo manda;
feedback positivo confirma; `aceptada=0` → fallo; errores/cancelados o estado
no completado → fallo (corrige el `DEFAULT 1`); aceptada y todo completado →
éxito; si no, indeterminado (mejor no usarlo como ejemplo).

`CaseRecord` reúne problema, objetivo, plan, agentes, tipos, contrato,
resultado, aceptación, errores, intentos de Plan B, tiempo, llamadas, tokens,
coste, feedback y lecciones.

Migración **12→13**: `exito_real`, `exito_real_motivo`, `indexado_fecha`,
`llamadas_llm`, `tokens_total`, `coste`. El retrieval
(`_cargar_candidatos_exitosos`) prefiere `exito_real = 1`; las filas aún no
indexadas caen al criterio antiguo (fallback) y las ya evaluadas como no
exitosas/indeterminadas se excluyen. `registrar_ejecucion_en_aprendizaje`
persiste el consumo del presupuesto.

## 5. BrowserVisionLoop

Cierra el ciclo que H11 fase 1 dejó abierto:

```
captura → VLM → decisión (allowlist) → acción Browser → nueva captura → …
```

Límites duros: `max_steps=20`, `max_segundos=120`, `max_capturas=30`,
`max_fallos_consecutivos=3` (configurables por entorno).

Cada acción deja traza `{accion, objetivo, razon, evidencia}` + `ok`,
`detalle`, `captura` y `duración`. La decisión (`core/vision.py`) ahora pide
`objetivo` y `evidencia` al modelo.

Guardrails: kill switch (`AGENTES_VISION_HABILITADA`), allowlist conservada,
y sin control de escritorio. El driver se inyecta (tests sin Playwright);
`crear_driver_playwright` reutiliza `BrowserExecutor._ejecutar_accion` (misma
allowlist y misma ejecución). El bucle es una acción **opt-in** del Browser
(`{"tipo": "vision", ...}`) y **no** está en el vocabulario del VLM, para que
el modelo no pueda proponerlo (evita recursión).

## 6. Refactor del Scheduler

Se extraen responsabilidades cohesivas a colaboradores, dejando al
`Scheduler` como orquestador que delega. **No** se mueve `scheduler.py` a un
paquete: el propio análisis del proyecto advierte de no dividir
artificialmente lo existente y toda la API pública se mantiene.

| Módulo nuevo | Responsabilidad | Líneas fuera |
|--------------|-----------------|--------------|
| `core/acceptance_manager.py` | veredicto de aceptación (puro) | ~80 |
| `core/dependency_manager.py` | grafo: resolver, ciclos, fuentes LOOP | ~120 |
| `core/execution_log.py` | resumen de resultado/error para el log | ~210 |

`core/scheduler.py` pasa de **2094 a 1738 líneas** (−356) y conserva todos sus
métodos públicos (`resolver_dependencias`, `detectar_ciclos`,
`_resumir_resultado_log`, `_calcular_aceptacion_internal`, …) como
delegaciones. Los colaboradores `RecoveryManager`, `BudgetManager` y
`JobCancellationRegistry` de los entregables 1-3 son parte de la misma
separación de responsabilidades.

Queda **deliberadamente pendiente** extraer `_intentar_plan_b` / reintentos:
es la parte con más acoplamiento (locks, tokens, executor, verificaciones) y
la más cara de romper; se hará con el mismo patrón (colaborador + delegación)
y tests de caracterización, no a ciegas.

---

## Validación

- Suite completa aislada (`tools/run_tests.sh`, `--forked`): **en verde** tras
  cada entregable (935 → 981 → 999 → 1011 → 1029 → 1049 → final).
- Tests nuevos: `test_recovery_manager.py` (38),
  `test_scheduler_paradas_duras.py` (10), `test_budget_manager.py` (18),
  `test_job_cancellation.py` (12), `test_historical_indexer.py` (18),
  `test_browser_vision_loop.py` (19), `test_scheduler_refactor.py` (20).
- Lint `ruff` limpio en todo lo tocado (los 11 hallazgos restantes son
  preexistentes en `tests/repro/`, `core/sandbox.py` y
  `tests/test_sandbox_contract.py`).
- El `SIGSEGV` intermitente de CPython 3.13 (fork+hilos) sigue siendo
  preexistente: se ejecuta con el runner aislado documentado en H3.

## Riesgos y decisiones

1. **Falsos positivos de parada dura**: se mitigó con patrones específicos y
   el early-return del fallo de aceptación. Ante duda, se permite el reintento.
2. **Migración 12→13**: idempotente (`ALTER TABLE` solo si falta la columna) y
   con rollback si una migración falla (no se actualiza la versión).
3. **Cancelación**: el hilo HTTP nunca toca el `Scheduler`; solo deja una
   solicitud. Es la única forma segura con Qt.
4. **Presupuesto**: sin precios configurados el coste es 0 y no hay límite de
   coste efectivo; es honesto, no una estimación inventada.
5. **Refactor**: se priorizó lo puro y verificable; lo acoplado se deja para
   una iteración con tests de caracterización.

## Lo que NO se hizo (a propósito)

- Entrenar un VLM propio (V5): primero hacen falta datos y ejecuciones
  verificadas; el indexado de V3.8-4 es el primer paso.
- Agente de escritorio (V4.1/V4.2): superficie de seguridad mucho mayor;
  requeriría allowlist + kill switch + confirmación humana + auditoría.
- Modo «Resolver tarea» (V4.0): es el siguiente salto y se apoya en todo lo
  anterior ya cerrado.
