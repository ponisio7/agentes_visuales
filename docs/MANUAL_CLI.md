# Manual de la CLI de Agentes Visuales

Guía de uso de `main.py` en línea de comandos, incluidos los modos
headless (`run`, `list-agents`, `serve`) y el entorno web (`web`) que
ejecutan el mismo pipeline que la GUI sin abrir ventana.

- **Versión del documento**: v3.3.0
- **Punto de entrada**: `main.py`
- **Rama**: `harness/fix-all-flask-web`

---

## 1. Requisitos

| Requisito | Detalle |
|---|---|
| Python | 3.11 o superior (el proyecto se desarrolla en 3.13) |
| Dependencias | `pip install -r requirements.txt` dentro del `.venv` |
| API key | `DEEPSEEK_API_KEY` (ver §2) |
| PyQt6 | Necesario incluso en headless: `Scheduler` es un `QObject` y usa `QtCore`. No hace falta pantalla ni servidor X |

```bash
cd ~/proyectosPython/agentes_visuales
source .venv/bin/activate
```

---

## 2. Configuración de la API key

La clave se busca, por orden, en:

1. La variable de entorno `DEEPSEEK_API_KEY`.
2. `~/.config/agentes_visuales/env`
3. `~/.config/deepseek.env`
4. `~/.deepseek_key`

Formato del fichero (una variable por línea):

```bash
mkdir -p ~/.config/agentes_visuales
echo 'DEEPSEEK_API_KEY="sk-tu-key"' > ~/.config/agentes_visuales/env
chmod 600 ~/.config/agentes_visuales/env
```

Comprobación:

```bash
python main.py --check-env
```

---

## 3. Sinopsis

```
python main.py [OPCIONES GLOBALES] [COMANDO] [OPCIONES DEL COMANDO]
```

| Comando | Para qué sirve |
|---|---|
| *(ninguno)* | Arranca la GUI |
| `run` | Ejecuta una tarea de principio a fin, sin GUI |
| `list-agents` | Lista los tipos de agente disponibles |
| `serve` | Servidor HTTP con `POST /run` para otras apps |
| `web` | Entorno web Flask (`/`, `/api/run`, `/api/health`, `/api/agents`) |

### Opciones globales

| Opción | Por defecto | Descripción |
|---|---|---|
| `--check-env` | — | Diagnostica API key y conectividad |
| `--timeout SEGUNDOS` | *(sin límite)* | Timeout por defecto. `--check-env` usa 5.0 si no se indica |
| `--version` | — | Muestra `agentes-visuales <versión>` |
| `-h`, `--help` | — | Ayuda |

> `--timeout` puede ir **antes o después** del subcomando: `--timeout 30 run …`
> y `run --timeout 30 …` son equivalentes. Si se indica en los dos sitios,
> gana el del subcomando.

---

## 4. Códigos de salida

Códigos de `main.py`:

| Código | Constante | Significado |
|---|---|---|
| `0` | `EXIT_OK` | Todo correcto |
| `1` | `EXIT_GUI_ERROR` | No se pudo arrancar la GUI |
| `2` | `EXIT_BAD_ARGS` | Argumentos o tarea de entrada inválidos |
| `3` | `EXIT_ENV_ERROR` | No se pudo cargar/ejecutar el diagnóstico de entorno |
| `4` | `EXIT_RUN_ERROR` | La ejecución falló (LLM no disponible, agentes con error, catálogo vacío…) |
| `130` | `EXIT_USER_ABORT` | Interrumpido con Ctrl-C |

`--check-env` tiene **sus propios códigos** (vienen de `core/env_checker.py`):

| Código | Significado |
|---|---|
| `0` | Todo OK |
| `1` | Error de configuración (falta la API key) |
| `2` | Error de red (no se alcanza `api.deepseek.com`) |
| `3` | Error inesperado |

> Ojo: `1` y `2` significan cosas distintas según el comando. Para
> integraciones, comprueba siempre `--json`/cuerpo además del código.

---

## 5. `run` — ejecución headless

Genera un plan con el LLM (`ProblemSolver`) y lo ejecuta con el
`Scheduler`: exactamente el mismo pipeline que la GUI, sin interfaz.

```
python main.py run (--prompt TEXTO | --file FICHERO | --stdin) [opciones]
```

### Opciones

| Opción | Por defecto | Descripción |
|---|---|---|
| `--prompt`, `-p TEXTO` | — | Instrucción a ejecutar |
| `--file`, `-f FICHERO` | — | Fichero JSON con la tarea (ver §5.1) |
| `--stdin` | — | Lee la tarea (JSON) de la entrada estándar |
| `--agent`, `-a NOMBRE` | todos | Ejecuta **solo** los agentes del plan con ese nombre |
| `--max-pasos N` | `6` | Máximo de pasos del plan |
| `--output`, `-o FICHERO` | — | Guarda el resultado completo en JSON |
| `--json` | — | Salida JSON por stdout |
| `--quiet`, `-q` | — | Solo errores en stderr |
| `--no-aprender` | — | No guarda en aprendizaje ni activa el Plan B (no toca la BD) |
| `--timeout SEGUNDOS` | *(sin límite)* | Timeout de toda la ejecución |

**Precedencia de entrada**: `--stdin` > `--file` > `--prompt`.

**Precedencia de valores**: los flags de la CLI ganan siempre sobre los de
la tarea (`--max-pasos`, `--agent`, `--timeout`). `--no-aprender` solo
puede desactivar el aprendizaje, nunca activarlo si la tarea trae
`"aprender": false`.

### 5.1 Formato de la tarea (JSON)

Para `--file` y `--stdin`:

```json
{
  "prompt": "Genera un informe de ventas en informe.md",
  "max_pasos": 6,
  "timeout": 600,
  "aprender": true,
  "agent": "EscribirInforme"
}
```

| Clave | Tipo | Obligatoria | Descripción |
|---|---|---|---|
| `prompt` | string | **Sí** | La instrucción. Se acepta también `problema` como alias |
| `max_pasos` | int | No | Máximo de pasos (por defecto 6) |
| `timeout` | número | No | Timeout de la ejecución en segundos |
| `aprender` | bool | No | `false` equivale a `--no-aprender` |
| `agent` | string | No | Filtro por nombre de agente |

### 5.2 Contrato de salida

| Canal | Contenido |
|---|---|
| **stdout** | **Solo el resultado**: texto, o JSON si `--json` |
| **stderr** | Logs, avisos (`⚠`) y agentes fallidos (`❌`) |
| **exit code** | `0` si todos los agentes terminaron `Completado`, `4` si no |

### 5.3 Ejemplos

```bash
# Lo mínimo
python main.py run --prompt "Genera un archivo saludo.txt con Hola Mundo"

# Guardar el JSON completo y silenciar logs
python main.py run --prompt "Resume este texto" --json --quiet -o resultado.json

# Desde fichero
python main.py run --file tarea.json --max-pasos 4

# Por tubería
cat tarea.json | python main.py run --stdin --json

# Sin tocar la base de datos de aprendizaje (recomendado en CI)
python main.py run --prompt "..." --no-aprender

# Con límite de tiempo
python main.py run --prompt "..." --timeout 300
```

Salida en modo texto (lo que va a stdout):

```
{
  "archivo": "hola.txt",
  "caracteres_escritos": 4,
  "contenido_preview": "hola"
}
```

Salida `--json` (ejemplo real, recortado):

```json
{
  "ok": true,
  "problema": "Crea un fichero hola.txt con el texto hola",
  "titulo": "Crear fichero hola.txt con texto hola",
  "plan_id": "8b52ad00",
  "pasos": [
    {"orden": 1, "nombre": "GenerarContenido", "tipo_agente": "Python"},
    {"orden": 2, "nombre": "EscribirFichero", "tipo_agente": "File"}
  ],
  "agentes": [
    {
      "nombre": "GenerarContenido",
      "tipo": "Python",
      "estado": "Completado",
      "ok": true,
      "error": "",
      "duracion": 0.03,
      "resultado": {"contenido": "hola"}
    },
    {
      "nombre": "EscribirFichero",
      "tipo": "File",
      "estado": "Completado",
      "ok": true,
      "error": "",
      "duracion": 0.0,
      "resultado": {
        "archivo": "hola.txt",
        "caracteres_escritos": 4,
        "contenido_preview": "hola"
      }
    }
  ],
  "resultado": "{\n  \"archivo\": \"hola.txt\",\n  \"caracteres_escritos\": 4,\n  \"contenido_preview\": \"hola\"\n}",
  "duracion": 0.04,
  "timeout_agotado": false,
  "ejecucion_id": null,
  "advertencias": []
}
```

| Campo | Significado |
|---|---|
| `ok` | `true` si **todos** los agentes terminaron `Completado` |
| `resultado` | Texto del último agente correcto (si su resultado es un dict de una sola clave de texto, se usa ese texto) |
| `duracion` | Segundos de **ejecución** de agentes (no incluye generar el plan) |
| `timeout_agotado` | `true` si venció `--timeout` y se detuvo la ejecución |
| `ejecucion_id` | ID en la BD de aprendizaje; `null` con `--no-aprender` |
| `advertencias` | Avisos del validador/planificador |

### 5.4 Persistencia y efectos secundarios

- Por defecto, `run` **guarda la ejecución en aprendizaje** (`agent_history.db`)
  y activa el Plan B, igual que la GUI. Con `--no-aprender` (`"aprender": false`)
  no se toca la base de datos.
- Las rutas son **relativas al directorio actual**: `agent_history.db`,
  `logs/` y los ficheros que generen los agentes se crean en el `cwd`.
  Ejecuta desde la raíz del proyecto salvo que quieras aislar la ejecución.
- Ctrl-C detiene la ejecución y sale con `130`.

---

## 6. `list-agents` — catálogo de agentes

```
python main.py list-agents [--json] [--quiet]
```

Lee el catálogo real de `core.agent` (`TipoAgente` + `TIPOS_AGENTES_CONFIG`).

```bash
$ python main.py list-agents

Archivos:
  File       Operaciones con archivos del sistema

Búsqueda:
  Search

Estructura:
  Loop       Itera sobre una lista de items

IA:
  LLM        Llama a modelos de lenguaje (DeepSeek)

Programación:
  Python     Ejecuta código Python en un sandbox aislado
...
```

```bash
$ python main.py list-agents --json | python -m json.tool
{
  "ok": true,
  "agentes": [
    {
      "nombre": "File",
      "categoria": "Archivos",
      "descripcion": "Operaciones con archivos del sistema",
      "campos_requeridos": ["operacion_file"]
    },
    {
      "nombre": "LLM",
      "categoria": "IA",
      "descripcion": "Llama a modelos de lenguaje (DeepSeek)",
      "campos_requeridos": ["prompt_llm", "modelo_llm"]
    },
    {
      "nombre": "Search",
      "categoria": "Búsqueda",
      "descripcion": "",
      "campos_requeridos": []
    }
  ]
}
```

Cada entrada del catálogo tiene **siempre** los cuatro campos:

| Campo | Tipo | Descripción |
|---|---|---|
| `nombre` | string | Nombre del tipo de agente (`File`, `LLM`, `Python`…) |
| `categoria` | string | Categoría para agrupar en la UI (`Archivos`, `IA`…) |
| `descripcion` | string | Descripción del tipo (puede ser `""`) |
| `campos_requeridos` | array de strings | Campos obligatorios de configuración; `[]` si no declara ninguno |

`campos_requeridos` se calcula con `obtener_config_tipo()` para cada
`TipoAgente`; es el mismo formato que sirve `GET /api/agents` en el entorno
web (§7.bis).

Salida: `0` si hay catálogo; `4` si no se puede cargar o está vacío (antes
devolvía una lista vacía con `ok: true` y exit 0).

> `Browser` y `Search` aparecen sin descripción porque no están en
> `TIPOS_AGENTES_CONFIG` (`core/agent.py`): el catálogo refleja lo que hay,
> no una lista inventada.

---

## 7. `serve` — servidor HTTP

```
python main.py serve [--host HOST] [--port PUERTO] [--quiet]
```

| Opción | Por defecto |
|---|---|
| `--host` | `127.0.0.1` |
| `--port` | `8765` |
| `--quiet`, `-q` | *(desactivado)* — solo errores por stderr |

El servidor HTTP corre en un hilo y las ejecuciones se despachan al hilo
principal (el único con bucle de eventos Qt, que es lo que necesita el
`Scheduler`). Los trabajos se **serializan**: una petición se ejecuta
después de la anterior.

### Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/health` | Estado del servidor |
| `POST` | `/run` | Ejecuta una tarea (mismo pipeline que `run`) |

`POST /run` acepta el mismo JSON de tarea que `--file`/`--stdin` (§5.1) y
devuelve el mismo objeto que `run --json` (§5.3).

### Códigos HTTP

| Código | Cuándo |
|---|---|
| `200` | Ejecución terminada correctamente (`ok: true`) |
| `400` | Cuerpo no es JSON, no es un objeto, o falta `prompt` |
| `404` | Ruta desconocida |
| `500` | La ejecución falló (`ok: false`) o excepción |
| `504` | Se agotó el `--timeout` global esperando el resultado |

### Ejemplo

```bash
python main.py serve --port 8765
# 🌐 Sirviendo en http://127.0.0.1:8765/run (Ctrl-C para parar)
```

En otra terminal:

```bash
# Salud
curl -s http://127.0.0.1:8765/health
# {"ok": true, "version": "3.3.0"}

# Ejecutar una tarea
curl -s -X POST http://127.0.0.1:8765/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"Genera un fichero nota.txt con OK","max_pasos":3,"aprender":false}' \
  | python -m json.tool

# Error de entrada
curl -s -X POST http://127.0.0.1:8765/run -d 'no soy json'
# {"ok": false, "error": "JSON inválido: ..."}
```

Ctrl-C detiene el servidor (sale con `130`).

**Nota sobre `--timeout` en `serve`**: el `--timeout` global limita cuánto
espera el cliente HTTP por el trabajo; el `timeout` del cuerpo JSON limita
la ejecución en sí. Si no se indica ninguno, la petición espera sin límite.

**Qué significa un `504` (y qué no)**: el `504` lo devuelve el servidor
cuando se agota el `--timeout` **global** esperando el resultado; **no
cancela la ejecución interna**. El `Scheduler` sigue ejecutando el plan en
el hilo principal hasta el final (o hasta el `timeout` del cuerpo JSON) y,
si `aprender` está activo, la ejecución se registra igualmente en
`agent_history.db` con su `ejecucion_id`. No hay endpoint de cancelación:
para cortar una ejecución en curso, detén el servidor o usa el `timeout`
del cuerpo JSON. Sube `--timeout` si tus tareas tardan más que ese valor.

---

## 7.bis. `web` — servidor Flask

```
python main.py web [--host HOST] [--port PUERTO] [--timeout SEGUNDOS] [--quiet]
```

| Opción | Por defecto |
|---|---|
| `--host` | `127.0.0.1` |
| `--port` | `5000` |
| `--timeout` | *(sin límite)* — espera máxima del cliente HTTP |
| `--quiet`, `-q` | *(desactivado)* — solo errores por stderr |

Arranca el entorno web Flask. **Reutiliza el mismo pipeline** que `run` y
`serve`: `_ejecutar_pipeline()` para ejecutar, `_listar_agentes()` para el
catálogo, `_asegurar_qt()` para la `QCoreApplication`, `main.__version__`
para `/api/health` y `obtener_llm_client_compartido()` (singleton) para el
LLM. No hay un segundo pipeline ni un `LLMClient()` nuevo por petición.

La integración es **Qt-safe**: Flask (werkzeug) atiende en un hilo
secundario y solo *encola* los trabajos en una `queue.Queue` compartida;
el hilo principal de Qt los consume con `QTimer.singleShot(25, …)` porque
el `Scheduler` es un `QObject` y necesita el bucle de eventos del hilo
principal. Cada trabajo lleva su propio `threading.Event` para que el
request HTTP espere el resultado sin bloquear a Qt, y las conexiones de
señales de este flujo usan `Qt.ConnectionType.QueuedConnection` (mismo
patrón que `simple_main_window.py` tras el fix del segfault por hilos).

### Endpoints

| Método | Ruta | Descripción |
|---|---|---|
| `GET` | `/` | Formulario HTML (prompt, `max_pasos`, `timeout` de la tarea, `agent`, `aprender`) |
| `POST` | `/api/run` | Ejecuta el pipeline; devuelve el mismo JSON que `run --json` (§5.3) |
| `GET` | `/api/health` | `{ok, version}` |
| `GET` | `/api/agents` | Catálogo de agentes (mismo formato que `list-agents --json`, §6) |

`POST /api/run` acepta el mismo JSON de tarea que `--file`/`--stdin`
(§5.1).

### Códigos HTTP

| Código | Cuándo |
|---|---|
| `200` | Ejecución terminada con `ok: true` |
| `400` | El cuerpo no es un objeto JSON o falta `prompt` |
| `404` | Ruta desconocida |
| `500` | La ejecución falló (`ok: false`) o hubo una excepción |
| `504` | Se agotó el `--timeout` global esperando el resultado (no cancela la ejecución interna, ver §7) |

### Ejemplo

```bash
python main.py web --port 5000
# 🌐 Web en http://127.0.0.1:5000/ (Ctrl-C para parar)
```

Abre el formulario en el navegador o usa la API:

```bash
# Salud
curl -s http://127.0.0.1:5000/api/health | jq
# {"ok": true, "version": "3.3.0"}

# Catálogo de agentes (mismo formato que list-agents --json)
curl -s http://127.0.0.1:5000/api/agents | jq '.agentes | length'
# 8

# Ejecutar una tarea
curl -s -X POST http://127.0.0.1:5000/api/run \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"di hola","aprender":false}' | jq .ok
# true

# Error de entrada
curl -s -X POST http://127.0.0.1:5000/api/run -d 'no soy json'
# {"ok": false, "error": "el cuerpo debe ser un objeto JSON"}
```

Ctrl-C detiene el servidor (sale con `130`).

Si Flask no está instalado, `web` **no arranca**: sale con `3`
(`EXIT_ENV_ERROR`) e imprime `pip install flask`. Desde v3.3.0 Flask entra
en `requirements.txt` (`flask>=3.0`).

---

## 8. `--check-env` y `--version`

```bash
python main.py --check-env                 # ping con timeout 5 s
python main.py --check-env --timeout 15    # red lenta
python main.py --version                   # agentes-visuales 3.3.0
```

Salida típica de `--check-env`:

```
▸ API key de DeepSeek
  ✅ API key encontrada: sk-54c46…b6dc
▸ Ping a api.deepseek.com (timeout 5s)
  ✅ Respuesta 200 en 487 ms
RESULTADO: ✅ Todo OK
```

Si falla por red, reintenta o sube el timeout: es el único punto de la CLI
que depende de la latencia de la red.

---

## 9. Recetas

**Encadenar con `jq`**

```bash
python main.py run --prompt "..." --json --quiet \
  | jq -r '.resultado'
```

**Solo el estado, sin el contenido**

```bash
python main.py run --prompt "..." --json --quiet \
  | jq '{ok, duracion, agentes: [.agentes[] | {nombre, estado}]}'
```

**Ejecutar en segundo plano y recoger el resultado**

```bash
python main.py run --prompt "..." --json --quiet -o /tmp/salida.json &
wait
```

**CI / sin efectos en la BD**

```bash
python main.py run --prompt "..." --no-aprender --json --quiet --timeout 600
```

**Servidor en segundo plano**

```bash
nohup python main.py serve --host 127.0.0.1 --port 8765 > logs/serve.log 2>&1 &
```

---

## 10. Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| `❌ LLM no disponible: configura DEEPSEEK_API_KEY` | Falta la clave o `llm.disponible` es `False`. Ejecuta `--check-env` |
| `RESULTADO: ❌ Error de red` en `--check-env` | Red lenta o caída. Reintenta o `--timeout 15` |
| `❌ la ejecución no pudo arrancar (revisa los logs)` | El plan no pasó la validación previa (ciclos o LOOP inválido). Mira `logs/agentes_visuales.log` |
| `❌ el plan no tiene ningún agente llamado 'X'` | `--agent` no coincide con ningún nombre del plan generado |
| `QEventLoop: Cannot be used without QCoreApplication` | No debería ocurrir; si lo ves, es un bug de la retención de la app Qt. Repórtalo |
| La ejecución se queda colgada | No hay `--timeout`; pásalo, o Ctrl-C (sale con `130`) |
| `❌ No se pudo abrir 127.0.0.1:8765` | Puerto ocupado: usa `--port` |
| Los agentes escriben ficheros en un sitio raro | Las rutas son relativas al `cwd`; ejecuta desde la raíz del proyecto |
| Ruido `weasyprint`/`INFO` en stderr | Añade `--quiet` (disponible en `run`, `list-agents`, `serve` y `web`) |
| `❌ Flask no está instalado` al usar `web` (exit `3`) | El subcomando `web` necesita Flask: `pip install flask` (está en `requirements.txt` desde v3.3.0) |
| `POST /api/run` devuelve `504` | Se agotó el `--timeout` global del servidor. Sube `--timeout` al arrancar `web`: **el 504 no cancela la ejecución**, que sigue y se registra en `agent_history.db` (ver §7) |
| `⚠️ El LLM no devolvió JSON válido en la parte 1/1` | Era el bug B1: el preámbulo anti-alucinación del `ProblemSolver` menciona JSON aunque la tarea sea de texto plano, y el ejecutor exigía JSON. Corregido en v3.3.0 (`tarea_pide_json()`); si reaparece con un prompt propio, pide el JSON explícitamente en la `TAREA:` |

---

## 11. Relación con la GUI

| | GUI | `run` / `serve` | `web` |
|---|---|---|---|
| Plan + ejecución | Sí | Sí (mismo `ProblemSolver` + `Scheduler`) | Sí (mismo pipeline) |
| Interfaz | Qt Widgets | Ninguna (solo `QtCore`) | HTML en el navegador |
| Aprendizaje / Plan B | Sí | Sí, salvo `--no-aprender` | Sí, salvo `"aprender": false` |
| Interrupción | Botón detener | Ctrl-C | Ctrl-C |
| Feedback interactivo | Sí | No | Formulario HTML |
| Apto para CI/servidor | No | Sí | Sí |

`serve` es, en la práctica, `run` expuesto por HTTP. `web` añade el
formulario HTML y los endpoints `/api/health` y `/api/agents` reutilizando
el mismo pipeline (§7.bis).
