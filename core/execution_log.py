"""
core/execution_log.py
Formateo de resultados y errores para el log (V3.8-6).

Estas funciones vivían dentro de ``Scheduler`` (~215 líneas) y no tocan
ningún estado del orquestador: solo convierten el resultado de un agente en
una línea legible. Sacarlas deja al Scheduler más pequeño y las hace
probables por separado.

El contrato es el mismo de antes; ``Scheduler._resumir_resultado_log`` y
``Scheduler._resumir_error_log`` delegan aquí para no romper llamadas
existentes.
"""
from __future__ import annotations

import logging
from typing import Any

from .agent import TipoAgente

logger = logging.getLogger(__name__)

MAX_RESULTADO_LOG = 200


def resumir_resultado(
    agente: Any,
    resultado: Any,
    max_len: int = MAX_RESULTADO_LOG,
) -> str:
    """Resumen legible del resultado de un agente para el log."""
    tipo = getattr(agente, "tipo", None)

    if resultado is None:
        return "sin resultado"

    try:
        # ── HTTP (formato unificado) ──
        if tipo == TipoAgente.HTTP and isinstance(resultado, dict):
            status = resultado.get('status_code', '?')
            url = resultado.get('url', '')
            json_data = resultado.get('json')
            body = resultado.get('body', '')

            if json_data is not None:
                if isinstance(json_data, dict):
                    claves_interes = [
                        k for k in ('data', 'results', 'items', 'message', 'status')
                        if k in json_data
                    ]
                    if claves_interes:
                        preview = {k: json_data.get(k) for k in claves_interes[:3]}
                        return f"HTTP {status} | {url[:40]} | JSON: {str(preview)[:max_len]}"
                    claves = list(json_data.keys())[:3]
                    return f"HTTP {status} | {url[:40]} | JSON claves: {claves}"
                elif isinstance(json_data, list):
                    return f"HTTP {status} | {url[:40]} | JSON array: {len(json_data)} items"
                else:
                    return f"HTTP {status} | {url[:40]} | JSON: {str(json_data)[:max_len]}"

            if body:
                body_preview = body[:max_len].replace('\n', ' ').strip()
                if body_preview:
                    return f"HTTP {status} | {url[:40]} | body: {body_preview}"

            if resultado.get('error'):
                return f"HTTP {status} | {url[:40]} | error: {resultado['error'][:max_len]}"

            return f"HTTP {status} | {url[:50]}"

        # ── LLM ──
        elif tipo == TipoAgente.LLM and isinstance(resultado, dict):
            respuesta = resultado.get('respuesta', '')
            tokens = resultado.get('tokens_uso', {})
            token_info = f" ({tokens.get('total', '?')} tokens)" if tokens else ""

            if len(respuesta) > max_len:
                return f"{respuesta[:max_len]}...{token_info}"
            return f"{respuesta}{token_info}" if respuesta else "sin respuesta"

        # ── Shell ──
        elif tipo == TipoAgente.SHELL and isinstance(resultado, dict):
            stdout = resultado.get('stdout', '')
            stderr = resultado.get('stderr', '')
            codigo = resultado.get('codigo', '?')

            if stdout:
                preview = stdout[:max_len] + "..." if len(stdout) > max_len else stdout
                preview = preview.replace('\n', ' ').strip()
                return f"exit={codigo} | {preview}"
            elif stderr:
                preview = stderr[:max_len] + "..." if len(stderr) > max_len else stderr
                preview = preview.replace('\n', ' ').strip()
                return f"exit={codigo} | stderr: {preview}"
            else:
                return f"exit={codigo} | sin salida"

        # ── File ──
        elif tipo == TipoAgente.FILE and isinstance(resultado, dict):
            archivo = resultado.get('archivo', '')
            tamaño = resultado.get('tamaño', resultado.get('caracteres_escritos', 0))
            operacion = getattr(agente, 'operacion_file', '')
            if operacion:
                return f"{operacion} | {archivo} | {tamaño} bytes"
            return f"{archivo} | {tamaño} bytes"

        # ── Loop ──
        elif tipo == TipoAgente.LOOP and isinstance(resultado, dict):
            total = resultado.get('total_items', 0)
            exitos = resultado.get('exitos', 0)
            errores = resultado.get('errores', 0)
            duracion = resultado.get('duracion_total', 0)
            return f"{total} items | ✅{exitos} ❌{errores} | {duracion:.1f}s"

        # ── Browser ──
        elif tipo == TipoAgente.BROWSER and isinstance(resultado, dict):
            if 'urls_navegadas' in resultado:
                navegadas = resultado.get('urls_navegadas', 0)
                errores = resultado.get('errores') or []
                urls = [
                    (r.get('url') or '')[:40]
                    for r in (resultado.get('resultados_por_url') or [])[:2]
                    if isinstance(r, dict)
                ]
                preview = f"{navegadas} URLs | {urls} | errores: {len(errores)}"
                if resultado.get('error'):
                    preview += f" | error: {str(resultado['error'])[:max_len]}"
                return preview

            titulo = (resultado.get('titulo') or '')[:40]
            url = (resultado.get('url_final') or '')[:40]
            datos = resultado.get('datos_extraidos') or {}
            acciones = resultado.get('acciones_ejecutadas') or []
            ok_acciones = sum(
                1 for a in acciones if isinstance(a, dict) and a.get('ok')
            )
            preview = f"{titulo} | {url}"
            if datos:
                preview += f" | extraído: {list(datos)[:3]}"
            if acciones:
                preview += f" | acciones {ok_acciones}/{len(acciones)}"
            if resultado.get('error'):
                preview += f" | error: {str(resultado['error'])[:max_len]}"
            return preview

        # ── Search ──
        elif tipo == TipoAgente.SEARCH and isinstance(resultado, dict):
            query = (resultado.get('query') or '')[:40]
            if resultado.get('error'):
                return f"'{query}' | error: {str(resultado['error'])[:max_len]}"
            total = resultado.get('total', 0)
            urls = [
                (r.get('href') or '')[:40]
                for r in (resultado.get('resultados') or [])[:2]
                if isinstance(r, dict)
            ]
            return f"'{query}' | {total} resultados | {urls}"

        # ── Python ──
        elif tipo == TipoAgente.PYTHON:
            if isinstance(resultado, dict):
                claves_interes = [
                    k for k in ('status', 'mensaje', 'data', 'resultado', 'output', 'result')
                    if k in resultado
                ]
                if claves_interes:
                    partes = []
                    for k in claves_interes[:3]:
                        v = resultado[k]
                        if isinstance(v, (dict, list)):
                            v_str = f"<{type(v).__name__} len={len(v)}>"
                        else:
                            v_str = str(v)
                            if len(v_str) > 60:
                                v_str = v_str[:57] + "..."
                        partes.append(f"{k}={v_str}")
                    return " | ".join(partes)

                claves = list(resultado.keys())[:4]
                preview = {k: resultado[k] for k in claves}
                texto = str(preview)
                return texto[:max_len] + "..." if len(texto) > max_len else texto

            texto = str(resultado)
            return texto[:max_len] + "..." if len(texto) > max_len else texto

        # ── Fallback ──
        if isinstance(resultado, dict):
            claves = list(resultado.keys())[:4]
            preview = {k: resultado[k] for k in claves}
            texto = str(preview)
            return texto[:max_len] + "..." if len(texto) > max_len else texto

        texto = str(resultado)
        return texto[:max_len] + "..." if len(texto) > max_len else texto

    except Exception as e:
        logger.debug(f"Error formateando resultado: {e}")
        return f"(error al formatear: {str(e)[:50]})"


def resumir_error(
    agente: Any,
    mensaje: str,
    resultado: Any,
    max_len: int = MAX_RESULTADO_LOG,
) -> str:
    """Resumen del error de un agente para el log."""
    # ``agente`` se mantiene en la firma por compatibilidad con el contrato
    # anterior; el resumen se construye con el mensaje y el resultado.
    resumen = str(mensaje) if mensaje else "Error desconocido"
    resumen = resumen.replace('\n', ' ').strip()

    if resultado and isinstance(resultado, dict):
        extras = []

        if 'stderr' in resultado and resultado['stderr']:
            stderr = str(resultado['stderr'])[:100].replace('\n', ' ').strip()
            extras.append(f"stderr: {stderr}")

        if 'error' in resultado and resultado['error']:
            error_detail = str(resultado['error'])[:100].replace('\n', ' ').strip()
            extras.append(f"error: {error_detail}")

        if 'stdout' in resultado and resultado['stdout']:
            stdout = str(resultado['stdout'])[:80].replace('\n', ' ').strip()
            extras.append(f"stdout: {stdout}")

        if 'status_code' in resultado:
            extras.append(f"HTTP {resultado['status_code']}")

        if extras:
            resumen = f"{resumen[:100]} | " + " | ".join(extras)

    return resumen[:max_len] + "..." if len(resumen) > max_len else resumen


__all__ = ["MAX_RESULTADO_LOG", "resumir_error", "resumir_resultado"]
