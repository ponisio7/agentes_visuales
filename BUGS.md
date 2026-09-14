# Registro de bugs

## Bug #1 — El agente `ObtenerImagenes` no produce imágenes reales

- **Detectado**: 2026-09-14 05:02 (ejecución ID 301)
- **Severidad**: 🟠 Robustez / 🔴 Correctitud (por confirmar)
- **Síntoma**: 
  - El problema pedía "cuento con imágenes".
  - El plan incluyó un agente `ObtenerImagenes` (Loop, 3 items).
  - El DOCX generado pesa 2961 bytes (solo texto).
  - El Loop tardó 0.21s → imposible si descargara imágenes reales.
  - `GuardarDOCX` tardó 0.00s → sin imágenes que empaquetar.
- **Reproducción**: 
  1. `python main.py`
  2. Pedir: "haz un cuento y guardalo como cuento.docx y si puedes colocarle imagenes es un plus"
  3. Inspeccionar `cuento.docx` con `zipfile` → no hay `word/media/`.
- **Traceback**: no hay error explícito. Falla silenciosa.
- **Hipótesis**: el Loop genera URLs/placeholders pero nadie las descarga ni las inserta.
- **Estado**: en análisis
- **Pendiente**: 
  - [ ] Inspeccionar el DOCX con zipfile.
  - [ ] Ver el `resultado` de `ObtenerImagenes` en la BD.
  - [ ] Ver el código de `ConstruirDocumento` y `GuardarDOCX`.
  - [ ] Confirmar hipótesis.

## Bug #2 — Log ruidoso de `httpx2`

- **Detectado**: 2026-09-14 05:01
- **Severidad**: 🟡 UX
- **Síntoma**: Cada petición HTTP escribe una línea `httpx2: HTTP Request: POST ...`.
- **Acción**: añadir `logging.getLogger("httpx2").setLevel(logging.WARNING)` en `main.py`.
- **Estado**: abierto

Sobre el bug del [07:23:40] ⏹ Ejecución detenida y 🗑 Todo limpiado

Sigue apareciendo al inicio de cada ejecución. Es el scheduler.limpiar() que se llama antes de arrancar. Ahora es cosmético, pero si algún día quieres arreglarlo, es una línea en simple_main_window._on_ejecutar. Lo dejo anotado como bug #3 pendiente.
## Sesión 2026-09-14: Soporte de imágenes en DOCX

### Bugs arreglados
- `cuento.docx` no era DOCX válido (faltaba dispatch por extensión). Arreglado.
- `content_extractor` perdía `cuento` al extraer. Arreglado con `_CLAVES_ESTRUCTURA_DOCUMENTO`.
- `main.py` con `basicConfig(...)` roto. Arreglado.
- `reparaciones_plan` no se creaba. Arreglado.
- `lessons.py` con `extraer()` duplicado. Arreglado.

### Features nuevas
- Post-procesado C+E en `problem_solver`: regenerar → parchear.
- `core/plan_repairs.py` con constantes nombradas.
- `LearningEngine.registrar_reparacion_plan()`.
- `lessons.py` con `_lecciones_por_reparaciones()`.

### Bugs pendientes (menores)
- Duración de agentes siempre 5.0.
- Log ruidoso de `httpx2`.
- UX "⏹ Ejecución detenida".
- `Database.close()` se llama dos veces.

## Bug #2 — httpx2 ruidoso

- **Estado**: ✅ Arreglado
- **Fecha**: 2026-09-14
- **Fix**: `logging.getLogger("httpx2").setLevel(logging.WARNING)` en `main.py::_configurar_logging`
- **Verificado**: en la ejecución 305 no aparece ninguna línea `httpx2: HTTP Request`
