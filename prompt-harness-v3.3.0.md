[harness:fix-all+flask-web]

## CONTEXTO DEL PROYECTO
- Proyecto: Agentes Visuales (Python 3.11+, PyQt6, DeepSeek)
- Directorio: /home/christian/proyectosPython/agentes_visuales
- Rama de trabajo: harness/fix-all-flask-web (ya existe, úsala)
- Punto de entrada: main.py (subcomandos GUI/run/list-agents/serve)
- Documentación: MANUAL_CLI.md v3.2.1 → se cierra esta entrega en v3.3.0

No avanzar a una fase si la anterior no pasa su verificación.

## FASE 0: .gitignore (antes de tocar código)
1. Diagnosticar la causa: `grep -n "^\*\.md" .gitignore`
2. Añadir excepciones al final de .gitignore:
```gitignore
   # Documentación versionable
   !MANUAL_CLI.md
   !CHANGELOG.md
   !CONTRIBUTING.md
   !docs/**/*.md
```
3. Validar que la salida quede vacía:
   `git check-ignore -v MANUAL_CLI.md CHANGELOG.md`

## FASE 1: BUGS (orden estricto)

### 🔴 B1 — Propagación thinking/reasoning en LLMAgent
Síntoma: el agente LLM falla con "El LLM no devolvió JSON válido en la parte 1/1".
Causa: core/agent.py define thinking_enabled_llm=False y
reasoning_effort_llm="low", pero core/llm_client.py inicializa el
cliente compartido con los valores por defecto (high+enabled).
LLMAgent no los propaga en la llamada a chat().
Fix: pasar reasoning_effort y thinking_enabled como parámetros de
LLMClient.chat() (o crear cliente por agente) y que LLMAgent los use.
Verificación: `python main.py run --prompt "di hola" --json --quiet`
debe devolver ok: true.

### 🔴 B2 — Plan B arranca tras shutdown
Síntoma: RuntimeError: cannot schedule new futures after interpreter
shutdown en core/scheduler.py:1029 _intentar_plan_b.
Fix: guard `if self._detenido: return` al inicio de _intentar_plan_b,
y Scheduler.detener() debe llamar a
self._executor.shutdown(wait=True, cancel_futures=True).

### 🟡 B3 — --quiet no llega a serve/list-agents
Fix: añadir --quiet/-q a p_serve y p_list en main.py, propagar a
_configurar_logging(quiet=...).

### 🟡 B4 — 504 en serve no cancela ejecución interna
Fix: documentar en MANUAL_CLI.md §7 que el 504 no cancela la ejecución
interna (sigue corriendo y se registra en agent_history.db).

### 🟢 B5 — Ejemplos JSON del manual incompletos
Fix: completar en MANUAL_CLI.md §6 el ejemplo de list-agents --json
con el campo campos_requeridos.

## FASE 2: ENTORNO WEB FLASK

### Estructura
web/
├── __init__.py
├── app.py          # create_app() con Blueprint api
├── templates/
│   └── index.html  # formulario mínimo
└── static/
    └── style.css   # opcional

### Endpoints
| Método | Ruta          | Descripción                               |
|--------|---------------|--------------------------------------------|
| GET    | /             | Formulario HTML (prompt, max_pasos, etc.) |
| POST   | /api/run      | Ejecuta pipeline, devuelve JSON de run    |
| GET    | /api/health   | {ok, version}                              |
| GET    | /api/agents   | Lista de agentes (mismo formato que CLI)  |

### Integración Qt-safe (obligatoria)
- Cola queue.Queue compartida entre hilo Flask y hilo Qt principal.
- QTimer.singleShot(25, _procesar_trabajos) consume la cola.
- Cada trabajo lleva threading.Event para que el request Flask espere.
- --timeout global limita la espera del cliente HTTP.
- Todas las conexiones de señales Qt usadas en este flujo deben llevar
  Qt.ConnectionType.QueuedConnection (mismo patrón ya aplicado en
  main_window.py tras el fix del segfault de hilos).

### Subcomando
python main.py web --host 127.0.0.1 --port 5000

### Reutilización obligatoria (no duplicar pipeline)
- main._ejecutar_pipeline(...) para ejecutar tareas.
- main._listar_agentes() para el catálogo.
- main._asegurar_qt() para la QCoreApplication.
- main.__version__ para /api/health.
- El cliente LLM debe obtenerse vía obtener_llm_client_compartido()
  (singleton ya existente en core/llm_client.py); no instanciar
  LLMClient() nuevo dentro del blueprint de Flask.

### Restricciones
- No romper exit codes existentes (0/1/2/3/4/130).
- No añadir dependencias fuera de Flask.
- Mantener contrato stdout/stderr de run.
- Si Flask no está instalado, web debe fallar con exit code claro
  (EXIT_ENV_ERROR = 3) y mensaje "pip install flask".

## FASE 3: DOCUMENTACIÓN Y DEPENDENCIAS
- requirements.txt: añadir flask>=3.0.
- MANUAL_CLI.md:
  - Nueva §7.bis "web — servidor Flask" con endpoints, ejemplos, códigos HTTP.
  - §10: añadir filas para los 3 problemas frecuentes nuevos.
  - §3: añadir web a la tabla de comandos.
  - §11: añadir columna web a la comparativa GUI/CLI.
  - Cabecera: subir "Versión del documento" a v3.3.0.
- main.py: docstring inicial + epilog del parser + help del subcomando web.
- CHANGELOG.md: nueva entrada v3.3.0.

## FASE 4: CRITERIOS DE ACEPTACIÓN (todos deben pasar)
1. `python main.py run --prompt "di hola" --json --quiet | jq .ok` → true
2. `python main.py web --port 5000 &` arranca sin errores.
3. `curl -s http://127.0.0.1:5000/api/health | jq` → {ok: true, version: "..."}
4. `curl -s -X POST http://127.0.0.1:5000/api/run -H 'Content-Type: application/json' -d '{"prompt":"di hola","aprender":false}' | jq .ok` → true
5. `curl -s http://127.0.0.1:5000/api/agents | jq '.agentes | length'` → >0
6. `python main.py --check-env` sigue con exit 0.
7. `python main.py` (sin args) sigue arrancando la GUI.
8. Sin regresiones en tests de run, list-agents, serve.
9. Sin regresión del fix de segfault por hilos (QueuedConnection intacto).

## FASE 5: ENTREGA Y VERSIONADO EN GIT (obligatorio, exacto)

```bash
# 1. Forzar rama de trabajo (no solo comprobar)
git checkout harness/fix-all-flask-web || { echo "ERROR: no se pudo cambiar a harness/fix-all-flask-web"; exit 1; }

# 2. Revisar cambios
git status
git diff --stat

# 3. Añadir TODO
git add -A

# 4. Verificar que no se cuela basura
git status
# Debe aparecer: main.py, core/agent.py, core/llm_client.py,
# core/scheduler.py, MANUAL_CLI.md, CHANGELOG.md, requirements.txt,
# .gitignore, web/**, y NADA de .venv/, __pycache__/, logs/,
# agent_history.db, *.pyc

# 5. Commit único
git commit -m "fix: corrige propagación LLM y añade entorno web Flask

Bugs corregidos:
- B1: LLMAgent propaga thinking_enabled/reasoning_effort al LLMClient
- B2: Plan B no arranca tras detener() + shutdown limpio del executor
- B3: --quiet disponible en serve y list-agents
- B4: documentado que el 504 en serve no cancela la ejecución interna
- B5: ejemplos JSON del manual completados

Nuevo:
- Subcomando 'web' con Flask (GET /, POST /api/run, GET /api/health, GET /api/agents)
- Reutiliza _ejecutar_pipeline, _asegurar_qt y obtener_llm_client_compartido(); Qt-safe vía cola + QTimer
- requirements.txt: flask>=3.0
- MANUAL_CLI.md v3.3.0 y CHANGELOG.md actualizados
- .gitignore: excepciones para MANUAL_CLI.md/CHANGELOG.md

Verificado:
- python main.py run --prompt 'di hola' --json --quiet -> ok:true
- python main.py web --port 5000 -> /api/health responde
- Sin regresiones en GUI, --check-env, list-agents, serve"

# 6. Tag anotado v3.3.0 (solo si TODOS los criterios de aceptación pasaron)
git tag -a v3.3.0 -m "v3.3.0 — correcciones CLI + entorno web Flask

- Fix propagación thinking/reasoning en LLMAgent
- Fix shutdown de Scheduler y guard en Plan B
- --quiet en serve y list-agents
- Nuevo subcomando 'web' (Flask)
- MANUAL_CLI.md v3.3.0, CHANGELOG.md v3.3.0"

# 7. Mostrar resultado
git log --oneline -1
git tag -l 'v3.3.0' -n99
git show --stat v3.3.0 | head -40
```

### Reglas estrictas del paso git
1. No añadir .venv/, logs/, __pycache__/, agent_history.db, *.pyc, .env.
   Si git status los muestra, añadirlos a .gitignore antes del commit.
2. Un solo commit para toda la entrega. No fragmentar.
3. Tag anotado (-a), no ligero. Debe llevar mensaje.
4. El tag solo se crea si el commit existe y TODOS los criterios de
   aceptación de FASE 4 pasan. Si algo falla, NO crear el tag: dejar
   el commit abierto y reportar qué falló.
5. No hacer git push ni git push --tags salvo instrucción explícita
   posterior del usuario.
6. Si el tag v3.3.0 ya existe, no sobreescribir: fallar con mensaje
   "tag v3.3.0 ya existe; usa v3.3.1 o elimínalo manualmente".

## VERIFICACIÓN FINAL OBLIGATORIA (pegar en el reporte)
```bash
git log --oneline -1
git tag -l 'v3.3.0'
git show v3.3.0 --stat | head -30
python main.py run --prompt "di hola" --json --quiet | jq .ok
python main.py list-agents --json | jq '.agentes | length'
```
Salida esperada:
- Commit "fix: corrige propagación LLM y añade entorno web Flask"
- Tag anotado v3.3.0
- ok: true
- Número de agentes > 0

## ENTREGABLES FINALES
1. Commit en harness/fix-all-flask-web con git add -A + mensaje completo.
2. Tag anotado v3.3.0 sobre ese commit.
3. MANUAL_CLI.md v3.3.0, CHANGELOG.md v3.3.0, requirements.txt con Flask, .gitignore corregido.
4. Reporte final con la salida de los comandos de verificación.
5. NO push (salvo instrucción explícita posterior del usuario).
