# HARNESS REPORT — v3.5.0-fix-docx

**Rama:** `v3.5.0-fix-docx` (hija de `release/v3.5.0`)
**Objetivo:** que la instrucción *«créame un cuento sobre las albóndigas frías
con imagen y guárdala en .docx»* produzca un `.docx` real con texto e imagen,
sin hardcodear el caso ni desactivar sandbox/validador/corrector/learning.

---

## FASE 1 — Reproducción (evidencia, no suposiciones)

- `tests/repro/` son **scripts de diagnóstico con `print` a nivel de módulo**,
  no tests pytest: `pytest tests/repro/` colecta **0 items**. Se conservan
  intactos; su salida es la traza del bug.
- Corrector (antes):
  `texto = respuesta` → `texto = contexto.get('GenerarCuento', {}).get('body', '{}')`.
  También rompía `def f(respuesta)`, claves de dict `'respuesta'` y
  `print(result)` por ser un reemplazo de regex sin conciencia de ámbito.
- Resultado real de `GenerarCuento` (agente LLM, ejecución 469, decodificado
  de `agent_history.db`): claves `respuesta`, `respuesta_limpia` (3915 chars),
  `json` (`{'titulo','cuento'}`), `modelo`, `tokens_uso`…
  `contexto.get('GenerarCuento', {}).get('body', '{}')` → **`'{}'`** (2 chars)
  → `ValueError('El cuento está vacío o es demasiado corto')` (sandbox).
- Plan inicial (5 pasos) falló primero en `GuardarDocumento`: `File` `copiar`
  con `archivo_origen == archivo_destino`
  (`'cuento_albondigas_frias.docx' and 'cuento_albondigas_frias.docx' are [the same file]`).
  Ese fallo gratuito activó `Plan B #1/#2`; el `UnrecognizedImageError`
  (log 4613) vino de `Plan B #2`, cuando `CrearImagenSVG` produjo un SVG y
  `doc.add_picture()` lo recibió.
- En disco: `albondigas_frias.png` contiene **bytes JPEG**, `albondigas_frias.svg`
  contiene SVG real, `albondigas.png` es PNG real → la extensión no informa del
  formato.

## FASE 2 — Diagnóstico de causa raíz

1. **Corrector (causa primaria del «cuento vacío»).**
   `core/problem_solver/code_corrector.py` sustituía `\brespuesta\b` por una
   plantilla **con forma de HTTP** (`.get('body','{}')`). Ningún agente no-HTTP
   tiene `body`, así que llegaba el literal `'{}'`. Además el reemplazo era
   textual (rompía parámetros/claves/`print`) y `_usa_contexto_correcto()`
   abortaba con solo ver `contexto.get` en cualquier parte.
2. **Validador (no bloquea antes de ejecutar).**
   `validar_plan` validaba `paso.configuracion['codigo']` (**crudo**), pero el
   sandbox ejecuta `agente.codigo_python` corregido en `builder._kwargs_python`
   sin reescribir la config → **se validaba un programa distinto del que corría**.
   No existía regla para nombres libres sin definir (`respuesta`), para claves
   que el tipo del productor no puede emitir (`body` sobre un LLM) ni para
   `File` `copiar/mover` con origen == destino.
3. **Recovery (no converge).** `PlanRecovery` validaba el Plan B solo con
   `_validar_sintaxis_agentes` y su reintento correctivo era un prompt
   **exclusivamente de sintaxis**. `UnrecognizedImageError` / «cuento vacío»
   no son sintaxis → se repetía la misma estructura. `_resumir_plan_fallido`
   no mostraba el código del paso fallido.
4. **Contrato de imagen inexistente.** `_file_escribir_docx` validaba con
   Pillow solo las imágenes **descargadas**; las rutas **locales** iban
   directas a `add_picture`. En el sandbox, el código del LLM llamaba a
   `doc.add_picture` sin ningún contrato. `python-docx` decide por **bytes**
   (`_ImageHeaderFactory`), nunca por extensión, y lanza
   `UnrecognizedImageError` (sin mensaje) si no reconoce la firma.

**Clasificación: combinación** corrector (primario) + validador + recovery +
ausencia de contrato de imagen. Hallazgo extra: `File copiar` con
origen == destino.

## FASE 3 — Corrección genérica

| Frente | Archivo | Qué se hizo |
|---|---|---|
| Contrato de runtime | `core/sandbox_contract.py` (nuevo) + `core/sandbox.py` | `dependencia(contexto, nombre[, clave])` resuelve el valor útil por **nombre semántico** (prioridad derivada del contrato de salida, no de extensiones ni de `if` por formato); `preparar_imagen(ruta)` detecta el **formato real** con Pillow, convierte lo no nativo (p. ej. WEBP) y rechaza SVG/corruptos con error accionable. El prelude se inyecta en el sandbox generado desde el código fuente del módulo. |
| Corrector | `core/problem_solver/code_corrector.py` | Reescritura guiada por **AST**: solo `Name` en `Load` **libres**. `respuesta/response/result/data` → `dependencia(contexto,'Dep')`; `json_data/respuesta_json` y `json.loads(alias)` → `dependencia(...,'json')`; `dependencias` → `contexto`. Sin dependencia **no inventa**. |
| Validador | `core/problem_solver/validator.py` + `builder.py` | El builder escribe el código corregido de vuelta al paso (se valida lo que corre). Reglas nuevas: **nombre libre no definido** (excluye builtins/dunder/nombres inyectados y agentes), **clave de otro tipo** (`body` sobre LLM), `File copiar/mover` mismo path y `File escribir` sin dependencias. |
| Recovery | `core/plan_recovery.py` | El prompt del Plan B incluye el **error de runtime** y el **código/config del paso fallido**; el Plan B se valida con el `PlanValidator` completo (no solo sintaxis); el reintento correctivo recibe el error real y reglas explícitas de contrato. |
| Imagen | `core/executors/file_executor.py` | El escritor de documentos **descubre** referencias por valor (`_extraer_referencias_imagen`: claves documentadas, claves con pista `imagen/image/foto/img`, y cualquier ruta local que Pillow reconozca) y valida el **MIME real** antes de insertar. `FileExecutor.ejecutar` conserva el diccionario completo para escritores especializados cuando trae imágenes (antes lo colapsaba a texto). |
| Prompt | `core/problem_solver/prompt_builder.py` | Documenta `dependencia(...)`, `preparar_imagen(...)` y la regla «imagen raster real; la extensión no garantiza nada». |

Sin dependencias nuevas (Pillow y python-docx ya estaban).

## FASE 4 — Verificación

**Tests nuevos (pytest real, sin LLM):**

- `tests/test_sandbox_contract.py` (23) — corrector semántico, semántica de
  `dependencia`, contrato de imagen y reglas del validador.
- `tests/test_docx_e2e_sin_llm.py` (6) — plan → builder → validador →
  sandbox/File produce un `.docx` con párrafo > 200 chars e imagen PNG;
  regresión del `respuesta` que llegaba `'{}'`; imagen no raster reportada sin
  romper; `imagen_ruta` descubierta; descripción textual no confundida con imagen.

**Resultado:** `tests/repro/` colecta 0 (sigue siendo scripts-diagnóstico). Toda
la suite, **archivo por archivo en proceso limpio**, verde: 330+ tests de los
archivos afectados/relacionados, incluidos `test_sandbox.py` (54),
`test_security.py` (63), `test_main_cli.py` (43), `test_validador.py` (29),
`test_exporters.py` (23), `test_integration.py` (18), `test_llm_executor.py` (14).

**Flujo real con la instrucción original** (`deepseek-v4-pro`):

```
5 agentes (LLM + 4 Python) → ✅5 ❌0 ⛔0
GenerarCuento, GenerarImagen, PrepararImagenParaDocx,
EnsamblarDocumento, VerificarDocumento
```

Verificación independiente de `cuento_albondigas_frias.docx` (49712 bytes):

```
párrafos > 200 chars : 7   (máx. 545)
inline_shapes        : 1
word/media/image1.png -> PNG real (12829 bytes)
python-docx abre sin excepciones
```

El planificador usó **`preparar_imagen()`** de forma espontánea en
`PrepararImagenParaDocx`, prueba de que el contrato documentado se propaga.

**Nota de entorno (pre-existente, no causado por este trabajo):** la suite
completa en un solo proceso sufre `SIGSEGV` (exit 139) intermitente en tests de
scheduler + sandbox por la carrera de `fork`/hilos de CPython 3.13 (el propio
`core/sandbox.py` lo documenta). Comprobado en baseline limpio: 3 ejecuciones de
`tests/test_scheduler.py` → `139, 139, 0`. En procesos separados, todo pasa.

## Hallazgo adicional (fuera de alcance, propuesta)

El matcher A/B (`learning/embedding_matcher.py`, `UMBRAL_DEFAULT = 0.68`)
sustituye el prompt del `GenerarCuento` por una reescritura de **otra tarea**
(`prompts_reescritos` id=76: «Escribe un cuento de terror…», similitud
0.70–0.77). Por eso el cuento trata de sótanos y no de albóndigas frías, tanto
en la ejecución fallida original como en las de verificación. **No se ha
cambiado** (afecta al comportamiento de aprendizaje). Propuesta: exigir un
umbral más alto y/o comprobar que el `prompt_original` del candidato pertenece a
la misma intención antes de inyectarlo.

## Auditoría — lo que NO se ha tocado

- `main`, el tag `v3.5.0` y `release/v3.5.0`: intactos (sin merge).
- `tests/repro/*`: sin cambios.
- No se desactivó sandbox, validador, corrector ni learning.
- Sin dependencias nuevas.
- Firmas públicas compatibles: `PythonCodeCorrector.corregir(codigo, dependencias)`,
  `PlanValidator._validar_codigo_python_ast(...)` y `_validar_codigo_python(...)`
  (parámetro opcional añadido), `FileExecutor._file_escribir_docx(...)`,
  `PlanRecovery.generar_plan_b(...)`.
- Commits previos (`fix(llm)`, `fix(scheduler)`, `fix(validator)`, `feat(cli)`,
  `feat(web)`, `docs:`, `test:`) no revertidos.

## Propuesta de merge (no ejecutada)

`v3.5.0-fix-docx` → `release/v3.5.0` una vez revisado este informe. Antes del
merge: decidir si se aplica el hallazgo del matcher A/B (cambio separado) y
confirmar que la flakiness `SIGSEGV` se aborda en su propio issue.
