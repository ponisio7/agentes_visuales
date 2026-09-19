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

        url = sustituir_variables(getattr(agente, 'url_browser', '') or "", variables).strip()
        if not url:
            cls.actualizar_progreso(agente, 100, "URL vacía")
            return False, "Browser: 'url_browser' está vacía", _resultado_vacio('empty_url')

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

        logger.info(
            f"Browser '{agente.nombre}': url={url[:80]} acciones={len(acciones)} "
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

            # ── Acciones declarativas ──
            for i, accion in enumerate(acciones, start=1):
                if cancellation_token and cancellation_token.esta_cancelado():
                    cancelado = True
                    break
                if not isinstance(accion, dict):
                    acciones_ejecutadas.append({
                        "tipo": "desconocida", "ok": False,
                        "error": f"la acción #{i} no es un dict",
                    })
                    continue

                registro = cls._ejecutar_accion(
                    page=page,
                    accion=accion,
                    variables=variables,
                    timeout_accion_ms=timeout_accion_ms,
                    timeout_s=timeout_s,
                    agente=agente,
                    datos_extraidos=datos_extraidos,
                    screenshots=screenshots,
                )
                acciones_ejecutadas.append(registro)
                cls.actualizar_progreso(
                    agente, min(90, 20 + int(70 * i / max(1, len(acciones)))),
                    f"Acción {i}/{len(acciones)}: {accion.get('tipo', '?')}",
                )

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
                locator = page.locator(selector)

                if multiple:
                    total = locator.count()
                    valores = []
                    for idx in range(total):
                        valores.append(cls._extraer_de(locator.nth(idx), formato, accion, variables))
                    valor = valores
                else:
                    valor = cls._extraer_de(locator.first, formato, accion, variables)

                nombre = str(accion.get("nombre") or f"extraccion_{len(datos_extraidos) + 1}")
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
