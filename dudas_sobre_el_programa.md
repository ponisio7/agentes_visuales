# Dudas sobre el programa

Documento vivo: recoge las preguntas sobre `agentes_visuales` y sus respuestas,
además de las propuestas anotadas que van saliendo.

- **Rama de referencia:** `harness/v3.7.0`
- **Última actualización:** 2026-09-20, tras la implementación de H1–H12
  (H1–H10 completas; H11 en fase 1 y H12 en preparación — ver §0 y §13).
- **Nota:** las secciones 1–12 conservan el **análisis original** (verificado
  contra el código de `v3.5.0-fix-docx`). Cada sección afectada lleva ahora un
  bloque **«Actualización v3.7.0»** con lo implementado y lo que sigue pendiente.
  La §13 (plan) está reescrita con el estado real.
- **Informe de la implementación:** `HARNESS_REPORT_v3.7.0-H1-H12.md`.
- **Análisis previo:** `HARNESS_ANALISIS_v3.7.0.md`.
- Cuando algo sigue siendo propuesta y no comportamiento actual, se marca como
  **[PROPUESTA]**.

---

## 0. Actualización v3.7.0 — qué cambió (resumen)

La fuente de verdad dejó de ser «los agentes terminaron» y pasó a ser **el
contrato de aceptación comprobado en disco/bytes**. Todo lo demás (recovery,
aprendizaje, API) se apoya ahora en ese veredicto.

| Fase | Estado | Qué se hizo | Dónde |
|------|--------|-------------|-------|
| **H1** Config API/modelo | ✅ | Archivo de secretos 700/600, `DEEPSEEK_MODEL`, botón `⚙ CONFIG`, reset del cliente sin reiniciar, el builder respeta el modelo | `core/ia_config.py`, `ui/config_dialog.py`, `core/llm_client.py`, `core/problem_solver/builder.py` |
| **H2** Matcher A/B | ✅ | Umbral 0.85 + compatibilidad de intención (firma/solape) + margen de duda + motivo registrado | `learning/embedding_matcher.py`, `core/problem_solver/builder.py` |
| **H3** Dependencias | ✅ | `sentence-transformers`, `torch`, `pypdf`, `ddgs` declarados; suite aislada por proceso | `requirements.txt`, `pytest.ini`, `tools/run_tests.sh`, `tools/verificar_dependencias.py` |
| **H5** Persistir problema | ✅ | Migración 10→11: `problema`, `plan_json`, `resultado`, `aceptada`, `motivo_fallo`, embedding | `storage/database.py`, `core/execution_recorder.py` |
| **H6** Verificación | ✅ | `VerificationEngine` determinista, gate real, `es_critico` operativo, evidencia en la API | `core/verification.py`, `core/scheduler.py`, `main.py` |
| **H10** Cliente de tareas | ✅ | Informe por tarea con verificación/artefactos y `--continue-on-error` | `tools/enviar_tarea_subproceso.py` |
| **H7** Plan B adaptativo | ✅ | Límites configurables, escalera de estrategias, anti-repetición por firma, reutilización de agentes válidos, `reparaciones_plan` en uso | `core/scheduler.py`, `core/plan_recovery.py`, `storage/database.py` |
| **H8** Retrieval de casos | ✅ | `obtener_casos_similares` solo sobre `aceptada=1`, umbral 0.85, 1–3 casos | `learning/engine.py`, `core/problem_solver/solver.py` |
| **H4** Log bus | ✅ | `core/log_bus.py` (deque acotado + handler + cursor), GUI, `GET /api/logs` y `GET /logs` | `core/log_bus.py`, `main.py`, `ui/simple_main_window.py`, `web/app.py` |
| **H9** API de jobs | ✅ | `POST/GET /api/jobs`, SSE de logs, cancel; `/run` intacto | `web/jobs.py`, `web/app.py`, `main.py` |
| **H11** Visión | ⚠️ | **Fase 1**: cliente multimodal + decisión validada por allowlist + kill switch. Sin bucle autónomo ni escritorio | `core/vision.py`, `core/llm_client.py` |
| **H12** VLM local | ⚠️ | **Preparación**: `DEEPSEEK_BASE_URL` local + herramienta de comprobación. Sin entrenamiento | `HARNESS_VLM_LOCAL.md`, `tools/comprobar_servidor_local.py` |

**Definición operativa de «tarea resuelta» (ya vigente):** `ok = true` solo si
**todos** los agentes terminaron bien **y** la aceptación pasó. Una ejecución con
`errores=1` (como la 469) ya **no** se guarda como `completada`. El `motivo`
estructurado alimenta al Plan B y las etiquetas del aprendizaje son reales.

**Validación:** suite completa aislada (`tools/run_tests.sh`, `--forked`):
**935 passed, 1 skipped**; prueba E2E determinista de
`fallo de aceptación → Plan B → artefacto correcto → aceptación con evidencia`
(`tests/test_flujo_e2e_aceptacion.py`).

**Sigue pendiente** (ver §13): paradas duras por error irrecuperable, reindexado
de embeddings de ejecuciones antiguas, cablear la visión como acción del navegador
y el agente de escritorio. Deuda técnica: `core/scheduler.py` supera las 500
líneas y conviene extraer el Plan B a un colaborador.

---

## 1. Si el usuario evalúa la respuesta como mala y deja un comentario, ¿el programa reescribe su prompt?

**Sí, pero de forma condicional y en dos etapas:** primero se *genera* una
reescritura y se guarda como **candidato**; solo después, y solo si gana el A/B,
pasa a ser la versión activa.

### Camino exacto

1. **El diálogo asigna el score** (`ui/simple_main_window.py:221`):
   Excelente `+1.0`, OK `0.0`, Mal `-1.0`.
2. **La UI guarda y decide** (`_guardar_y_procesar_feedback`, `:1036`):
   - Inserta en `feedback_usuario` con `alcance='plan'` y **`agente_ejecucion_id=NULL`** (`:1050-1063`).
   - `score > 0` → **no** reescribe (`:1074`).
   - `score <= 0` sin comentario → **no** reescribe (`:1076`).
   - `score <= 0` con comentario → lanza `FeedbackProcessor` en hilo daemon (`:1079-1105`).
3. **`FeedbackProcessor.procesar_feedback`** (`learning/feedback_processor.py:110`):
   - Exige `score<=0` y comentario (`:137-140`).
   - Elige el agente LLM: el de `agente_ejecucion_id` si es LLM; si no, **el último agente LLM por `orden DESC`** (`_agente_relevante`, `:242-272`).
   - Recupera el prompt crudo quitando el endurecimiento (`:153-163`).
   - Llama al LLM con `META_PROMPT` (prompt original + comentario + nombre/tipo/resultado) pidiendo `{"prompt_nuevo","razon"}` o `{"skip":true}` (`:43-75`, `:274-301`).
   - `skip` o vacío → no hay reescritura (`:177-189`).
   - Si no, guarda en `prompts_reescritos` con **`estado='candidato'`, `activo=1`**, firma y embedding (`_guardar_reescritura`, `:303-359`).
4. **Uso en runtime** (`PlanBuilder._kwargs_llm`, `core/problem_solver/builder.py:371-427`):
   busca por **similitud semántica** (embeddings, `UMBRAL_DEFAULT = 0.68`)
   una reescritura para `activo` y otra para `candidato`, y
   `PromptABEvaluator.elegir_variante` decide (`learning/prompt_ab_evaluator.py:66-95`):
   - activo + candidato → candidato con probabilidad **20%** (`PROBABILIDAD_CANDIDATO = 0.20`);
   - solo activo → activo;
   - **solo candidato → candidato al 100%**;
   - ninguno → prompt original.
5. **Promoción/descarte** (`core/execution_recorder.py:53-129`, `prompt_ab_evaluator.py:176-261`):
   se registra el uso, se le asigna el **score del evaluador LLM del plan**
   (no el score del usuario) y se promueve el candidato si mejora ≥ `+0.05`
   con ≥ 5 usos puntuados, o se descarta si empeora ≤ `-0.05` o empata tras 20 usos.

### Matices observados (con datos)

- **No hay ninguna reescritura `activo`** en la BD: 11 `candidato`, 5 `descartado`, 0 `activo`.
  Como el builder consulta activo y candidato por separado, al no haber activo
  cae en «solo candidato → 100%»: el 20% de exploración no se está aplicando.
- **El match es por similitud, no por tarea**: por eso una reescritura puede
  invadir otro problema (caso visto: `prompts_reescritos` id 76, «cuento de
  terror», similitud 0.70-0.77, sustituyendo el prompt de las albóndigas).
- **El feedback se atribuye al último agente LLM**, no al que falló
  (`agente_ejecucion_id=NULL` + `alcance='plan'`).
- **El meta-prompt va sin descripción del agente**: `_agente_relevante` no
  selecciona la columna `descripcion` (aunque la tabla sí la tiene).
- **El comentario solo redacta la reescritura**; el A/B se puntúa con el
  `EvaluadorLLM` (`evaluaciones_llm`, `alcance='plan'`). `ExtractorLecciones`
  tampoco usa el comentario.
- **Solo la UI procesa feedback**; CLI y web no llaman a `procesar_feedback`.
  El diálogo se ofrece 1 de cada 3 ejecuciones (`:1003-1007`).
- **No siempre reescribe aunque sea negativo**: 17 feedbacks negativos (todos
  con comentario) y varios sin fila (p. ej. fid 27 → `pid=None`); pudo devolver
  `skip`, fallar la llamada o no haber `prompt_usado`.
- `consultar_reescritura` (firma exacta) existe (`:209`) pero **no se usa en
  runtime**; el camino vivo es embedding + A/B.

### Actualización v3.7.0 (H2) — el matcher ya no secuestra prompts

El matiz «el match es por similitud, no por tarea» **está corregido**. Ahora una
variante solo se aplica si, además de superar el umbral:

1. **Sube el listón**: `UMBRAL_DEFAULT = 0.85` (antes 0.68), configurable con
   `AGENTES_AB_UMBRAL`.
2. **Pasa la comprobación de intención**
   (`compatible_por_intencion`, `learning/embedding_matcher.py`): misma **firma**
   semántica **o** solape léxico (Jaccard) ≥ 0.5 con la tarea que originó la
   reescritura. La similitud semántica **por sí sola ya no basta**.
3. **No hay duda**: si el segundo mejor candidato es de otra tarea y está a menos
   de `margen_duda` (0.03), no se aplica ninguna reescritura y se conserva el
   prompt original.

Además **se registra el motivo** de la elección: en el log, en el campo
`prompt_reescrito_motivo` del `Agente`, y en la columna `motivo` de
`prompt_reescrito_usos` (migración 9→10). El comportamiento prohibido
`prompt de A → reescritura de B → prompt incorrecto` queda cubierto por tests
con tareas semánticamente distintas (`tests/test_ab_matcher.py`).

**Sigue pendiente:** el desequilibrio del A/B (0 filas `activo`, varios
`candidato` ⇒ «solo candidato → 100%») no se toca; ahora es *seguro* (filtro de
intención), pero sigue sin haber explotación/exploración real. Y el feedback se
sigue atribuyendo al último agente LLM, no al que falló.

---

## 2. Los embeddings son muy útiles, ¿son «el programa»?

**No.** Son una pieza pequeña y opcional del subsistema de aprendizaje, con un
único cometido: reutilizar reescrituras de prompt.

### Dónde SÍ se usan (2 sitios reales)

1. `core/problem_solver/builder.py:378-427` — antes de ejecutar un agente LLM,
   busca por similitud coseno la reescritura más parecida al prompt crudo.
2. `learning/feedback_processor.py:320-354` — al guardar una reescritura,
   calcula y almacena el embedding del `prompt_original`.
3. Infraestructura: columnas `embedding`/`embedding_model` + índice
   (migración 7→8, `storage/database.py:931-953`). Utilidades sin llamadores:
   `recodificar_todos()`, `reset_matcher()`.

Modelo: `paraphrase-multilingual-MiniLM-L12-v2` (384 dims, ~470 MB) vía
`sentence-transformers`.

### Dónde NO se usan

- Planificación (`ProblemSolver`/`PromptBuilder`), ejecución (`Scheduler`,
  ejecutores, `sandbox.py`), validación (`validator.py`), recuperación
  (`plan_recovery.py`): cero embeddings.
- Los otros pilares de `learning/`: `FailurePredictor`/`PlanScorer`
  (features tabulares + scikit-learn), `EvaluadorLLM` (LLM),
  `ExtractorLecciones` (SQL/reglas).

### Avisos

- **Es la pieza más pesada y no está declarada**: en el venv hay
  `sentence-transformers 6.0.1` y `torch 2.14.0+cu130`, pero **ni
  `requirements.txt` ni `requirements-dev.txt` los listan**. Carga ~10 s.
- **Degrada en silencio**: sin el paquete, `calcular()` y `buscar_match()`
  devuelven `None` y el builder sigue con el prompt original.
- **Es el origen del «secuestro» de prompts** (match global a 0.68 sobre hasta
  500 candidatos).

### Actualización v3.7.0 (H2, H3, H5, H8) — dónde se usan ahora

- **Ya está declarado** (H3): `sentence-transformers>=3.0.0` y `torch>=2.0.0`
  figuran en `requirements.txt`; `tools/verificar_dependencias.py` comprueba la
  instalación limpia. Sigue degradando en silencio si faltan.
- **El secuestro a 0.68 está corregido** (H2, ver §1): umbral 0.85 + intención.
- **Hay un tercer uso real** (H8): `LearningEngine.obtener_casos_similares`
  (`learning/engine.py`) busca **ejecuciones con `aceptada=1`** por similitud del
  `problema` y devuelve 1–3 casos para inyectarlos en el prompt de planificación
  (`core/problem_solver/solver.py`, junto a las lecciones). Umbral conservador
  0.85 (`AGENTES_RETRIEVAL_UMBRAL`), máximo 1–3 (`AGENTES_RETRIEVAL_MAX`).
- **El embedding del problema se guarda** en `ejecuciones.problema_embedding` /
  `problema_embedding_model` (H5 + H8), calculado en background por
  `core/execution_recorder.py`.
- **Sigue sin usarse** en ejecución (`Scheduler`, ejecutores, `sandbox.py`),
  validación y recovery. En planificación entra solo como texto recuperado.

---

## 3. [✅ implementada en v3.7.0] Recuperar preguntas similares con éxito e inyectar el enfoque que funcionó

**Idea del usuario:** usar embeddings para encontrar preguntas ya almacenadas
con alta tasa de éxito e inyectar en el prompt que «esa solución sirvió en un
contexto similar».

**Veredicto (original):** es una extensión natural y de esfuerzo medio, pero
**hoy no se puede implementar** tal cual por dos motivos, y hay un riesgo de
calidad serio.

> ⚠️ **Actualización v3.7.0:** los dos prerequisitos están resueltos y la
> propuesta **está implementada** (H5 + H6 + H8). El análisis que sigue se
> conserva como contexto histórico; al final de la sección está el detalle de lo
> construido.

### Prerequisito 1 — la pregunta no se guarda

`ejecuciones` es: `id, fecha, duracion_total, agentes_total, completados,
errores, cancelados, estado, ejecutor, tags, notas`. **No hay ninguna columna
con la pregunta/problema del usuario.** Solo quedan los agentes generados
(`agentes_ejecucion.prompt_usado`/`descripcion`), no la pregunta original. Sin
ese dato no hay nada que embeber; hay que empezar a guardarlo desde ahora
(no hay histórico retroactivo limpio).

### Prerequisito 2 — «100% de éxito» por estado NO es éxito

Datos de `agent_history.db`:

- `ejecuciones.estado` vale **`completada` en las 469 filas** → no discrimina.
- La etiqueta existente es por **agente** (`learning/feature_extraction.py:143`,
  `etiqueta_exito` mira `estado` = Completado): significa «el agente terminó»,
  no «el resultado sirve».
- **394 ejecuciones con 0 errores**, y **15 de ellas tienen feedback negativo**
  del usuario. Ejemplo real: la ejecución 469 fue `✅2 ❌1`; otra `✅5/5`
  produjo un `.docx` sin imagen.

→ Filtrar por «0 errores» inyectaría basura como si fuera una buena solución.

### Diseño propuesto

1. **Persistir la pregunta**: columna `problema` en `ejecuciones` (o tabla
   `casos`) + migración de BD (v8 → v9).
2. **Embedding de la pregunta**: reutilizar `EmbeddingMatcher`
   (`learning/embedding_matcher.py`), que ya serializa float32 a BLOB.
3. **Etiqueta de éxito compuesta** (guardada como columna `exito` 0/1):
   - `errores == 0`, **y**
   - `evaluaciones_llm` de alcance `plan` por encima de un umbral
     (hoy hay 87 filas con score de plan), **y**
   - `feedback_usuario.score >= 0` cuando exista.
4. **Inyección en el punto que ya existe**: `ProblemSolver.resolver_problema`
   (`core/problem_solver/solver.py:122-132`) ya añade lecciones al prompt con
   `engine.obtener_lecciones_para_prompt()`. Ahí iría
   `engine.obtener_casos_similares(problema)` → top-1..3 con umbral **alto
   (≥0.85, no 0.68)**, mostrando: pregunta, plan resumido (nombres + tipos +
   claves de contrato) y score. **Nunca** el resultado crudo completo.
5. **Filtro por modelo**: la tabla ya tiene `embedding_model`; si se cambia de
   modelo hay que recalcular (`recodificar_todos()` existe pero no lo llama
   nadie).

### Guardrails

- **Similitud ≠ misma tarea**: el umbral 0.68 actual ya secuestra prompts entre
  tareas distintas. Para inyectar *soluciones*, subir mucho el listón y, mejor,
  pedir al LLM que confirme que el caso es aplicable, o exigir solapamiento de
  entidades/claves del contrato.
- **Sesgo de supervivencia**: mostrar solo éxitos oculta que una tarea es
  difícil y puede empujar a copiar una estructura que no encaja.
- **Contaminación del historial**: limpiar/etiquetar con el feedback antes de
  usarlo (hoy hay 15 casos «sin errores pero malos»).
- **Es una pista, no una verdad**: no puede sustituir al validador, al sandbox
  ni al recovery.
- **Coste**: embeddings + torch (~10 s de carga) y sin declarar en
  `requirements.txt`.

### Formulación recomendada

Presentarlo como *«enfoques previos que funcionaron en problemas parecidos;
verifícalos»*, no como *«esta solución sirvió»*.

**Esfuerzo estimado:** medio — migración de BD + 1 columna + un método en
`LearningEngine` + un método en `EmbeddingMatcher` + el bloque de prompt +
tests (con fallback si no hay match y con el bloque desactivable).

### ✅ Actualización v3.7.0 (H5 + H6 + H8) — implementado

- **Prerequisito 1 resuelto (H5).** `ejecuciones` ya guarda `problema`,
  `plan_json`, `resultado`, `aceptada` y `motivo_fallo` (migración 10→11), más
  `problema_embedding` / `problema_embedding_model`. El recorder los rellena
  antes de lanzar el hilo de aprendizaje.
- **Prerequisito 2 resuelto (H6).** El éxito fiable no es `estado='completada'`
  (que ya es honesto) sino **`aceptada = 1`**, el veredicto del
  `VerificationEngine` sobre el artefacto. El retrieval filtra por `aceptada=1`.
- **Método implementado (H8).** `LearningEngine.obtener_casos_similares(
  problema, max_casos=None, umbral=None)` reutiliza `EmbeddingMatcher`, con
  umbral 0.85 y 1–3 casos; `formatear_casos_para_prompt` /
  `obtener_casos_para_prompt` generan el bloque de prompt.
- **Inyección en el punto previsto.** `core/problem_solver/solver.py` añade el
  bloque justo después de las lecciones, solo si hay casos.
- **Cómo se presenta** (la formulación recomendada): «ENFOQUES UTILIZADOS
  ANTERIORMENTE EN PROBLEMAS SIMILARES… evalúa si son aplicables; **NO los copies
  ciegamente**», con problema resumido, plan, tipos de agente, criticidad/contrato
  y resultado/score. **Nunca** el resultado crudo completo (se recorta).
- **Filtro por modelo** ya existe: los candidatos deben tener
  `problema_embedding_model` igual al del matcher actual.
- **Guardrails cubiertos:** similitud ya no basta sola (H2), solo se inyectan
  éxitos verificados, y el bloque es una pista (no sustituye a validador,
  sandbox ni recovery).
- **Sigue pendiente:** reindexar (`recodificar_todos`/backfill) las ejecuciones
  antiguas sin embedding; hoy solo aparecen las nuevas. Y no se inyecta
  feedback explícito del usuario, solo el score LLM si existe.

---

## 4. ¿Puede haber al final de una tarea un agente que verifique la salida?

**Sí, de hecho ya ocurre**, pero **no existe un mecanismo de verificación de
salida de primera clase**: es el planificador el que, a veces, añade un último
paso `Python` de verificación. No hay un tipo de agente «Verificador».

> ✅ **Actualización v3.7.0 (H6):** ya existe una **capa de verificación de
> primera clase** (`core/verification.py`) y un **gate de estado**. Sigue sin
> haber un *tipo de agente* `Verificador` (Nivel 3), pero el problema de fondo
> —«éxito» = «los agentes terminaron»— está resuelto. El análisis original se
> conserva debajo; el detalle de lo implementado está al final de la sección.

### Lo que ya pasa

- Tipos disponibles: `Python, Shell, LLM, HTTP, File, Loop, Browser, Search`
  (`core/agent.py`, enum `TipoAgente`). Ninguno es «verificador».
- El LLM sí lo genera a menudo: en `agent_history.db` hay **56 ejecuciones con
  un paso de verificación/validación como último `orden`**, y nombres como
  `VerificarArchivo` (25 usos, 17 completados), `ValidarCuento` (11 usos, 10),
  `VerificarResultado` (6 usos, 6). En la corrida real del fix apareció
  `VerificarDocumento`.
- El prompt de diseño pide «CUBRE TODOS LOS REQUISITOS EXPLÍCITOS» y una
  autocomprobación *del plan* antes de responder
  (`core/problem_solver/prompt_builder.py:608-616`), pero **no obliga** a un
  paso final de verificación.

### Lo que NO existe (mecanismos reales y sus límites)

- `es_resultado_sospechoso` (`core/executors/helpers.py:96`): heurística de
  resultado vacío. **Solo avisa**: en `python_executor.py:80-86` añade
  «(⚠️ sospechoso: …)» al mensaje y en `llm_executor.py:593-597` solo loguea.
  No falla el agente.
- `EvaluadorLLM` (`learning/reward_llm.py`): puntúa la salida con un LLM por
  agente y por plan, **en background** (`core/execution_recorder.py`). Alimenta
  aprendizaje/A-B; **no bloquea ni reintenta**.
- `PlanValidator` (`validator.py`) y `Agente.validar_configuracion`: validan
  **configuración y plan antes de ejecutar**, no la salida.
- `es_critico` (`models.py:26`, `builder.py:127`): se parsea y se usa **solo**
  como feature numérica para el predictor (`learning/feature_extraction.py:127`).
  **El scheduler no lo usa**: un paso «crítico» fallido no marca la ejecución
  como fallida (la ejecución 469 se guardó como `completada` con `❌1`).

### [PROPUESTA] Cómo hacerlo bien, en tres niveles

**Nivel 1 — barato:** activar `es_critico` como gate real. Si un paso crítico
falla o su resultado es sospechoso → la ejecución se marca fallida (no
`completada`) y se dispara recovery con ese motivo.

**Nivel 2 — recomendado:** «criterios de aceptación» declarados + verificador
determinista. El plan declara qué produce (archivos, claves) e invariantes
comprobables: existe, tamaño > 0, MIME real (`formato_imagen_real`), > N
caracteres, JSON parseable, claves del contrato presentes. El sistema los
comprueba **en disco/bytes**, no en el texto que el LLM dice haber generado.
Si falla → motivo a `PlanRecovery`.

**Nivel 3 — semántico:** reutilizar `EvaluadorLLM` con umbral y bloqueo
opcional; y/o un tipo de agente `Verificador` de primera clase (tocaría enum,
prompt, executor y validador).

### Guardrails

- Verificar el **artefacto real** (disco/bytes), no la afirmación del LLM.
- Distinguir «no verificable» de «verificado»; no dar por bueno lo que no se
  pudo comprobar.
- Límite de reintentos (ya existe: máx. 2 Plan B) para no entrar en bucle.
- El verificador no puede aprobarse a sí mismo ni sustituir al validador/sandbox.
- No bloquear la UI si la verificación es costosa.

**Mi recomendación:** Nivel 1 + Nivel 2. Se puede empezar por declarar
invariantes en los pasos que producen archivos (el caso `.docx` de esta sesión
habría detectado «0 imágenes insertadas» sin depender del LLM).

### ✅ Actualización v3.7.0 (H6) — Nivel 1 + Nivel 2 implementados

**Diseño implementado**

1. **Clases / API** (`core/verification.py`):
   - `ContratoAceptacion` (en `core/problem_solver/models.py`): qué debe cumplir
     la salida (`archivos`, `imagenes`, `formato_imagen`, `min_bytes`,
     `min_caracteres`, `json_parseable`, `claves_requeridas`,
     `requiere_imagen`/`min_imagenes`, `min_items`, `max_errores`, `directorio`,
     `min_archivos`, `archivos_esperados`, `validar_contenedor`).
   - `Comprobacion` y `ResultadoVerificacion` (= **`VerificationResult`**), con
     `ok`, `estado` (`verificado`/`no_verificable`/`fallido`), `motivos`,
     `advertencias`, `evidencias`, `criterios_comprobados`, `criterios_fallidos`.
   - Fachada **`VerificationEngine`** (`verificar`, `verificar_contrato`,
     `criterios_disponibles`).
2. **Flujo**: tras ejecutar cada paso, el scheduler llama a
   `verificar_agente(agente, resultado)` **fuera del lock** (toca disco) y guarda
   el veredicto por agente. Si el contrato no se cumple → el paso pasa a `ERROR`
   con el motivo, **sin reintentar** (el contrato es determinista).
3. **Gate de estado**: `Scheduler.obtener_resultado_aceptacion()`. La ejecución
   solo se acepta si todos los agentes terminaron bien y todos los pasos
   críticos/con contrato pasaron la verificación. `main._ejecutar_pipeline`
   calcula `ok`/`estado` con eso y añade `aceptacion` al JSON.
4. **`es_critico` operativo (Nivel 1)**: un paso crítico con resultado vacío o
   sospechoso (`es_resultado_sospechoso`) pasa a `ERROR` y tumba la ejecución.
   Ya no es solo una feature del predictor.
5. **Motivo al Plan B**: el texto `Aceptación fallida: …` viaja como `razon` a
   `PlanRecovery` (§5).
6. **Registro**: `execution_recorder` guarda `estado`, `aceptada` y
   `motivo_fallo` (migración 10→11) y la migración 8→9 reclasifica las
   ejecuciones históricas con `errores>0` que figuraban como `completada`
   (incluida la **469**).
7. **Validador estático** (`validator.py`): rechaza de forma `BLOQUEANTE` un
   contrato imposible (un archivo que ningún paso produce; imágenes exigidas sin
   ninguna fuente raster).
8. **Criterios comprobables**: existencia y tamaño, **formato real de imagen**
   (`formato_imagen_real`, no la extensión), imágenes incrustadas en
   `.docx/.odt/.pptx/.xlsx/.pdf`, JSON parseable, claves, longitud de texto,
   items/errores, **directorio** (`min_archivos`, `archivos_esperados`) y
   **contenedor válido** (estructura interna: detecta documentos corruptos).
9. **Guardrails**: verifica el artefacto real; distingue «no verificable» de
   «verificado»; solo lee (no escribe, no ejecuta); no sustituye al validador ni
   al sandbox; no bloquea la UI.

**Tests**: `tests/test_verification.py`, `tests/test_aceptacion_plan.py`,
`tests/test_scheduler_aceptacion.py` y el E2E
`tests/test_flujo_e2e_aceptacion.py` (fallo → Plan B → artefacto correcto →
aceptación con evidencia). Cubren los 4 casos obligatorios: `.docx` sin imagen →
fallida con motivo; `.docx` con imagen → completada; JSON no parseable → fallida;
`errores=1` → fallida.

**Nivel 3 (sigue pendiente):** no hay tipo de agente `Verificador` de primera
clase ni bloqueo por `EvaluadorLLM` con umbral. La verificación semántica no
bloquea.

---

## 5. ¿Puede el Plan B ser infinito hasta conseguir ejecutar la tarea, usando los agentes que fallaron como ejemplo para reestructurar o cambiar tipos?

**Hoy no: está capado en 2 intentos**, y el cap está *hardcodeado*. Pero sí
recibe el fallo como contexto y **sí puede cambiar tipos de agente** (por
instrucción del prompt, no por una lógica estructural).

> ✅ **Actualización v3.7.0 (H7):** sigue **sin ser infinito** (a propósito), pero
> el cap ya es **configurable** (por defecto 3) y hay **presupuesto de tiempo**,
> **escalera de estrategias**, **anti-repetición por firma**, **memoria de fallos
> por intento** en `reparaciones_plan` y **reutilización de los agentes que ya
> completaron y verificaron**. Faltan las paradas duras por error irrecuperable.
> Detalle al final de la sección; el análisis original se conserva debajo.

### Límite actual

- `Scheduler.__init__`: **`self._max_intentos_plan_b = 2`**
  (`core/scheduler.py:127`). No es configurable (no hay constante ni ajuste en
  `configs/`).
- `_reclamar_plan_b` (`:840-862`) solo concede el turno si hay `recovery`
  inyectado, no hay un Plan B en curso y `_plan_b_intentos < max`.
- Agotado el cap: `_intentar_plan_b` devuelve `False`, los dependientes se
  bloquean y los agentes no terminales pasan a `BLOQUEADO`
  («🚫 Plan B agotado…», `:987-1002`). La ejecución termina.
- `_plan_b_intentos` se reinicia en `set_contexto_plan_b` (`:180-186`), que se
  llama una vez por ejecución (`main.py:512`, `ui/simple_main_window.py:847`).

### Lo que SÍ hace ya (usar el fallo como ejemplo)

- `generar_plan_b(problema_original, plan_fallido=self._plan_original,
  agente_fallido, error)` (`scheduler.py:972-977`).
- `_resumir_plan_fallido` lista los pasos del plan fallido; y desde el fix
  `v3.5.0-fix-docx`, `_resumen_agente_fallido` incluye el **código/config del
  paso que falló** (`codigo_python`, `prompt_llm`, `url_http`, …) en el prompt.
- `PROMPT_PLAN_B` ordena: «NO repitas los mismos pasos», «cambia de
  estrategia», distingue error de sintaxis vs contrato, y ofrece todos los
  tipos `Python|Shell|HTTP|LLM|File|Loop|Browser|Search`
  → **puede cambiar tipos**, pero nada lo fuerza ni lo verifica.
- Tras cada intento, `self._plan_original = plan_b` (`:1033`), así que el
  siguiente Plan B ve el plan que acaba de fallar.

### Lo que falta para un modo «insistir hasta lograrlo»

- **Cap configurable**: exponer `_max_intentos_plan_b` (constructor/config) con
  un techo de seguridad, en vez de `2` fijo.
- **Presupuesto global**: límite de tiempo y/o de llamadas/tokens por ejecución.
  Sin esto, «infinito» es una factura abierta.
- **Guardia anti-repetición**: firmar cada plan intentado (nombres + tipos +
  hash de config clave) y recordar los fallidos; si el nuevo plan repite una
  firma ya fallida, rechazarlo y reintentar con «esto ya falló: `<error>`».
  Hoy solo se pide «no repitas» en el prompt.
- **Escalera de estrategias**: intento 1 arreglo puntual → intento 2 cambio de
  fuente/enfoque → intento 3 cambio de tipo de agente / descomposición →
  intento 4 fallback (datos sintéticos, librería alternativa).
- **Memoria de fallos por intento**: inyectar la lista
  `(paso, tipo, error)` ya probada. La tabla `reparaciones_plan` existe en el
  esquema pero **está vacía: nadie escribe en ella**.
- **Condiciones de parada duras**: errores irrecuperables (falta de
  dependencia/API key, problema inválido, bloqueo de seguridad) → abortar sin
  gastar intentos.
- **No tirar el trabajo bueno**: hoy cada Plan B hace `agentes.clear()` y
  reinicia TODO desde cero (`:1015-1030`), incluidos los agentes que ya habían
  completado. Con muchos intentos eso multiplica coste y tiempo.

### Riesgos de hacerlo infinito sin guardarraíles

- Coste y tiempo sin techo; la UI podría no terminar nunca.
- «Thrashing»: planes distintos que fallan por lo mismo una y otra vez.
- Deriva de contexto: cada reescritura parte de la anterior y puede acumular
  supuestos equivocados.
- El Plan B solo se dispara cuando el fallo **bloquearía dependientes**
  (camino crítico), no ante cualquier fallo.

**Recomendación:** cap configurable (p. ej. 5-10) + presupuesto de
tiempo/tokens + guardia anti-repetición + escalera de estrategias + paradas
duras. Es decir, «insistir mucho» pero **acotado**, no infinito.

### ✅ Actualización v3.7.0 (H7) — implementado (y lo que falta)

- **Cap configurable.** `Scheduler(max_intentos_plan_b=…)` o la variable
  `AGENTES_PLAN_B_MAX_INTENTOS` (por defecto **3**). Ya no está fijo en 2.
- **Presupuesto de tiempo.** `presupuesto_plan_b_seg` /
  `AGENTES_PLAN_B_MAX_SEGUNDOS` (por defecto 300 s) en `_reclamar_plan_b`: sin
  esto «infinito» sería una factura abierta. **No** hay presupuesto de tokens.
- **Anti-repetición por firma.** `firma_plan` / `firma_agente`
  (`core/plan_recovery.py`) resumen tipos, nombres, dependencias y configuración
  relevante (ignoran estado/resultado). El plan que acaba de fallar entra en
  `_firmas_plan_fallidas`; si el plan generado repite una firma fallida,
  `generar_plan_b` lo **descarta** y se pide otra estrategia (además de avisarlo
  en el prompt).
- **Escalera de estrategias** explícita:
  `correccion_puntual → cambiar_configuracion → cambiar_tipo_agente →
  reestructurar_plan → fallback_alternativo`; cada intento usa la siguiente.
- **Memoria de fallos por intento.** `_reparaciones_intentadas` alimenta el
  bloque `formatear_error_estructurado` del prompt
  (`PASO / TIPO / ERROR / ESTRATEGIA / YA INTENTADO / NO REPETIR`), y cada
  intento se persiste en **`reparaciones_plan`** (migración 11→12 añade
  `ejecucion_id`, `intento`, `agente`, `error`, `estrategia`, `plan_firma`,
  `resultado`, `exito`). La tabla **ya no está vacía**.
- **No tirar el trabajo bueno.** Antes de reiniciar, se guardan los agentes
  `COMPLETADO` cuyo artefacto **pasó la verificación**; si el plan nuevo contiene
  un agente con la **misma firma**, se reutiliza su resultado y su verificación
  sin reejecutarlo (`A→B→C→D` con `D` fallido no repite `A/B/C`).

**Sigue pendiente:** condiciones de parada duras (falta de API key/dependencia,
problema inválido, bloqueo de seguridad) para no gastar intentos; y presupuesto
de tokens/coste. Nota: el Plan B se **intenta ante cualquier fallo terminal**
(error, timeout o fallo de aceptación), tenga o no dependientes; `_bloquear_dependientes`
primero pide el Plan B y solo si no lo consigue bloquea a los dependientes.

---

## 6. ¿Se puede hacer que todos los logs del terminal salgan también en el «stdout» de la GUI y en la página web?

**Sí, es viable y el cambio es contenido.** Hoy las tres salidas están
desconectadas entre sí.

> ✅ **Actualización v3.7.0 (H4):** ya existe el **bus de logs unificado**
> (`core/log_bus.py`) y la GUI, la web y el terminal beben de la misma fuente.
> Quedan ~130 `print()` sin migrar (los de depuración de la UI sí se migraron).
> Detalle al final de la sección.

### Estado actual

- **Terminal**: `main._configurar_logging()` (`main.py:248-287`) configura el
  root logger con `logging.basicConfig(force=True)` (consola) +
  `RotatingFileHandler` a `logs/agentes_visuales.log`. Todo lo que use
  `logging` acaba ahí.
- **GUI**: el panel «── stdout ──» es un `QTextEdit` (`ui/simple_main_window.py:593`)
  y solo se escribe desde `_log()` (`:678`), que recibe:
  - mensajes propios de la UI,
  - `scheduler.log_mensaje` (señal Qt, `:671`),
  - `_log_desde_worker` (señal con `QueuedConnection`, `:466`).
  **No está suscrito al root logger**, por eso el terminal muestra mucho más.
- **Web**: `web/app.py` solo tiene `/`, `/api/health`, `/api/agents`, `/api/run`
  (`:162-230`). **No hay endpoint de logs**; la plantilla solo pinta
  `<pre id="resultado">` (`web/templates/index.html:55`).

### Dos fuentes distintas en «la terminal»

1. Registros de `logging` (la mayoría).
2. **`print()` crudos**: hay ~136 en `core/` + `ui/` (p. ej.
   `execution_recorder.py` imprime `[TERMINADA]…`, `feedback_processor.py`
   imprime «Respuesta cruda…»). Un handler de logging **no** los captura; para
   esos hay que redirigir `sys.stdout`/`sys.stderr` o convertirlos a `logger`.

### Cómo hacerlo

1. **Handler propio** (`logging.Handler`) que formatea cada registro y lo
   añade a un buffer thread-safe acotado (`collections.deque(maxlen=2000)`),
   instalado en el root logger **después** de `_configurar_logging()` (porque
   `force=True` borra handlers). Ese buffer es la fuente única para GUI y web.
2. **GUI**: el handler emite una `pyqtSignal(str)` y la ventana la conecta con
   `Qt.ConnectionType.QueuedConnection` → los logs de hilos de trabajo se
   pintan en el hilo principal sin tocar el `QTextEdit` desde otro hilo. Es el
   mismo patrón que ya usa `_log_desde_worker`. Poner
   `self.log.document().setMaximumBlockCount(...)` para no crecer sin límite y
   evitar recursión (no loguear desde dentro del handler).
3. **Web**: exponer el buffer con
   - **polling** `GET /api/logs?desde=<cursor>` → `{registros, cursor}` (más
     robusto: el modo `serve` usa `ThreadingHTTPServer` propio, no Flask), o
   - **SSE** `GET /api/logs/stream` (`text/event-stream`, requiere
     `stream_with_context` y un generador vivo).
   Y añadir un `<pre id="log">` + `setInterval`/`EventSource` en `index.html`.
4. **Los dos a la vez**: hoy no existe un modo que muestre GUI y web en el
   mismo proceso. `main.py web` (`:901-996`) levanta Flask + bucle Qt **sin
   ventana**; la GUI es otro modo. Para tener ambos habría que arrancar Flask
   desde la ventana (o añadir `--web` al modo GUI); el buffer compartido lo
   haría directo.

### Guardas a respetar

- Añadir el handler **después** de `basicConfig(force=True)`.
- Thread-safety: `deque.append` es atómico en CPython, pero el cursor para
  «desde» conviene con lock o contador monotónico.
- Volumen: la consola en INFO genera mucho; buffer acotado, nivel configurable
  y filtro por logger. Decidir si los logs de `werkzeug` entran o se filtran.
- No romper `--quiet` (nivel ERROR) ni el `RotatingFileHandler`.
- No bloquear la UI: si el buffer va lleno, descartar lo más viejo, nunca
  esperar.

**Esfuerzo:** bajo-medio, sin tocar la lógica de agentes (handler + señal Qt +
endpoint + panel en la plantilla + tests).

### ✅ Actualización v3.7.0 (H4) — implementado

- **Fuente única**: `core/log_bus.py` con `BusLogs` (un
  `collections.deque(maxlen=2000)` + lock + **cursor monotónico**) y
  `HandlerBus` (`logging.Handler`) instalado en el root logger por
  `main._configurar_logging()` tras `basicConfig(force=True)`.
  `instalar_handler()` es idempotente.
- **Anti-recursión / no bloqueo**: `emit()` se ignora a sí mismo (logger
  `core.log_bus`) y lleva un guard por hilo; el emisor solo paga un `append` bajo
  lock. Si el bus falla, traga la excepción para no romper a quien loguea.
- **Terminal y fichero**: se mantienen `basicConfig`, `RotatingFileHandler`,
  `--quiet` y los niveles; el handler del bus es **uno más**.
- **GUI**: `SimpleMainWindow._iniciar_puente_logs()` lee el bus por cursor con un
  `QTimer` (400 ms) y pinta solo lo nuevo, con color por nivel. Hay un **dedupe
  de 1.5 s** para no duplicar lo que ya llega por `scheduler.log_mensaje`. No se
  toca el `QTextEdit` desde hilos.
- **Web / `serve`**: `GET /api/logs?cursor=&limit=` (Flask) y `GET /logs`
  (`serve`, stdlib) devuelven `{entradas, cursor}` para *polling* incremental.
- **Jobs**: `GET /api/jobs/<id>/logs` usa el mismo bus en **SSE** (H9).
- **`print()`**: se migraron a `logging` los 4 de depuración
  `[TERMINADA] …` de la GUI. **No** se tocaron los prints contractuales (salida
  de la CLI, `--check-env`) ni el protocolo del sandbox (`__RESULT__`/`__ERROR__`
  del proceso hijo).

**Sigue pendiente:** convertir a `logging` el resto de `print()` de `core/`
(~130, muchos en ramas de diagnóstico) sin cambiar el comportamiento observable;
y un panel de logs en `web/templates/index.html` que consuma `/api/logs`.

---

## 7. ¿Se podrían añadir agentes de visión de pantalla y control de ratón/teclado?

**Técnicamente sí, pero hoy no hay nada de eso y no lo metería sin un diseño de
seguridad.** Lo que existe es automatización **dentro del navegador**
(Playwright), no del escritorio.

### Lo que ya existe

- Agente `Browser` (Playwright) con acciones `esperar, extraer, click,
  rellenar, scroll, screenshot, ejecutar_js, navegar`
  (`core/executors/browser_executor.py:63-65`). Es decir, **ratón y teclado ya
  existen a nivel de página**, y las capturas se guardan en
  `outputs/screenshots` (`:55`).
- **Nada de escritorio**: `pyautogui`, `pynput`, `mss`, `cv2` **no están
  instalados ni declarados** en `requirements.txt`.
- **El cliente LLM es texto plano**: `chat` construye
  `{"role": "user", "content": prompt}` (`core/llm_client.py:503-509`); no hay
  bloques de imagen, base64 ni `image_url`. Hacer «visión» exige (a) capturar
  pantalla y (b) un modelo multimodal que entienda la imagen → habría que
  extender el cliente y verificar que el modelo/proveedor lo soporta.
  → ⚠️ **Actualización v3.7.0:** ya existe `LLMClient.completar_multimodal` y
  `core/vision.py` (fase 1, kill switch desactivado por defecto). Ver el bloque
  de actualización al final de la sección.
- **El sandbox no restringe imports** (su propio docstring dice que «NO es un
  sandbox de seguridad real»), así que un agente `Python` generado por el LLM
  podría hacer `import pyautogui` en cuanto la librería estuviera instalada.
  La capacidad «entra» sin necesidad de un tipo nuevo, y eso es parte del
  riesgo.

### [PROPUESTA] Cómo lo haría, por fases

**Fase 1 — visión de página (barata y segura).** Ampliar el agente `Browser`:
Playwright ya da click/teclado/screenshot; «ver» = pasar el PNG a un modelo
multimodal y decidir la siguiente acción. Es un bucle percepción-acción acotado
al navegador (hay DOM, selectores y `ejecutar_js`, así que no dependes solo de
píxeles).

**Fase 2 — escritorio.** Un tipo nuevo `Escritorio`/`Vision`, con:
- **Captura**: `mss`/`PIL.ImageGrab` → PNG (multiplataforma y rápido).
- **Grounding**: modelo multimodal que devuelva acción + coordenadas; o mejor,
  árbol de accesibilidad/UI en lugar de solo píxeles (más robusto).
- **Actuador**: `pyautogui`/`pynput` (ratón, teclado, atajos).
- **Contrato de salida documentado** como los demás tipos
  (`{accion, ok, captura, ...}`) en `prompt_builder.CONTRATOS_SALIDA`.
- **Reglas en el prompt + validador** (coordenadas dentro de pantalla, acciones
  permitidas) + tests. No basta con añadirlo al enum.

### Riesgos y guardas obligatorias

- **Fragilidad**: resolución, escalado/DPI, tema y ventanas que se mueven
  cambian las coordenadas → un click puede acabar en «Borrar todo». Sin DOM ni
  «deshacer».
- **Seguridad**: controlar ratón/teclado permite hacer cualquier cosa (borrar
  archivos, enviar mensajes, comprar). Haría falta: **allowlist** de
  apps/ventanas, confirmación humana para acciones destructivas, **kill switch**
  (atajo de pausa/abort), límite de acciones por tarea y registro auditable.
- **El sandbox no protege**: al no ser un sandbox real, hay que asumir que el
  LLM puede ejecutar lo que quiera. Hoy ya puede usar `Shell` (con validaciones
  y elevación `pkexec`); el escritorio amplía muchísimo la superficie.
- **Coste/latencia**: capturar y enviar una imagen al modelo en cada paso es
  caro y lento.
- **Tests**: en CI sin pantalla hay que mockear captura y actuador.

**Recomendación:** empezar por «visión de página» con Playwright (segura y
testable) usando un modelo multimodal; el escritorio, solo como agente
**opt-in**, con allowlist y kill switch, y nunca activado por defecto.

### ⚠️ Actualización v3.7.0 (H11, fase 1) — visión de página, sin escritorio

- **Cliente multimodal** (`core/llm_client.py`): `completar_multimodal(texto,
  imagenes, …)` construye mensajes con `image_url` + base64 (formato compatible
  con OpenAI) reutilizando el mismo `base_url`/modelo, así que también sirve para
  un servidor local (H12). No cambia el comportamiento por defecto.
- **Capa de visión** (`core/vision.py`):
  `construir_mensajes_multimodales`, `codificar_imagen` y
  `decidir_accion_desde_captura(llm, captura, instruccion, …)` →
  `{"razon", "accion"}` con la acción **validada contra el allowlist**
  (`esperar, extraer, click, rellenar, scroll, screenshot, ejecutar_js,
  navegar`). **No ejecuta** la acción: la decisión la ejecutaría el agente
  Browser con sus mecanismos de siempre.
- **Kill switch**: `AGENTES_VISION_HABILITADA` (por defecto **desactivado**). Con
  la visión apagada **ni se consulta** al modelo.
- **Seguridad**: sin ratón/teclado de escritorio, sin `pyautogui`/`pynput`/`mss`
  (siguen sin instalarse); la visión es de **página** y la acción pasa por el
  allowlist.

**Sigue pendiente (fase 1 completa):** cablear el bucle opt-in
`screenshot → decidir → ejecutar` como acción declarativa del `Browser` (hoy son
piezas probadas por separado). **Fase 2 (escritorio):** no implementada, como
recomendaba la sección.

---

## 8. ¿Puede el LLM de visión ser entrenado con mi propia pantalla?

**Respuesta corta: sí, puede**, y depende de tres cosas: si el modelo es local o
de API, la política del proveedor y si tú haces *fine-tuning*. No es lo mismo
«enviar una captura» (inferencia) que «entrenar con ella», pero la frontera la
define la política del proveedor.

### Casos

1. **Modelo local** (un VLM en tu máquina vía Ollama/llama.cpp/transformers):
   lo que captura la pantalla **no sale del equipo**; no se entrena con tu
   pantalla salvo que tú ejecutes un *fine-tuning* con tus capturas.
2. **API de un proveedor**: la captura viaja como *input*. La pregunta es qué
   hace el proveedor con ese input.
3. **Fine-tuning propio**: si tú entrenas un modelo con tus capturas, entonces
   sí — pero es una decisión explícita tuya.

### Qué dice DeepSeek (el proveedor que usa el proyecto)

Según su [política de privacidad](https://cdn.deepseek.com/policies/en-US/deepseek-privacy-policy.html)
(actualizada 2026-02-10):

- Recoge **«User Input»**: texto, voz, *prompt*, **archivos subidos, fotos**,
  historial de chat, etc.
- Entre los usos declarados está: **«to improve and develop the Services and to
  train and improve our technology, such as our machine learning models and
  algorithms»**. También comparte datos con su grupo corporativo para
  *«foundation model training and optimization»*.
- Almacena y procesa los datos en **China**.
- Reconoce el **derecho a opt-out** del uso de tus datos para entrenar modelos.
- **Matiz importante**: la propia política dice que **no cubre** los datos de
  usuarios finales que llegan a través de aplicaciones de terceros construidas
  sobre su *open platform*; ahí el **responsable es el desarrollador** (tú) y
  debes informar a tus usuarios.

Para otros proveedores conviene mirar su página de controles de datos (p. ej.
OpenAI documenta controles específicos para la API en
[developers.openai.com](https://developers.openai.com/api/docs/guides/your-data)):
las condiciones cambian entre API, planes de consumo y planes empresariales.

### Además del proveedor

- La app ya guarda **capturas en `outputs/screenshots`** y logs en `logs/`, y
  resultados en `agent_history.db`. Eso es local y no se envía… salvo que acabe
  dentro de un prompt.
- Hoy **no hay ninguna ruta de imagen** en el cliente LLM: `chat` envía solo
  texto (`core/llm_client.py:503-509`). Nada de pantalla se manda actualmente.
  Si se añade visión, el punto de salida sería ese mismo cliente hacia
  `api.deepseek.com`.

### Cómo blindarlo (si algún día se añade visión)

- **Modelo local** para pantalla: es la única forma de garantizar que no se
  entrena con nada tuyo.
- Si usas API: activar el **opt-out de entrenamiento**, revisar términos/DPA de
  empresa y usar retención cero si el proveedor la ofrece.
- **Minimizar antes de enviar**: recortar la región de interés y **difuminar**
  datos sensibles; no mandar pantalla completa.
- **No usarlo** con apps sensibles (banca, salud, correo) ni con datos de
  terceros.
- Si hay usuarios finales, cumplir como responsable (GDPR): informar y pedir
  consentimiento.

**En resumen:** con la API actual de DeepSeek, la política permite usar los
inputs para entrenar y ofrece opt-out; con un modelo local, no. Y hoy el
programa no envía ninguna captura porque no tiene visión.

### Actualización v3.7.0 (H11) — matiz sobre las capturas

- **Por defecto sigue sin enviarse ninguna captura**: la visión está apagada
  (`AGENTES_VISION_HABILITADA`). Si se activa, `decidir_accion_desde_captura`
  manda el PNG como `image_url` **al mismo proveedor configurado** (DeepSeek o el
  `DEEPSEEK_BASE_URL` que tengas). Es decir, las advertencias de esta sección
  pasan a aplicar en cuanto se encienda el kill switch.
- **La salida limpia** es un **modelo local** vía `DEEPSEEK_BASE_URL` (§9 y
  `HARNESS_VLM_LOCAL.md`): ahí la captura no sale del equipo.
- Siguen aplicando las recomendaciones: minimizar la región, difuminar datos
  sensibles, revisar opt-out/DPA del proveedor y no usar visión con apps
  sensibles.

---

## 9. ¿Puedo entrenar yo un LLM de visión con mi propia pantalla y ejecutarlo en mi PC?

**Sí, es posible, pero en tu equipo actual la parte de *entrenar* no es
práctica.** Lo viable hoy es **ejecutar localmente** un VLM abierto ya
entrenado; el *fine-tuning* con tus capturas pide GPU.

### Tu hardware (verificado)

- **Sin GPU dedicada**: `nvidia-smi` no existe y `torch.cuda.is_available()` es
  `False`.
- **8 CPU**, **15 GB de RAM** (~8 GB libres), **349 GB** de disco libre.
- `torch` y `transformers` instalados (torch sin CUDA); **no** hay
  `peft`, `trl`, `bitsandbytes`, `accelerate`, `datasets` ni `unsloth`.
- No hay `ollama`, `llama.cpp` ni `vllm` instalados.
- El cliente LLM ya es configurable: `DEEPSEEK_BASE_URL` (arg > env > archivo >
  default) apunta el SDK de `openai` a donde quieras.

### Qué es realista en esta máquina

| Opción | Viabilidad aquí |
|---|---|
| Entrenar desde cero | **No** (hace falta un clúster) |
| *Fine-tuning* completo de 7B+ | **No** sin GPU |
| **LoRA/QLoRA** de un VLM pequeño (2-3B) | Solo con GPU (propia ≥ 8-12 GB o alquilada) |
| **Inferencia local** de un VLM pequeño cuantizado | **Sí**: cabe en 15 GB de RAM (lento, segundos por imagen, no tiempo real) |

### Ruta realista (en 3 fases)

1. **Inferencia local sin entrenar** (lo que puedes hacer ya): bajar un VLM
   abierto pequeño (p. ej. Qwen2.5-VL-3B, Moondream2, SmolVLM2, Florence-2) y
   servirlo con **Ollama** o **llama.cpp** (formato GGUF). Ventaja: **nada sale
   de tu PC** y valida la idea sin gastar en entrenamiento.
2. ***Fine-tuning* con tus capturas** (requiere GPU): recolectar pares
   `(captura, acción)` y entrenar con `transformers` + `peft` (LoRA/QLoRA) +
   `trl`/`unsloth`; exportar a GGUF y servir en local. En tu PC actual esto no
   es viable; habría que alquilar GPU o ampliar el equipo.
3. **Integración en el programa**: apuntar `DEEPSEEK_BASE_URL` al servidor local
   y **extender el cliente** para mandar imágenes (hoy `chat` construye mensajes
   solo de texto, `core/llm_client.py:503-509`). Ojo: no todos los servidores
   locales aceptan el mismo esquema multimodal (vLLM/llama.cpp usan
   `image_url` + base64; la API nativa de Ollama usa `images`).
   → ⚠️ **Actualización v3.7.0 (H12):** lo primero ya funciona
   (`DEEPSEEK_BASE_URL` local) y el cliente ya manda imágenes con
   `completar_multimodal` (`image_url` + base64). El runtime local del VLM es
   cosa tuya; la app solo apunta a él. Ver `HARNESS_VLM_LOCAL.md`.

### La parte difícil no es el modelo, son los datos

- Necesitas **grabar** pantalla + eventos de ratón/teclado con marca de tiempo
  (`mss` para frames, `pynput` para eventos) y construir pares
  `(frame → acción)` etiquetados. Miles o decenas de miles de ejemplos para algo
  decente.
- Riesgo de **sobreajuste a tu resolución, tu tema y tus apps**.
- Alternativa: partir de datasets públicos de *GUI grounding* y adaptarlos.

### Avisos

- Un VLM pequeño entrenado en casa será **mucho peor** que un modelo grande de
  API; el punto débil es la **precisión de coordenadas**.
- Entrenar con tu pantalla mete tu pantalla **en los pesos**: si compartes el
  modelo, compartes tus datos. Mantenlo local.
- Es un proyecto en sí mismo: grabar + etiquetar + entrenar + evaluar.
- Los tests automáticos de visión son difíciles (hay que mockear captura y
  actuador).

**Recomendación:** empieza por la fase 1 (VLM local ya entrenado, sin
entrenar nada). Si funciona, plantéate la fase 2 en una GPU alquilada. Y no
metas control de ratón/teclado real hasta tener las guardas del §7 (allowlist,
confirmación y kill switch).

### ⚠️ Actualización v3.7.0 (H12) — arquitectura preparada, sin entrenar

- **Runtime**: basta `DEEPSEEK_BASE_URL` (GUI ⚙ Configuración o el archivo de
  config) para apuntar a un servidor local compatible con OpenAI.
- **Diagnóstico**: `tools/comprobar_servidor_local.py --base-url
  http://localhost:8000/v1` hace `GET {base_url}/v1/models` (acepta base con o
  sin `/v1`) y explica cómo configurar la app.
- **Cliente multimodal**: ya manda imágenes (H11), mismo `base_url`.
- **Separación documentada** en `HARNESS_VLM_LOCAL.md`: inferencia (servidor
  aparte) / runtime (cliente) / dataset (`tools/`) / *fine-tuning* (fuera del
  núcleo). **No** se entrena nada desde la app, tal como pedía esta sección.

---

## 10. ¿Puedo llamar a la app desde otra aplicación Python, con una lista de tareas, enviando la siguiente cuando la anterior responde OK?

**Sí, y encaja casi sin trabajo**: el servidor HTTP expone una llamada
**síncrona** que devuelve cuando la tarea ha terminado, así que un simple bucle
`for` en tu app es suficiente.

> ✅ **Actualización v3.7.0 (H9 + H6 + H10):** además del `/run` síncrono, hay
> **API de trabajos** (`POST /api/jobs` → `job_id`, `GET /api/jobs/<id>`, logs en
> SSE y cancelación) y `ok` ya **no** significa «los agentes terminaron» sino
> «el artefacto pasó el contrato de aceptación». El JSON añade `estado` y
> `aceptacion`. El cliente de tareas en serie ahora reporta verificación y tiene
> `--continue-on-error`. Detalle al final de la sección.

### Dos rutas de integración

**A) HTTP (recomendada)** — `python main.py serve --host 127.0.0.1 --port 8765`:

- `GET /health` → `{ok, version}`
- `POST /run` con JSON:
  `{ "prompt": "...", "max_pasos": 6, "timeout": 300, "aprender": true, "agent": null }`
- Respuesta (HTTP 200 si `ok`, 500 si falló, 400 petición inválida, 504 timeout)
  con este esquema (`main._ejecutar_pipeline`):

```json
{
  "ok": true,
  "estado": "completada",
  "problema": "...", "titulo": "...", "plan_id": "...",
  "pasos": [{"orden": 1, "nombre": "...", "tipo_agente": "Python"}],
  "agentes": [{"nombre": "...", "tipo": "...", "estado": "Completado",
               "ok": true, "error": "", "duracion": 1.2, "resultado": {}}],
  "resultado": "texto del último agente correcto",
  "duracion": 12.3, "timeout_agotado": false,
  "ejecucion_id": 469,
  "aceptacion": {
    "aceptada": true, "verificada": true, "motivos": [], "resumen": "aceptada",
    "pasos": [{"nombre": "...", "aceptado": true,
               "criterios_comprobados": ["archivo:documento.docx"],
               "criterios_fallidos": [], "evidencias": ["..."],
               "advertencias": []}]
  },
  "advertencias": []
}
```

> ⚠️ El ejemplo de la 469 es solo ilustrativo del **formato**: hoy esa ejecución
> se guarda como `fallida` con `aceptada: 0` y su `motivo_fallo`. `ok: true`
> exige que la aceptación pase.

También existe el modo Flask `python main.py web` con el mismo contrato en
`POST /api/run`, más `/api/agents`, `/api/health`, `/api/logs` y la API de
trabajos (`/api/jobs`).

**B) Subproceso CLI** — `python main.py run --prompt "..." --json` (o
`--stdin` con el JSON de tarea, y `--output fichero.json`). Devuelve el JSON por
stdout; cómodo con `subprocess.run(..., capture_output=True)`.

**C) Importar `main._ejecutar_pipeline` en tu proceso**: posible pero es una
función privada, exige un `QCoreApplication` y acopla tu app a este proceso y a
su BD. No lo recomiendo.

### Bucle secuencial (patrón)

```python
import requests

BASE = "http://127.0.0.1:8765"

for tarea in lista_de_tareas:
    r = requests.post(f"{BASE}/run", json={
        "prompt": tarea, "max_pasos": 6, "timeout": 300, "aprender": True,
    }, timeout=600)
    data = r.json()
    if not data.get("ok"):
        print("Fallo:", data.get("error") or data.get("agentes"))
        break
    print("OK:", data["resultado"][:200], "| ejecucion_id:", data.get("ejecucion_id"))
```

### Cosas que debes saber

- **«OK» = `ok: true`**, que exige que **todos** los agentes hayan completado.
  Pero `ok` **no garantiza que el resultado sirva** (vimos un `.docx` con
  `✅5/5` y sin imagen). Para una lista de tareas, valida tú el artefacto
  (fichero existe, contenido, etc.) o mira `agentes[].error` /
  `timeout_agotado`.
  → ✅ **v3.7.0 (H6):** `ok` ya exige que la **aceptación** pase; esa garantía la
  da el `VerificationEngine`. Sigue siendo tu responsabilidad validar requisitos
  que el plan no haya declarado como contrato.
- **Se procesa una tarea a la vez**: el servidor consume de una cola con guarda
  anti-reentrada, así que las peticiones concurrentes se serializan. El
  `timeout` de tu cliente debe ser mayor que la duración de la tarea.
- **`aprender: false` desactiva también el Plan B**: en `_ejecutar_pipeline` el
  recovery solo se inyecta `if aprender:`. Si quieres recuperación ante fallos,
  déjalo en `true` (toca `agent_history.db`).
- **Los artefactos se escriben en el `cwd` del proceso servidor**, no en el de
  tu app. Arranca `serve` en el directorio que quieras como salida.
- ⚠️ **Comprobado en real**: incluso con `--no-aprender`, al generar el plan se
  inicializa el `LearningEngine` (para inyectar lecciones) y **crea/abre
  `agent_history.db` y `learning_models/` en el `cwd`**. Es decir, lanzar la
  tarea desde otra carpeta crea una **BD nueva** allí. Si quieres usar la BD
  real, ejecuta con `cwd=<raíz del proyecto>`.
- **`--json` deja stdout limpio**: el JSON va a stdout y los logs a stderr, así
  que parsear `stdout` es fiable (con `--quiet` solo salen errores por stderr).

**Ejemplo probado** (subproceso CLI, sin servidor):
`tools/enviar_tarea_subproceso.py` — lanza `run --json` con `subprocess`, envía
una lista **en serie** (no manda la siguiente hasta que la anterior responde
OK), parsea el JSON y opcionalmente muestra los logs en vivo. Ejecutado con
`--ver-logs "di hola"`: exit 0, `ok=true`, `duración=1.33s`, `ejecucion_id=None`
(aprender desactivado).
- ✅ **v3.7.0 (H10):** cada tarea imprime un **informe estructurado**
  (`estado`, `duración`, `ejecucion_id`, errores, **artefactos verificados** y
  veredicto de aceptación con motivos) y acepta `--continue-on-error` para no
  detener la lista en el primer fallo (sin el flag, se detiene, como antes).
- **`agent`** limita la ejecución a los agentes del plan con ese nombre.
- **No hay autenticación** y escucha en `127.0.0.1` por defecto. Si lo expones,
  pon un proxy con auth.
- **No hay modo asíncrono** (encolar y consultar estado): cada `POST /run`
  espera. Si algún día quieres «lanzar y preguntar», habría que añadir
  endpoints de trabajo (la cola `ColaTrabajos` ya existe internamente).
  → ✅ **v3.7.0 (H9):** ya existe. `POST /api/jobs` devuelve `job_id` al instante,
  `GET /api/jobs/<id>` da estado/resultado/aceptación y
  `GET /api/jobs/<id>/logs` transmite logs en vivo (SSE). `POST /run` se mantiene
  por compatibilidad. En `serve` hay paridad con `/jobs` y logs por *polling*.
- **SQLite compartida**: si lanzas varias instancias para paralelizar, todas
  escriben en `agent_history.db`; evita escrituras concurrentes.

**Recomendación:** usa `serve` + `POST /run` en bucle si quieres simplicidad, o
`POST /api/jobs` + `GET /api/jobs/<id>` si quieres lanzar y consultar. En ambos
casos `ok` ya es fiable (pasa por el contrato de aceptación), pero valida tú
cualquier requisito que el plan no haya declarado.

### ✅ Actualización v3.7.0 (H9) — API de trabajos

- `POST /api/jobs` (Flask) → **202** con `{job_id, estado: "queued"}`. El
  trabajo lo consume el hilo de Qt; el HTTP **no** espera.
- `GET /api/jobs/<id>` → `{estado, resultado_ok, ejecucion_id, aceptada,
  motivos, resultado, cancelado, …}` con estados
  `queued / running / completed / failed / cancelled`.
- `GET /api/jobs/<id>/logs?cursor=` → **SSE**: un evento por entrada y
  `event: fin` al terminar el trabajo.
- `POST /api/jobs/<id>/cancel` → best-effort: un trabajo **encolado** no llega a
  ejecutarse; uno **en ejecución** no se interrumpe desde HTTP (el pipeline corre
  en el hilo de Qt) y termina su ejecución actual.
- `GET /api/jobs` lista los últimos trabajos.
- En el servidor stdlib (`serve`), los mismos endpoints con logs por *polling*
  (`GET /jobs/<id>/logs?cursor=`) y `/run` intacto.

---

## 11. ¿Cuál es el próximo paso para llevar el programa al siguiente nivel?

**Diagnóstico en una frase:** el programa ya sabe **ejecutar** planes con
agentes, pero **no sabe si el resultado sirve**. Casi todo lo demás (aprendizaje,
recovery, integración, feedback) se apoya en esa verdad, y hoy la verdad es
débil: «éxito» significa «los agentes terminaron».

### El paso nº 1: una capa de verificación/aceptación de la salida

Convertir «éxito» en una propiedad **del artefacto**, no del estado del agente.

- **Qué**: cada paso/tarea declara qué produce y qué invariantes debe cumplir
  (el fichero existe, tamaño > 0, formato real con `formato_imagen_real`, JSON
  parseable, > N caracteres, claves del contrato presentes). Un verificador
  **determinista** lo comprueba **en disco/bytes**.
- **Gate de estado**: una ejecución solo se marca `completada` si pasa la
  aceptación; si no, `fallida` con el motivo concreto.
- **Efectos**: el motivo alimenta al Plan B (§5), la etiqueta de éxito alimenta
  al aprendizaje (§3) y el `ok` de la API pasa a ser fiable (§10).

**Evidencia de que hace falta:**
- La ejecución 469 se guardó como `completada` con `errores=1`.
- Otra ejecución salió `✅5/5` y produjo un `.docx` **sin imagen**.
- `es_critico` se parsea pero solo se usa como feature
  (`learning/feature_extraction.py:127`); el scheduler lo ignora.
- `es_resultado_sospechoso` **solo avisa**; `EvaluadorLLM` puntúa en background
  y no bloquea.

**Coste:** medio-bajo, sin tocar la generación de planes. Reutiliza utilidades
que ya existen y se puede empezar por activar `es_critico` como gate real.

### Prioridades siguientes (por orden)

2. **Arreglar el matcher A/B** (barato y urgente): 0 filas `activo`, 11
   `candidato`, umbral 0.68 → una reescritura de otra tarea sustituye el prompt
   pedido (el caso albóndigas → cuento de terror). Subir el umbral y/o exigir
   verificación de intención. Mejora de calidad inmediata.
3. **Persistir la pregunta y la etiqueta de éxito real** (columna en
   `ejecuciones`): habilita el *retrieval* de casos similares (§3), analítica y
   depuración del historial. Hoy la pregunta no se guarda.
4. **API de trabajos**: `POST /run` bloqueante → cola con id de trabajo,
   consulta de estado y **streaming de logs** (§6). Es lo que convierte el
   programa en un servicio integrable de verdad.
5. **Estabilizar y declarar**: el `SIGSEGV` de la suite completa (pre-existente
   en CPython 3.13) y las dependencias implícitas (`sentence-transformers`,
   `torch` no están en `requirements.txt`).

### Lo que NO haría ahora

- **Visión de pantalla / control de ratón-teclado** (§7, §9): caro, frágil y
  con superficie de seguridad enorme; no ataca el cuello de botella actual.
- **Entrenar embeddings/modelos propios**: primero hay que tener datos y una
  definición de éxito fiables (pasos 1 y 3).
- **Más tipos de agente**: sin verificación, amplían la superficie sin mejorar
  el resultado.

### Definición de «siguiente nivel»

Una tarea se considera resuelta **solo si su artefacto cumple el contrato**, y
esa verdad es la misma para GUI, web, CLI y API; el recovery recibe el motivo
concreto; y el aprendizaje se entrena con etiquetas reales. Eso es pasar de
«orquestador que ejecuta» a **«sistema que garantiza resultados»**.

### ✅ Actualización v3.7.0 — el paso nº 1 y las prioridades 2–5 están hechos

| Prioridad (análisis original) | Estado v3.7.0 |
|---|---|
| **1. Capa de verificación/aceptación** | ✅ H6: `core/verification.py` + gate + `es_critico` real + motivo al Plan B |
| **2. Arreglar el matcher A/B** | ✅ H2: umbral 0.85 + intención + margen de duda + motivo |
| **3. Persistir pregunta y éxito real** | ✅ H5: `problema`, `resultado`, `aceptada`, `motivo_fallo` (migración 10→11) |
| **4. API de trabajos** | ✅ H9: `/api/jobs` + estado + SSE de logs |
| **5. Estabilizar y declarar** | ✅ H3: deps declaradas + suite aislada `--forked` (documentada) |

Añadido en la misma tanda: **H7** (Plan B acotado y anti-repetición), **H8**
(retrieval de casos), **H10** (cliente de tareas con verificación), **H4** (log
bus), **H1** (config) y **H12** (servidor local). H11 quedó en fase 1.

### Prioridades NUEVAS (lo que yo haría ahora)

1. **Paradas duras del recovery**: errores irrecuperables (falta de API key o de
   dependencia, problema inválido, bloqueo de seguridad) deben **abortar sin
   gastar intentos**. Hoy se gastan igual.
2. **Reindexar el retrieval**: rellenar `problema_embedding` de las ejecuciones
   exitosas antiguas (y un `exito` compuesto con feedback del usuario, no solo
   la aceptación). Sin esto, el histórico no alimenta a H8.
3. **Cablear la visión de página** (H11 fase 1) como acción opt-in del `Browser`
   (`screenshot → decidir → ejecutar`) y **escritorio solo con allowlist + kill
   switch + confirmación**, nunca por defecto.
4. **Dividir `core/scheduler.py`**: extraer el Plan B/reintentos a un
   colaborador; es el archivo que más ha crecido y donde más caro es un fallo.
5. **Cancelación real de ejecución**: hoy `/jobs/<id>/cancel` no interrumpe un
   pipeline en marcha (el `CancellationToken` ya existe; falta enlazarlo al job).
6. **Presupuesto de tokens/coste** por ejecución (hoy solo hay tiempo).

### Lo que sigue sin hacer (y por qué)

- **Control de ratón/teclado del escritorio**: coste, fragilidad y superficie de
  seguridad enormes; requiere allowlist, kill switch, confirmación y auditoría.
- **Entrenar un VLM propio**: primero datos y una definición de éxito fiables
  (ya hay definición de éxito; faltan datos).
- **Tipo de agente `Verificador`**: el Nivel 1+2 de H6 cubre el caso real sin
  tocar el enum.

---

## 12. ¿Podría haber un botón «Configuración» para poner la API Key y elegir Flash o Pro?

**Sí, y es un cambio pequeño.** Hoy **no existe** ningún ajuste en la UI: la
clave se resuelve por variables de entorno/archivos y el modelo está fijado en
código. La GUI solo muestra «● IA NO DISPONIBLE — falta DEEPSEEK_API_KEY» y
desactiva el botón de ejecutar (`ui/simple_main_window.py:604-610`).

> ✅ **Actualización v3.7.0 (H1): implementado.** Existe el botón `[ ⚙ CONFIG ]`
> y el diálogo `ui/config_dialog.py`, con persistencia segura en
> `~/.config/agentes_visuales/env` (700/600), `DEEPSEEK_MODEL`, «Probar
> conexión», «Borrar clave» y `reset_llm_client_compartido()`. El análisis
> original se conserva debajo; el detalle está al final de la sección.

### Cómo funciona hoy

- Clave: `DEEPSEEK_API_KEY` (entorno) → si no, archivos
  `~/.config/agentes_visuales/env`, `~/.config/deepseek.env`, `~/.deepseek_key`
  (`core/llm_client.py:44-56`, `cargar_entorno_desde_archivos`).
- Solo se aceptan 5 variables: `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`,
  `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` (`_VARIABLES_SOPORTADAS`). **El modelo
  no es configurable por archivo.**
- Modelos: `DEFAULT_MODEL = "deepseek-v4-pro"` y
  `FALLBACK_MODELS = ["deepseek-v4-flash"]`; el solver prueba
  `["deepseek-v4-pro", "deepseek-v4-flash"]`. Ya existe `set_modelo()`.
- El cliente es un **singleton** (`obtener_llm_client_compartido`, `:759`) sin
  función de reset: hoy cambiar la clave exige reiniciar la app.

### [PROPUESTA] Diseño

1. **Botón `⚙ Configuración`** junto al de Admin (mismo patrón: `_on_admin`),
   que abra un `QDialog` con:
   - API key: `QLineEdit` en modo `Password`, con casilla «mostrar» y pista de
     dónde se guarda; al cargar, mostrar solo los últimos 4 caracteres.
   - Modelo: `QComboBox` → **Pro** (`deepseek-v4-pro`) / **Flash**
     (`deepseek-v4-flash`).
   - Avanzado (opcional): `base_url` y proxies.
   - Botones: **Guardar**, **Probar conexión**, **Borrar clave**, Cancelar.
2. **Persistencia**: escribir en `~/.config/agentes_visuales/env`
   (directorio `700`, fichero `600`) con `DEEPSEEK_API_KEY=...`,
   `DEEPSEEK_BASE_URL=...` y un nuevo `DEEPSEEK_MODEL=...`.
   **Nunca** en el repo ni en `agent_history.db` (esa BD se comparte/copia).
   Para que el modelo se lea: añadir `DEEPSEEK_MODEL` a `_VARIABLES_SOPORTADAS`
   y leerlo en `LLMClient.__init__`.
3. **Aplicar sin reiniciar**: añadir `reset_llm_client_compartido()` en
   `core/llm_client.py` (vaciar `_shared_client` bajo lock) y que la ventana
   reconstruya `self.solver = ProblemSolver(obtener_llm_client_compartido())` y
   reactive el botón. Nunca hacerlo con una tarea en curso.
4. **Tests**: round-trip del fichero de config, que `LLMClient` tome el modelo
   del archivo, y que borrar la clave deje `disponible=False`.

### Avisos

- El modelo también aparece **por agente** en el plan (`builder._kwargs_llm`
  usa `config.get('modelo', 'deepseek-v4-pro')`). Si quieres que la elección de
  la UI gane siempre, hay que cambiar ese default para que lea el modelo
  configurado; si no, el campo del plan (que decide el LLM) manda.
- «Flash» no activa Pro como fallback automático: `FALLBACK_MODELS` seguiría
  apuntando a Flash. Habría que reordenar la lista según la elección.
- La clave en claro en `~/.config/...` es lo que ya hace el programa; si se
  quiere más, usar el llavero del sistema (`keyring`) implica dependencia nueva.
- Mostrar/registrar la clave en logs: no hacerlo nunca.

**Esfuerzo:** bajo-medio (1 diálogo + 1 reset + 1 variable soportada + tests),
sin tocar scheduler, sandbox ni validación.

### ✅ Actualización v3.7.0 (H1) — implementado

- **Botón y diálogo**: `[ ⚙ CONFIG ]` en `ui/simple_main_window.py` abre
  `ui/config_dialog.py` con API key (`QLineEdit` en modo `Password`, placeholder
  enmascarado), modelo **Pro/Flash**, `base_url`, `HTTP_PROXY`, `HTTPS_PROXY`,
  `NO_PROXY`, y botones **Probar conexión** (en un `QThread`, no bloquea la
  ventana), **Guardar**, **Borrar clave** y **Cancelar**.
- **Persistencia segura** (`core/ia_config.py`): escritura **atómica** en
  `~/.config/agentes_visuales/env`, directorio `700` y fichero `600`. Acepta
  `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `DEEPSEEK_BASE_URL`, `HTTP_PROXY`,
  `HTTPS_PROXY`, `NO_PROXY`. La clave **nunca** va a SQLite, logs, repo ni
  `print`; se muestra enmascarada. Un valor `None` conserva y `""` borra.
- **Modelo**: `DEEPSEEK_MODEL` se resuelve como arg > entorno > archivo >
  default (`deepseek-v4-pro`); `normalizar_modelo` acepta `pro`/`flash` y nombres
  completos. `LLMClient.__init__` lo usa.
- **Sin reiniciar**: `reset_llm_client_compartido()` cierra el cliente anterior
  (si expone `close()`) y vacía el singleton; la ventana reconstruye
  `self.solver` y reactiva el botón.
- **Prohibido con tarea en curso**: el botón se deshabilita durante la ejecución
  y `_on_config` avisa si se intenta.
- **El modelo del usuario manda en los agentes LLM**: `builder._kwargs_llm` y
  `validator.validar_configuracion_paso` usan `modelo_por_defecto()` cuando el
  plan no fija `modelo`.
- **Nota sobre el planificador**: el solver mantiene su propia elección
  (`HARNESS_TASKS.md`: planificador en `deepseek-v4-pro`); lo configurable
  aplica a los agentes LLM y al evaluador.
- **`--check-env`** muestra el modelo efectivo.
- **Tests**: `tests/test_ia_config.py` (permisos 600/700, enmascarado, borrado,
  que la key no aparece en logs, reset del singleton, modelo en el builder).

**Sigue pendiente:** `keyring` del sistema (implica dependencia nueva) y
reordenar `FALLBACK_MODELS` según la elección Flash/Pro.

---

## 13. Plan de implementación ordenado (todo lo preguntado)

Escala de esfuerzo: **S** ≤ 1 día · **M** ≈ 3-5 días · **L** ≈ 1-2 semanas ·
**XL** > 2 semanas. El orden combina valor/esfuerzo con dependencias.

> **Estado (v3.7.0):** el plan se ejecutó en el orden
> `H1 → H2 → H3 → H5 → H6 → H10 → H7 → H8 → H4 → H9 → H11 → H12`.
> `H1, H2, H3, H4, H5, H6, H7, H8, H9, H10` están **implementadas y con tests**;
> `H11` en **fase 1** y `H12` en **preparación**. Informe completo:
> `HARNESS_REPORT_v3.7.0-H1-H12.md`; commits por fase en `HARNESS_STATUS.md`.

### Resumen de estado

| Fase | Estado | Migración BD | Tests |
|------|--------|--------------|-------|
| H1 Config API/modelo | ✅ | — | `test_ia_config.py` |
| H2 Matcher A/B | ✅ | 9→10 (`motivo`) | `test_ab_matcher.py` |
| H3 Dependencias/estabilidad | ✅ | — | `test_dependencias.py` |
| H4 Log bus | ✅ | — | `test_log_bus.py` |
| H5 Persistir problema | ✅ | 10→11 | `test_database_unit.py`, `test_execution_recorder.py` |
| H6 Verificación/aceptación | ✅ | 8→9 (backfill `estado`) | `test_verification.py`, `test_aceptacion_plan.py`, `test_scheduler_aceptacion.py`, `test_flujo_e2e_aceptacion.py` |
| H7 Plan B adaptativo | ✅ | 11→12 (`reparaciones_plan`) | `test_plan_b_adaptativo.py` |
| H8 Retrieval de casos | ✅ | (usa 10→11) | `test_retrieval_casos.py` |
| H9 API de trabajos | ✅ | — | `test_jobs_api.py` |
| H10 Cliente de tareas | ✅ | — | `test_tarea_serie.py` |
| H11 Visión (fase 1) | ⚠️ | — | `test_vision.py` |
| H12 VLM local (prep) | ⚠️ | — | `test_ia_config.py` + `tools/` |

`DB_VERSION = 12`. Ninguna migración borra datos. La suite completa aislada
(`tools/run_tests.sh`) da **935 passed, 1 skipped**.

### Fase 0 — Cimientos y victorias rápidas

**H1. Botón «Configuración»: API key + Pro/Flash (§12) — S** ✅
- *Hecho*: `core/ia_config.py` (archivo 700/600, atómico), `ui/config_dialog.py`,
  botón `[ ⚙ CONFIG ]`, `DEEPSEEK_MODEL`, `reset_llm_client_compartido()`, modelo
  respetado por `builder._kwargs_llm` y `validator`.
- *Aceptación*: cumplida. Nunca se guarda la clave en repo/SQLite/logs; no se
  puede cambiar con una tarea en curso.

**H2. Arreglar el matcher A/B (§1, §2) — S** ✅
- *Hecho*: umbral 0.85 configurable + `compatible_por_intencion` (firma o solape
  léxico) + margen de duda (0.03) + motivo registrado (log, agente, columna
  `motivo`).
- *Aceptación*: cumplida (test con albóndigas vs cuento de terror y vectores
  controlados).

**H3. Declarar dependencias y estabilizar la suite — S/M** ✅
- *Hecho*: `sentence-transformers`, `torch`, `pypdf`, `ddgs` en
  `requirements.txt`; `tools/verificar_dependencias.py`; `tools/run_tests.sh`
  con `--forked` documentado en `pytest.ini`.
- *Aceptación*: instalación limpia verificable; suite sin `SIGSEGV` en un
  proceso aislado (no se desactivó ninguna prueba).

**H4. Logs unificados: terminal + GUI + web (§6) — M** ✅
- *Hecho*: `core/log_bus.py` (deque + handler + cursor), GUI por `QTimer` con
  dedupe, `GET /api/logs` y `GET /logs`, SSE por job. Prints de depuración de la
  UI migrados a `logging`.
- *Aceptación*: un `logger.info` aparece en terminal, fichero, GUI y web; sin
  recursión ni bloqueo de UI; buffer acotado.
- *Pendiente*: migrar el resto de `print()` y panel de logs en la plantilla web.

**H5. Persistir la pregunta y la etiqueta de éxito real (§3) — S/M** ✅
- *Hecho*: migración 10→11 (`problema`, `plan_json`, `resultado`, `aceptada`,
  `motivo_fallo`, `problema_embedding`, `problema_embedding_model`);
  `execution_recorder` los rellena.
- *Decisión*: la «etiqueta de éxito» es **`aceptada`** (veredicto de H6), no un
  `exito` compuesto; el `score` LLM y el feedback se conservan como señales
  separadas para no meter ruido en el retrieval.

### Fase 1 — El salto de nivel: aceptación de la salida

**H6. Capa de verificación/aceptación (§4, §11) — M** ✅
- *Hecho*: `core/verification.py` (`VerificationEngine`, `VerificationResult`,
  `ContratoAceptacion`), gate en `Scheduler.obtener_resultado_aceptacion()`,
  `es_critico` operativo, motivo al Plan B, `ok`/`estado` honestos en
  `main._ejecutar_pipeline`, rechazo estático en `validator.py`, backfill 8→9
  (la 469 deja de figurar como `completada`).
- *Criterios*: existencia/tamaño, formato real de imagen, imágenes incrustadas,
  JSON parseable, claves, longitud, items/errores, directorio y contenedor
  válido. Distingue «no verificable» de «verificado».
- *Aceptación*: cumplida, incluida la prueba E2E de fallo → Plan B → aceptación.
- *Pendiente (Nivel 3)*: tipo de agente `Verificador` y bloqueo por
  `EvaluadorLLM` con umbral. La verificación semántica no bloquea.

### Fase 2 — Recuperación y aprendizaje real

**H7. Plan B adaptativo y acotado (§5) — M/L** ✅
- *Hecho*: cap configurable (3 por defecto) + presupuesto de tiempo (300 s),
  escalera de estrategias, anti-repetición por firma, error estructurado,
  reutilización de agentes completados y verificados, traza en
  `reparaciones_plan` (migración 11→12).
- *Pendiente*: **paradas duras** por error irrecuperable y presupuesto de
  tokens/coste.

**H8. Retrieval de casos similares (§3) — M** ✅
- *Hecho*: `LearningEngine.obtener_casos_similares` sobre `aceptada=1`, umbral
  0.85, 1–3 casos, bloque de prompt con aviso de no copiar; embedding del
  problema guardado en background.
- *Aceptación*: cumplida (sin match no inyecta nada; solo éxitos verificados).
- *Pendiente*: reindexar ejecuciones antiguas sin embedding.

### Fase 3 — Servicio

**H9. API de trabajos + streaming (§6, §10) — M** ✅
- *Hecho*: `web/jobs.py` + `POST/GET /api/jobs`, `GET /api/jobs/<id>/logs` (SSE),
  `POST /api/jobs/<id>/cancel`; `POST /run` intacto; paridad en `serve` con logs
  por polling.
- *Aceptación*: lanzar sin bloquear, consultar estado y ver logs en vivo.
- *Pendiente*: cancelación real de una ejecución en curso.

**H10. Cliente de lista de tareas en producción (§10) — S** ✅
- *Hecho*: `tools/enviar_tarea_subproceso.py` reporta por tarea estado, duración,
  `ejecucion_id`, errores, **artefactos verificados** y veredicto de aceptación;
  `--continue-on-error`.
- *Aceptación*: la lista se detiene en la primera que no cumple el contrato
  (salvo con el flag).

### Fase 4 — Pesado / opcional

**H11. Visión de página → escritorio (§7) — L/XL** ⚠️ *(fase 1)*
- *Hecho*: `core/vision.py` (captura → decisión multimodal validada por
  allowlist) + `LLMClient.completar_multimodal` + kill switch
  `AGENTES_VISION_HABILITADA` (desactivado por defecto). Tests con LLM doble.
- *Pendiente*: cablear el bucle `screenshot → decidir → ejecutar` como acción
  opt-in del `Browser`. **Escritorio no implementado** (allowlist, kill switch y
  confirmación serían obligatorios).

**H12. VLM propio entrenado con tu pantalla + runtime local (§9) — XL** ⚠️ *(prep)*
- *Hecho*: `DEEPSEEK_BASE_URL` local + `tools/comprobar_servidor_local.py` +
  cliente multimodal + `HARNESS_VLM_LOCAL.md` (separación
  inferencia/dataset/fine-tuning/runtime).
- *Pendiente*: dataset de capturas y *fine-tuning* (GPU alquilada), fuera del
  núcleo de la app.

### Lo que NO era implementación (sigue siendo explicación)

- §1 (feedback → reescritura), §2 (alcance de embeddings), §8 (política de datos
  del proveedor) y §9 (hardware/viabilidad) son **análisis**, no tareas.
  §1 y §2 llevan ahora su bloque de actualización porque el comportamiento
  cambió (H2).

### Orden ejecutado

`H1 → H2 → H3 → H5 → H6 → H10 → H7 → H8 → H4 → H9 → H11 → H12`

Con una salvedad que se confirmó: **H6 es el que cambia el nivel del programa**;
el resto se apoya en su veredicto.

### Próximos pasos (nuevos)

1. Paradas duras del recovery (no gastar intentos en errores irrecuperables).
2. Reindexar `problema_embedding` + `exito` compuesto con feedback del usuario.
3. Cablear la visión de página (H11 fase 1) como acción opt-in del `Browser`.
4. Dividir `core/scheduler.py` (Plan B/reintentos a un colaborador).
5. Cancelación real de ejecuciones en curso.
6. Presupuesto de tokens/coste por ejecución.

---

<!-- Próximas preguntas: añadir aquí debajo siguiendo el mismo formato. -->
