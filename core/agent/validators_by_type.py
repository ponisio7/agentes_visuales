# core/agent/validators_by_type.py
"""Validaciones específicas por tipo de agente.

Cada función recibe un Agente y devuelve (es_valido, mensaje_error).
"""
import logging
from typing import TYPE_CHECKING, Dict, Optional, Tuple

from .validator import AgenteValidator

if TYPE_CHECKING:
    from .model import Agente

logger = logging.getLogger(__name__)


def validar_python(agente: "Agente") -> Tuple[bool, str]:
    """Valida configuración de Python."""
    if agente.timeout_python < 1:
        return False, (
            f"Python: 'timeout_python' debe ser >= 1 "
            f"(actual: {agente.timeout_python})"
        )
    if agente.codigo_python:
        valido, mensaje = AgenteValidator.validar_codigo(agente.codigo_python)
        if not valido:
            return False, f"Python: {mensaje}"
    return True, ""


def validar_shell(agente: "Agente") -> Tuple[bool, str]:
    """Valida configuración de Shell."""
    if not agente.comando_shell or not agente.comando_shell.strip():
        return False, "Shell: 'comando_shell' es obligatorio"
    if agente.timeout_shell < 1:
        return False, (
            f"Shell: 'timeout_shell' debe ser >= 1 "
            f"(actual: {agente.timeout_shell})"
        )
    peligrosos = ['rm -rf', 'dd if=', 'mkfs', '> /', '| /']
    for peligroso in peligrosos:
        if peligroso in agente.comando_shell:
            logger.warning(
                f"Comando shell contiene operación potencialmente peligrosa: {peligroso}"
            )
    return True, ""


def validar_http(agente: "Agente") -> Tuple[bool, str]:
    """Valida configuración de HTTP."""
    if not agente.url_http or not agente.url_http.strip():
        return False, "HTTP: 'url_http' es obligatoria"
    valido, mensaje = AgenteValidator.validar_url(agente.url_http)
    if not valido:
        return False, f"HTTP: {mensaje}"
    if agente.timeout_http < 1:
        return False, (
            f"HTTP: 'timeout_http' debe ser >= 1 "
            f"(actual: {agente.timeout_http})"
        )
    metodos_validos = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
    if agente.metodo_http.upper() not in metodos_validos:
        return False, f"HTTP: método '{agente.metodo_http}' no soportado"
    return True, ""


def validar_llm(agente: "Agente") -> Tuple[bool, str]:
    """Valida configuración de LLM."""
    if not agente.prompt_llm or not agente.prompt_llm.strip():
        return False, "LLM: 'prompt_llm' es obligatorio"
    if not agente.modelo_llm or not agente.modelo_llm.strip():
        return False, "LLM: 'modelo_llm' es obligatorio"
    if agente.temperatura_llm < 0 or agente.temperatura_llm > 2:
        return False, (
            f"LLM: 'temperatura_llm' debe estar entre 0 y 2 "
            f"(actual: {agente.temperatura_llm})"
        )
    if agente.max_tokens_llm < 1:
        return False, (
            f"LLM: 'max_tokens_llm' debe ser >= 1 "
            f"(actual: {agente.max_tokens_llm})"
        )
    if agente.reasoning_effort_llm not in ("low", "medium", "high"):
        return False, (
            f"LLM: 'reasoning_effort_llm' debe ser low|medium|high "
            f"(actual: {agente.reasoning_effort_llm})"
        )
    if not isinstance(agente.thinking_enabled_llm, bool):
        return False, (
            f"LLM: 'thinking_enabled_llm' debe ser booleano "
            f"(actual: {agente.thinking_enabled_llm})"
        )
    return True, ""


def validar_file(agente: "Agente") -> Tuple[bool, str]:
    """Valida configuración de File."""
    operaciones_validas = {"leer", "escribir", "copiar", "mover", "eliminar"}
    if agente.operacion_file not in operaciones_validas:
        return False, f"File: operación '{agente.operacion_file}' no soportada"

    modos_validos = {"auto", "contenido", "json", "texto"}
    if agente.modo_salida_file not in modos_validos:
        return False, (
            f"File: 'modo_salida_file' inválido: '{agente.modo_salida_file}'. "
            f"Opciones: {sorted(modos_validos)}"
        )
    if agente.operacion_file in ("copiar", "mover") and not agente.archivo_origen:
        return False, (
            f"File: 'archivo_origen' es obligatorio para operación "
            f"'{agente.operacion_file}'"
        )
    if agente.operacion_file in ("escribir", "copiar", "mover") and not agente.archivo_destino:
        return False, (
            f"File: 'archivo_destino' es obligatorio para operación "
            f"'{agente.operacion_file}'"
        )
    if agente.operacion_file in ("leer", "eliminar") and not agente.archivo_origen:
        return False, (
            f"File: 'archivo_origen' es obligatorio para operación "
            f"'{agente.operacion_file}'"
        )
    if agente.archivo_origen:
        valido, mensaje = AgenteValidator.validar_ruta(agente.archivo_origen)
        if not valido:
            return False, f"File: {mensaje}"
    if agente.archivo_destino:
        valido, mensaje = AgenteValidator.validar_ruta(agente.archivo_destino)
        if not valido:
            return False, f"File: {mensaje}"
    return True, ""


def validar_loop(
    agente: "Agente",
    agentes_disponibles: Optional[Dict[str, "Agente"]] = None
) -> Tuple[bool, str]:
    """Valida configuración de Loop."""
    fuente = (agente.fuente_items or "").strip()
    if not fuente:
        return False, "Loop: 'fuente_items' es obligatorio"

    partes = [p.strip() for p in fuente.split('.') if p.strip()]
    if len(partes) < 2:
        return False, (
            f"Loop: 'fuente_items' debe tener formato 'Dependencia.clave' "
            f"(actual: '{fuente}')"
        )

    nombre_dependencia = partes[0]
    if agentes_disponibles:
        nombres_agentes = {a.nombre for a in agentes_disponibles.values()}
        if nombre_dependencia not in nombres_agentes:
            return False, (
                f"Loop: la fuente '{nombre_dependencia}' no corresponde a un "
                f"agente disponible. Agentes disponibles: {sorted(nombres_agentes)}"
            )

    if not agente.codigo_por_item or not agente.codigo_por_item.strip():
        return False, "Loop: 'codigo_por_item' es obligatorio"
    if agente.max_iteraciones < 1:
        return False, (
            f"Loop: 'max_iteraciones' debe ser >= 1 "
            f"(actual: {agente.max_iteraciones})"
        )
    if agente.timeout_loop < 1:
        return False, (
            f"Loop: 'timeout_loop' debe ser >= 1 "
            f"(actual: {agente.timeout_loop})"
        )
    if agente.timeout_python < 1:
        return False, (
            f"Loop: 'timeout_python' debe ser >= 1 "
            f"(actual: {agente.timeout_python})"
        )

    if agente.codigo_por_item:
        try:
            compile(agente.codigo_por_item, '<string>', 'exec')
        except SyntaxError as e:
            return False, f"Loop: Error de sintaxis en 'codigo_por_item': {e}"
    return True, ""


__all__ = [
    "validar_python",
    "validar_shell",
    "validar_http",
    "validar_llm",
    "validar_file",
    "validar_loop",
]