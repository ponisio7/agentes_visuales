# Contratos de salida entre pasos

Este documento define la **forma exacta** del JSON que cada paso
productor debe devolver y que cada paso consumidor espera recibir.

Regla: si un paso LLM alimenta a otro paso, su prompt DEBE incluir el
bloque `CONTRATO DE SALIDA OBLIGATORIO` con la forma exacta que aparece
aquí.

---

## Pipeline: cuento → imágenes → HTML

### `GenerarCuento` (productor)

**Consumidor:** `GenerarURLs`

**Forma exacta:**
```json
{
  "cuento": "texto completo del cuento, en prosa, sin markdown",
  "descripciones_imagenes": [
    "descripción de la imagen 1, en inglés, apta para un generador de imágenes",
    "descripción de la imagen 2",
    "..."
  ]
}