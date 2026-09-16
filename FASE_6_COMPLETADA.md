# Fase 6 — Validación post-generación (16-sep-2026)

## Objetivo
El LLM generador de planes Python a veces escribe código que no compila
o usa variables inexistentes. Antes, ese código se ejecutaba y fallaba
en el sandbox con `NameError`, y Plan B regeneraba con el mismo prompt
y el mismo resultado.

## Solución en 3 commits

| # | Commit | Contenido |
|---|--------|-----------|
| 2a | `9a38d01` | `PlanValidator` detecta SyntaxError, nombres de agentes como variables, placeholders literales |
| 2b | `63a77e8` | Errores de código Python se marcan con prefijo `BLOQUEANTE:` |
| 2c | `3f391a0` | `resolver_problema` reintenta la generación hasta 2 veces con instrucción correctiva; `refinar_plan` solo registra |

## Verificación en producción (16-sep-2026)

Ejecución real de "Escribe un cuento corto con imágenes":
- LLM genera plan de 4 pasos
- `embedding_matcher` encuentra match: `id=24, similitud=0.815`
- A/B elige candidato: `✨ AB: 'GenerarCuento' usa id=24`
- `code_corrector` valida: `✅ Código ya usa contexto correctamente`
- `PlanValidator` no encuentra errores: `Advertencias: []`
- Plan listo para ejecutar

**No se disparó ningún reintento** porque el LLM generó código correcto.

## Deuda técnica documentada
- Los tests de `test_sandbox.py` con timeout reportado como "inesperado"
- El segfault de py3.13 + Qt + fork (mitigación: `run_all_tests.py`)
