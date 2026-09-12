# core/sandbox/script_builder.py
"""Construcción del script Python que se ejecuta en el subproceso del sandbox."""
import json
import platform
import re
from typing import Any, Dict, Optional

from ._logging import _safe_log


def escapar_codigo(codigo: str) -> str:
    """
    Escapa el código del usuario para prevenir inyección.

    Solo elimina caracteres de control nulos. No rompe docstrings
    ni f-strings. Advierte sobre palabras clave peligrosas.
    """
    codigo_escapado = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", codigo)

    for keyword in ("eval(", "exec(", "__import__"):
        if keyword in codigo:
            _safe_log(
                "warning",
                "Código contiene palabra clave potencialmente peligrosa: %s",
                keyword,
            )

    # compile( es legítimo como re.compile(...): solo advertimos si
    # aparece sin el prefijo 're.'
    if "compile(" in codigo and not re.search(r"\bre\.compile\s*\(", codigo):
        _safe_log(
            "warning",
            "Código contiene 'compile(' sin prefijo 're.' "
            "(posible uso de compile() built-in)",
        )

    return codigo_escapado


def construir_script(
    codigo_escapado: str,
    contexto: Dict,
    memory_limit_mb: Optional[int] = None,
) -> str:
    """
    Construye el script completo a ejecutar en el hijo.

    Usa globals() para capturar 'resultado'. Inyecta automáticamente
    las variables del Loop (item, indice, total). El límite de memoria
    (RLIMIT_AS) se aplica DENTRO de este script hijo.
    """
    # ── Serializar contexto ──
    try:
        contexto_json = json.dumps(contexto, default=str, ensure_ascii=False)
        contexto_json = (
            contexto_json
            .replace("true", "True")
            .replace("false", "False")
            .replace("null", "None")
        )
    except Exception as e:
        _safe_log("warning", "Contexto no serializable directamente: %s", e)
        contexto_simplificado: Dict[str, Any] = {}
        for k, v in contexto.items():
            try:
                json.dumps(v, default=str)
                contexto_simplificado[k] = v
            except Exception as e2:
                _safe_log(
                    "warning",
                    "   → clave '%s' (tipo %s) no serializable: %s. "
                    "Se convierte a str(): %s",
                    k, type(v).__name__, e2, str(v)[:200],
                )
                contexto_simplificado[k] = str(v)
        contexto_json = json.dumps(
            contexto_simplificado, default=str, ensure_ascii=False
        )

    # ── Bloque de límite de memoria (aplicado en el hijo) ──
    memoria_bloque = ""
    if memory_limit_mb and memory_limit_mb > 0 and platform.system() != "Windows":
        limit_bytes = memory_limit_mb * 1024 * 1024
        memoria_bloque = f'''
# ============================================================
# LÍMITE DE MEMORIA (RLIMIT_AS aplicado aquí, en el hijo)
# ============================================================
try:
    import resource as _resource_mod
    _resource_mod.setrlimit(_resource_mod.RLIMIT_AS, ({limit_bytes}, {limit_bytes}))
    try:
        _resource_mod.setrlimit(_resource_mod.RLIMIT_CORE, (0, 0))
    except (ValueError, OSError):
        pass
except (ValueError, OSError, ImportError) as _mem_err:
    print("__ERROR__" + json.dumps({{
        'error': f"No se pudo aplicar el limite de memoria: {{_mem_err}}",
        'tipo': 'ResourceError'
    }}, ensure_ascii=False))
    sys.exit(1)
'''
    elif memory_limit_mb and memory_limit_mb > 0:
        _safe_log(
            "warning",
            "memory_limit_mb=%s ignorado en Windows "
            "(resource.setrlimit no disponible)",
            memory_limit_mb,
        )

    # ── Script completo ──
    script = f'''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script generado automáticamente por el sandbox.
NO MODIFICAR MANUALMENTE.
"""

import json
import os
import sys
import traceback
import time
from datetime import datetime
{memoria_bloque}
# ============================================================
# CONTEXTO PROPORCIONADO
# ============================================================
contexto = {contexto_json}

# ============================================================
# INYECCIÓN AUTOMÁTICA DE VARIABLES DEL LOOP
# (Necesario para loops: item, indice, total)
# ============================================================
item = contexto.get('item')
indice = contexto.get('indice', 0)
total = contexto.get('total', 0)

# ============================================================
# CÓDIGO DEL USUARIO
# ============================================================
{codigo_escapado}

# ============================================================
# CAPTURA DE RESULTADO
# ============================================================
def _capturar_resultado():
    """Captura el resultado de la ejecución del código del usuario."""
    try:
        if 'resultado' in globals():
            resultado_final = globals()['resultado']
        else:
            resultado_final = {{
                k: v for k, v in globals().items()
                if not k.startswith('_')
                and not callable(v)
                and type(v).__name__ != 'module'
                and k not in ['contexto', 'json', 'sys', 'traceback',
                              'datetime', 'time', 'item', 'indice', 'total',
                              'resultado_final', '_capturar_resultado', 'os']
            }}

        if not resultado_final:
            resultado_final = {{'status': 'ok', 'mensaje': 'Código ejecutado correctamente'}}

        try:
            resultado_serializado = json.loads(json.dumps(resultado_final, default=str))
        except Exception as e_ser:
            claves_problematicas = {{}}
            if isinstance(resultado_final, dict):
                for k, v in resultado_final.items():
                    try:
                        json.dumps(v, default=str)
                    except Exception as e2:
                        claves_problematicas[k] = {{
                            'tipo': type(v).__name__,
                            'error': str(e2),
                            'valor': str(v)[:200]
                        }}
            resultado_serializado = {{
                'error': 'Resultado no serializable',
                'error_original': str(e_ser),
                'tipo': type(resultado_final).__name__,
                'claves_problematicas': claves_problematicas,
                'contenido': str(resultado_final)[:1000]
            }}

        return resultado_serializado

    except Exception as e:
        return {{
            'error': str(e),
            'tipo': type(e).__name__,
            'traceback': traceback.format_exc()
        }}


def _safe_emit(marcador, payload):
    """
    Escribe el marcador + JSON a stdout con flush explícito.
    Si el padre ya cerró el pipe, sale silenciosamente con os._exit().
    """
    try:
        sys.stdout.write(marcador + json.dumps(payload, ensure_ascii=False) + "\\n")
        sys.stdout.flush()
        return True
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except Exception:
            pass
        os._exit(0)
    except OSError as e:
        if e.errno == 32:  # EPIPE
            try:
                devnull = os.open(os.devnull, os.O_WRONLY)
                os.dup2(devnull, sys.stdout.fileno())
            except Exception:
                pass
            os._exit(0)
        raise


# ============================================================
# EJECUCIÓN PRINCIPAL
# ============================================================
if __name__ == "__main__":
    try:
        resultado_final = _capturar_resultado()
        _safe_emit("__RESULT__", resultado_final)
        os._exit(0)
    except BrokenPipeError:
        os._exit(0)
    except Exception as e:
        _safe_emit("__ERROR__", {{
            'error': str(e),
            'tipo': type(e).__name__,
            'traceback': traceback.format_exc()
        }})
        os._exit(1)
'''
    return script


# Alias retrocompatibles
_escapar_codigo = escapar_codigo
_construir_script = construir_script


__all__ = [
    "escapar_codigo",
    "construir_script",
    "_escapar_codigo",
    "_construir_script",
]