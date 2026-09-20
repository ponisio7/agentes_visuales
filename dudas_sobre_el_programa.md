# Dudas sobre el programa

Documento vivo: recoge las preguntas sobre `agentes_visuales` y sus respuestas,
además de las propuestas anotadas que van saliendo.

- **Rama de referencia:** `v3.5.0-fix-docx`
- **Fecha:** 2026-09-20
- **Nota:** todo lo que sigue está verificado contra el código del repo y/o
  contra `agent_history.db`. Cuando algo es propuesta y no comportamiento
  actual, se marca como **[PROPUESTA]**.

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

---

## 3. [PROPUESTA] Recuperar preguntas similares con éxito e inyectar el enfoque que funcionó

**Idea del usuario:** usar embeddings para encontrar preguntas ya almacenadas
con alta tasa de éxito e inyectar en el prompt que «esa solución sirvió en un
contexto similar».

**Veredicto:** es una extensión natural y de esfuerzo medio, pero **hoy no se
puede implementar** tal cual por dos motivos, y hay un riesgo de calidad serio.

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

---

## 4. ¿Puede haber al final de una tarea un agente que verifique la salida?

**Sí, de hecho ya ocurre**, pero **no existe un mecanismo de verificación de
salida de primera clase**: es el planificador el que, a veces, añade un último
paso `Python` de verificación. No hay un tipo de agente «Verificador».

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

---

## 5. ¿Puede el Plan B ser infinito hasta conseguir ejecutar la tarea, usando los agentes que fallaron como ejemplo para reestructurar o cambiar tipos?

**Hoy no: está capado en 2 intentos**, y el cap está *hardcodeado*. Pero sí
recibe el fallo como contexto y **sí puede cambiar tipos de agente** (por
instrucción del prompt, no por una lógica estructural).

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

---

## 6. ¿Se puede hacer que todos los logs del terminal salgan también en el «stdout» de la GUI y en la página web?

**Sí, es viable y el cambio es contenido.** Hoy las tres salidas están
desconectadas entre sí.

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

---

## 10. ¿Puedo llamar a la app desde otra aplicación Python, con una lista de tareas, enviando la siguiente cuando la anterior responde OK?

**Sí, y encaja casi sin trabajo**: el servidor HTTP expone una llamada
**síncrona** que devuelve cuando la tarea ha terminado, así que un simple bucle
`for` en tu app es suficiente.

### Dos rutas de integración

**A) HTTP (recomendada)** — `python main.py serve --host 127.0.0.1 --port 8765`:

- `GET /health` → `{ok, version}`
- `POST /run` con JSON:
  `{ "prompt": "...", "max_pasos": 6, "timeout": 300, "aprender": true, "agent": null }`
- Respuesta (HTTP 200 si `ok`, 500 si falló, 400 petición inválida, 504 timeout)
  con este esquema (`main._ejecutar_pipeline`, `main.py:617-632`):

```json
{
  "ok": true,
  "problema": "...", "titulo": "...", "plan_id": "...",
  "pasos": [{"orden": 1, "nombre": "...", "tipo_agente": "Python"}],
  "agentes": [{"nombre": "...", "tipo": "...", "estado": "Completado",
               "ok": true, "error": "", "duracion": 1.2, "resultado": {}}],
  "resultado": "texto del último agente correcto",
  "duracion": 12.3, "timeout_agotado": false,
  "ejecucion_id": 469, "advertencias": []
}
```

También existe el modo Flask `python main.py web` con el mismo contrato en
`POST /api/run` (más `/api/agents` y `/api/health`).

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
- **Se procesa una tarea a la vez**: el servidor consume de una cola con guarda
  anti-reentrada, así que las peticiones concurrentes se serializan. El
  `timeout` de tu cliente debe ser mayor que la duración de la tarea.
- **`aprender: false` desactiva también el Plan B**: en `_ejecutar_pipeline` el
  recovery solo se inyecta `if aprender:` (`main.py:507-522`). Si quieres
  recuperación ante fallos, déjalo en `true` (toca `agent_history.db`).
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
- **`agent`** limita la ejecución a los agentes del plan con ese nombre.
- **No hay autenticación** y escucha en `127.0.0.1` por defecto. Si lo expones,
  pon un proxy con auth.
- **No hay modo asíncrono** (encolar y consultar estado): cada `POST /run`
  espera. Si algún día quieres «lanzar y preguntar», habría que añadir
  endpoints de trabajo (la cola `ColaTrabajos` ya existe internamente).
- **SQLite compartida**: si lanzas varias instancias para paralelizar, todas
  escriben en `agent_history.db`; evita escrituras concurrentes.

**Recomendación:** usa `serve` + `POST /run` en bucle, con tu propia validación
del artefacto y un `timeout` holgado. Es la ruta estable y ya probada por la CLI
y la web.

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

---

## 12. ¿Podría haber un botón «Configuración» para poner la API Key y elegir Flash o Pro?

**Sí, y es un cambio pequeño.** Hoy **no existe** ningún ajuste en la UI: la
clave se resuelve por variables de entorno/archivos y el modelo está fijado en
código. La GUI solo muestra «● IA NO DISPONIBLE — falta DEEPSEEK_API_KEY» y
desactiva el botón de ejecutar (`ui/simple_main_window.py:604-610`).

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

---

## 13. Plan de implementación ordenado (todo lo preguntado)

Escala de esfuerzo: **S** ≤ 1 día · **M** ≈ 3-5 días · **L** ≈ 1-2 semanas ·
**XL** > 2 semanas. El orden combina valor/esfuerzo con dependencias.

### Fase 0 — Cimientos y victorias rápidas

**H1. Botón «Configuración»: API key + Pro/Flash (§12) — S**
- *Dónde*: `ui/simple_main_window.py` (botón + `QDialog`, patrón de
  `_on_admin`), `core/llm_client.py` (`reset_llm_client_compartido()`,
  `DEEPSEEK_MODEL` en `_VARIABLES_SOPORTADAS` y en `__init__`),
  `core/problem_solver/builder.py` (default del modelo).
- *Aceptación*: guardar clave/modelo en `~/.config/agentes_visuales/env` (600),
  «Probar conexión» responde, y al guardar se reconstruye el solver **sin
  reiniciar** y se reactiva el botón de ejecutar.
- *Riesgo*: no guardar nunca la clave en el repo ni en `agent_history.db`; no
  aplicar cambios con una tarea en curso.

**H2. Arreglar el matcher A/B (§1, §2) — S**
- *Dónde*: `learning/embedding_matcher.py` (`UMBRAL_DEFAULT`), `core/problem_solver/builder.py`
  (verificación de intención antes de sustituir el prompt).
- *Aceptación*: una reescritura de otra tarea ya no sustituye el prompt (test
  con dos prompts de temas distintos y similitud ~0.70).

**H3. Declarar dependencias y estabilizar la suite — S/M**
- *Dónde*: `requirements.txt` (`sentence-transformers`, `torch`),
  `pytest.ini`/CI (`--forked` por archivo).
- *Aceptación*: instalación limpia reproduce el entorno; la suite corre sin
  `SIGSEGV` en un proceso.
- *Riesgo*: el `SIGSEGV` es de CPython 3.13 (fork+hilos); puede requerir
  investigar `core/sandbox.py` (`_SPAWN_LOCK`).

**H4. Logs unificados: terminal + GUI + web (§6) — M**
- *Dónde*: nuevo `core/log_bus.py` (handler + `deque`), `main.py`
  (`_configurar_logging`), `ui/simple_main_window.py` (señal Qt con
  `QueuedConnection`), `web/app.py` (`GET /api/logs`), `web/templates/index.html`.
- *Aceptación*: un `logger.info` cualquiera aparece en los tres sitios; sin
  recursión ni bloqueo de UI; buffer acotado.

**H5. Persistir la pregunta y la etiqueta de éxito real (§3) — S/M**
- *Dónde*: `storage/database.py` (migración v8→v9: columna `problema`),
  `core/execution_recorder.py` (pasar `problema` y `exito`).
- *Aceptación*: cada ejecución guarda la pregunta y un `exito` compuesto
  (`errores=0` + score LLM + feedback ≥ 0).

### Fase 1 — El salto de nivel: aceptación de la salida

**H6. Capa de verificación/aceptación (§4, §11) — M**
- *Dónde*: nuevo `core/verification.py`; contrato de aceptación en
  `core/problem_solver/models.py`; gate de estado en `core/scheduler.py`;
  `builder.py`/`prompt_builder.py` (declarar invariantes); `main.py`
  (`ok`/estado honestos); `validator.py` (rechazo estático de lo comprobable).
- *Aceptación*: un `.docx` sin imagen se marca fallido con motivo; la ejecución
  469 dejaría de figurar como `completada`; el motivo llega al Plan B.
- *Reutiliza*: `formato_imagen_real`, `validar_ruta_archivo`,
  `es_resultado_sospechoso`. Empieza activando `es_critico` como gate real.

### Fase 2 — Recuperación y aprendizaje real

**H7. Plan B adaptativo y acotado (§5) — M/L** *(depende de H6)*
- *Dónde*: `core/scheduler.py` (`_max_intentos_plan_b` configurable,
  `_reclamar_plan_b`, `_intentar_plan_b`), `core/plan_recovery.py` (escalera de
  estrategias, anti-repetición), `reparaciones_plan` (hoy vacía) para memoria.
- *Aceptación*: cap configurable + presupuesto de tiempo/tokens; un plan con la
  misma firma que uno fallido se rechaza; paradas duras para errores
  irrecuperables.

**H8. Retrieval de casos similares (§3) — M** *(depende de H5 y H6)*
- *Dónde*: `learning/embedding_matcher.py` (buscar en ejecuciones),
  `learning/engine.py` (`obtener_casos_similares`), `core/problem_solver/solver.py`
  (bloque nuevo junto a las lecciones, `:122-132`).
- *Aceptación*: con umbral alto (≥0.85) inyecta 1-3 casos con su plan resumido;
  sin match, no inyecta nada.

### Fase 3 — Servicio

**H9. API de trabajos + streaming (§6, §10) — M** *(depende de H4)*
- *Dónde*: `web/app.py` y `main.py` (`serve`/`web`): `POST /run` devuelve
  `job_id`; `GET /jobs/<id>`; `GET /jobs/<id>/logs` (SSE).
- *Aceptación*: lanzar sin bloquear y consultar estado; logs en vivo.

**H10. Cliente de lista de tareas en producción (§10) — S** *(depende de H6)*
- *Dónde*: partir de `tools/enviar_tarea_subproceso.py`; añadir validación de
  artefacto, reintentos y `--output` por tarea.
- *Aceptación*: 3 tareas en serie, para en la primera que no cumpla el contrato.

### Fase 4 — Pesado / opcional (solo después)

**H11. Visión de página → escritorio (§7) — L/XL** *(depende de H1 y H6)*
- *Dónde*: `core/llm_client.py` (mensajes multimodales), `core/agent.py` +
  `core/executors/` (nuevo tipo), `builder.py`/`constants.py`/`prompt_builder.py`/
  `validator.py`, allowlist + kill switch.
- *Aceptación*: fase 1 (visión de página con Playwright) con captura mockeada en
  tests; el escritorio, opt-in y con confirmación.

**H12. VLM propio entrenado con tu pantalla + runtime local (§9) — XL** *(depende de H1, H11)*
- *Dónde*: `tools/` (grabador con `mss`+`pynput`), entrenamiento LoRA en GPU
  alquilada, `core/llm_client.py` apuntando a `DEEPSEEK_BASE_URL` local.
- *Aceptación*: inferencia local sin salir de la máquina; nada de entrenar sin
  GPU.

### Lo que NO es implementación

- §1 (feedback → reescritura), §2 (alcance de embeddings) y §8 (política de
  datos) son **explicaciones del comportamiento actual**, no trabajo pendiente.

### Orden recomendado

`H1 → H2 → H3 → H4 → H5 → H6 → H10 → H7 → H8 → H9 → H11 → H12`

Con una salvedad: **H6 es el que cambia el nivel del programa**; si solo se
pudiera hacer uno, ese.

---

<!-- Próximas preguntas: añadir aquí debajo siguiendo el mismo formato. -->
