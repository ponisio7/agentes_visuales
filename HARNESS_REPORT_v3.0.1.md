# HARNESS REPORT — agentes_visuales v3.0.1

**Fecha:** 2026-09-20
**Workspace:** `/home/christian/proyectosPython/agentes_visuales`
**Rama:** `main` · **Commit:** `8a116fe` · **Tag:** `v3.0.1`
**Ejecución:** solo diagnóstico (Regla 1 respetada: no se modificó código del proyecto)

---

## Resumen ejecutivo

| Fase | Resultado |
|---|---|
| 1 — Reconocimiento | ✅ OK |
| 2 — Diagnóstico de entorno | ✅ OK |
| 3 — Suite de tests | ❌ **FALLA** (3 grupos con error, 1 grupo no recolectado) |
| 4 — Smoke test end-to-end | ⏸️ **NO EJECUTADA** (gate de Fase 3: "si algún grupo falla, NO continúes") |
| 5 — Reporte | ✅ OK |

**El tag `v3.0.1` está taggeado sobre una suite de tests rota.** `pre_tag_check.sh` devuelve
exit 0 y no lo detecta, porque su smoke test (sección 11) no ejecuta pytest.

**3 bloqueadores críticos** y **4 hallazgos menores**. El más grave: la suite **escribe en la
base de datos de producción** `agent_history.db` (Regla 2).

---

## Fase 1 — Reconocimiento ✅

### Comandos ejecutados

```bash
git branch --show-current
git log --oneline --decorate -5
git status --short
git tag -l
ls -la
ls core/ core/executors/ core/problem_solver/ storage/ learning/ ui/ export/ tools/
./tools/pre_tag_check.sh
```

### Salida (git)

```
main
8a116fe (HEAD -> main, tag: v3.0.1) chore(ui): eliminar log de debug obsoleto en feedback
a30f871 chore(tools): completar v3.0.1 en el comando push del pre_tag_check
0b43e7c chore(tools): completar textos v3.0.1 en pre_tag_check
e642dba chore(tools): actualizar textos del pre_tag_check a v3.0.1
8a50212 docs: añadir README y CHANGELOG para v3.0.1
# git status --short → (vacío)
```

✅ Rama correcta, tag en HEAD, working tree limpio.

### Árbol de módulos (resumen de una línea por archivo)

**`core/`**
| Archivo | Líneas | Propósito |
|---|---|---|
| `agent.py` | 1451 | Modelo de datos para agentes ejecutables |
| `ai_assistant.py` | 570 | Asistente IA para crear y mejorar agentes |
| `bridge.py` | 8 | Puente mínimo entre componentes |
| `cancellation.py` | 291 | Sistema de cancelación para operaciones en ejecución |
| `env_checker.py` | 436 | Diagnóstico del entorno de configuración de IA |
| `event_bus.py` | 479 | Bus de eventos para comunicación desacoplada |
| `execution_recorder.py` | 206 | Registro de ejecuciones |
| `llm_client.py` | 711 | Cliente LLM para DeepSeek (API oficial) |
| `plan_recovery.py` | 677 | Plan B: recuperación de planes vía LLM |
| `sandbox.py` | 1245 | Subproceso aislado con medidas de seguridad |
| `scheduler.py` | 1537 | Orquestador DAG (thread-safe) |
| `text_parser.py` | 406 | Parser de DSL estructurado |

**`core/executors/`**: `browser_executor.py` (900, Playwright), `cache.py` (98, LRU+TTL),
`content_extractor.py` (367), `dispatcher.py` (128), `file_executor.py` (1360),
`helpers.py` (109), `http_executor.py` (356), `llm_executor.py` (598), `loop_executor.py` (249),
`python_executor.py` (96), `search_executor.py` (198, DuckDuckGo), `security.py` (146),
`shell_executor.py` (256).

**`core/problem_solver/`**: `builder.py` (495), `cli.py` (199), `code_corrector.py` (87),
`constants.py` (57), `file_normalizer.py` (76), `models.py` (73), `parser.py` (159),
`prompt_builder.py` (633), `solver.py` (585), `validator.py` (366).

**`storage/`**: `config_manager.py` (1113), `database.py` (1997, SQLite WAL).
**`learning/`**: `dataset.py`, `embedding_matcher.py` (303), `engine.py` (275),
`feature_extraction.py` (148), `feedback_processor.py` (469), `lessons.py` (523),
`models.py` (269), `prompt_ab_evaluator.py` (335), `reward_llm.py`, `schema.py`.
**`ui/`**: `admin_panel.py` (405), `simple_main_window.py` (1168).
**`export/`**: `exporters.py` (1166), `utils.py` (87).
**`tools/`**: `pre_tag_check.sh`, `verify_browser_search.py` (131).

### `./tools/pre_tag_check.sh` — exit code **0**

```
✅ Working tree limpio
✅ Rama: main
⚠️  No se pudo hacer fetch          ← esperado: repo local sin remoto
⚠️  Sin upstream configurado         ← esperado
⚠️  Ya existen tags v3.x: v3.0.1     ← esperado: el tag ya existe
✅ Existe: main.py / core/sandbox.py / core/scheduler.py / core/llm_client.py
✅ Existe: storage/database.py / learning/engine.py
✅ Todos los .py compilan
⚠️  pyflakes no instalado (pip install pyflakes)
⚠️  Posibles prints de debug (revisar): run_all_tests.py:50,51,83,88,255,256,300-330
✅ Sin marcadores de debug
✅ README.md · ✅ CHANGELOG.md
Plan: Generar archivo saludo.txt con Hola Mundo | Pasos: 2 | Agentes: 2
  1. [Python] CrearContenido
  2. [File] EscribirArchivo
✅ Smoke test completado exitosamente
✅ LISTO PARA TAGGEAR (revisa los ⚠️  manualmente)
```

**Nota:** los ⚠️ de la sección 8 son falsos positivos: `run_all_tests.py` es un CLI, sus
`print()` son salida legítima de usuario; el script excluye `tests/` y `tools/` pero no
la raíz.

---

## Fase 2 — Diagnóstico de entorno ✅

### 4. `python main.py --check-env` — exit code **0**

```
▸ Sistema
  ℹ️  Python: 3.13.5
  ℹ️  Plataforma: Linux-7.1.8+deb13-amd64-x86_64-with-glibc2.41
  ℹ️  Ejecutable: .../agentes_visuales/.venv/bin/python
  ℹ️  Directorio: /home/christian/proyectosPython/agentes_visuales
▸ API key de DeepSeek
  ✅ API key encontrada: sk-54c46…b6dc
  ℹ️  Origen: archivo
▸ Configuración de red
  ℹ️  Sin proxies configurados (conexión directa)
▸ Ping a api.deepseek.com (timeout 5s)
  ✅ Respuesta 200 en 15713 ms
RESULTADO: ✅ Todo OK
```

✅ **Conexión con DeepSeek OK.**

⚠️ **Observación menor:** se declara `timeout 5s` pero la latencia reportada es **15 713 ms**
(15,7 s) y aun así se marca OK. El timeout no se está aplicando de forma estricta
(probablemente por fase de conexión vs. lectura). No es bloqueante, pero el diagnóstico
puede reportar "OK" en escenarios que deberían degradar.

### 5. Instalación de `pyflakes` y re-ejecución

```bash
source .venv/bin/activate
pip install pyflakes        # → Successfully installed pyflakes-3.4.0
./tools/pre_tag_check.sh    # → exit 0
```

**Diferencias run1 → run2** (diff del log, colores eliminados):

| Sección | Run 1 (sin pyflakes) | Run 2 (con pyflakes 3.4.0) |
|---|---|---|
| 7. pyflakes | `⚠️ pyflakes no instalado` | `⚠️ pyflakes reporta issues` + **traceback de crash** |
| 11. Smoke test | `[Python] CrearContenido` | `[Python] GenerarContenido` (no determinismo del LLM) |

❌ **Hallazgo (menor):** la sección 7 **se rompe** al instalar pyflakes. Ver Bloqueador B4.

---

## Fase 3 — Suite de tests ❌ FALLA

### 6. Comando ejecutado

```bash
source .venv/bin/activate
python run_all_tests.py        # duración total 00:44, exit code 1
```

### Resultado por grupo

| Grupo | Resultado | Detalle |
|---|---|---|
| `A_ui_datos` | ✅ OK | 97 passed, 1 skipped (3,7 s) |
| `B_scheduler` | ❌ **FALLÓ** | 57 passed, **6 errors** (rc=1, 14,7 s) |
| `C_integration` | ❌ **FALLÓ** | 8 passed, **10 errors** (rc=1, 2,9 s) |
| `D_sandbox` | ✅ OK | 61 passed (11,0 s) |
| `E_loops` | ❌ **FALLÓ** | 42 passed, **3 errors** (rc=1, 6,9 s) |
| `F_ligeros` | ❌ **FALLÓ** | **rc=4, 0 items recolectados, 0 tests ejecutados** (0,3 s) |
| `Z_sin_clasificar` | ✅ OK | 69 passed (4,8 s) |

**Total: 19 errores de setup + 1 grupo entero sin ejecutar.**
Los 19 errores **no son fallos de lógica**: son `fixture not found` en la fase de setup,
es decir, los tests ni siquiera llegan a ejecutarse.

### Tracebacks (los 3 primeros, literales)

**1. `tests/test_scheduler.py::TestSchedulerEjecucion::test_ejecucion_basica`**

```
________ ERROR at setup of TestSchedulerEjecucion.test_ejecucion_basica ________
E       fixture 'qapp' not found
>       available fixtures: anyio_backend, cache, capfd, caplog, capsys, monkeypatch,
>       pytestconfig, recwarn, scheduler_basico, scheduler_con_agentes_independientes,
>       subtests, tmp_path, tmp_path_factory, tmpdir, tmpdir_factory
>       use 'pytest --fixtures [testpath]' for help on them.
```

**2. `tests/test_scheduler.py::TestSchedulerEjecucion::test_orden_ejecucion`**

```
________ ERROR at setup of TestSchedulerEjecucion.test_orden_ejecucion _________
E       fixture 'scheduler_rapido' not found
>       available fixtures: anyio_backend, cache, capfd, caplog, capsys, monkeypatch,
>       pytestconfig, recwarn, scheduler_basico, scheduler_con_agentes_independientes,
>       subtests, tmp_path, tmp_path_factory, tmpdir, tmpdir_factory
```

**3. `tests/test_scheduler.py::TestSchedulerEjecucion::test_limite_concurrencia`**

```
______ ERROR at setup of TestSchedulerEjecucion.test_limite_concurrencia _______
E       fixture 'qapp' not found
```

**F_ligeros (bloque completo relevante):**

```
▶️  GRUPO F_ligeros  (17 archivos)
collecting ... collected 0 items
============================= no tests ran in 0.01s =============================
ERROR: file or directory not found: tests/test_contrato_salida.py
   ❌ Grupo F_ligeros: FALLÓ (returncode=4, 0.3s, 10 líneas)
```

### Causa raíz de los 19 errores de setup — Bloqueador B2

`tests/conftest.py` tiene **hoy 6 líneas** y no define ninguna fixture:

```python
import os
import sys

_RAIZ_PROYECTO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _RAIZ_PROYECTO not in sys.path:
    sys.path.insert(0, _RAIZ_PROYECTO)
```

El commit **`a988b46`** (`test(solver): tests unitarios de _postprocesar_plan`, 2026-09-19)
redujo `tests/conftest.py` de **120 → 6 líneas**, eliminando `qapp`, `esperar`,
`scheduler_rapido` y `scheduler_basico`:

```
4112a0e   120 líneas | fixtures: 4     ← última revisión sana
996180a   120 líneas | fixtures: 4
a988b46     6 líneas | fixtures: 0     ← REGRESIÓN
HEAD        6 líneas | fixtures: 0
```

Recuperables con: `git show 4112a0e:tests/conftest.py` (define `esperar_condicion`,
`qapp`, `esperar`, `scheduler_rapido`, `scheduler_basico`).
En el tag `v3.0.1`, `def qapp` **solo** existe dentro de `tests/test_integration.py.bak:38`
y `tests/test_loop_safety.py.bak:34` — nunca como código vivo.

### Causa raíz de F_ligeros (rc=4) — Bloqueador B1

`run_all_tests.py:153` lista `tests/test_contrato_salida.py`, un archivo **que ya no existe**:

- El archivo fue **borrado en `1dd69c9`** (`refactor(builder): eliminar CONTRATOS_SALIDA_POR_PASO`),
  cuyo propio mensaje dice *"Elimina tests/test_contrato_salida.py, que probaba lo eliminado"*.
- `run_all_tests.py` **no se actualizó** en ese commit (la lista se añadió antes, en `4112a0e`).

Como pytest recibe un path inexistente, **aborta la recolección con rc=4 y ninguno de los 17
tests de `F_ligeros` se ejecuta**. Es una **pérdida silenciosa de cobertura**: el runner
sigue reportando "ALGUNAS PRUEBAS FALLARON" pero no distingue "0 tests corridos" de
"tests fallidos". Verificación:

```python
import run_all_tests as r, os
# FALTA tests/test_contrato_salida.py  (listado en grupo F_ligeros)
```

### 🔴 Hallazgo crítico adicional — la suite escribe en la BD de producción (Regla 2)

`agent_history.db` **cambió durante la ejecución de los tests**:

```
ANTES  (09:37:5x): 6962f1c10a25f3a948536749b380d2103805b4a6120a0c6ab2483c8e60c33424  6 537 216 bytes
DESPUÉS(09:38:01): d6b7d586062e01cefc2e069a034cdce79491f5f975dc45d5407f6e881f0cfdd3  6 545 408 bytes
```

El mtime `09:38:01` coincide exactamente con el grupo `A_ui_datos` (primero en correr).
Culpable: **`tests/test_ab_sintetico.py:34`**

```python
DB = "agent_history.db"          # ← ruta de producción hardcodeada
...
with sqlite3.connect(DB, timeout=10) as conn:      # línea 45
    conn.execute("DELETE FROM prompts_reescritos WHERE firma = ?", (firma,))   # línea 57
    # + INSERTs de escenarios sintéticos
```

No usa `tmp_path` ni `--db /tmp/...`: **escribe directamente en la BD real del usuario**.
(Contrasta con `tests/test_integration.py:34`, que sí define una fixture `temp_db`.)

**Nota de honestidad:** el comando ejecutado fue exactamente el sancionado por el prompt
(`python run_all_tests.py`); la violación de la Regla 2 la provoca la propia suite, no una
instrucción mía. La BD **no está bajo control de versiones**, por lo que este cambio
**no es reversible con git**.

### Verificaciones de seguridad realizadas

- Hash de `agent_history.db` **idéntico** antes/después de `pre_tag_check.sh` y `--check-env`
  (6962f1c1…) → esas dos fases no tocaron la BD.
- No se ejecutó la GUI (Regla 6 respetada).
- No se hizo commit/push/tag (Regla 3 respetada).
- Ningún comando superó los 5 minutos (Regla 5 respetada): el más largo fue la suite, 44 s.
- Solo se instaló `pyflakes` vía `pip` dentro de `.venv` (Regla 4 respetada, ninguna
  dependencia del sistema).

### Código del proyecto: pyflakes limpio salvo 9 avisos

El `pyflakes` de `pre_tag_check.sh` **nunca llegó a analizar el código del proyecto**
(crasheó antes, dentro de `.venv`). Ejecutado correctamente acotado:

```bash
.venv/bin/pyflakes core learning ui storage export tools main.py run_all_tests.py tests
```

```
core/sandbox.py:880:17: `nonlocal proceso` is unused: name is never assigned in scope
core/problem_solver/solver.py:27:1: 're' imported but unused
core/problem_solver/solver.py:407:9: redefinition of unused 're' from line 27
core/executors/shell_executor.py:147:17: `nonlocal proceso` is unused: name is never assigned in scope
core/executors/http_executor.py:175:13: redefinition of unused 'cancelar_peticion' from line 160
core/executors/http_executor.py:176:17: `nonlocal session` is unused: name is never assigned in scope
tests/test_urls_plantilla.py:10:1: 'pytest' imported but unused
tests/test_exporters.py:23:1: 'export.exporters.ExportResult' imported but unused
tests/test_llm_executor.py:14:1: 'core.executors.llm_executor' imported but unused
```

Ninguno es bloqueante. El más interesante es `http_executor.py:175`, que **redefine**
`cancelar_peticion` sobre la definición de la línea 160 (posible bug de cancelación
enmascarado).

---

## Fase 4 — Smoke test end-to-end ⏸️ NO EJECUTADA

**Motivo:** el prompt indica en la Fase 3, paso 7: *"Si algún grupo falla, **NO continúes**"*,
y a nivel global *"detente si algún paso falla"*. La Fase 3 falló, por lo que el flujo se
detiene aquí y **no se ejecutó** el comando del paso 8.

**Evidencia indirecta disponible:** el smoke test idéntico **sí pasó**, dos veces, dentro de
`pre_tag_check.sh` (sección 11), que ejecuta exactamente el mismo código:

```
Plan: Generar archivo saludo.txt con Hola Mundo | Pasos: 2 | Agentes: 2
  1. [Python] CrearContenido      (run 1)
  2. [File] EscribirArchivo
--- y en run 2 ---
  1. [Python] GenerarContenido
  2. [File] EscribirArchivo
```

✅ El camino `ProblemSolver → LLM → plan` funciona y genera **2 pasos (≥2)**, con tipos de
agente válidos (`Python`, `File`). ⚠️ El nombre del primer paso **varía entre ejecuciones**
(`CrearContenido` vs `GenerarContenido`): no determinismo del LLM, sin impacto funcional.

---

## Bloqueadores encontrados

### Críticos

**B1 — `F_ligeros` no ejecuta ningún test (pérdida silenciosa de cobertura)**
`run_all_tests.py:153` referencia `tests/test_contrato_salida.py`, borrado en `1dd69c9`.
pytest aborta con `rc=4` → **0 de 17 archivos recolectados**.
→ `run_all_tests.py:153`

**B2 — 19 errores de setup por fixtures eliminadas**
`tests/conftest.py` fue reducido de 120 → 6 líneas en `a988b46`, borrando `qapp`, `esperar`,
`scheduler_rapido`, `scheduler_basico`. Afecta a `B_scheduler` (6), `C_integration` (10),
`E_loops` (3). Los tests no llegan a ejecutarse.
→ `tests/conftest.py:1-6` · recuperable de `4112a0e:tests/conftest.py`

**B3 — La suite escribe en la BD de producción (viola la Regla 2)**
`tests/test_ab_sintetico.py` hardcodea `DB = "agent_history.db"` y hace `DELETE`/`INSERT`
sobre la BD real. Confirmado por cambio de hash y de tamaño durante el grupo `A_ui_datos`.
→ `tests/test_ab_sintetico.py:34` (y `:45`, `:57`)

### Menores

**B4 — `pre_tag_check.sh` sección 7 crashea al instalar pyflakes**
`pyflakes .` escanea `.venv/` y `.env/` (18 523 líneas de ruido) y muere con
`RecursionError: maximum recursion depth exceeded` en
`.venv/lib/python3.13/site-packages/sympy/...`. El `grep -v` solo filtra la *salida*, no
acota el *escaneo*. Efecto colateral: `echo: error de escritura: Tubería rota` (por `head -30`).
Resultado: la verificación de lint **nunca analiza el código del proyecto** y encima
devuelve exit 0, dando falsa sensación de limpieza.
→ `tools/pre_tag_check.sh:91`

**B5 — Versión inconsistente en todo el repo**
`python main.py --version` → **`agentes-visuales 1.1.0`** mientras el tag es `v3.0.1`.
`CHANGELOG.md` documenta `[v3.0]`, no `v3.0.1`; `README.md` dice "v3.0";
`pyproject.toml` no tiene campo `version`.
→ `main.py:17`

**B6 — Higiene de repositorio**
- El reporte que genera la Fase 5 queda **ignorado por git**: `.gitignore:5` (`*.md`).
  Confirmado con `git check-ignore -v HARNESS_REPORT_v3.0.1.md` → `.gitignore:5:*.md`.
- **14 ficheros `.bak`/backup están *trackeados* en git**, incluidas las carpetas
  `.backups_fix_*` y `core/executors/file_executor.py.bak_manual`, `learning/lessons.py.bak2`,
  `learning/lessons.py.bak3`, `tests/*.bak`.
- `.gitignore` tiene **bloques duplicados** (líneas 57-82 repetidas en 84+).
- Existe un directorio espurio `./~/` con `reporte_actualizacion.txt` dentro del repo.

**B7 — 7 tests sin clasificar**
`run_all_tests.py` genera un grupo `Z_sin_clasificar` con 7 archivos no listados en
`GRUPOS_TESTS` (`test_browser_executor.py`, `test_desenvolver_contenido_web.py`,
`test_llm_executor.py`, `test_prompt_builder.py`, `test_search_executor.py`,
`test_sustitucion_variables.py`, `test_urls_plantilla.py`). Pasan (69 passed), pero el
prompt solo contemplaba los grupos A–F.

---

## Recomendaciones para v3.1

1. **Restaurar `tests/conftest.py` inmediatamente** (B2) — es el arreglo de mayor impacto:
   ```bash
   git show 4112a0e:tests/conftest.py > tests/conftest.py
   ```
   Recupera de golpe 19 tests que hoy ni se ejecutan.

2. **Corregir `run_all_tests.py:153`** (B1) y, mejor, hacer que
   `descubrir_grupos_completos()` **valide que cada path listado exista** y aborte con un
   mensaje claro en lugar de dejar que pytest devuelva `rc=4` en silencio. Distinguir
   explícitamente *"0 tests recolectados"* de *"tests fallidos"*.

3. **Aislar la BD en los tests** (B3): eliminar el hardcode de `agent_history.db` y usar
   `tmp_path`/`--db`. Añadir al runner un guardarraíl que impida escribir en la BD de
   producción durante los tests. Verificar el estado actual de `agent_history.db` y
   restaurarlo desde un backup *no versionado* si existe.

4. **Acotar el escaneo de pyflakes** (B4):
   ```bash
   pyflakes core learning ui storage export tools main.py run_all_tests.py tests
   ```
   y añadir `-not -path "./.venv/*" -not -path "./.env/*"`. Quitar el `| head -30` que
   provoca la tubería rota. **Además: el proyecto tiene 9 avisos reales** que hoy nunca se ven.

5. **Unificar la versión** (B5): `main.py:__version__` debe derivar de una única fuente
   (p. ej. `importlib.metadata` o `pyproject.toml`), y `CHANGELOG`/`README`/`pre_tag_check.sh`
   deben coincidir con el tag.

6. **`pre_tag_check.sh` debe ejecutar la suite de tests** (o al menos un subconjunto
   rápido). Hoy devuelve exit 0 con la suite completamente rota: el smoke test de la
   sección 11 no es suficiente como puerta de calidad.

7. **Limpieza de repo** (B6): `git rm --cached` de los 14 `.bak`/backups, deduplicar
   `.gitignore`, añadir `!HARNESS_REPORT_*.md` si se quiere versionar el reporte, y borrar
   el directorio `./~/`.

8. **Clasificar los 7 tests de `Z_sin_clasificar`** (B7) en sus grupos correspondientes.

9. **Añadir `pytest -q` de humo al CI local** y fijar el no determinismo del LLM en los
   smoke tests (assert sobre `len(plan.pasos) >= 2` y tipos de agente, nunca sobre nombres).

10. **Revisar `http_executor.py:175`**: redefine `cancelar_peticion` sobre la de la línea 160;
    puede enmascarar un bug de cancelación.

---

## Anexo — Reproducibilidad

Logs completos guardados en `tmp/harness_v3.0.1/` (ignorado por git):

| Archivo | Contenido |
|---|---|
| `pre_tag_1.log` / `pre_tag_2.log` | `pre_tag_check.sh` antes y después de instalar pyflakes |
| `check_env.log` | `main.py --check-env` |
| `tests_run.log` / `tests_run_clean.log` | Suite completa (con y sin códigos ANSI) |
| `pyflakes_full.log` | Crash de pyflakes escaneando `.venv` (18 523 líneas) |
| `pyflakes_project.log` | 9 avisos reales del proyecto |
| `db_before.txt` / `db_before_tests.txt` | Huellas SHA-256 de `agent_history.db` |

**Secuencia exacta ejecutada:**

```bash
git branch --show-current && git log --oneline --decorate -5 && git status --short && git tag -l
ls -la && ls core/ core/executors/ core/problem_solver/ storage/ learning/ ui/ export/ tools/
./tools/pre_tag_check.sh                     # Fase 1.3  → exit 0  [paralelo con ↓]
python main.py --check-env                   # Fase 2.4  → exit 0
.venv/bin/pip install pyflakes               # Fase 2.5  → pyflakes 3.4.0
./tools/pre_tag_check.sh                     # Fase 2.5  → exit 0 (sección 7 crashea)
python run_all_tests.py                      # Fase 3.6  → exit 1  [GATE: flujo detenido]
# Fase 4 (paso 8): NO EJECUTADA por el gate de Fase 3
```
