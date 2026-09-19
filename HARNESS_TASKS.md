# Tareas para el Harness: Cleanup de hardcodeo específico de HTML/CSS/JS

## Contexto

El proyecto `agentes_visuales` es un pipeline que usa LLM para descomponer
problemas en planes de agentes. Originalmente era genérico. Con el tiempo,
se le añadieron reglas y validaciones específicas para que "calculadoras
HTML" y "cuentos con imágenes" funcionaran. Eso ha generado deuda técnica.

El tag `v2.1` captura el estado con hardcodeo. Queremos un `v2.2` sin él.

## Objetivo

Eliminar el hardcodeo específico de HTML/CSS/JS del pipeline, dejando que el
LLM planificador decida la topología y los contratos de cada paso. Mantener
únicamente los fixes genéricos.

## Tareas concretas

### Tarea 1 — Limpiar `core/problem_solver/prompt_builder.py`

- Eliminar `DOCX_IMAGE_RULES` (reglas específicas de cuentos con imágenes).
- Eliminar `BINARY_FORMAT_RULES` (reglas específicas de PDF/DOCX/XLSX).
- En `DESIGN_RULES`, eliminar:
  - "REGLA ESTRICTA PARA PROBLEMAS QUE GENEREN HTML+CSS+JS"
  - "COHERENCIA OBLIGATORIA ENTRE HTML, CSS Y JS"
- En `PYTHON_RULES`, eliminar:
  - "GENERACIÓN DE HTML: el agente final DEBE devolver `resultado = {'html': '...'}`"
  - "GENERACIÓN DE MARKDOWN: ..."
- **MANTENER**:
  - Todas las reglas de indentación Python.
  - `AGENT_TYPES`, `CONTRATOS_SALIDA`, `TYPE_CONFIGS`, `SHELL_RULES`, `FILE_RULES`.
  - Reglas genéricas de diseño (atomicidad, DAG, sin repeticiones).
  - Reglas genéricas de Python (contrato de salida, acceso a `contexto`).

### Tarea 2 — Limpiar `core/problem_solver/builder.py`

- Eliminar `CONTRATOS_SALIDA_POR_PASO`.
- Eliminar `_inyectar_contrato_salida`.
- Eliminar la llamada a `_inyectar_contrato_salida` en `_kwargs_llm`.
- **MANTENER** el resto de la lógica de `_kwargs_llm` (modelo, temperatura, etc.).

### Tarea 3 — Limpiar `core/problem_solver/solver.py`

- Eliminar `_PATRON_ENSAMBLAR_HTML` y `_CODIGO_ENSAMBLAR_HTML`.
- Eliminar `_PATRON_VALIDAR_COHERENCIA` y `_CODIGO_VALIDAR_COHERENCIA`.
- Simplificar `_postprocesar_plan` a solo una validación genérica de HTML
  (o eliminarlo entero si no aporta valor).
- **MANTENER** el planificador `deepseek-v4-pro` (DEFAULT_MODELS).
- **MANTENER** el uso de `deepseek-v4-pro` en `refinar_plan`.

### Tarea 4 — Eliminar `core/utils/ensamblar_html.py`

- `git rm core/utils/ensamblar_html.py`
- Editar `core/utils/__init__.py` para quitar los imports de ese módulo.

### Tarea 5 — Verificar que `file_executor.py` mantiene los fixes genéricos

- **MANTENER** `_desenvolver_contenido_web` (Fix 3).
- **MANTENER** el bloque Fix 5B (desenvolver strings JSON).
- **MANTENER** el bloque Fix 5A (auto-corregir `None`/`True`/`False` en JS).
- **NO TOCAR** lo genérico.

### Tarea 6 — Verificación

1. Ejecutar la suite de tests:
   ```bash
   python -m pytest tests/ -v