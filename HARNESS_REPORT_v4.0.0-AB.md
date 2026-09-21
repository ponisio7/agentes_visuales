# HARNESS_REPORT v4.0.0-AB — Cerrar el bug de promoción A/B

- Fecha: 2026-09-20
- Base: v3.8.0 (`harness/v3.8.0`)
- Alcance: **punto 1 del roadmap V4.0** («cerrar el bug de promoción A/B antes
  de construir nada encima»). Nada más se toca.
- Estado: **cerrado**, con tests de caracterización y demostración sobre copia
  de la BD real.

## 1. Síntoma

La capa de auto-mejora de prompts no promovía **nunca**:
`SELECT estado, COUNT(*) FROM prompts_reescritos` devolvía
`candidato=12, descartado=5, activo=0`. Cualquier cosa construida encima (p. ej.
auto-crítica del LLM que reescriba prompts) heredaría el defecto en silencio.

## 2. Causa raíz (cuatro defectos encadenados)

### 2.1 El brazo de control no existía

`PromptABEvaluator.elegir_variante` usaba el candidato **al 100 %** cuando había
candidato y no había `activo` («solo candidato → candidato»). En la BD real,
36 selecciones de variante y **0 filas `activo`**: el 100 % de las ejecuciones
con match usaron la reescritura y **ninguna** el prompt original. Sin ejecuciones
de control no hay comparación posible, ni dentro ni fuera del A/B.

### 2.2 La referencia de decisión no era un control

Sin `activo` con ≥ 3 usos, `evaluar_candidato` comparaba contra
`_score_global()`: la media de **todos** los scores de `prompt_reescrito_usos`.
Esa población mezcla escalas (agente ≈ 0.60, plan ≈ 0.41), firmas distintas y
filas huérfanas. En la BD real valía **0.616**.

Además era un círculo vicioso: como nadie promovía, nunca había `activo`, así
que **todas** las decisiones se tomaban contra esa media, para siempre.

### 2.3 El `activo` se destruía al crear un candidato

`FeedbackProcessor._guardar_reescritura` ejecutaba
`SET activo=0, estado='descartado' WHERE firma=? AND estado='activo'`: cada
reescritura nueva tiraba el incumbent promocionado. Aun arreglando 2.1 y 2.2,
ninguna fila `activo` habría sobrevivido.

### 2.4 Estadísticas contaminadas por filas huérfanas

Las conexiones auxiliares (`prompt_ab_evaluator`, `feedback_processor`) abrían
SQLite con `foreign_keys=OFF`. Los borrados en cascada de `ejecuciones` →
`feedback_usuario` → `prompts_reescritos` dejaban atrás sus
`prompt_reescrito_usos`: **2679 de 2691** filas con score apuntaban a versiones
inexistentes (todas de `ejecucion_id >= 800000`, residuo de un bug histórico de
tests que escribían en la BD de producción). Esas filas alimentaban
`_score_global`, `_stats_version` y el contador `n_usos`.

## 3. Aritmética del punto muerto

Firma `73f260b440`, candidato 223 con media real **0.65**; el prompt original
rendía **0.438**:

| Regla | Referencia | Delta | Resultado |
|---|---|---|---|
| antigua | media global 0.616 | **+0.034** | `espera` → descarte a los 20 usos |
| nueva | baseline de la firma 0.438 | **+0.212** | **promovido** |

El mejor candidato del histórico era invisible para el sistema.

## 4. Corrección

1. **`elegir_variante` con incumbente** (`learning/prompt_ab_evaluator.py`):
   candidato con probabilidad `PROBABILIDAD_CANDIDATO = 0.20`; el resto, el
   incumbente (`activo` si existe, si no el prompt original con id 0).
2. **Tabla de control `prompt_reescrito_baseline`** (`learning/schema.py`):
   `(firma, ejecucion_id, score, fecha, motivo)`. Se rellena desde
   `execution_recorder` cuando el builder eligió el original de una firma con
   reescritura conocida, y se puntúa con el mismo score del evaluador de plan.
3. **Referencia válida y explícita**: `activo` (≥ `MIN_USOS_ACTIVO_PARA_COMPARAR`)
   → baseline de la firma (≥ `MIN_USOS_BASELINE_PARA_COMPARAR`) → `espera`.
   La media global queda solo como diagnóstico (`_score_global` documentado).
4. **El incumbent sobrevive**: `_guardar_reescritura` supera solo a los
   `candidato` previos de la firma.
5. **Higiene de datos**: `JOIN` con `prompts_reescritos` en `_stats_version` y
   `_score_global`; `PRAGMA foreign_keys=ON` en las conexiones auxiliares; nuevo
   `PromptABEvaluator.limpiar_huerfanos(db_path)` (mantenimiento, no se ejecuta
   solo).
6. **Plomería**: `Agente.prompt_firma` + `to_dict`; `execution_recorder` registra
   el uso de control y evalúa la firma también en ejecuciones de control, para
   que la decisión no espere a que vuelva a tocar explorar.

## 5. Ficheros

| Fichero | Cambio |
|---|---|
| `learning/prompt_ab_evaluator.py` | incumbente, baseline, referencia válida, higiene |
| `learning/schema.py` | tabla + índices `prompt_reescrito_baseline` |
| `learning/feedback_processor.py` | conserva el `activo`; FK ON |
| `core/problem_solver/builder.py` | pasa el original como incumbente; expone `prompt_firma` |
| `core/agent.py` | campo `prompt_firma` y su serialización |
| `core/execution_recorder.py` | registro del brazo de control; firma → evaluación |
| `tests/test_ab_promocion_baseline.py` | **13 tests nuevos** |
| `run_all_tests.py` | el nuevo fichero entra en `F_ligeros` |

## 6. Validación

- `tests/test_ab_promocion_baseline.py`: 13/13.
- A/B existentes (`test_ab_sintetico.py`, `test_ab_matcher.py`): 17/17 sin
  cambios de expectativa.
- Regresión relacionada (agent, serialización, recorder, builder, validador,
  retrieval, indexer): 126/126.
- `ruff check` limpio en todo lo tocado.
- Demostración sobre **copia** de la BD real: 0 → 1 fila `activo`, candidato 223
  promovido contra baseline 0.438.
- Suite completa aislada (`tools/run_tests.sh`): ver `HARNESS_STATUS.md`.

## 7. Notas y límites honestos

- **El baseline empieza vacío en la BD real.** A partir de ahora se rellena; el
  primer candidato de una firma necesitará ≥ 3 ejecuciones de control y ≥ 5 usos
  puntuados antes de decidirse. Es el precio de tener un control de verdad.
- **Sigue habiendo un sesgo de selección**: el baseline son ejecuciones con el
  prompt original (80 %) y el candidato se mide en el 20 % restante, no en un
  reparto aleatorio por par de tareas. Es una mejora enorme sobre la media
  global, pero no es un ensayo controlado aleatorizado estricto.
- **La escala del score** sigue siendo la del `EvaluadorLLM` de plan; el baseline
  y el candidato comparten escala, que es lo que exige la comparación.
- **No se ha limpiado la BD de producción.** `limpiar_huerfanos()` está
  disponible; ejecutarlo es una decisión aparte (borra 2679 filas históricas).
  El código ya no depende de ello: las consultas excluyen huérfanas.
- **Pendiente del roadmap, sin tocar aquí**: V4.0 «Resolver tarea», clasificador
  de fallo sobre `HistoricalIndexer`, ciclo de auto-crítica del LLM, refactor de
  `_intentar_plan_b`, agente de escritorio.
