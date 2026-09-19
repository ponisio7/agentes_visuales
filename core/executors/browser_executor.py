# core/executors/browser_executor.py
"""
Ejecutor de agentes Browser: navegación web real con Playwright (Chromium).

Permite navegar una URL y ejecutar una lista de acciones declarativas:
``esperar``, ``extraer``, ``click``, ``rellenar``, ``scroll``,
``screenshot``, ``ejecutar_js`` y ``navegar``.

Contrato de salida (siempre presente, también en error):

    {
        "url_final": str,             # URL tras redirecciones/acciones
        "titulo": str,                # <title> de la página
        "html": str,                  # HTML final de la página
        "texto": str,                 # texto visible (innerText del body)
        "datos_extraidos": dict,      # {nombre: valor} de las acciones 'extraer'
        "acciones_ejecutadas": list,  # [{"tipo", "ok", "error"|"detalle"}]
        "screenshots": list,          # rutas de los screenshots guardados
        "html_truncado": bool,        # True si 'html' se recortó
        "error": str | None,          # None si todo fue bien
        "duracion": float,            # segundos
    }

Es un executor genérico: no conoce ningún dominio concreto. La URL y los
valores de las acciones aceptan referencias a dependencias con la sintaxis
``{Agente.clave}``, que se sustituyen antes de ejecutar (igual que en los
agentes HTTP/Shell).
"""

import logging
import os
import time

from core.agent import Agente
from core.cancellation import CancellationToken

from .content_extractor import sustituir_variables, variables_disponibles

logger = logging.getLogger(__name__)

try:  # pragma: no cover - depende del entorno
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_DISPONIBLE = True
except ImportError:  # pragma: no cover
    sync_playwright = None
    PlaywrightTimeoutError = Exception
    PLAYWRIGHT_DISPONIBLE = False


DEFAULT_TIMEOUT = 30            # segundos (timeout global del paso)
DEFAULT_ACTION_TIMEOUT = 10000  # milisegundos (timeout por acción)
DEFAULT_MAX_URLS = 5              # máximo de URLs a navegar con 'urls_desde'
MAX_HTML_CHARS = 2_000_000      # tope defensivo para 'html'
SCREENSHOTS_DIR = os.path.join("outputs", "screenshots")

# User-agent realista por defecto (muchas webs bloquean clientes sin UA).
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

ACCIONES_VALIDAS = (
    "esperar", "extraer", "click", "rellenar",
    "scroll", "screenshot", "ejecutar_js", "navegar",
)

# Tipos de recurso que se pueden bloquear para acelerar la carga.
RECURSOS_BLOQUEABLES = {"image", "font", "stylesheet", "media"}

# ── Extracción de texto principal (readability ligera, sobre el DOM vivo) ──
FORMATO_TEXTO_PRINCIPAL = "texto_principal"
MIN_CHARS_CONTENIDO = 200      # texto mínimo para considerar que la página cargó
ESPERA_CONTENIDO_MS = 5000     # espera máxima a que aparezca contenido
ESPERA_CONTENIDO_PASO_MS = 250

# Devuelve el texto del contenedor con más contenido, descartando navegación,
# cabeceras, pies, banners de cookies y bloques llenos de enlaces. Genérico:
# no conoce ningún sitio concreto.
_JS_TEXTO_PRINCIPAL = r"""
(opciones) => {
  const ruido = [
    'script','style','noscript','svg','iframe','form','template','button',
    'nav','header','footer','aside',
    '[role="navigation"]','[role="banner"]','[role="contentinfo"]','[role="search"]',
    '[aria-hidden="true"]',
    '[class*="cookie"]','[id*="cookie"]','[class*="consent"]','[id*="consent"]',
    '[class*="gdpr"]','[id*="gdpr"]','[class*="newsletter"]','[class*="subscribe"]',
    '[class*="advert"]','[id*="advert"]','[class*="ads-"]','[class*="social"]',
    '[class*="menu"]','[class*="sidebar"]','[class*="breadcrumb"]'
  ].join(',');
  const normalizar = (t) => (t || '').replace(/[ \t]+/g, ' ').replace(/\n{3,}/g, '\n\n').trim();

  const selector = (opciones && opciones.selector) ||
    'article,main,[role="main"],.content,#content,.post,#main,.article,section,div';

  let candidatos = [];
  try { candidatos = Array.from(document.querySelectorAll(selector)); } catch (e) { candidatos = []; }
  if (!candidatos.length && document.body) candidatos = [document.body];

  let mejor = '';
  let mejorPuntos = -1;
  for (const nodo of candidatos) {
    let copia;
    try { copia = nodo.cloneNode(true); } catch (e) { continue; }
    try { Array.from(copia.querySelectorAll(ruido)).forEach((n) => n.remove()); } catch (e) {}
    const texto = normalizar(copia.innerText || copia.textContent);
    if (texto.length < 40) continue;
    const enlaces = copia.querySelectorAll('a').length;
    const puntos = texto.length - enlaces * 30;
    if (puntos > mejorPuntos) { mejorPuntos = puntos; mejor = texto; }
  }

  if (!mejor && document.body) mejor = normalizar(document.body.innerText);
  return mejor;
}
"""

# Longitud del texto visible: sirve para esperar a que la página cargue.
_JS_LONGITUD_TEXTO = "(document.body && document.body.innerText ? document.body.innerText.trim().length : 0)"


def _resultado_multi_vacio(error: str | None = None) -> dict:
    """Contrato de salida vacío del modo multi-URL ('urls_desde')."""
    return {
        "urls_navegadas": 0,
        "resultados_por_url": [],
        "errores": [],
        "screenshots": [],
        "error": error,
        "duracion": 0.0,
    }


def _resultado_vacio(error: str | None = None) -> dict:
    """Contrato de salida vacío (con error opcional)."""
    return {
        "url_final": "",
        "titulo": "",
        "html": "",
        "texto": "",
        "datos_extraidos": {},
        "acciones_ejecutadas": [],
        "screenshots": [],
        "html_truncado": False,
        "error": error,
        "duracion": 0.0,
    }


class BrowserExecutor:
    """Navega URLs y ejecuta acciones declarativas con Playwright."""

    @staticmethod
    def actualizar_progreso(agente, progreso: int, mensaje: str = ""):
        agente.progreso = min(100, max(0, progreso))
        if mensaje:
            agente.mensaje = mensaje
        if hasattr(agente, '_bridge') and agente._bridge is not None:
            try:
                agente._bridge.agente_actualizado.emit(agente.id)
            except Exception:
                pass

    @classmethod
    def ejecutar(
        cls,
        agente: Agente,
        contexto: dict,
        cancellation_token: CancellationToken | None = None
    ) -> tuple[bool, str, dict]:
        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", _resultado_vacio('cancelled')

        inicio = time.time()

        if not PLAYWRIGHT_DISPONIBLE:
            cls.actualizar_progreso(agente, 100, "Browser no disponible")
            return False, (
                "La navegación web no está disponible: instala playwright "
                "(pip install playwright && python -m playwright install chromium)"
            ), _resultado_vacio('browser_unavailable')

        variables = variables_disponibles(agente, contexto)

        # ── Modo multi-URL: 'urls_desde' apunta a una lista del contexto ──
        urls_desde = (getattr(agente, 'urls_desde_browser', '') or "").strip()
        modo_multi = bool(urls_desde)
        max_urls = cls._entero(getattr(agente, 'max_urls_browser', None), DEFAULT_MAX_URLS, 1)
        acciones_por_url = getattr(agente, 'acciones_por_url_browser', None) or []
        if not isinstance(acciones_por_url, list):
            acciones_por_url = []

        # ── Modo una URL ──
        url = ""
        if not modo_multi:
            url = sustituir_variables(getattr(agente, 'url_browser', '') or "", variables).strip()
            if not url:
                cls.actualizar_progreso(agente, 100, "URL vacía")
                return False, "Browser: 'url_browser' está vacía", _resultado_vacio('empty_url')

            # Si tras sustituir quedan placeholders, la dependencia no estaba
            # disponible o la ruta no existe: error claro en vez de navegar a
            # una URL literal con llaves.
            if "{" in url and "}" in url:
                cls.actualizar_progreso(agente, 100, "URL sin resolver")
                return False, (
                    f"Browser: la URL quedó sin resolver tras sustituir variables: "
                    f"{url[:120]}"
                ), _resultado_vacio('unresolved_url')

            if not url.lower().startswith(("http://", "https://", "file://", "about:", "data:")):
                url = "https://" + url

        timeout_s = cls._entero(getattr(agente, 'timeout_browser', None), DEFAULT_TIMEOUT, 1)
        timeout_accion_ms = cls._entero(
            getattr(agente, 'timeout_accion_browser', None), DEFAULT_ACTION_TIMEOUT, 1
        )
        headless = bool(getattr(agente, 'headless_browser', True))
        bloquear = bool(getattr(agente, 'bloquear_recursos_browser', False))
        user_agent = (getattr(agente, 'user_agent_browser', '') or "").strip() or DEFAULT_USER_AGENT

        acciones = getattr(agente, 'acciones_browser', None) or []
        if not isinstance(acciones, list):
            acciones = []

        objetivo_log = (
            f"urls_desde={urls_desde} max_urls={max_urls}" if modo_multi
            else f"url={url[:80]}"
        )
        logger.info(
            f"Browser '{agente.nombre}': {objetivo_log} "
            f"acciones={len(acciones_por_url if modo_multi else acciones)} "
            f"headless={headless} timeout={timeout_s}s"
        )

        acciones_ejecutadas: list[dict] = []
        datos_extraidos: dict = {}
        screenshots: list[str] = []
        url_final = url
        titulo = ""
        html = ""
        texto = ""
        html_truncado = False
        error: str | None = None
        cancelado = False
        resultado_multi: tuple[bool, str, dict] | None = None

        page = None
        contexto_browser = None
        browser = None
        playwright = None

        try:
            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=headless)
            contexto_browser = browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1366, "height": 900},
                ignore_https_errors=True,
            )
            contexto_browser.set_default_timeout(timeout_s * 1000)

            if bloquear:
                def _bloquear(route, request):
                    if request.resource_type in RECURSOS_BLOQUEABLES:
                        route.abort()
                    else:
                        route.continue_()
                contexto_browser.route("**/*", _bloquear)

            page = contexto_browser.new_page()

            if modo_multi:
                resultado_multi = cls._navegar_varias_urls(
                    page=page,
                    agente=agente,
                    contexto=contexto,
                    urls_desde=urls_desde,
                    max_urls=max_urls,
                    acciones=acciones_por_url,
                    variables=variables,
                    timeout_s=timeout_s,
                    timeout_accion_ms=timeout_accion_ms,
                    cancellation_token=cancellation_token,
                    inicio=inicio,
                )
            else:
                cls.actualizar_progreso(agente, 20, "Navegando...")

                try:
                    page.goto(url, timeout=timeout_s * 1000, wait_until="load")
                except PlaywrightTimeoutError:
                    # La página no terminó de cargar: seguimos con lo que haya.
                    logger.warning(f"Browser '{agente.nombre}': timeout de carga en {url}")
                    acciones_ejecutadas.append({
                        "tipo": "navegar", "ok": False,
                        "error": f"timeout de carga tras {timeout_s}s",
                    })

                cls._esperar_contenido(
                    page,
                    min(ESPERA_CONTENIDO_MS, timeout_accion_ms),
                    cancellation_token=cancellation_token,
                )

                registros, cancelado = cls._ejecutar_acciones(
                    page=page,
                    acciones=acciones,
                    variables=variables,
                    timeout_s=timeout_s,
                    timeout_accion_ms=timeout_accion_ms,
                    agente=agente,
                    datos_extraidos=datos_extraidos,
                    screenshots=screenshots,
                    cancellation_token=cancellation_token,
                    progreso=lambda i, n, a: cls.actualizar_progreso(
                        agente, min(90, 20 + int(70 * i / max(1, n))),
                        f"Acción {i}/{n}: {a.get('tipo', '?')}",
                    ),
                )
                acciones_ejecutadas.extend(registros)

                cls.actualizar_progreso(agente, 92, "Recogiendo resultado...")
                try:
                    url_final = page.url or url
                    titulo = page.title() or ""
                    html = page.content() or ""
                    try:
                        texto = page.inner_text("body") or ""
                    except Exception:
                        texto = ""
                except Exception as e:
                    logger.warning(f"Browser '{agente.nombre}': error recogiendo resultado: {e}")
                    error = f"{type(e).__name__}: {e}"

                if len(html) > MAX_HTML_CHARS:
                    html = html[:MAX_HTML_CHARS]
                    html_truncado = True

        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            logger.warning(f"Browser '{agente.nombre}' falló: {error}")
        finally:
            # Cerrar SIEMPRE, incluso con error o cancelación.
            for recurso, metodo in (
                (contexto_browser, "close"),
                (browser, "close"),
                (playwright, "stop"),
            ):
                if recurso is None:
                    continue
                try:
                    getattr(recurso, metodo)()
                except Exception as e:
                    logger.debug(f"Browser: error cerrando {metodo}: {e}")

        # ── Modo multi-URL: devolver el resultado agregado ──
        if modo_multi:
            if resultado_multi is not None:
                return resultado_multi
            vacio = _resultado_multi_vacio(error or 'browser_error')
            vacio["duracion"] = time.time() - inicio
            return False, f"Browser falló: {error}", vacio

        hubo_extraccion = any(
            isinstance(a, dict) and str(a.get("tipo", "")).lower() == "extraer"
            for a in acciones
        )
        if hubo_extraccion and not datos_extraidos:
            logger.warning(
                f"Browser '{agente.nombre}': ninguna acción 'extraer' devolvió datos"
            )

        duracion = time.time() - inicio
        resultado = {
            "url_final": url_final,
            "titulo": titulo,
            "html": html,
            "texto": texto,
            "datos_extraidos": datos_extraidos,
            "acciones_ejecutadas": acciones_ejecutadas,
            "screenshots": screenshots,
            "html_truncado": html_truncado,
            "error": error,
            "duracion": duracion,
        }

        if cancelado or (cancellation_token and cancellation_token.esta_cancelado()):
            resultado["error"] = "cancelled"
            cls.actualizar_progreso(agente, 100, "Cancelado")
            return False, "Cancelado durante la navegación", resultado

        if error is not None:
            cls.actualizar_progreso(agente, 100, "Error en la navegación")
            return False, f"Browser falló: {error}", resultado

        cls.actualizar_progreso(agente, 100, "Navegación completada")
        resumen = f"Browser: {titulo[:60] or url_final[:60]}"
        if datos_extraidos:
            resumen += f" | extraído: {list(datos_extraidos)[:3]}"
        return True, resumen, resultado

    # ── Multi-URL ────────────────────────────────────────────────

    @staticmethod
    def _resolver_urls(contexto: dict, ruta: str, max_urls: int) -> tuple[list[str] | None, str | None]:
        """
        Resuelve 'urls_desde' ("Agente.clave") contra el contexto y devuelve
        la lista de URLs. Cada item puede ser un string o un dict con
        'url', 'href' o 'link'.

        Devuelve (urls, error). Si no se puede resolver, urls es None.
        """
        partes = [p for p in str(ruta or "").split(".") if p]
        if not partes:
            return None, "'urls_desde' está vacío"

        valor = (contexto or {}).get(partes[0])
        for parte in partes[1:]:
            if isinstance(valor, dict) and parte in valor:
                valor = valor[parte]
            else:
                valor = None
                break

        if valor is None:
            return None, f"no se encontró '{ruta}' en el contexto"
        if isinstance(valor, dict):
            return None, f"'{ruta}' apunta a un dict, no a una lista"
        if not isinstance(valor, (list, tuple)):
            return None, f"'{ruta}' no es una lista (es {type(valor).__name__})"

        urls: list[str] = []
        for item in list(valor):
            if len(urls) >= max_urls:
                break
            if isinstance(item, str) and item.strip():
                urls.append(item.strip())
            elif isinstance(item, dict):
                for clave in ("url", "href", "link"):
                    candidato = item.get(clave)
                    if isinstance(candidato, str) and candidato.strip():
                        urls.append(candidato.strip())
                        break
        return urls, None

    @classmethod
    def _navegar_varias_urls(
        cls,
        page,
        agente,
        contexto: dict,
        urls_desde: str,
        max_urls: int,
        acciones: list,
        variables: dict,
        timeout_s: int,
        timeout_accion_ms: int,
        cancellation_token: CancellationToken | None,
        inicio: float,
    ) -> tuple[bool, str, dict]:
        """Navega la lista de URLs de 'urls_desde' y agrega los resultados."""
        urls, error_resolucion = cls._resolver_urls(contexto, urls_desde, max_urls)
        if error_resolucion is not None:
            vacio = _resultado_multi_vacio('urls_desde_not_found')
            vacio["duracion"] = time.time() - inicio
            return False, f"Browser: {error_resolucion}", vacio

        if not urls:
            vacio = _resultado_multi_vacio(None)
            vacio["duracion"] = time.time() - inicio
            return True, f"Browser: 0 URLs en '{urls_desde}'", vacio

        resultados_por_url: list[dict] = []
        errores: list[dict] = []
        screenshots: list[str] = []
        cancelado = False
        total = len(urls)

        for i, url in enumerate(urls, start=1):
            if cancellation_token and cancellation_token.esta_cancelado():
                cancelado = True
                break

            cls.actualizar_progreso(
                agente, min(90, 10 + int(80 * (i - 1) / max(1, total))),
                f"Navegando URL {i}/{total}",
            )

            registro = {
                "url": url,
                "titulo": "",
                "datos_extraidos": {},
                "acciones_ejecutadas": [],
                "error": None,
            }
            destino = url if url.lower().startswith(
                ("http://", "https://", "file://", "about:", "data:")
            ) else "https://" + url

            try:
                try:
                    page.goto(destino, timeout=timeout_s * 1000, wait_until="load")
                except PlaywrightTimeoutError:
                    logger.warning(f"Browser '{agente.nombre}': timeout de carga en {destino}")
                    registro["acciones_ejecutadas"].append({
                        "tipo": "navegar", "ok": False,
                        "error": f"timeout de carga tras {timeout_s}s",
                    })

                cls._esperar_contenido(
                    page, min(ESPERA_CONTENIDO_MS, timeout_accion_ms)
                )

                registros, _ = cls._ejecutar_acciones(
                    page=page,
                    acciones=acciones,
                    variables=variables,
                    timeout_s=timeout_s,
                    timeout_accion_ms=timeout_accion_ms,
                    agente=agente,
                    datos_extraidos=registro["datos_extraidos"],
                    screenshots=screenshots,
                )
                registro["acciones_ejecutadas"].extend(registros)

                try:
                    registro["url"] = page.url or destino
                    registro["titulo"] = page.title() or ""
                except Exception as e:
                    registro["error"] = f"{type(e).__name__}: {e}"
            except Exception as e:
                registro["error"] = f"{type(e).__name__}: {e}"
                logger.warning(f"Browser '{agente.nombre}': falló la URL {url}: {e}")

            if registro["error"]:
                errores.append({"url": url, "error": registro["error"]})
            resultados_por_url.append(registro)

        # ¿Ninguna URL devolvió datos con las acciones de extracción?
        extraccion_vacia = bool(resultados_por_url) and all(
            not (r.get("datos_extraidos") or {})
            or all(
                cls._extraccion_vacia(v)
                for v in (r.get("datos_extraidos") or {}).values()
            )
            for r in resultados_por_url
        )
        if extraccion_vacia:
            logger.warning(
                f"Browser '{agente.nombre}': la extracción quedó VACÍA en las "
                f"{len(resultados_por_url)} URLs (revisa los selectores o usa "
                f"'{FORMATO_TEXTO_PRINCIPAL}')"
            )

        exitos = len(resultados_por_url) - len(errores)
        resultado = {
            "urls_navegadas": len(resultados_por_url),
            "resultados_por_url": resultados_por_url,
            "errores": errores,
            "screenshots": screenshots,
            "extraccion_vacia": extraccion_vacia,
            "error": 'cancelled' if cancelado else (None if exitos else 'all_urls_failed'),
            "duracion": time.time() - inicio,
        }

        if cancelado:
            cls.actualizar_progreso(agente, 100, "Cancelado")
            return False, "Cancelado durante la navegación", resultado

        if exitos == 0:
            cls.actualizar_progreso(agente, 100, "Ninguna URL procesada")
            return False, (
                f"Browser: ninguna de las {len(resultados_por_url)} URLs se pudo procesar"
            ), resultado

        cls.actualizar_progreso(agente, 100, f"{exitos}/{len(resultados_por_url)} URLs")
        resumen = f"Browser: {exitos}/{len(resultados_por_url)} URLs procesadas"
        if extraccion_vacia:
            resumen += " | AVISO: extracción vacía en todas las URLs"
        return True, resumen, resultado

    @classmethod
    def _ejecutar_acciones(
        cls,
        page,
        acciones: list,
        variables: dict,
        timeout_s: int,
        timeout_accion_ms: int,
        agente,
        datos_extraidos: dict,
        screenshots: list,
        cancellation_token: CancellationToken | None = None,
        progreso=None,
    ) -> tuple[list[dict], bool]:
        """Ejecuta una lista de acciones. Devuelve (registros, cancelado)."""
        registros: list[dict] = []
        for i, accion in enumerate(acciones, start=1):
            if cancellation_token and cancellation_token.esta_cancelado():
                return registros, True
            if not isinstance(accion, dict):
                registros.append({
                    "tipo": "desconocida", "ok": False,
                    "error": f"la acción #{i} no es un dict",
                })
                continue

            registros.append(cls._ejecutar_accion(
                page=page,
                accion=accion,
                variables=variables,
                timeout_accion_ms=timeout_accion_ms,
                timeout_s=timeout_s,
                agente=agente,
                datos_extraidos=datos_extraidos,
                screenshots=screenshots,
            ))
            if progreso is not None:
                progreso(i, len(acciones), accion)
        return registros, False


    # ── Helpers ──────────────────────────────────────────────────

    @staticmethod
    def _entero(valor, por_defecto: int, minimo: int = 1) -> int:
        try:
            numero = int(float(valor))
        except (TypeError, ValueError):
            numero = por_defecto
        return max(minimo, numero)

    @classmethod
    def _ejecutar_accion(
        cls,
        page,
        accion: dict,
        variables: dict,
        timeout_accion_ms: int,
        timeout_s: int,
        agente,
        datos_extraidos: dict,
        screenshots: list,
    ) -> dict:
        """Ejecuta una acción. Nunca lanza: devuelve el registro del resultado."""
        tipo = str(accion.get("tipo", "") or "").strip().lower()
        registro: dict = {"tipo": tipo, "ok": False}
        ms = cls._entero(accion.get("timeout"), timeout_accion_ms, 1)

        def _txt(valor) -> str:
            return sustituir_variables(str(valor), variables) if valor is not None else ""

        try:
            if tipo == "esperar":
                selector = _txt(accion.get("selector"))
                if selector:
                    page.wait_for_selector(
                        selector, timeout=ms,
                        state=str(accion.get("estado", "visible") or "visible"),
                    )
                    registro["detalle"] = f"selector listo: {selector}"
                else:
                    page.wait_for_timeout(cls._entero(accion.get("milisegundos"), ms, 1))
                    registro["detalle"] = "espera temporal"
                registro["ok"] = True

            elif tipo == "extraer":
                formato = str(accion.get("formato", "text") or "text").lower()
                selector = _txt(accion.get("selector")) or "body"
                multiple = bool(accion.get("multiple", False))
                nombre = str(accion.get("nombre") or f"extraccion_{len(datos_extraidos) + 1}")

                if formato == FORMATO_TEXTO_PRINCIPAL:
                    # Readability ligera sobre el DOM vivo (descarta ruido).
                    valor = cls._texto_principal(page, _txt(accion.get("selector")) or None)
                    if cls._extraccion_vacia(valor):
                        raise ValueError(
                            "no se pudo extraer texto principal de la página"
                        )
                else:
                    valor = cls._extraer_con_fallback(
                        page=page,
                        selector=selector,
                        formato=formato,
                        accion=accion,
                        multiple=multiple,
                        variables=variables,
                        registro=registro,
                    )

                datos_extraidos[nombre] = valor
                registro["ok"] = True
                registro["detalle"] = f"{nombre} ({formato})"
                registro["nombre"] = nombre

            elif tipo == "click":
                selector = _txt(accion.get("selector"))
                page.click(selector, timeout=ms)
                registro["ok"] = True
                registro["detalle"] = f"click en {selector}"

            elif tipo == "rellenar":
                selector = _txt(accion.get("selector"))
                valor = _txt(accion.get("valor", ""))
                page.fill(selector, valor, timeout=ms)
                registro["ok"] = True
                registro["detalle"] = f"rellenado {selector}"

            elif tipo == "scroll":
                hasta = str(accion.get("hasta", "bottom") or "bottom").lower()
                pixeles = accion.get("pixeles")
                if pixeles is not None:
                    page.evaluate(f"window.scrollBy(0, {int(pixeles)});")
                    registro["detalle"] = f"scroll {int(pixeles)}px"
                elif hasta == "top":
                    page.evaluate("window.scrollTo(0, 0);")
                    registro["detalle"] = "scroll arriba"
                elif hasta in ("bottom", "fin", "final"):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight);")
                    registro["detalle"] = "scroll abajo"
                else:
                    page.locator(_txt(accion.get("hasta"))).first.scroll_into_view_if_needed(timeout=ms)
                    registro["detalle"] = f"scroll hasta {_txt(accion.get('hasta'))}"
                registro["ok"] = True

            elif tipo == "screenshot":
                ruta = cls._ruta_screenshot(accion, agente)
                selector = _txt(accion.get("selector"))
                if selector:
                    page.locator(selector).first.screenshot(path=ruta, timeout=ms)
                else:
                    page.screenshot(path=ruta, full_page=bool(accion.get("full_page", False)))
                screenshots.append(ruta)
                registro["ok"] = True
                registro["detalle"] = ruta

            elif tipo == "ejecutar_js":
                script = _txt(accion.get("script"))
                valor = page.evaluate(script)
                nombre = str(accion.get("nombre") or "").strip()
                if nombre:
                    datos_extraidos[nombre] = valor
                if isinstance(valor, (str, int, float, bool)) or valor is None:
                    registro["detalle"] = str(valor)[:200]
                else:
                    registro["detalle"] = type(valor).__name__
                registro["ok"] = True

            elif tipo == "navegar":
                destino = _txt(accion.get("url"))
                if not destino.lower().startswith(("http://", "https://", "file://", "about:", "data:")):
                    destino = "https://" + destino
                page.goto(
                    destino, timeout=cls._entero(accion.get("timeout"), timeout_s * 1000, 1),
                    wait_until=str(accion.get("esperar_hasta", "load") or "load"),
                )
                registro["ok"] = True
                registro["detalle"] = f"navegado a {destino[:80]}"

            else:
                registro["error"] = f"acción no soportada: '{tipo}'"

        except PlaywrightTimeoutError as e:
            registro["error"] = f"timeout: {str(e)[:150]}"
            logger.warning(f"Browser '{agente.nombre}': timeout en acción '{tipo}'")
        except Exception as e:
            registro["error"] = f"{type(e).__name__}: {str(e)[:200]}"
            logger.warning(f"Browser '{agente.nombre}': error en acción '{tipo}': {e}")

        return registro

    @classmethod
    def _texto_principal(cls, page, selector: str | None = None) -> str:
        """Texto del contenido principal, descartando navegación y ruido."""
        try:
            texto = page.evaluate(_JS_TEXTO_PRINCIPAL, {"selector": selector or ""})
        except Exception as e:
            logger.warning(f"Browser: falló la extracción de texto principal: {e}")
            return ""
        return (texto or "").strip()

    @classmethod
    def _esperar_contenido(cls, page, timeout_ms: int,
                           minimo: int = MIN_CHARS_CONTENIDO,
                           cancellation_token: CancellationToken | None = None) -> int:
        """
        Espera (hasta timeout_ms) a que la página tenga texto visible.

        Devuelve la longitud encontrada. Sirve para webs que pintan el
        contenido con JavaScript después de 'load'.
        """
        restante = max(0, int(timeout_ms))
        ultimo = -1
        estable = 0
        while restante > 0:
            if cancellation_token and cancellation_token.esta_cancelado():
                return 0
            try:
                largo = int(page.evaluate(_JS_LONGITUD_TEXTO) or 0)
            except Exception:
                return 0
            if largo >= minimo:
                return largo
            # Si el texto deja de crecer, la página ya pintó lo que tenía.
            if largo > 0 and largo == ultimo:
                estable += 1
                if estable >= 2:
                    return largo
            else:
                estable = 0
            ultimo = largo
            try:
                page.wait_for_timeout(ESPERA_CONTENIDO_PASO_MS)
            except Exception:
                return max(0, largo)
            restante -= ESPERA_CONTENIDO_PASO_MS
        return max(0, ultimo)

    @staticmethod
    def _extraccion_vacia(valor) -> bool:
        """¿El valor extraído no aporta nada?"""
        if valor is None:
            return True
        if isinstance(valor, str):
            return not valor.strip()
        if isinstance(valor, (list, tuple)):
            if not valor:
                return True
            return all(
                v is None or (isinstance(v, str) and not v.strip())
                for v in valor
            )
        return False

    @classmethod
    def _extraer_con_fallback(cls, page, selector: str, formato: str, accion: dict,
                              multiple: bool, variables: dict, registro: dict):
        """
        Extrae con el selector pedido. Si no devuelve nada (o falla), cae al
        texto principal como red de seguridad y lo deja anotado.
        """
        valor = None
        try:
            locator = page.locator(selector)
            if multiple:
                total = locator.count()
                valor = [
                    cls._extraer_de(locator.nth(idx), formato, accion, variables)
                    for idx in range(total)
                ]
            else:
                valor = cls._extraer_de(locator.first, formato, accion, variables)
        except Exception as e:
            logger.warning(
                f"Browser: la extracción con selector '{selector}' falló: {e}"
            )
            valor = None

        if cls._extraccion_vacia(valor):
            principal = cls._texto_principal(
                page, selector if selector and selector != "body" else None
            )
            if principal:
                logger.warning(
                    f"Browser: el selector '{selector}' no devolvió contenido; "
                    f"se usa '{FORMATO_TEXTO_PRINCIPAL}' como fallback"
                )
                registro["fallback"] = FORMATO_TEXTO_PRINCIPAL
                return principal

        if cls._extraccion_vacia(valor):
            raise ValueError(
                f"no se pudo extraer contenido con el selector '{selector}'"
            )
        return valor

    @staticmethod
    def _extraer_de(locator, formato: str, accion: dict, variables: dict):
        """Extrae un valor de un locator según el formato pedido."""
        if formato == "html":
            return locator.inner_html()
        if formato == "attr":
            atributo = accion.get("atributo")
            return locator.get_attribute(atributo)
        return locator.inner_text()

    @staticmethod
    def _ruta_screenshot(accion: dict, agente) -> str:
        """Ruta destino de un screenshot, creando el directorio si hace falta."""
        os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
        nombre = str(accion.get("nombre") or "").strip()
        if not nombre:
            nombre = f"{agente.nombre}_{time.strftime('%Y%m%d_%H%M%S')}"
        if not nombre.lower().endswith((".png", ".jpg", ".jpeg")):
            nombre += ".png"
        # Evitar rutas absolutas o traversal: solo el nombre base.
        nombre = os.path.basename(nombre)
        return os.path.join(SCREENSHOTS_DIR, nombre)
