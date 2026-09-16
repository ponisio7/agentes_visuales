## Decisión de diseño: matching por embeddings (16-sep-2026)

### Contexto
El A/B testing necesitaba encontrar "la misma tarea" entre ejecuciones
para comparar versiones del prompt. La firma exacta (1-2 palabras
significativas) tenía 0 usos reales porque el LLM varía el texto entre
ejecuciones.

### Decisión
**El matching semántico por embeddings es el mecanismo oficial.**
Implementado en `EmbeddingMatcher.buscar_match()`:
- Calcula embedding del prompt crudo (384 dims, paraphrase-multilingual-MiniLM-L12-v2)
- Busca la reescritura más similar con similitud coseno
- Umbral por defecto: 0.68

### Rol de la firma exacta (`_firmar`)
**NO se usa para matching.** Se usa solo para:
- Agrupar versiones al insertar una nueva reescritura (desactivar
  las anteriores de la "misma tarea")
- Metadato de trazabilidad en `prompts_reescritos.firma`

### Lo que queda legacy
- `PromptABEvaluator.consultar_versiones` sigue existiendo pero ya
  no se llama desde el builder. Se puede eliminar en una limpieza futura.
- La firma con 2 palabras (`cd360c6`) quedó en 1 palabra, pero da igual:
  el matching real va por embeddings.

### Verificación en producción
Ejecución real del 16-sep-2026:
- `🎯 Match encontrado: id=24 (similitud=0.815, estado=candidato)`
- `✨ AB: 'GenerarCuento' usa id=24`

### Cómo verificar que funciona
```bash
sqlite3 agent_history.db "
SELECT id, length(embedding), embedding_model, estado
FROM prompts_reescritos
WHERE embedding IS NOT NULL
ORDER BY id DESC LIMIT 5;
"
```
