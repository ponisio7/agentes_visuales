# HARNESS_REPORT v4.0.0 — Modo «Resolver tarea»

- Fecha: 2026-09-20
- Base: v3.8.0 + V4.0-AB (`HARNESS_REPORT_v4.0.0-AB.md`)
- Alcance: **punto 2 del roadmap V4.0** («modo Resolver tarea»).
- Estado: **entregado**, con 33 tests nuevos y suite completa en verde.

## 1. Qué faltaba de verdad

Conviene ser preciso, porque el pipeline ya hacía más de lo que sugiere el
eslogan «el usuario describe el plan»:

- `ProblemSolver.resolver_problema(problema)` **ya** toma lenguaje natural y
  decide agentes, dependencias, `es_critico` y contratos `aceptacion`
  (`core/problem_solver/prompt_builder.py`).
- Ya inyecta **lecciones** y **casos exitosos del histórico** (H8).
- El `Scheduler` **ya** verifica (H6), tiene gate de aceptación (V3.8-6),
  Plan B (H7) y presupuesto (V3.8-2).

Lo que **no** existía era el bucle dirigido por el objetivo: `run` era de un
solo tiro. Si la aceptación fallaba, la ejecución terminaba; el Plan B repara
pasos sueltos, pero **nadie volvía a plantear el plan** desde el objetivo. Y el
sistema **no exigía** que el plan fuera verificable antes de gastar una
ejecución: si el LLM no declaraba `aceptacion`, se ejecutaba igual y el
resultado no se podía comprobar.

## 2. Qué se ha construido

### `core/goal_resolver.py` — el bucle

```
planificar → ¿verificable? ──no──► re-planificar con el motivo (sin ejecutar)
     │ sí
     ▼
 ejecutar (Scheduler + Plan B) → verificar (aceptación H6)
     │                                  │
     │◄────────── no aceptado ──────────┘
     ▼ aceptado
  RESUELTO
```

- **`problemas_de_verificabilidad(plan)`** — el sistema decide la verificación.
  Un plan es verificable si algún paso que cierra la tarea (`es_critico` o
  terminal) declara un `aceptacion` no vacío. Si no, **no se ejecuta**: se
  devuelve al planificador con la instrucción de declararlo.
- **`construir_evidencia(intento)`** — la re-planificación no repite a ciegas:
  recibe los `motivos` concretos de la aceptación fallida o los errores de los
  agentes, y se le permite cambiar de estrategia, no solo retocar.
- **`GoalResolver`** — orquesta intentos con un **presupuesto compartido**
  (`BudgetManager`) y para de forma honesta: `verificado`,
  `intentos_agotados` o `presupuesto_agotado`.
- **`resultado_desde_salida(salida)`** — adapta el dict del pipeline y aplica
  H6: «terminado» **no** es «aceptado».

Decisiones:

1. **Módulo puro**: no importa Qt ni el Scheduler. Planificador y ejecutor se
   inyectan como callables → el bucle se prueba sin LLM ni GUI.
2. **Aditivo**: `run` no cambia de comportamiento. `resolve` es un modo nuevo.
3. **Parar es un resultado**: nunca se reporta éxito sin aceptación, y siempre
   se dice por qué se paró y cuánto costó.

### `main.py` — reutilización real, no duplicación

- Se extrae **`_ejecutar_plan(plan, …)`** de `_ejecutar_pipeline`: ejecuta **un
  plan ya construido** con el mismo Scheduler, el mismo Plan B y el mismo gate.
  `_ejecutar_pipeline` (y por tanto `run`, `serve` y `web`) pasa a ser
  «planificar + `_ejecutar_plan`», sin cambio de contrato.
- `_resolver_objetivo(...)` cablea el `GoalResolver` real: planificador =
  `ProblemSolver.resolver_problema` (con `_instruccion_extra=evidencia`),
  ejecutor = `_ejecutar_plan`, presupuesto único compartido.
- Nuevo subcomando **`resolve OBJETIVO`** con `--max-intentos`, `--max-pasos`,
  `--sin-exigir-verificacion`, `--output`, `--json`, `--no-aprender`, `--timeout`.
- `Scheduler` recibe el `presupuesto` compartido para que **todos** los intentos
  sumen al mismo límite.
- `MAX_INTENTOS_DEFAULT` se duplica a propósito en `main.py` (importar `core`
  arrastra PyQt6 y `--version`/`--check-env` deben seguir sin Qt); un test
  verifica que no diverge.

## 3. Ficheros

| Fichero | Cambio |
|---|---|
| `core/goal_resolver.py` | **nuevo**: bucle, gate de verificabilidad, evidencia, dataclasses |
| `main.py` | `_ejecutar_plan` extraído; `_resolver_objetivo`; subcomando `resolve` |
| `tests/test_goal_resolver.py` | **nuevo**: 21 tests del bucle (con dobles) |
| `tests/test_main_resolve.py` | **nuevo**: 12 tests de parser, handler y cableado |
| `run_all_tests.py` | los dos ficheros nuevos entran en `F_ligeros` |
| `docs/MANUAL_CLI.md` | sección §5.bis `resolve` |

## 4. Validación

- `tests/test_goal_resolver.py`: 21/21.
- `tests/test_main_resolve.py`: 12/12.
- `tests/test_main_cli.py` (contrato de `run` intacto tras el refactor): 43/43.
- `ruff check` limpio en todo lo tocado.
- Suite completa aislada (`tools/run_tests.sh`): ver `HARNESS_STATUS.md`.

## 5. Límites honestos

- **El tope de intentos es bajo a propósito** (`2`). El bucle pasa el motivo
  concreto, pero no cambia de *modelo* ni de estrategia de planificación por su
  cuenta: si el planificador insiste en el mismo enfoque, el segundo intento
  puede fallar igual. Subirlo es barato (una bandera), pero gasta presupuesto.
- **El gate de verificabilidad es sintáctico**: comprueba que existe un
  contrato declarado, no que sus invariantes sean los *correctos* para el
  objetivo. Un contrato pobre (p. ej. solo `archivos: ["salida.txt"]`) pasa el
  gate y luego el artefacto puede ser aceptado siendo inútil. Es la frontera
  entre «verificable» y «bien especificado», y no la resuelve este sprint.
- **No hay re-planificación parcial**: el intento fallido se descarta entero.
  Reutilizar los pasos que sí pasaron su contrato es una optimización evidente
  para una entrega siguiente.
- **`resolve` no está expuesto aún en web/API ni en la GUI**. El diseño lo
  permite (el bucle es puro y el ejecutor es una función), pero no se ha hecho.
- **El presupuesto sigue siendo opt-in**: sin variables de entorno no hay
  límites, así que «presupuesto agotado» no se disparará por defecto.
