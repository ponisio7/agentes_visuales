## Decisión de diseño: matching por embeddings (16-sep-2026)

### Contexto
El A/B testing necesitaba encontrar "la misma tarea" entre ejecuciones
para comparar versiones del prompt. La firma exacta (1-2 palabras
significativas) tenía 0 usos reales porque el LLM varía el texto entre
ejecuciones.

### Decisión
**El matching semántico por embeddings es el mecanismo oficial.**
Implementado en `EmbeddingMatcher.buscar_match()`:
- Calcula embedding del prompt crudo (384 dims, paraphrase-multilingual-MiniLM-L12-v2)
- Busca la reescritura más similar con similitud coseno
- Umbral por defecto: 0.68

### Rol de la firma exacta (`_firmar`)
**NO se usa para matching.** Se usa solo para:
- Agrupar versiones al insertar una nueva reescritura (desactivar
  las anteriores de la "misma tarea")
- Metadato de trazabilidad en `prompts_reescritos.firma`

### Lo que queda legacy
- `PromptABEvaluator.consultar_versiones` sigue existiendo pero ya
  no se llama desde el builder. Se puede eliminar en una limpieza futura.
- La firma con 2 palabras (`cd360c6`) quedó en 1 palabra, pero da igual:
  el matching real va por embeddings.

### Verificación en producción
Ejecución real del 16-sep-2026:
- `🎯 Match encontrado: id=24 (similitud=0.815, estado=candidato)`
- `✨ AB: 'GenerarCuento' usa id=24`

### Cómo verificar que funciona
```bash
sqlite3 agent_history.db "
SELECT id, length(embedding), embedding_model, estado
FROM prompts_reescritos
WHERE embedding IS NOT NULL
ORDER BY id DESC LIMIT 5;
"
```

---

## Decisión de diseño: agente de escritorio V4.1/V4.2 — NO ahora (20-sep-2026)

### Contexto

El roadmap V4.0 dejó el agente de escritorio (control de ratón/teclado y
visión de pantalla) fuera a propósito por superficie de seguridad. Con el
modo «Resolver tarea» funcionando, la pregunta vuelve: ¿toca construirlo?

### Decisión

**No se construye V4.1/V4.2 en esta etapa.** No es un «no» definitivo: es un
**aplazamiento con condiciones explícitas** (abajo). Hoy no se dan.

### Por qué

1. **Rompe el modelo de seguridad actual, no lo amplía.** El sandbox
   (`core/sandbox.py`) protege el código Python y el executor Shell avisa de
   comandos peligrosos (`DANGEROUS_SHELL_COMMANDS` en
   `core/executors/security.py`)… pero **solo avisa: `logger.warning`, no
   bloquea**. Un agente que controla ratón y teclado no pasa por ninguno de
   los dos: puede abrir un terminal y teclear el comando, o pulsar «Enviar»
   en un correo. La superficie no crece un poco: cambia de naturaleza.
2. **El allowlist de visión es de navegador, no de escritorio.**
   `core/vision.py` limita las acciones a
   `esperar, extraer, click, rellenar, scroll, screenshot, ejecutar_js,
   navegar` y el `BrowserVisionLoop` (V3.8-5) añade 20 pasos, 120 s, 30
   capturas y 3 fallos consecutivos. Eso funciona porque el «mundo» es una
   pestaña. En el escritorio el mismo `click` puede ser «Aceptar» en un
   diálogo de sudo o «Borrar» en el gestor de ficheros.
3. **No hay confirmación humana en el bucle.** El pipeline es autónomo por
   diseño; con escritorio, un paso mal planificado tiene efectos fuera del
   `cwd`, sin papelera y sin traza del artefacto.
4. **El valor marginal no compensa hoy.** El cuello de botella medido no es
   «no puedo mover el ratón»: es la calidad de la planificación y de la
   verificación. El punto 3 (clasificador de fallo) y el 4 (auto-crítica) son
   donde está el retorno; el escritorio no ataca ninguno.
5. **Precedente interno.** V3.8 dejó fuera el VLM propio por la misma razón
   (faltaban datos y etiquetas fiables). Ese criterio no ha cambiado para
   escritorio.

### Qué tendría que ser cierto para reabrirlo (condiciones)

Las cinco a la vez, y verificables con tests, no como intención:

1. **Allowlist de aplicaciones y ventanas**, con denylist dura (terminales,
   gestores de contraseñas, ajustes del sistema, diálogos de privilegios).
2. **Confirmación humana por acción irreversible**, con la acción descrita en
   lenguaje natural y un botón de «no» que aborte el plan entero — no solo el
   paso.
3. **Kill switch de sesión** (como `AGENTES_VISION_HABILITADA`, pero por
   sesión y con revocación inmediata) y **límites duros** de pasos/tiempo
   como los del `BrowserVisionLoop`.
4. **Auditoría completa**: cada acción con captura antes/después, ventana
   objetivo y justificación, persistida y consultable.
5. **Modo de sólo lectura primero**: capturar y proponer, sin ejecutar. Solo
   si eso resulta útil se discute habilitar la escritura.

### Alternativa que sí hacemos

Extender el patrón **seguro** que ya funciona: más capacidades dentro de
`BrowserVisionLoop` (que sí tiene allowlist, límites y kill switch), en vez
de saltar a control de escritorio. Es el mismo tipo de tarea con una
superficie acotada y ya auditada.

### Cómo se revierte esta decisión

Cuando las cinco condiciones se cumplan, esta nota se sustituye por el
informe de V4.1 con sus tests. Mientras tanto, `DECISIONES.md` es la
referencia: si alguien propone el agente de escritorio, la respuesta es
«condiciones 1–5 primero», no «depende».

## Decisión de diseño: SIGSEGV con la suite sin aislar — mitigación definitiva (21-sep-2026)

### Contexto

`python -m pytest -q` (la suite completa **en un solo proceso**) muere con
`SIGSEGV` de forma intermitente. Reproducido dos veces en la sesión del
21-sep-2026, con 186 módulos de extensión cargados (PyQt6, NumPy, SciPy,
scikit-learn, pandas, lxml…). La traza apunta a `tests/conftest.py`
(`esperar_condicion`) y a `tests/test_flujo_e2e_aceptacion.py`, es decir, a
tests que **ejecutan el Scheduler**, que a su vez lanza subprocesos del
sandbox (`fork+exec`) desde hilos de su `ThreadPoolExecutor`.

La causa no está en nuestra lógica: **`fork()` en un proceso con hilos es
inseguro en CPython 3.13** (solo sobrevive el hilo que llama a `fork`, y otro
hilo puede tener tomado el lock del asignador en ese instante). Es la misma
razón por la que Python 3.14 cambió el método de arranque por defecto de
`multiprocessing` en Linux, dejando de usar `fork`.

### Decisión

**La mitigación operativa es la definitiva: la suite se ejecuta aislada por
proceso con `tools/run_tests.sh` (`pytest --forked`).** No se persigue un
arreglo en el código de la aplicación mientras la base siga siendo CPython
3.13.

### Por qué (y qué ya se intentó)

`core/sandbox.py` ya acumula cuatro defensas específicas contra esto:

1. `os.register_at_fork(after_in_child=...)` para recrear los singletons
   (`_temp_manager`, `_cache`, `_class_lock`) en el hijo y no heredar locks
   tomados.
2. `_SPAWN_LOCK`: serializa la creación de subprocesos.
3. `tempfile.TemporaryFile` en lugar de `PIPE` para stdout/stderr (con `PIPE`,
   `communicate()` repetido desde varios hilos provocaba el SIGSEGV).
4. `cwd` explícito para forzar `fork+exec` clásico y `stdin=subprocess.DEVNULL`.

Sigue fallando porque el hueco que queda **no es nuestro**: cualquier `fork`
disparado desde un hilo mientras otro hilo está en medio de una operación de
memoria es un fallo del intérprete. `_SPAWN_LOCK` serializa *nuestros* forks,
no los demás hilos (Qt, pool de ejecución). Arreglarlo de verdad exige cambiar
la arquitectura (un *forkserver* o un proceso auxiliar dedicado a lanzar los
subprocesos), lo que es desproporcionado para el beneficio y añade superficie
de fallo nueva en la pieza más crítica del sistema.

Coste de la mitigación: ~2 min de suite (125 s frente a ~90 s) y ninguna prueba
desactivada. El aislamiento por proceso **no oculta fallos**: un SIGSEGV en un
hijo se reporta como fallo de ESE test.

### Qué tendría que ser cierto para reabrirlo

1. La base pasa a **CPython ≥ 3.14** y `python -m pytest -q` completa en verde.
2. O aparece un reproductor mínimo que señale a **código nuestro** y no al
   intérprete (p. ej. un manejo incorrecto de descriptores en el sandbox).
3. O se implementa un *forkserver* y la suite sin `--forked` pasa a ser la
   forma normal de ejecutarla.

### Cómo se revierte esta decisión

Cuando se cumpla (1), (2) o (3): se quita `--forked` de `tools/run_tests.sh`,
se comprueba que la suite completa pasa sin aislar y esta nota se sustituye por
el informe correspondiente. Mientras tanto, si alguien ve el SIGSEGV, la
respuesta es «usa `tools/run_tests.sh`», no «depende».
