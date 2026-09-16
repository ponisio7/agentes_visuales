# Fase 5b — Completada (16-sep-2026)

## Objetivo
Recuperar los archivos de Fase 5b guardados el 16-sep y reintegrarlos
sobre v1.5 sin arrastrar los bugs introducidos al final de la sesión
del 15-sep.

## Pasos ejecutados y verificados

| # | Paso | Commit | Verificación |
|---|------|--------|--------------|
| 1 | Rollback a v1.5 | tag `b0ffa36` | — |
| 2 | Sandbox: fix f-strings con `{}` y triples comillas | `2250839` | 3/3 smoke test |
| 3 | Scheduler: eliminar bucle infinito de Plan B | `6588249` | Ejecución real: 2 Plan B máx |
| 4 | embedding_matcher + migración DB v8 | `2250ee7`, `0c43316` | Columnas embedding OK |
| 5 | FeedbackProcessor calcula embedding | `3ceee4c` | 1536 bytes, modelo OK |
| 6 | Builder: matching por embeddings + A/B | `0aca565` | Idempotencia OK |
| 7 | Execution recorder: registrar usos A/B | `22ebf35` | Log: "devolvió 405" |
| 8 | Panel admin: botón en simple_main_window | `86db753` | Abre y carga |

## Verificación en producción
Ejecución real de "Escribe un cuento corto con imágenes":
- Plan B se lanzó 2 veces y se detuvo correctamente (no bucle)
- Ejecución guardada como ID 405
- Log: `[TERMINADA] registrar_ejecucion devolvió 405` (no None)

## Deuda técnica documentada (NO de Fase 5b)
- 3 tests sandbox con timeout reportado como "inesperado"
- segfault py3.13 + PyQt6 + NumPy + Pandas + fork (mitigación: run_all_tests.py)
- tests Grupo A/C con imports rotos (herencia de main_nueva)

## Bugs detectados DESPUÉS de Fase 5b (nueva fase)
- El LLM generador de planes Python usa variables globales inexistentes
  (`GenerarCuentoConSVG`) en lugar de `contexto.get(...)`.
- El LLM no cumple siempre el formato JSON pedido.
- No hay validación post-generación del código del plan.

## Tags y ramas
- v1.5: `b0ffa36`
- main_nueva: `a026be6` (merge Fase 5b)
- fix-desde-v1.5: `cb6b3a9`
- checkpoint/pre-orden: `16a9bb2` (runner 3 grupos)
