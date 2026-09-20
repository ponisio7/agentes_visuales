#!/usr/bin/env python3
# tools/comprobar_servidor_local.py
"""Comprueba un servidor LLM local compatible con OpenAI (H12).

No entrena nada ni forma parte del núcleo: es una herramienta de diagnóstico
para apuntar ``DEEPSEEK_BASE_URL`` a un servidor local (vLLM, llama.cpp,
Ollama con API OpenAI, TGI...) y verificar que responde.

Uso:
    python tools/comprobar_servidor_local.py --base-url http://localhost:8000/v1
    python tools/comprobar_servidor_local.py --base-url http://localhost:11434/v1 \
        --api-key local

Los servidores locales suelen aceptar cualquier API key; se usa "local" por
defecto si no hay ninguna configurada.
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True,
                        help="URL base del servidor (p. ej. http://localhost:8000/v1)")
    parser.add_argument("--api-key", default=None,
                        help="API key (por defecto la configurada o 'local')")
    parser.add_argument("--timeout", type=float, default=8.0)
    args = parser.parse_args()

    from core.ia_config import probar_conexion, resolver_api_key

    key, origen = resolver_api_key(args.api_key)
    if not key:
        key, origen = "local", "valor por defecto para servidores locales"

    print(f"Servidor : {args.base_url}")
    print(f"API key  : ({origen})")
    ok, mensaje = probar_conexion(key, args.base_url, timeout=args.timeout)
    print(("✅ " if ok else "❌ ") + mensaje)
    if not ok:
        print(
            "\nSugerencias:\n"
            "  · ¿está arrancado el servidor y escuchando en esa URL?\n"
            "  · ¿la ruta es la compatible con OpenAI (termina en /v1)?\n"
            "  · para usar este servidor en la app:\n"
            f"      export DEEPSEEK_BASE_URL=\"{args.base_url}\"\n"
            "    (o configúralo en ⚙ Configuración de la GUI)"
        )
        return 1
    print(
        "\nSiguiente paso: apunta la app a este servidor con DEEPSEEK_BASE_URL "
        "(GUI ⚙ Configuración o ~/.config/agentes_visuales/env)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
