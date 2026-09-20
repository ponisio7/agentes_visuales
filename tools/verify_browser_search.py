#!/usr/bin/env python3
"""
Arnés de verificación en vivo de los agentes Search y Browser.

No usa el LLM ni la API key: ejecuta los dos executors directamente para
comprobar que obtienen datos reales de la web.

Uso:
    python tools/verify_browser_search.py --query "consulta"
    python tools/verify_browser_search.py --query "consulta" --selector "table"
    python tools/verify_browser_search.py --url "https://ejemplo.com" --selector "table.wikitable"

Si no se pasa --url, se navega a la primera URL que devuelva Search.
"""

import argparse
import os
import sys
import textwrap

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.agent import Agente, TipoAgente  # noqa: E402
from core.executors.browser_executor import BrowserExecutor  # noqa: E402
from core.executors.search_executor import SearchExecutor  # noqa: E402


def _linea(titulo: str = "") -> None:
    print("\n" + "=" * 72)
    if titulo:
        print(titulo)
        print("=" * 72)


def ejecutar_search(query: str, max_resultados: int = 5) -> list[dict]:
    _linea(f"SEARCH  query={query!r} max_resultados={max_resultados}")
    agente = Agente(
        nombre="BuscarFuentes",
        tipo=TipoAgente.SEARCH,
        query_search=query,
        max_resultados_search=max_resultados,
    )
    ok, mensaje, resultado = SearchExecutor.ejecutar(agente, {})
    print(f"ok={ok} | {mensaje}")
    print(f"contrato: total={resultado['total']} error={resultado['error']!r}")
    for i, r in enumerate(resultado["resultados"], 1):
        print(f"  {i}. {r['title'][:70]}")
        print(f"     {r['href']}")
        if r["body"]:
            print(f"     {textwrap.shorten(r['body'], 110)}")
    return resultado["resultados"]


def ejecutar_browser(url: str, selector: str | None, screenshot: bool) -> dict:
    _linea(f"BROWSER  url={url}")
    acciones = []
    if selector:
        acciones.append({"tipo": "esperar", "selector": selector, "timeout": 20000})
        acciones.append({
            "tipo": "extraer", "selector": selector,
            "formato": "html", "nombre": "contenido",
        })
    acciones.append({"tipo": "extraer", "selector": "body", "formato": "text", "nombre": "texto"})
    if screenshot:
        acciones.append({"tipo": "screenshot", "nombre": "verificacion", "full_page": False})

    agente = Agente(
        nombre="NavegarWeb",
        tipo=TipoAgente.BROWSER,
        url_browser=url,
        acciones_browser=acciones,
        timeout_browser=45,
        headless_browser=True,
    )
    ok, mensaje, resultado = BrowserExecutor.ejecutar(agente, {})
    print(f"ok={ok} | {mensaje}")
    print(f"url_final={resultado['url_final']}")
    print(f"titulo={resultado['titulo']!r}")
    print(f"html={len(resultado['html'])} chars | texto={len(resultado['texto'])} chars")
    for accion in resultado["acciones_ejecutadas"]:
        estado = "OK " if accion.get("ok") else "FALLO"
        detalle = accion.get("detalle") or accion.get("error") or ""
        print(f"  [{estado}] {accion.get('tipo')}: {str(detalle)[:90]}")
    if resultado["screenshots"]:
        print(f"screenshots: {resultado['screenshots']}")
    return resultado


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default="python programming language",
                        help="consulta para Search")
    parser.add_argument("--max-resultados", type=int, default=5)
    parser.add_argument("--url", default=None,
                        help="URL para Browser (si se omite, usa la 1ª de Search)")
    parser.add_argument("--selector", default=None,
                        help="selector CSS a extraer en Browser (p. ej. 'table')")
    parser.add_argument("--screenshot", action="store_true")
    args = parser.parse_args()

    resultados = ejecutar_search(args.query, args.max_resultados)

    url = args.url
    if not url:
        if not resultados:
            print("\nSearch no devolvió resultados; no hay URL para Browser.")
            return 1
        url = resultados[0]["href"]

    resultado = ejecutar_browser(url, args.selector, args.screenshot)

    # ── Resumen de datos reales obtenidos ──
    _linea("RESUMEN")
    contenido = resultado["datos_extraidos"].get("contenido") or ""
    texto = resultado["datos_extraidos"].get("texto") or resultado["texto"] or ""
    print(f"resultados de búsqueda: {len(resultados)}")
    print(f"página final: {resultado['url_final']} | título: {resultado['titulo']!r}")
    if contenido:
        print(f"contenido extraído ({args.selector}): {len(contenido)} chars")
        print("  primeras líneas:")
        for linea in [ln.strip() for ln in contenido.splitlines() if ln.strip()][:6]:
            print(f"    {linea[:100]}")
    if texto:
        print("  texto visible (primeras líneas):")
        for linea in [ln.strip() for ln in texto.splitlines() if ln.strip()][:6]:
            print(f"    {linea[:100]}")
    return 0 if resultado["error"] is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
