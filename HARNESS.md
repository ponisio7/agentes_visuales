# HARNESS.md — Reglas de trabajo para el agente Harness

## Rama de trabajo
- Trabaja **solo** en `harness/v3.7.0`.
- Nunca hagas push directo a `release/v3.6.0` ni a `main`.
- Commits atómicos, mensajes en el estilo del repo (`tipo(scope): resumen`).

## Fuente de verdad
- `dudas_sobre_el_programa.md` §13 define el plan (H1–H12).
- Orden recomendado: H1 → H2 → H3 → H4 → H5 → H6 → H10 → H7 → H8 → H9 → H11 → H12.
- **H6 es la prioridad si hay que elegir una sola.**
- No implementes lo que el doc marca como "explicación del comportamiento
  actual" (§1, §2, §8): no son tareas.

## Qué puedes hacer
- Crear, modificar y borrar archivos dentro del repo.
- Añadir tests, migraciones de BD, endpoints, módulos nuevos.
- Refactorizar lo necesario para cumplir cada tarea.

## Qué NO puedes hacer
- **No** commitear secretos, API keys, `agent_history.db`, `learning_models/`,
  `logs/` ni `outputs/`.
- **No** modificar `LICENSE`, `README.md` (excepto si la tarea lo pide) ni
  `.gitignore` sin justificarlo.
- **No** introducir dependencias nuevas sin declararlas en `requirements.txt`.
- **No** romper los contratos públicos: `POST /run`, `POST /api/run`, CLI
  `run/serve/web`, esquema JSON de respuesta.
- **No** tocar `release/v3.6.0` ni tags.
- **No** hacer `git push --force`, `git rebase` sobre commits publicados, ni
  `git reset --hard` sobre la rama remota.
- **No** usar `git add -f` para saltarte `.gitignore` sin justificarlo en el
  commit.

## Criterios de calidad
- Cada tarea (H1, H2, ...) debe incluir sus tests.
- `pytest` debe pasar en local antes de commitear.
- Si una tarea toca el scheduler o el sandbox, documenta el cambio en el
  mensaje del commit con el porqué.
- Si algo no se puede completar, para y deja una nota en `HARNESS_STATUS.md`.

## Estado
Harness debe mantener `HARNESS_STATUS.md` actualizado con:
- Tarea en curso.
- Tareas completadas (con hash de commit).
- Tareas bloqueadas (con motivo).