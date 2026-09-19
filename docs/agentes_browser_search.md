# Agentes Browser y Search

Dos tipos de agente para obtener **datos reales de la web** cuando no existe
una API JSON:

- **Browser**: navega una URL real con un navegador (Playwright/Chromium) y
  ejecuta acciones declarativas (esperar, extraer, click, rellenar, scroll,
  screenshot, ejecutar_js, navegar).
- **Search**: busca información en la web (DuckDuckGo, sin API key) y devuelve
  una lista de resultados con título, URL y snippet.

Ambos son **genéricos**: no conocen ningún dominio ni problema concreto.

---

## 1. Instalación

```bash
pip install -r requirements.txt          # incluye playwright y duckduckgo-search
python -m playwright install chromium    # descarga el navegador (~150 MB)
```

En Linux, si faltan librerías del sistema para Chromium:

```bash
python -m playwright install-deps chromium   # requiere privilegios
```

Notas:

- `duckduckgo-search` fue renombrado a `ddgs`. El executor importa `ddgs` si
  está instalado y, si no, `duckduckgo-search`; ambos funcionan.
- Si Chromium no está instalado, el agente Browser devuelve
  `error="browser_unavailable"` (no rompe el pipeline).
- Los navegadores se descargan fuera del repo. Para instalarlos en otra ruta:
  `PLAYWRIGHT_BROWSERS_PATH=/ruta python -m playwright install chromium`.

---

## 2. Browser

### 2.1 Campos de `configuracion`

| Campo | Tipo | Por defecto | Descripción |
|---|---|---|---|
| `url` | str | — | URL a navegar (obligatoria). Sin esquema se asume `https://`. |
| `acciones` | list | `[]` | Lista de acciones declarativas (ver abajo). |
| `timeout` | int (s) | `30` | Timeout global del paso y de la carga inicial. |
| `timeout_accion` | int (ms) | `10000` | Timeout por acción (si la acción no define el suyo). |
| `headless` | bool | `true` | Navegador sin ventana. `false` para verlo. |
| `bloquear_recursos` | bool | `false` | No descargar imágenes, fuentes, CSS ni media (más rápido). |
| `user_agent` | str | realista | User-agent a usar. Vacío = uno realista por defecto. |

### 2.2 Acciones

Cada acción es un objeto con `tipo` y sus campos. Todas aceptan `timeout`
en milisegundos (opcional).

| `tipo` | Campos | Qué hace |
|---|---|---|
| `esperar` | `selector`, `estado` (`visible`/`attached`), `milisegundos` | Espera a que aparezca un selector (o una espera temporal si no hay selector). |
| `extraer` | `selector`, `formato` (`html`/`text`/`attr`/`texto_principal`), `nombre`, `atributo`, `multiple` | Extrae contenido y lo guarda en `datos_extraidos[nombre]`. Con `multiple: true` devuelve una lista. |
| `click` | `selector` | Pulsa un elemento. |
| `rellenar` | `selector`, `valor` | Escribe en un campo. |
| `scroll` | `hasta` (`bottom`/`top`/selector CSS), `pixeles` | Desplaza la página. |
| `screenshot` | `nombre`, `selector`, `full_page` | Guarda una captura en `outputs/screenshots/`. |
| `ejecutar_js` | `script`, `nombre` | Ejecuta JavaScript y guarda el resultado si hay `nombre`. |
| `navegar` | `url`, `esperar_hasta` (`load`/`domcontentloaded`/`networkidle`) | Navega a otra URL a mitad del flujo. |

Un error en una acción **no aborta** el resto: se registra en
`acciones_ejecutadas` con `ok: false` y `error`.

#### Extracción de texto principal

`formato: texto_principal` devuelve el contenido útil de la página (el
contenedor con más texto) **descartando** navegación, cabeceras, pies,
formularios y bloques de cookies/consentimiento/menús/sidebars. Es lo
recomendado para artículos y páginas de contenido: con un `selector: "p"`
a pelo suelen salir antes los avisos de cookies que el propio contenido.

- Acepta `selector` para acotar la búsqueda a un contenedor.
- Si una extracción normal (`html`/`text`/`attr`) falla o devuelve vacío,
  el executor **cae automáticamente** a `texto_principal` y lo anota en la
  acción (`fallback: texto_principal`).
- Tras cargar la página se espera (hasta 5 s, o hasta que el texto deje de
  crecer) a que haya contenido visible, para webs que pintan con JS.

### 2.3 Ejemplo genérico

```yaml
tipo: Browser
configuracion:
  url: "https://ejemplo.com/pagina-con-tabla"
  acciones:
    - tipo: esperar
      selector: "table.datos"
      timeout: 15000
    - tipo: extraer
      selector: "table.datos"
      formato: html
      nombre: tabla
    - tipo: extraer
      selector: "article, main"
      formato: texto_principal
      nombre: contenido
    - tipo: scroll
      hasta: bottom
    - tipo: screenshot
      nombre: captura.png
  timeout: 30
  headless: true
```

### 2.4 Navegar varias URLs en un solo paso

En lugar de un `Loop` (que no puede invocar Browser) se usa `urls_desde`:

```yaml
tipo: Browser
dependencias: ["Buscar"]
configuracion:
  urls_desde: "Buscar.resultados"   # lista de URLs (strings o dicts con url/href/link)
  max_urls: 5
  acciones_por_url:
    - tipo: extraer
      selector: "article, main"
      formato: texto_principal
      nombre: contenido
  timeout: 30
```

Si `urls_desde` está presente, `url` no hace falta y el resultado es
agregado (ver 2.5). Respeta la cancelación entre URLs y cierra el
navegador siempre.

### 2.5 Contrato de salida

**Una URL** (`url`):

```python
{
    "url_final": str,            # URL tras redirecciones y acciones
    "titulo": str,               # <title> de la página
    "html": str,                 # HTML final (recortado a 2 MB)
    "texto": str,                # texto visible del body
    "datos_extraidos": dict,     # {nombre: valor} de 'extraer'/'ejecutar_js'
    "acciones_ejecutadas": list, # [{"tipo", "ok", "error"|"detalle"}]
    "screenshots": list,         # rutas de las capturas
    "html_truncado": bool,
    "error": str | None,         # None si todo fue bien
    "duracion": float,           # segundos
}
```

**Varias URLs** (`urls_desde`):

```python
{
    "urls_navegadas": int,
    "resultados_por_url": [
        {"url": str, "titulo": str, "datos_extraidos": dict,
         "acciones_ejecutadas": list, "error": str | None},
    ],
    "errores": [{"url": str, "error": str}],
    "screenshots": list,
    "extraccion_vacia": bool,    # True si NINGUNA URL devolvió datos
    "error": str | None,         # None, 'urls_desde_not_found', 'all_urls_failed', 'cancelled'
    "duracion": float,
}
```

`extraccion_vacia: true` (con su aviso en el log y en el resumen del paso)
significa que la navegación fue bien pero los selectores no encontraron
contenido: hay que revisarlos o usar `texto_principal`.

---

## 3. Search

### 3.1 Campos de `configuracion`

| Campo | Tipo | Por defecto | Descripción |
|---|---|---|---|
| `query` | str | — | Consulta (obligatoria). |
| `max_resultados` | int | `5` | Número de resultados (máx. 50). |
| `region` | str | `wt-wt` | Región de DuckDuckGo, p. ej. `es-es`, `us-en`, `wt-wt`. |
| `timeout` | int (s) | `30` | Timeout de la consulta. |

### 3.2 Ejemplo genérico

```yaml
tipo: Search
configuracion:
  query: "términos de búsqueda"
  max_resultados: 5
  region: "wt-wt"
```

### 3.3 Contrato de salida

```python
{
    "query": str,          # consulta ejecutada
    "resultados": [        # lista de resultados
        {"title": str, "href": str, "body": str},
    ],
    "total": int,
    "error": str | None,
    "duracion": float,
}
```

El executor intenta primero el backend automático de DuckDuckGo y, si no
devuelve resultados (algunas redes bloquean ciertos backends), reintenta con
el backend `html`. Todo interno y transparente.

---

## 4. Sustitución de variables

Tanto la URL y los valores de las acciones de Browser como la `query` de
Search aceptan referencias a dependencias con la sintaxis `{Agente.clave}`,
que se sustituyen antes de ejecutar (igual que en los agentes HTTP y Shell):

```yaml
tipo: Browser
dependencias: ["BuscarFuentes"]
configuracion:
  url: "{BuscarFuentes.href}"     # la primera URL que devolvió el Search
  acciones:
    - tipo: extraer
      selector: "h1"
      nombre: "{BuscarFuentes.title}"
```

---

## 5. Comportamiento ante errores y cancelación

- Ambos respetan `cancellation_token`: lo comprueban antes de empezar, entre
  acciones y antes de devolver. El navegador se cierra **siempre** en un
  `finally`, también en error o cancelación.
- Ninguno lanza excepciones al pipeline: devuelven `ok=False` con el contrato
  completo y `error` informativo.
- El HTML final se recorta a 2 MB (`html_truncado=True`) para no saturar el
  contexto de los pasos siguientes.

---

## 6. Cuándo usar cada uno

| Necesitas… | Usa |
|---|---|
| Datos de una **API REST JSON** | `HTTP` |
| **Descubrir** URLs o datos actuales que no conoces | `Search` |
| Datos de una **página HTML**, o interactuar (click, formularios, scroll) | `Browser` |
| Convertir HTML/texto en datos estructurados | `LLM` |
| Escribir el resultado (CSV, JSON, MD…) | `File` |

Patrón típico: `Search` → `Browser` → `LLM` (estructurar) → `Python`
(normalizar) → `File` (guardar).
