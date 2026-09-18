# se_hizo2.md — Ampliación de la suite de tests y robustez (v2.0)

Segunda sesión de trabajo sobre **agentes_visuales**, centrada en
**revisar la suite de tests, crear los tests que faltaban para cubrir
puntos frágiles del programa, ejecutarlos en verde y cerrar la versión
`v2.0`**. Esta sesión continúa el trabajo de calidad documentado en
`se_hizo.md` (correcciones de sandbox, scheduler, base de datos, LLM,
ejecutores, etc.).

---

## 1. Objetivo

1. Revisar la suite existente (~321 tests) y localizar módulos y rutas
   sin cobertura directa.
2. Escribir tests de **regresión y robustez** para los puntos que la
   auditoría previa había tocado (y que, por tanto, eran los más
   propensos a romperse de nuevo).
3. Ejecutar toda la suite y dejarla en verde.
4. Commitear el trabajo y crear el tag **v2.0**.
5. Documentar lo realizado en este archivo.

---

## 2. Punto de partida

| Elemento | Valor |
|---|---|
| Rama de trabajo | `mejora/codigo-calidad` |
| Último commit | `603d970` (chore: deps) |
| Suite existente | 321 tests recolectados |
| Cambios de código pendientes | 34 archivos (auditoría previa, sin commitear) |
| Estado de la suite | A/C/D/E/F verdes; **B_scheduler con SIGSEGV intermitente** |

El baseline se ejecutó con `run_all_tests.py --no-retry`:

```
✅ A_ui_datos    ✅ C_integration   ✅ D_sandbox
✅ E_loops       ✅ F_ligeros       ❌ B_scheduler (rc=-11 SIGSEGV)
```

---

## 3. Revisión: huecos detectados

Se buscaron módulos importados por el código de producción pero **sin
referencia alguna en `tests/`**. Los principales huecos eran:

| Módulo | Funciones críticas sin test |
|---|---|
| `core/utils.py` | `extraer_json_balanceado`, `extraer_json_de_llm`, `limpiar_codigo` |
| `core/executors/helpers.py` | `parsear_json_robusto`, `es_resultado_sospechoso`, `limpiar_fences_markdown` |
| `core/executors/security.py` | `validar_ruta_archivo`, `validar_url`, `es_lista_valida` |
| `core/cancellation.py` | tokens, callbacks, gestor (todo el módulo) |
| `core/event_bus.py` | suscripciones, historial, snapshot (todo el módulo) |
| `core/env_checker.py` | `_verificar_api_key`, `_ping_http_deepseek`, códigos de salida |
| `core/agent.py` | round-trip `to_dict`/`from_dict`, `clonar`, validadores |
| `core/execution_recorder.py` | snapshots y resumen de plan |
| `storage/database.py` | transacciones, compresión, backup/restore |
| `core/scheduler.py` | estados terminales TIMEOUT/SALTADO y `running` |
| `core/executors/file_executor.py` | borrado seguro / symlinks |
| `core/sandbox.py` | salidas grandes (regresión del deadlock) |

---

## 4. Tests nuevos creados (267 tests)

| Archivo | Tests | Qué protege |
|---|---:|---|
| `tests/test_utils_json.py` | 25 | Extracción de JSON del LLM: llaves dentro de cadenas, escapes, JSON concatenados, markdown, reparación de comillas/comas. |
| `tests/test_executor_helpers.py` | 21 | `parsear_json_robusto`, fences markdown y detección de resultados vacíos/sospechosos. |
| `tests/test_security.py` | 46 | Rutas peligrosas (`.`, `..`, `~`, absolutas, ocultas), URLs, citado `shlex.quote` anti-inyección y detección de comandos privilegiados. |
| `tests/test_cancellation.py` | 22 | Unicidad de IDs, callbacks (incluidos los que fallan), reentrada sin deadlock, estadísticas y singleton. |
| `tests/test_event_bus.py` | 20 | Suscripciones, callbacks `partial` sin `__name__`, historial limitado, snapshot defensivo y estado del bus. |
| `tests/test_env_checker.py` | 28 | `_enmascarar_key`, proxies, API key por env/archivo y ping HTTP mockeado (200/401/429/5xx/404/timeout/ValueError). |
| `tests/test_agent_serialization.py` | 36 | Round-trip `to_dict`/`from_dict`, `dependencias` string, triturado de campos largos, `clonar`, `__post_init__` y validadores. |
| `tests/test_execution_recorder.py` | 12 | Snapshots de agentes (deduplicados) y elección del agente final del plan. |
| `tests/test_database_unit.py` | 24 | Integridad, guardado/consulta, normalización de estados, commit/rollback, compresión GZIP y backup/restore. |
| `tests/test_scheduler_terminal.py` | 12 | TIMEOUT/SALTADO/BLOQUEADO cuentan para `ejecucion_terminada`; reinicio de cualquier estado terminal; `running` limpio. |
| `tests/test_file_executor_seguridad.py` | 6 | Rechazo de rutas peligrosas y de **symlinks que apuntan fuera del cwd** al borrar. |
| `tests/test_sandbox_robustez.py` | 3 | **Regresión del deadlock de tuberías**: stdout/stderr > 64 KB no bloquean ni pierden el resultado. |

Se priorizaron casos límite y regresiones de bugs ya corregidos, no
"tests de fachada". Varios tests documentan explícitamente el bug que
evitan reintroducir (p. ej. IDs de cancelación generados con
`uuid`, callbacks ejecutados fuera del lock, `functools.partial` en el
bus de eventos, `ValueError` no capturado en `requests`).

---

## 5. Mejora del runner: aislamiento con `pytest-forked`

### El problema

`tests/test_scheduler.py::TestSchedulerEstadisticas::test_estadisticas_finales`
provocaba un `SIGSEGV` **solo cuando se ejecutaba tras el resto de tests
del grupo** (en aislamiento pasaba). La traza mostraba el fallo en un
hilo worker dentro de `tempfile`/`sandbox._ejecutar_script` mientras el
hilo principal procesaba eventos de Qt: el conocido *flake* de
**CPython 3.13 + PyQt6 + extensiones nativas + `fork()` desde hilos**.
Los 3 reintentos del runner caían en el mismo test, así que el reintento
por señal no bastaba.

### La solución

`pytest-forked` (ya declarado en `requirements-dev.txt`) ejecuta **cada
test en su propio proceso**, de modo que no se acumulan hilos ni estado
que corrompan el heap. `run_all_tests.py` ahora:

- Detecta si `pytest_forked` está instalado (`importlib.util`).
- Añade `--forked` a los grupos que crean subprocesos:
  `B_scheduler`, `C_integration`, `D_sandbox`, `E_loops`.
- Expone `--no-forked` para desactivarlo.
- Mantiene el reintento por señal como red de seguridad.

Resultado: el grupo B pasó de `rc=-11` a **63 passed**, y la suite
completa quedó verde de forma estable.

---

## 6. Cobertura aportada por los tests nuevos

Ejecutando **solo** los 267 tests nuevos (sin el resto de la suite):

| Módulo | Cobertura |
|---|---:|
| `core/cancellation.py` | 100% |
| `core/utils.py` | 96% |
| `core/executors/security.py` | 93% |
| `core/event_bus.py` | 92% |
| `core/executors/helpers.py` | 90% |
| `core/env_checker.py` | 84% |
| `storage/database.py` | 47% |
| `core/agent.py` | 44% |
| `core/execution_recorder.py` | 34% |

(Los tres últimos son módulos grandes con muchas rutas de UI/estado; los
tests cubren las funciones que se tocaron en la auditoría previa.)

---

## 7. Resultado de la suite completa

`./.venv/bin/python run_all_tests.py`

```
✅ Grupo A_ui_datos:    97 passed, 1 skipped   (1.9s)
✅ Grupo B_scheduler:   63 passed              (19.0s)   ← antes SIGSEGV
✅ Grupo C_integration: 18 passed              (18.0s)
✅ Grupo D_sandbox:     61 passed              (9.3s)
✅ Grupo E_loops:       45 passed              (9.6s)
✅ Grupo F_ligeros:     298 passed, 5 deselected (1.8s)

✅ TODAS LAS PRUEBAS PASARON  (EXIT 0)

Total: 582 passed, 1 skipped, 5 deselected  ·  ~63s
```

Comprobaciones adicionales:

- `ruff check .` → **All checks passed!**
- `python -m compileall core storage learning export ui main.py run_all_tests.py tests` → **OK**.

---

## 8. Hallazgos y observaciones

1. **SIGSEGV de CPython 3.13 + Qt + fork.** Mitigado en tests con
   `pytest-forked`, pero el problema de fondo sigue en producción: el
   sandbox hace `fork()` desde una app Qt con hilos. La solución
   definitiva sería un *fork server* de un solo hilo o `os.posix_spawn`
   desde un auxiliar sin extensiones nativas.
2. **Log flood por stderr grande.** El sandbox registra con
   `logger.error` el stderr completo del hijo; un proceso que escribe
   cientos de KB inunda consola y `logs/sandbox_debug.log`. Convendría
   truncar (p. ej. primeras/últimas N líneas) o limitar el tamaño.
3. **`validar_ruta_archivo` y rutas Windows.** El docstring afirma
   rechazar rutas absolutas de Windows, pero en POSIX `C:\...` se acepta
   (`os.path.isabs` es `False` y `os.path.splitdrive` no detecta unidad).
   No es un problema en Linux, pero conviene documentarlo o normalizar.
4. **Duplicación de extracción de JSON** entre `core/utils.py`,
   `core/executors/helpers.py` y `learning/reward_llm.py` (ya anotado en
   `se_hizo.md`): los tests nuevos cubren dos de las tres copias.

---

## 9. Commits y tag

| Commit | Contenido |
|---|---|
| `702a40d` | `fix(robustez): endurecer sandbox, scheduler, base de datos y LLM` |
| `c331031` | `test(robustez): ampliar la suite con 267 tests de regresión` |
| *(este archivo)* | `docs: documentar la mejora de robustez y la ampliación de tests` |
| **`v2.0`** | Tag anotado sobre el commit de documentación |

```bash
git tag -l            # incluye v2.0
git show v2.0 --stat
```

---

## 10. Cómo reproducir

```bash
# Suite completa (grupos aislados + pytest-forked + reintentos)
./.venv/bin/python run_all_tests.py

# Sin aislamiento por test (si no está instalado pytest-forked)
./.venv/bin/python run_all_tests.py --no-forked

# Solo los tests nuevos
./.venv/bin/python -m pytest \
    tests/test_utils_json.py tests/test_executor_helpers.py \
    tests/test_security.py tests/test_cancellation.py \
    tests/test_event_bus.py tests/test_env_checker.py \
    tests/test_agent_serialization.py tests/test_execution_recorder.py \
    tests/test_database_unit.py tests/test_scheduler_terminal.py \
    tests/test_file_executor_seguridad.py tests/test_sandbox_robustez.py
```

---

## 11. Pendientes sugeridos

- Sustituir el `fork()` del sandbox por un *fork server* de un solo hilo
  (eliminaría de raíz el SIGSEGV, no solo en tests).
- Truncar/limitar el volcado de stderr del sandbox a los logs.
- Centralizar la extracción/reparación de JSON en un único módulo.
- Ampliar cobertura de `storage/database.py` (mantenimiento, migraciones
  y exportación/importación JSON) y de `core/agent.py` (validaciones por
  tipo).
