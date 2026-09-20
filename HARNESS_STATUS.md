# HARNESS_STATUS

- Rama: harness/v3.7.0
- Inicio: 2026-09-20
- Plan: dudas_sobre_el_programa.md §13

## En curso
- (ninguna)

## Completadas
- **H6 — Capa de verificación/aceptación de la salida (§4, §11)** — completada.
  - `e78529a` feat(verification): verificador determinista y contrato de aceptación.
    Nuevo `core/verification.py` + `ContratoAceptacion` en
    `core/problem_solver/models.py`. Comprueba el artefacto REAL (disco/bytes):
    existe, tamaño > 0, formato de imagen real, JSON parseable, claves, texto
    mínimo, imágenes incrustadas, items y errores. Reutiliza
    `formato_imagen_real`, `validar_ruta_archivo` y `es_resultado_sospechoso`.
  - `a462b0b` feat(problem_solver): declarar y validar invariantes.
    `prompt_builder` pide `es_critico` y `aceptacion`; `builder` deriva
    invariantes automáticos del paso File (y exige imagen incrustada si el
    enunciado la pide y el destino es un documento); `validator` rechaza de
    forma estática lo comprobable.
  - `6a1ff4f` feat(scheduler): gate de aceptación y `es_critico` real.
    Un paso crítico con resultado vacío o un contrato incumplido pasa a ERROR
    con motivo (sin reintentos) y el motivo llega al Plan B.
    `obtener_resultado_aceptacion()` decide el veredicto de la ejecución.
  - `1080be8` feat(main): `ok`/`estado` honestos y etiqueta de éxito real.
    `ok` exige aceptación; el JSON añade `estado` y `aceptacion`; el recorder
    guarda `fallida` cuando corresponde; migración 8→9 reclasifica las
    ejecuciones históricas con `errores>0` guardadas como `completada`
    (la ejecución 469).
  - Tests: `tests/test_verification.py` (19), `tests/test_scheduler_aceptacion.py` (5),
    `tests/test_aceptacion_plan.py` (11) y ampliaciones en
    `tests/test_execution_recorder.py` y `tests/test_database_unit.py`.
    Cubren .docx sin imagen → fallido con motivo, .docx con imagen →
    completado, JSON no parseable → fallido y ejecución con `errores=1` →
    fallida.
  - Contratos públicos intactos: `POST /run`, `POST /api/run`, CLI y JSON de
    respuesta solo reciben claves aditivas; firma de `Scheduler` y de los
    ejecutores sin cambios.

## Bloqueadas
- (ninguna)

## Notas
- La suite completa hereda un `SIGSEGV` intermitente de CPython 3.13 en el
  sandbox (fork + hilos; §11 del doc). No es de H6: los tests afectados pasan
  al ejecutarse por fichero o en aislamiento.
