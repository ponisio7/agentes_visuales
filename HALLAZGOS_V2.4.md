# Hallazgos v2.4 — Ejecución 427: "1 errores bloqueantes" × 2 reintentos

Fecha del análisis: 2026-09-19
Rama: `harness/v2.4-cleanup`

## Resumen

La ejecución 427 (`crea un cuento con imagen en formato .html`) reintentó la
generación del plan 2 veces por "1 errores bloqueantes" y acabó dejando pasar
el plan. La causa raíz **no era el plan**: era un **falso positivo del
validador**, que marcaba como bloqueante cualquier `{{` del código Python,
incluido el escape legítimo de llaves dentro de un f-string (CSS embebido).
El plan era código Python correcto.

Además, el log solo indicaba el **número** de errores, nunca cuáles, lo que
hacía imposible diagnosticarlo desde el log.

## 1. Qué pasó

Problema: `crea un cuento con imagen en formato .html`

Plan generado (4 pasos):

| # | Paso | Tipo | Depende de |
|---|------|------|------------|
| 1 | `GenerarCuento` | LLM | — |
| 2 | `ObtenerImagen` | HTTP | — |
| 3 | `EnsamblarHTML` | Python | GenerarCuento, ObtenerImagen |
| 4 | `GuardarHTML` | File (`cuento.html`) | EnsamblarHTML |

Log de `logs/agentes_visuales.log`:

```
12:22:07 [ERROR] core.problem_solver.solver: ❌ Plan con 1 errores bloqueantes. Reintentando generación (1/2)...
12:22:18 [ERROR] core.problem_solver.solver: ❌ Plan con 1 errores bloqueantes. Reintentando generación (2/2)...
12:22:30 [ERROR] core.problem_solver.solver: ❌ Plan con errores bloqueantes tras 2 reintentos. Se dejará pasar; Plan B en ejecución decidirá.
12:22:34 [INFO]  storage.database: Ejecución guardada: ID=427, 4 agentes, ✅4 ❌0 ⛔0
```

El plan "bloqueado" se ejecutó igualmente y terminó con 4/4 agentes OK.

## 2. Causa raíz

`PlanValidator._validar_codigo_python_ast` (check #4) hacía:

```python
if "{{" in codigo:
    errores.append(f"BLOQUEANTE: {nombre}: usa sintaxis de plantilla '{{{{...}}}}' ...")
```

Pero `{{` y `}}` son el **escape legítimo de una llave literal** dentro de un
f-string. El paso `EnsamblarHTML` genera el HTML con CSS embebido:

```python
html = f"""<!DOCTYPE html>
...
    <style>
        body {{
            font-family: 'Segoe UI', ...;
            max-width: 800px;
        }}
    </style>
...
"""
```

Ese código es Python correcto (produce `body { ... }` literal en el CSS). El
validador lo marcaba como "sintaxis de plantilla `{{...}}`" → error
BLOQUEANTE → regeneración forzada. El LLM, lógicamente, volvía a generar el
mismo escape correcto, y tras 2 reintentos se dejaba pasar el plan.

La intención original del check era detectar el anti-patrón documentado en
`FIELD_RULES`: `{{Dependencia.clave}}` en lugar de `contexto.get(...)`.

## 3. Reproducción

Con las respuestas crudas guardadas (`logs/llm_response_20260919_12*_deepseek-v4-pro.txt`)
se reconstruye el plan y se ejecuta el validador:

```python
import json, glob
from core.problem_solver.validator import PlanValidator

for path in sorted(glob.glob("logs/llm_response_20260919_122*_deepseek-v4-pro.txt")):
    txt = open(path, encoding="utf-8").read()
    plan = json.loads(txt[txt.index("{"):txt.rindex("}")+1])
    nombres = {p["nombre"] for p in plan["pasos"]}
    for paso in plan["pasos"]:
        if paso.get("tipo") == "Python":
            print(path, PlanValidator._validar_codigo_python_ast(
                paso["configuracion"]["codigo"], paso["nombre"], nombres))
```

Resultado **antes** del fix (3 intentos de la ejecución 427):

```
122154_074891: BLOQUEANTE: EnsamblarHTML: usa sintaxis de plantilla '{{...}}' ...
122217_641193: BLOQUEANTE: EnsamblarHTML: usa sintaxis de plantilla '{{...}}' ...
122229_998657: BLOQUEANTE: EnsamblarHTML: usa sintaxis de plantilla '{{...}}' ...
```

Resultado **después** del fix: `0 errores bloqueantes` en los 3.

Se comprobó también la ejecución 425 (`crea un cuento en html con imagenes`,
11:33), con el mismo patrón: intento 1 marcado (`EnsamblarHtml`), intento 2
sin `{{` → pasó (de ahí que 425 solo reintentara una vez).

## 4. Fix aplicado

### 4.1 Validador — commit `4579779`

Solo se considera plantilla sin resolver si el placeholder referencia a un
**agente del plan**, que es el anti-patrón real:

```python
patron_plantilla = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*(?:\.[^}]*)?\}\}")
for m in patron_plantilla.finditer(codigo):
    if m.group(1) not in nombres_agentes:
        continue
    errores.append(f"BLOQUEANTE: {nombre}: usa sintaxis de plantilla ...")
```

- `{{GenerarCuento.titulo}}` → sigue detectándose (agente conocido).
- `body {{ margin: 0 }}` (escape de f-string) → ya no se marca.
- El test existente con `{{CrearImagenes}}` sigue dando 2 errores.

Se añadieron 2 casos de regresión en `tests/test_validador.py`.

### 4.2 Observabilidad — commit `37ec5c0`

El log ahora lista los errores concretos, tanto en el reintento como al
agotar los intentos. Sin esto, el diagnóstico desde el log era imposible.

## 5. Verificación

```
python -m pytest tests/test_validador.py tests/test_plan_validator.py tests/test_plan_recovery.py -q
  -> 18 passed

python -m pytest tests/ -q
  -> 566 passed, 1 skipped, 19 errors
```

Los 19 errores son **preexistentes** y ajenos a este cambio: tests que
requieren el fixture GUI `qapp` (10 en `test_integration.py`, 5 en
`test_scheduler.py`, 3 en `test_loop_safety.py`, 1 en `test_scheduler_orden.py`).

## 6. Limitaciones de esta verificación

- **No se pudo ejecutar el pipeline en vivo**: no hay `DEEPSEEK_API_KEY` en el
  entorno. La reproducción se hizo con las respuestas crudas ya guardadas en
  `logs/`, que es evidencia equivalente para este fallo (el validador es
  determinista y no depende del LLM).
- **Pendiente de validación humana**: lanzar
  `crea un cuento con 3 imágenes en .docx` para comprobar que el plan incluye
  pasos de imágenes (verificación de la Tarea 3, ver más abajo).

## 7. Recomendaciones / deuda pendiente

1. **Ejecutar la verificación en vivo de la Tarea 3** (imágenes en `.docx`)
   cuando haya API key. `plan_repairs` se eliminó por evidencia de log/BD
   (0 filas en `reparaciones_plan`), no por comprobación en vivo.
2. **Tabla `reparaciones_plan`**: se mantiene en `learning/schema.py` aunque ya
   nadie la escribe ni la lee. Eliminarla requiere migración; se dejó por
   seguridad.
3. **Test de red intermitente**: `tests/test_conexion_a_DeepSeek_manualmente.py::TestConexionRealDeepSeek::test_conexion_basica`
   falló en 1 de 4 pasadas de la suite completa (timeout de red, ~104 s). No
   está relacionado con estos cambios; convendría marcarlo/saltarlo cuando no
   haya API key.
4. **Errores preexistentes de `qapp`**: falta el fixture GUI en el entorno de
   test; 19 errores en la suite completa.

## 8. Estado de las tareas v2.4

| Tarea | Estado | Commits |
|-------|--------|---------|
| 1. Ensombrecimiento `core/utils` | Hecha | `1637eee` |
| 2. Test `test_pausar_reanudar` determinista | Hecha | `63291e1` |
| 3. `plan_repairs` + regla de requisitos | Hecha (validación en vivo pendiente) | `dd563b2`, `34f303e` |
| 4. Causa raíz de la ejecución 427 | Hecha | `4579779`, `37ec5c0`, + este informe |
