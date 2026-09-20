# VLM local — arquitectura y plan (H12)

> Esta fase **prepara la arquitectura**. No se entrena ningún modelo desde
> cero ni dentro del núcleo de la aplicación, tal como exige el encargo.

## Objetivo

Poder apuntar `agentes_visuales` a un servidor de inferencia **local**
(compatible con la API de OpenAI) sin tocar el código de agentes ni el
scheduler, y separar con claridad cuatro responsabilidades que no deben
mezclarse.

## Separación de responsabilidades

| Capa | Qué es | Dónde vive |
|------|--------|------------|
| **Inferencia** | Servidor que sirve el modelo (vLLM, llama.cpp, Ollama con API OpenAI, TGI) | Fuera de la app (proceso aparte) |
| **Runtime** | Cliente que consume ese servidor | `core/llm_client.py` vía `DEEPSEEK_BASE_URL` |
| **Dataset** | Capturas + etiquetas para evaluar/afinar | Herramientas en `tools/` (no en el núcleo) |
| **Fine-tuning** | Entrenamiento (LoRA, etc.) en GPU | Fuera del repo del núcleo o en `tools/` |

## Cómo se conecta hoy

`LLMClient` ya resuelve, en orden:

1. argumento explícito,
2. variable de entorno (`DEEPSEEK_BASE_URL`, `DEEPSEEK_API_KEY`,
   `DEEPSEEK_MODEL`),
3. archivo `~/.config/agentes_visuales/env` (ver `core/ia_config.py`),
4. valor por defecto (`https://api.deepseek.com`).

Por tanto, **no hace falta código nuevo** para apuntar a un servidor local:
basta con `DEEPSEEK_BASE_URL=http://localhost:8000/v1` (y `DEEPSEEK_MODEL` si
el servidor expone un nombre distinto). El cliente multimodal
(`LLMClient.completar_multimodal`, H11) usa el mismo `base_url`, así que un
servidor local con un VLM compatible también sirve para la visión de página.

## Herramienta de comprobación

```bash
python tools/comprobar_servidor_local.py --base-url http://localhost:8000/v1
```

Hace un `GET {base_url}/v1/models` autenticado y explica cómo apuntar la app.
No descarga modelos ni entrena.

## Qué NO se implementa aquí

- Entrenamiento de un VLM (requiere GPU, dataset y tiempo; el encargo pide
  explícitamente no meterlo en el núcleo).
- Captura de pantalla del escritorio y control de ratón/teclado (H11
  escritorio): solo se contempla la **visión de página** con Playwright, y
  desactivada por defecto (`AGENTES_VISION_HABILITADA`).
- Cualquier dependencia nueva pesada.

## Estado

- [x] `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` configurables (H1).
- [x] Cliente multimodal compatible con OpenAI (H11 fase 1).
- [x] Herramienta de comprobación de servidor local.
- [ ] Dataset de capturas etiquetadas (fase futura, en `tools/`).
- [ ] Fine-tuning LoRA y runtime local del VLM (fase futura, fuera del núcleo).
