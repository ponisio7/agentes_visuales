# core/vision.py
"""Visión de página con modelo multimodal (H11, fase 1).

Flujo soportado:

    screenshot (Playwright) → modelo multimodal → decisión → acción Playwright

Esta capa NO ejecuta la acción: produce una **decisión validada** (un dict de
acción del vocabulario de ``browser_executor``) para que el agente Browser la
ejecute con sus mecanismos habituales. Así la visión no añade una vía de
ejecución nueva ni se salta el allowlist.

Guardrails:

* **Kill switch**: deshabilitado por defecto. Se activa con
  ``AGENTES_VISION_HABILITADA=1`` (o ``true``/``yes``/``on``).
* **Allowlist**: la acción decidida debe pertenecer al vocabulario permitido
  (``esperar, extraer, click, rellenar, scroll, screenshot, ejecutar_js,
  navegar``); cualquier otra se rechaza.
* **Sin escritorio**: esto es visión de PÁGINA. El control de ratón/teclado
  del escritorio no se implementa aquí.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Vocabulario permitido (espejo de browser_executor.ACCIONES_VALIDAS). Se
# mantiene local para no importar Playwright en contextos sin navegador.
ACCIONES_PERMITIDAS = (
    "esperar", "extraer", "click", "rellenar",
    "scroll", "screenshot", "ejecutar_js", "navegar",
)

MIME_POR_DEFECTO = "image/png"


def vision_habilitada() -> bool:
    """Kill switch de la visión (deshabilitada por defecto)."""
    return str(os.environ.get("AGENTES_VISION_HABILITADA", "")).strip().lower() in (
        "1", "true", "yes", "on", "si", "sí",
    )


def codificar_imagen(datos: bytes | str, mime: str = MIME_POR_DEFECTO) -> str:
    """Convierte una captura en un data URL base64 para el modelo multimodal."""
    if isinstance(datos, str):
        texto = datos
        if texto.startswith("data:"):
            return texto
        datos = texto.encode("utf-8")
    b64 = base64.b64encode(datos).decode("ascii")
    return f"data:{mime};base64,{b64}"


def construir_mensajes_multimodales(
    texto: str,
    imagenes: list[str],
    system_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Construye los mensajes con formato multimodal compatible con OpenAI.

    ``imagenes`` puede contener rutas ya codificadas como data URL o bytes.
    """
    contenido: list[dict[str, Any]] = [{"type": "text", "text": texto}]
    for imagen in imagenes:
        if isinstance(imagen, (bytes, bytearray)):
            url = codificar_imagen(bytes(imagen))
        elif isinstance(imagen, str) and imagen.startswith("data:"):
            url = imagen
        else:
            url = codificar_imagen(str(imagen))
        contenido.append({"type": "image_url", "image_url": {"url": url}})

    mensajes: list[dict[str, Any]] = []
    if system_prompt:
        mensajes.append({"role": "system", "content": system_prompt})
    mensajes.append({"role": "user", "content": contenido})
    return mensajes


_PROMPT_DECISION = """Eres un agente que opera una página web. Recibes una \
captura de pantalla y debes decidir la SIGUIENTE acción.

INSTRUCCIÓN DEL USUARIO:
{instruccion}

ESTADO ACTUAL (texto visible, puede estar vacío):
{contexto}

ACCIONES PERMITIDAS (usa EXACTAMENTE uno de estos 'tipo'):
{acciones}

Responde ÚNICAMENTE con un JSON con esta forma:
{{"razon": "por qué esta acción", "accion": {{"tipo": "click", "selector": "#boton"}}}}

Reglas:
- Si la página ya muestra lo pedido, responde {{"razon": "...", "accion": null}}.
- Usa selectores CSS estables (id, name, aria-label) y evita coordenadas.
- NO propongas acciones fuera de la lista permitida.
"""


def decidir_accion_desde_captura(
    llm: Any,
    captura: bytes | str,
    instruccion: str,
    *,
    contexto: str = "",
    acciones_permitidas: tuple[str, ...] = ACCIONES_PERMITIDAS,
    max_tokens: int = 800,
) -> dict[str, Any] | None:
    """Pide al modelo multimodal la siguiente acción a partir de una captura.

    Devuelve ``{"razon": str, "accion": dict | None}`` con la acción ya
    validada contra el allowlist, o ``None`` si la visión está deshabilitada,
    el modelo no responde o la acción no es válida.

    NO ejecuta la acción.
    """
    if not vision_habilitada():
        logger.info(
            "Visión deshabilitada (AGENTES_VISION_HABILITADA no está activo); "
            "no se consulta al modelo multimodal"
        )
        return None

    if llm is None or not getattr(llm, "disponible", False):
        logger.debug("Visión: cliente LLM no disponible")
        return None

    from core.utils import extraer_json_de_llm

    prompt = _PROMPT_DECISION.format(
        instruccion=(instruccion or "").strip()[:1000],
        contexto=(contexto or "").strip()[:2000] or "(sin contexto de texto)",
        acciones=", ".join(acciones_permitidas),
    )
    mensajes = construir_mensajes_multimodales(prompt, [captura])

    try:
        resultado = llm.completar(
            mensajes,
            temperature=0.1,
            max_tokens=max_tokens,
            reasoning_effort="low",
            thinking_enabled=False,
        )
        texto = getattr(resultado, "contenido", None) or ""
    except Exception as e:
        logger.warning(f"Visión: fallo consultando al modelo multimodal: {e}")
        return None

    datos = extraer_json_de_llm(texto)
    if not isinstance(datos, dict):
        logger.warning("Visión: el modelo no devolvió JSON válido")
        return None

    razon = str(datos.get("razon", ""))[:500]
    accion = datos.get("accion")
    if accion is None:
        return {"razon": razon, "accion": None}
    if not isinstance(accion, dict):
        logger.warning("Visión: 'accion' no es un objeto")
        return None

    tipo = str(accion.get("tipo", "")).strip().lower()
    if tipo not in acciones_permitidas:
        logger.warning(
            f"Visión: acción '{tipo}' no permitida; se descarta la decisión"
        )
        return None
    accion["tipo"] = tipo
    return {"razon": razon, "accion": accion}


__all__ = [
    "ACCIONES_PERMITIDAS",
    "codificar_imagen",
    "construir_mensajes_multimodales",
    "decidir_accion_desde_captura",
    "vision_habilitada",
]
