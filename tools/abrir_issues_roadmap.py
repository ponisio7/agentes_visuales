#!/usr/bin/env python3
"""Abre en GitHub los issues del Bloque 1 y Bloque 2 de ``ROADMAP.md``.

Por qué existe: ``gh`` no está instalado en este entorno, así que se usa la
API REST de GitHub con ``urllib`` (sin dependencias nuevas).

Uso:
    # 1. Ver qué haría, sin tocar nada (por defecto):
    python tools/abrir_issues_roadmap.py

    # 2. Crear de verdad (requiere token con permiso 'issues: write'):
    export GITHUB_TOKEN=ghp_xxx
    python tools/abrir_issues_roadmap.py --apply

    # Solo un bloque:
    python tools/abrir_issues_roadmap.py --bloque 1 --apply

Es **idempotente**: si ya existe un issue con el mismo título (abierto o
cerrado), lo salta. Los títulos llevan el ID del roadmap para poder
re-ejecutarlo sin duplicar.

El repositorio se deduce de ``git remote origin`` salvo que se indique
``--repo`` o ``GITHUB_REPOSITORY=owner/name``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"

# ---------------------------------------------------------------------------
# Contenido de los issues, derivado de ROADMAP.md §3 (Bloque 1) y §4 (Bloque 2).
# El título empieza por el ID para que la idempotencia sea fiable.
# ---------------------------------------------------------------------------

BLOQUE_1: list[dict] = [
    {
        "titulo": "[bug] 3.1 El plan de fallback nunca produce el artefacto pedido (1.2 + 1.10)",
        "labels": ["bug", "bloque-1-estabilidad"],
        "cuerpo": """## Síntoma observable

Ante un fallo de API key, «crear `saludo.txt`» termina con `status: ok` sin que
el fichero exista.

```
Comando: python main.py run --prompt "crear saludo.txt"
Esperado: existe saludo.txt
Obtenido: status ok, sin fichero
```

## Evidencia

- `core/problem_solver/solver.py:416-448` — `_crear_plan_fallback` devuelve un
  dict de resultado con `status: ok`; no incluye ningún paso que materialice el
  artefacto.
- `core/problem_solver/builder.py:112` — mismo patrón en el paso por defecto.
- `core/problem_solver/validator.py:205` — tercera copia del mismo código.

## Causa raíz

La escritura la haría un agente `File` que el plan de respaldo no incluye. El
acta original describía esto como un `SyntaxError` hardcodeado, pero el código
es sintácticamente válido (`data = contexto if contexto else {}` con `contexto`
en el scope del `exec`): **el diagnóstico del acta es incorrecto**, el defecto
real es la ausencia del paso de escritura.

## Acción propuesta

1. Que el plan de fallback declare un paso `File`/`Shell` que materialice el
   artefacto pedido, o
2. que devuelva un fallo honesto en vez de `status: ok`.

**Prohibido**: que el fallback simule que hizo algo.

## Criterio de cierre

`test_regression_NNN` ejecuta el plan de fallback y comprueba
`os.path.exists("saludo.txt")` con contenido no vacío.
""",
    },
    {
        "titulo": "[bug] 3.2 EvaluadorLLM recibe None y degrada el aprendizaje en silencio (1.14)",
        "labels": ["bug", "bloque-1-estabilidad", "aprendizaje"],
        "cuerpo": """## Síntoma observable

Log literal: `Evaluador no disponible: 'NoneType' object has no attribute 'chat'`.
El aprendizaje por refuerzo queda **silenciosamente degradado**: el evaluador
devuelve score neutro 0.5 y el flujo continúa como si nada.

## Evidencia

- `learning/reward_llm.py:63` — `self.llm_client.chat(...)`.
- `learning/reward_llm.py:75` — el `except` produce exactamente ese mensaje.
- `learning/engine.py:125` — `EvaluadorLLM(llm_client)` con el valor recibido.
- `learning/__init__.py:22` — `obtener_learning_engine(..., llm_client=None)`.
- **3 rutas no pasan cliente**: `core/problem_solver/solver.py:125`,
  `core/problem_solver/solver.py:137`, `core/scheduler.py:832`.
- Solo `core/execution_recorder.py:150` lo pasa bien
  (`obtener_llm_client_compartido()`).

## Causa raíz

`obtener_learning_engine` es un singleton perezoso: fija el `llm_client` de la
**primera** llamada. Como 3 de 4 rutas no lo pasan, el resultado depende del
orden de arranque (si `execution_recorder` llega primero, se salva por azar).

## Acción propuesta

- `EvaluadorLLM.set_llm_client(llm)` tras `obtener_llm_client_compartido()`, o
  resolución perezosa del cliente dentro de `EvaluadorLLM`.
- Que la ausencia de cliente sea un **error visible**, no un score neutro.

## Criterio de cierre

Test que crea el engine desde `solver.py` y verifica que el evaluador tiene un
cliente real (no `None`).
""",
    },
    {
        "titulo": "[bug] 3.3 Placeholder fabricado por código generado por el LLM (1.7 + 1.8)",
        "labels": ["bug", "bloque-1-estabilidad", "problem-solver"],
        "cuerpo": """## Síntoma observable

`GenerarHTML` muestra «Descripción no disponible» aunque el paso LLM sí
devolvió datos. En planes de 4 agentes: `ExtraerProductos` → `{'productos': []}`
y `FormatearTexto` → `{'texto': ''}`, con el JSON de entrada correcto.

## Evidencia

- `logs/llm_response_20260913_091705_deepseek-v4-flash.txt:33` — el string
  «Descripción no disponible» aparece **dentro del código Python que generó el
  LLM**:
  ```python
  if not isinstance(ideas, list) or len(ideas) != 5:
      ideas = []
      for i in range(1, 6):
          ideas.append({'titulo': f'Idea {i}', 'descripcion': 'Descripción no disponible'})
  ```
- `core/executors/llm_executor.py:580-582` — el contrato del executor expone
  `respuesta` **y** `json` (`json_auto`, que puede ser `None`).

## Causa raíz

El código generado asume `respuesta_llm.get('json', {})` con forma
`{"ideas": [...]}`. Si el JSON tiene otra forma, el fallback **fabrica datos
falsos** en vez de fallar, y el éxito falso se propaga hasta el HTML.

Nota: `content_extractor` y los nombres `SeleccionarTop10` /
`GenerarDescripciones` / `CombinarDatos` **no existen en el código**: son
nombres de agente generados por el LLM en runtime. El acta apuntaba al módulo
equivocado.

## Acción propuesta

1. Prohibir en el prompt del planificador los fallbacks que inventan contenido.
2. Que un resultado vacío sea fallo del paso y dispare Plan B.
3. Validar la forma del `json` contra lo que el paso consumidor espera.

## Criterio de cierre

El caso «5 ideas» falla de forma visible o se recupera vía Plan B; **nunca**
muestra «Descripción no disponible».
""",
    },
    {
        "titulo": "[bug] 3.4 SIGSEGV intermitente en la suite / sandbox (1.13)",
        "labels": ["bug", "bloque-1-estabilidad", "sandbox"],
        "cuerpo": """## Síntoma observable

`python -m pytest -q` muere con SIGSEGV. Traza reproducida:

```
Current thread (most recent call first):
  File "tests/conftest.py", line 49 in esperar_condicion
  File "tests/test_flujo_e2e_aceptacion.py", line 79 in _ejecutar
  File "tests/test_flujo_e2e_aceptacion.py", line 110 in test_fallo_de_aceptacion_dispara_plan_b_y_acaba_aceptado
```

## Evidencia y mitigaciones ya aplicadas

- `tools/run_tests.sh` — mitigación **operativa** vigente: aislamiento por
  proceso con `--forked`. Con ella: `1166 passed, 1 skipped`.
- `core/sandbox.py:853-870` — `cwd` explícito para evitar
  `posix_spawn`/`vfork`, y `TemporaryFile` en vez de `PIPE` (evita el bloqueo
  de buffer y el `communicate()` desde varios hilos).
- `core/sandbox.py:872-879` — `_SPAWN_LOCK` serializa los `fork()` y
  `stdin=subprocess.DEVNULL`.

## Causa raíz

`fork` + hilos en CPython 3.13 con extensiones nativas cargadas. El fix
propuesto en el acta (`NamedTemporaryFile(delete=False)` + cleanup explícito)
**no está aplicado**; la línea 853 ya no es el `TemporaryFile` compitiendo con
`cleanup_worker`.

## Acción propuesta

Decidir de forma explícita entre:
- (a) asumir `tools/run_tests.sh --forked` como definitivo y documentarlo, o
- (b) aplicar un ciclo de vida explícito de los temporales.

## Criterio de cierre

`python -m pytest -q` sin `--forked` completa en verde, **o** decisión escrita
en `DECISIONES.md` de no perseguirlo.
""",
    },
    {
        "titulo": "[bug] 3.5 BrokenPipeError al cerrar el pipe (run --json | jq) (1.18)",
        "labels": ["bug", "bloque-1-estabilidad", "cli"],
        "cuerpo": """## Síntoma observable

`python main.py run --json ... | jq ...` imprime un traceback de
`BrokenPipeError` si `jq` muere o cierra el pipe antes de tiempo.

## Evidencia

`grep -rn "BrokenPipeError" --include=*.py core/ main.py` → **ninguna
coincidencia** en el código del proyecto (solo en `.venv`).

## Acción propuesta

- Capturar `BrokenPipeError` en `_configurar_logging` (`main.py:309`) y en el
  `print` final.
- Redirigir `sys.stdout` a `os.devnull` y salir con `EXIT_OK`.

## Coste e impacto

Minutos de trabajo; elimina ruido y códigos de salida incorrectos en scripts.

## Criterio de cierre

`python main.py run --json ... | head -1` termina sin traceback y con código de
salida 0.
""",
    },
    {
        "titulo": "[bug] 3.6 weasyprint no está silenciado y contamina los logs (1.16)",
        "labels": ["bug", "bloque-1-estabilidad", "logging"],
        "cuerpo": """## Síntoma observable

`weasyprint.progress` escribe en los logs y tapa la información útil.

## Evidencia

`main.py:343-345` silencia:

```python
for ruidoso in ("urllib3", "openai", "matplotlib", "PIL",
                "httpx", "httpx2", "httpcore", "charset_normalizer"):
    logging.getLogger(ruidoso).setLevel(logging.WARNING)
```

**`weasyprint` no está en la lista**, y `core/executors/file_executor.py:18` lo
importa (se usa en `:1401` para generar PDF).

## Acción propuesta

Añadir `"weasyprint"` a la tupla de loggers ruidosos.

## Coste e impacto

**2 minutos** de trabajo, impacto alto en legibilidad de logs. El mejor
ratio de todo el roadmap.

## Criterio de cierre

Generar un PDF y comprobar que no aparece ninguna línea de
`weasyprint.progress`.
""",
    },
    {
        "titulo": "[bug] 3.7 duracion no acumulativa en reintentos (1.5)",
        "labels": ["bug", "bloque-1-estabilidad", "scheduler"],
        "cuerpo": """## Síntoma observable

Con 3 intentos de 5 s + 7 s + 4 s, `agente.duracion` acaba en 4 s (el último
intento), no en 16 s.

## Evidencia

- `core/scheduler.py:594` — `duracion = tiempo_fin - tiempo_inicio` (por intento).
- `core/scheduler.py:605` — `agente.duracion = duracion` **sobrescribe**.
- `core/scheduler.py:726-751` — bucle de reintento que reejecuta el agente.

## Precisión sobre el acta

El acta afirmaba que `_ejecutar_agente` **no actualiza** `agente.duracion`. Eso
es **falso**: sí la actualiza (línea 605). El defecto real es que no es
acumulativa entre reintentos.

## Acción propuesta

Acumular por agente (`agente.duracion += delta`), decidiendo si se expone
además la duración del último intento por separado.

## Criterio de cierre

Test con 3 intentos de 5/7/4 s → `agente.duracion == 16`.
""",
    },
    {
        "titulo": "[bug] 3.8 Gate py_compile + rollback en PythonCodeCorrector (1.3)",
        "labels": ["bug", "bloque-1-estabilidad", "problem-solver"],
        "cuerpo": """## Síntoma observable

No hay ninguna red de seguridad que garantice que una corrección automática de
código no lo empeora.

## Evidencia

- `core/problem_solver/code_corrector.py:82` — `corregir(codigo, dependencias)`.
- `grep -n "py_compile\\|rollback" core/problem_solver/code_corrector.py` →
  **sin coincidencias**. El corrector es AST + reemplazos (`:156`, `:185`,
  `:209`), sin gate de compilación ni vuelta atrás.

## Precisión sobre el acta

La corrupción concreta descrita (`data = contexto if contexto else {}` →
`contexto contexto...`) **no está en el repo**: las 3 ocurrencias
(`solver.py:437`, `builder.py:112`, `validator.py:205`) están intactas. El
valor de este punto es blindar el corrector, no arreglar un string roto.

## Acción propuesta

Pipeline obligatorio:
`generar corrección → py_compile → tests → aceptar; si falla → rollback`.

## Criterio de cierre

Test que pasa al corrector una transformación que rompe la sintaxis y verifica
que se descarta y se devuelve el código original.
""",
    },
    {
        "titulo": "[bug] 3.9 Reintento correctivo ante respuesta «solo razonamiento» (1.9)",
        "labels": ["bug", "bloque-1-estabilidad", "llm"],
        "cuerpo": """## Síntoma observable

El LLM devuelve razonamiento en lugar de respuesta final; el paso bloquea en
cascada a sus dependientes.

## Evidencia

`core/executors/llm_executor.py:566-576` **detecta** el caso y solo hace
`logger.warning`:

```python
if any(respuesta_lower.startswith(p.lower()) for p in palabras_thinking) \\
        and len(respuesta) > 300:
    logger.warning(f"⚠️ LLM parece haber devuelto razonamiento en lugar de respuesta...")
```

No hay reintento correctivo ni ajuste de `max_tokens`. Nota:
`learning/reward_llm.py:28` ya subió su `max_tokens` a 500 con
`thinking_enabled=False`, pero el `max_tokens` de los pasos LLM del plan lo fija
el plan, no el executor.

## Acción propuesta

Ante la detección: reintentar **una vez** con instrucción correctiva y/o subir
`max_tokens` antes de marcar el paso como fallido.

## Criterio de cierre

Test con un LLM simulado que primero devuelve razonamiento y luego JSON:
el paso acaba en éxito tras el reintento.
""",
    },
    {
        "titulo": "[bug] 3.10 Medir cobertura del validador de Fase 6 sobre corpus real (1.12)",
        "labels": ["bug", "bloque-1-estabilidad", "problem-solver"],
        "cuerpo": """## Síntoma observable

El LLM genera código que usa nombres de agente como variables globales (en vez
de `contexto.get('Agente', {})`) y, a veces, JSON inválido pese a pedirlo.

## Evidencia

El detector **ya existe**:
- `core/problem_solver/validator.py:665-680` — descarta identificadores que
  están en `nombres_agentes` y emite `BLOQUEANTE: ... usa '{identificador}' sin
  definirla`.
- `core/problem_solver/code_corrector.py:82` — reescribe vía AST.
- Otras ramas `BLOQUEANTE`: `validator.py:539` (SyntaxError), `:572`
  (json.loads con placeholder literal), `:643`, `:717` (clave inexistente).

## Lo que falta

**Medir la cobertura** sobre el corpus de `logs/`, no fiarse del caso probado a
mano. El acta pedía verificar que el validador lo detecta «en todos los casos».

## Acción propuesta

1. Extraer de `logs/` todos los planes con nombres de agente como variable
   global.
2. Pasar cada uno por el validador.
3. Reportar cuántos detecta; convertir los fallos en tests de regresión.

## Criterio de cierre

Informe con `detectados / total` sobre el corpus, y tests para los huecos.
""",
    },
    {
        "titulo": "[bug] 3.11 --timeout no acota el comando completo (1.17)",
        "labels": ["bug", "bloque-1-estabilidad", "cli"],
        "cuerpo": """## Síntoma observable

`--check-env --timeout 5` puede tardar ~11 s (`Respuesta 200 en 10949 ms`)
porque el timeout se aplica a la conexión/lectura, no al comando completo.

## Evidencia

- `core/env_checker.py:239` — `timeout=timeout` se pasa a `requests`, que es
  por operación (connect + read), no total.
- `core/env_checker.py:283-284` — el manejo de timeout reporta
  `Timeout tras {timeout}s`, dando a entender que acota el total.
- `core/env_checker.py:433` — ya existe la pista de subirlo manualmente.

## Acción propuesta

Endpoint más ligero o `signal.alarm` global que acote el comando completo.

## Prioridad

Impacto bajo (solo afecta a `--check-env`). Candidato a cerrar si sobra tiempo.
""",
    },
]

BLOQUE_2: list[dict] = [
    {
        "titulo": "[deuda] 4.1 Refactorizar Scheduler._ejecutar_agente",
        "labels": ["deuda-tecnica", "bloque-2-deuda", "scheduler"],
        "cuerpo": """## Deuda concreta

`_ejecutar_agente` es el bloque más grande del `Scheduler` sin dividir. El
refactor V4.0-5 se limitó a `_intentar_plan_b`, dejando este fuera.

## Evidencia

- `HARNESS_STATUS.md:186-187` — deuda **ya declarada** por el propio proyecto:
  «`_ejecutar_agente` (Scheduler) no se ha tocado; el refactor V4.0-5 se limitó
  a `_intentar_plan_b`».
- `core/scheduler.py` — 1792 líneas en total.

## Plan de refactor

1. **Tests de caracterización ANTES de tocar** (patrón ya validado en V4.0-5):
   `tests/test_scheduler_plan_b_caracterizacion.py`.
2. Extraer por fases, siguiendo el modelo de `_pb_*` de `_intentar_plan_b`:
   preparación, ejecución, verificación de aceptación (FASE 4b), finalización
   (FASE 5) y decisión de reintento.
3. Mantener la API pública del `Scheduler` sin cambios.

## Criterio de cierre

`core/scheduler.py` con `_ejecutar_agente` como orquestador corto + helpers, y
suite completa aislada en verde (`tools/run_tests.sh`).
""",
    },
    {
        "titulo": "[deuda] 4.2 Causa raíz de registrar_ejecucion_en_aprendizaje",
        "labels": ["deuda-tecnica", "bloque-2-deuda", "aprendizaje"],
        "cuerpo": """## Deuda concreta

`_ultima_ejecucion_id` se resuelve con un parche `SELECT MAX(id)` que rodea el
síntoma en vez de arreglar la causa. Si hay ejecuciones concurrentes, el id
puede no corresponder a la ejecución actual, con lo que el feedback humano se
asocia a la fila equivocada.

## Evidencia

- `ui/simple_main_window.py:1085` —
  `conn.execute("SELECT MAX(id) AS ultimo FROM ejecuciones")` dentro de
  `_obtener_ultimo_ejecucion_id_fallback`.
- El propio docstring admite la limitación: «puede no corresponder exactamente
  a esta ejecución si hay ejecuciones concurrentes».
- `ui/execution_recorder` / `registrar_ejecucion_en_aprendizaje` falla en
  silencio (por eso existe el fallback).

## Acción propuesta

1. Diagnosticar **por qué** `registrar_ejecucion_en_aprendizaje` falla; hoy se
   desconoce.
2. Propagar el `ejecucion_id` real por el camino de la ejecución en lugar de
   inferirlo por `MAX(id)`.
3. Bug menor incluido: `logger.info` **duplicado** en
   `ui/simple_main_window.py:1088-1093` (cada línea se registra dos veces).

## Criterio de cierre

El `ejecucion_id` se propaga explícitamente; test que ejecuta con dos
ejecuciones concurrentes y verifica que el feedback va a la fila correcta.
""",
    },
]


def _repo_desde_git() -> str | None:
    try:
        url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    for prefijo in ("https://github.com/", "git@github.com:"):
        if url.startswith(prefijo):
            return url[len(prefijo):].removesuffix(".git")
    return None


def _peticion(metodo: str, ruta: str, token: str, payload: dict | None = None):
    datos = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(f"{API}{ruta}", data=datos, method=metodo)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if datos:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        cuerpo = resp.read().decode()
    return json.loads(cuerpo) if cuerpo else None


def _asegurar_labels(repo: str, token: str, labels: set[str], aplicar: bool) -> None:
    """GitHub rechaza (422) un issue con labels inexistentes."""
    existentes = set()
    pagina = 1
    while True:
        lote = _peticion("GET", f"/repos/{repo}/labels?per_page=100&page={pagina}", token)
        if not lote:
            break
        existentes.update(lbl["name"] for lbl in lote)
        if len(lote) < 100:
            break
        pagina += 1

    colores = {
        "bug": "d73a4a",
        "bloque-1-estabilidad": "b60205",
        "deuda-tecnica": "fbca04",
        "bloque-2-deuda": "c5def5",
        "aprendizaje": "5319e7",
        "problem-solver": "1d76db",
        "sandbox": "0e8a16",
        "cli": "bfd4f2",
        "logging": "fef2c0",
        "scheduler": "d4c5f9",
        "llm": "5319e7",
    }
    for nombre in sorted(labels - existentes):
        if not aplicar:
            print(f"    · crearía label ausente: {nombre}")
            continue
        _peticion("POST", f"/repos/{repo}/labels", token,
                  {"name": nombre, "color": colores.get(nombre, "ededed")})
        print(f"    · label creada: {nombre}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY"),
                        help="owner/name (por defecto: derivado de git remote origin)")
    parser.add_argument("--bloque", choices=["1", "2", "todos"], default="todos")
    parser.add_argument("--apply", action="store_true",
                        help="Crea los issues. Sin esta bandera solo simula (dry-run).")
    args = parser.parse_args()

    repo = args.repo or _repo_desde_git()
    if not repo:
        print("❌ No se pudo determinar el repositorio. Usa --repo owner/name "
              "o define GITHUB_REPOSITORY.", file=sys.stderr)
        return 1

    pendientes = []
    if args.bloque in ("1", "todos"):
        pendientes += BLOQUE_1
    if args.bloque in ("2", "todos"):
        pendientes += BLOQUE_2

    print(f"Repositorio : {repo}")
    print(f"Modo        : {'APLICAR (crea issues)' if args.apply else 'DRY-RUN (no toca nada)'}")
    print(f"Bloque      : {args.bloque}  ·  {len(pendientes)} issues en el roadmap\n")

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        if args.apply:
            print("❌ Falta GITHUB_TOKEN (o GH_TOKEN) con permiso 'issues: write'.",
                  file=sys.stderr)
            print("   export GITHUB_TOKEN=ghp_xxx", file=sys.stderr)
            return 1
        # DRY-RUN sin token: se omite la comprobación de duplicados (necesita
        # autenticación si el repo es privado) y se listan todos como pendientes.
        print("ℹ️  Sin GITHUB_TOKEN: dry-run en modo offline "
              "(no se comprueban duplicados).\n")
        for issue in pendientes:
            print(f"  +  crearía  : {issue['titulo']}")
        print(f"\nResumen: {len(pendientes)} issues definidos en el roadmap.")
        print("Ejecuta con GITHUB_TOKEN=... y --apply para crearlos de verdad.")
        return 0

    try:
        existentes = set()
        pagina = 1
        while True:
            lote = _peticion("GET",
                             f"/repos/{repo}/issues?state=all&per_page=100&page={pagina}",
                             token)
            if not lote:
                break
            existentes.update(i["title"] for i in lote if "pull_request" not in i)
            if len(lote) < 100:
                break
            pagina += 1

        labels_necesarias = {lbl for i in pendientes for lbl in i["labels"]}
        _asegurar_labels(repo, token, labels_necesarias, args.apply)

        creados = saltados = 0
        for issue in pendientes:
            titulo = issue["titulo"]
            if titulo in existentes:
                print(f"  ⏭  ya existe: {titulo}")
                saltados += 1
                continue
            if not args.apply:
                print(f"  +  crearía  : {titulo}")
                continue
            nuevo = _peticion("POST", f"/repos/{repo}/issues", token,
                              {"title": titulo, "body": issue["cuerpo"],
                               "labels": issue["labels"]})
            print(f"  ✅ creado   : #{nuevo['number']} {titulo}")
            creados += 1

    except urllib.error.HTTPError as e:
        detalle = e.read().decode(errors="replace")[:500]
        print(f"\n❌ GitHub respondió {e.code}: {detalle}", file=sys.stderr)
        if e.code == 401:
            print("   Token inválido o caducado.", file=sys.stderr)
        elif e.code == 403:
            print("   Sin permiso 'issues: write' o rate limit.", file=sys.stderr)
        elif e.code == 404:
            print(f"   ¿Existe el repo '{repo}' y el token lo ve?", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"\n❌ Error de red: {e.reason}", file=sys.stderr)
        return 1

    print()
    if args.apply:
        print(f"Resumen: {creados} creados, {saltados} ya existían.")
    else:
        print(f"Resumen: {len(pendientes) - saltados} por crear, {saltados} ya existen.")
        print("Ejecuta con --apply para crearlos de verdad.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
