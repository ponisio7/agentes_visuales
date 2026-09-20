# agentes_visuales

Orquestador de agentes que **planifica, ejecuta y valida** tareas compuestas
usando un LLM como planificador. Cada tarea se descompone en agentes
especializados (Python, Shell, LLM, HTTP, File, Loop, Browser, Search) que se
ejecutan en un sandbox con contratos de runtime, y el resultado se verifica
contra invariantes declarados.

## Características

- **Planificación con LLM**: el problema se descompone en un plan de agentes
  con contratos de entrada/salida explícitos.
- **Ejecución en sandbox** con contrato de `dependencia()` y `preparar_imagen()`.
- **Validación de artefactos**: verificación MIME real, contratos de pasos y
  corrector AST.
- **Recovery / Plan B**: reintentos acotados que reciben el fallo como contexto.
- **Aprendizaje**: reescritura de prompts por feedback, evaluador LLM y
  recuperación de casos similares vía embeddings.
- **Multi-interfaz**: GUI (Qt), CLI headless, API HTTP (Flask) y servidor `serve`.

## Estado

- Rama activa: `release/v3.6.0`
- Último release: **v3.6.0** — fix `.docx` end-to-end + cliente CLI de tareas.

## Instalación rápida

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Configura la API key (una de estas vías):

```bash
export DEEPSEEK_API_KEY="tu_clave"
# o en ~/.config/agentes_visuales/env
```

## Uso

```bash
# GUI
python main.py

# CLI headless
python main.py run --prompt "..." --json

# Servidor HTTP
python main.py serve --host 127.0.0.1 --port 8765

# Web (Flask)
python main.py web
```

## Documentación

- `dudas_sobre_el_programa.md` — comportamiento verificado, propuestas y plan
  de implementación priorizado.
- `MANUAL_CLI.md` — uso de la CLI.
- `CHANGELOG.md` — historial de cambios.

## Conocido

- La suite completa puede dar `SIGSEGV` en CPython 3.13 (fork + hilos).
- `sentence-transformers` y `torch` no están declarados en `requirements.txt`.
- Ver `dudas_sobre_el_programa.md` §13 para el plan de estabilización.

## Licencia

Este proyecto se distribuye bajo la **GNU Affero General Public License v3.0
(AGPLv3)**. El texto completo está en [`LICENSE`](./LICENSE).

### Qué implica la AGPLv3 (en corto)

La AGPLv3 es una licencia de software libre con **copyleft fuerte**. En la
práctica, para quien use, modifique o despliegue este proyecto, significa:

- **Puedes usar, estudiar, modificar y redistribuir** el software, incluso con
  fines comerciales, **sin pagar nada**.
- **Si redistribuyes** el software o una versión modificada, **debes hacerlo
  bajo la misma licencia** y **ofrecer el código fuente** correspondiente.
- **Si ofreces el software como servicio en red** (por ejemplo, una API o una
  web accesible por terceros), **también debes ofrecer el código fuente** de la
  versión que estás ejecutando, incluidas tus modificaciones. Este es el punto
  que distingue a la AGPLv3 de la GPLv3.
- **No hay obligación de publicar** si solo ejecutas el software en privado,
  para ti, sin darlo a otros ni exponerlo como servicio.

### Aclaraciones

- **No hay coste de licencia.** La AGPLv3 es gratuita; no se paga nada por
  adoptarla ni por usarla.
- **Uso comercial permitido.** Cualquiera puede usar este software con fines
  comerciales, siempre que respete las obligaciones de copyleft anteriores.
- **Sin garantía.** El software se ofrece "tal cual", sin garantías de ningún
  tipo, según los términos de la licencia.
- **Contribuciones.** Salvo que se indique lo contrario, cualquier contribución
  enviada a este repositorio se entiende licenciada bajo la misma AGPLv3.
- **Dependencias de terceros.** Este proyecto usa librerías de terceros (Qt,
  Flask, Playwright, scikit-learn, entre otras) que mantienen **sus propias
  licencias**. La AGPLv3 aplica al código de este repositorio, no sustituye ni
  modifica las licencias de esas dependencias. Revísalas antes de redistribuir
  el conjunto.
- **Marcas y servicios externos.** El uso de APIs de terceros (por ejemplo,
  DeepSeek) está sujeto a sus propios términos y políticas, ajenos a esta
  licencia. 
- **Fecha de copyright.** 
  `Copyright (C) 2026 ponisio7`.