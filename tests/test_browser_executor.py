# tests/test_browser_executor.py
"""
Tests unitarios del BrowserExecutor.

Los tests de comportamiento usan un doble de Playwright: no lanzan ningún
navegador ni tocan la red. Al final hay un test de integración real con una
página local (file://) que se salta si Chromium no está instalado.
"""

import os

import pytest

from core.agent import Agente, TipoAgente
from core.cancellation import CancellationToken
from core.executors import browser_executor
from core.executors.browser_executor import BrowserExecutor


class _TimeoutFalso(Exception):
    """Sustituye a PlaywrightTimeoutError en los tests."""


class _FakeLocator:
    def __init__(self, page, selector, indice=None):
        self.page = page
        self.selector = selector
        self.indice = indice

    @property
    def first(self):
        return _FakeLocator(self.page, self.selector)

    def nth(self, i):
        return _FakeLocator(self.page, self.selector, i)

    def count(self):
        if self.selector in self.page.selectores_vacios:
            return 0
        if self.selector in self.page.multi:
            return len(self.page.multi[self.selector])
        return 1

    def inner_text(self):
        if self.selector in self.page.selectores_vacios:
            raise browser_executor.PlaywrightTimeoutError(f"timeout {self.selector}")
        if self.selector in self.page.multi:
            items = self.page.multi[self.selector]
            return items[self.indice or 0]
        return self.page.textos.get(self.selector, f"texto{self.selector}")

    def inner_html(self):
        return self.page.htmls.get(self.selector, f"<b>{self.selector}</b>")

    def get_attribute(self, atributo):
        return self.page.atributos.get((self.selector, atributo), "valor-attr")

    def scroll_into_view_if_needed(self, timeout=None):
        self.page.llamadas.append(("scroll_into_view", self.selector))

    def screenshot(self, path=None, timeout=None):
        self.page._crear_archivo(path)
        self.page.llamadas.append(("locator_screenshot", self.selector))

    def click(self, timeout=None):
        self.page.llamadas.append(("click", self.selector))

    def fill(self, valor, timeout=None):
        self.page.llamadas.append(("fill", self.selector, valor))


class _FakePage:
    def __init__(self):
        self.url = ""
        self.textos: dict = {}
        self.htmls: dict = {}
        self.atributos: dict = {}
        self.multi: dict = {}
        self.llamadas: list = []
        self.timeout_selectors: set = set()
        self.timeout_goto_urls: set = set()
        self.error_selectors: dict = {}
        self.error_goto_urls: dict = {}
        self.al_esperar = None  # callback opcional
        self.al_navegar = None  # callback opcional (recibe la url)
        self.selectores_vacios: set = set()
        self.texto_principal = "Texto principal de prueba"
        self.longitud_texto = 1000  # suficiente para no esperar contenido

    # ── API usada por el executor ──
    def title(self):
        return "Título de prueba"

    def content(self):
        return "<html><body>contenido</body></html>"

    def inner_text(self, selector):
        return self.textos.get(selector, "texto del body")

    def wait_for_selector(self, selector, timeout=None, state=None):
        self.llamadas.append(("wait_for_selector", selector, timeout))
        if self.al_esperar is not None:
            self.al_esperar()
        if selector in self.error_selectors:
            raise self.error_selectors[selector]
        if selector in self.timeout_selectors:
            raise browser_executor.PlaywrightTimeoutError(f"timeout {selector}")

    def wait_for_timeout(self, ms):
        self.llamadas.append(("wait_for_timeout", ms))

    def locator(self, selector):
        return _FakeLocator(self, selector)

    def click(self, selector, timeout=None):
        self.llamadas.append(("page_click", selector))

    def fill(self, selector, valor, timeout=None):
        self.llamadas.append(("page_fill", selector, valor))

    def evaluate(self, script, arg=None):
        self.llamadas.append(("evaluate", script))
        if script == browser_executor._JS_TEXTO_PRINCIPAL:
            return self.texto_principal
        if script == browser_executor._JS_LONGITUD_TEXTO:
            return self.longitud_texto() if callable(self.longitud_texto) else self.longitud_texto
        if script == "document.title":
            return "Título JS"
        if script == "1+1":
            return 2
        return {"script": script}

    def goto(self, url, timeout=None, wait_until=None):
        self.llamadas.append(("goto", url, wait_until))
        if self.al_navegar is not None:
            self.al_navegar(url)
        if url in self.error_goto_urls:
            raise self.error_goto_urls[url]
        if url in self.timeout_goto_urls:
            raise browser_executor.PlaywrightTimeoutError(f"timeout goto {url}")
        self.url = url

    def llamadas_goto(self):
        return [c for c in self.llamadas if c and c[0] == "goto"]

    def screenshot(self, path=None, full_page=False):
        self._crear_archivo(path)
        self.llamadas.append(("page_screenshot", path, full_page))

    def _crear_archivo(self, path):
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "wb") as f:
                f.write(b"PNG")


class _FakeContext:
    def __init__(self, page):
        self.page = page
        self.cerrado = False
        self.rutas = []

    def new_page(self):
        return self.page

    def set_default_timeout(self, ms):
        self.page.llamadas.append(("set_default_timeout", ms))

    def route(self, patron, handler):
        self.rutas.append((patron, handler))

    def close(self):
        self.cerrado = True


class _FakeBrowser:
    def __init__(self, page):
        self.page = page
        self.contexto = _FakeContext(page)
        self.cerrado = False
        self.headless = None

    def new_context(self, **kwargs):
        self.page.llamadas.append(("new_context", kwargs))
        return self.contexto

    def close(self):
        self.cerrado = True


class _FakeChromium:
    def __init__(self, browser):
        self.browser = browser

    def launch(self, headless=True):
        self.browser.headless = headless
        return self.browser

    def __call__(self, **kwargs):  # pragma: no cover - no usado
        return self.browser


class _FakePlaywright:
    def __init__(self, browser):
        self.chromium = _FakeChromium(browser)


class _FakeSyncPlaywright:
    def __init__(self, browser):
        self.browser = browser
        self.detenido = False

    def start(self):
        return _FakePlaywright(self.browser)

    def stop(self):  # pragma: no cover - el executor llama a stop() del objeto devuelto por start()
        self.detenido = True


class _FakePlaywrightHandle:
    """Lo que devuelve sync_playwright(): un objeto con .start()."""

    def __init__(self, browser):
        self.browser = browser
        self.detenido = False

    def start(self):
        pw = _FakePlaywright(self.browser)
        pw.stop = self._marcar_detenido  # type: ignore[attr-defined]
        return pw

    def _marcar_detenido(self):
        self.detenido = True


@pytest.fixture
def fake_browser(monkeypatch):
    """Instala el doble de Playwright y devuelve (page, browser, handle)."""
    page = _FakePage()
    browser = _FakeBrowser(page)
    handle = _FakePlaywrightHandle(browser)
    monkeypatch.setattr(browser_executor, "sync_playwright", lambda: handle)
    monkeypatch.setattr(browser_executor, "PlaywrightTimeoutError", _TimeoutFalso)
    monkeypatch.setattr(browser_executor, "PLAYWRIGHT_DISPONIBLE", True)
    return page, browser, handle


def _agente(acciones=None, **kwargs) -> Agente:
    base = dict(
        nombre="NavegarWeb",
        tipo=TipoAgente.BROWSER,
        url_browser="https://ejemplo.test",
        acciones_browser=acciones or [],
    )
    base.update(kwargs)
    return Agente(**base)


def test_navegacion_y_extraccion(fake_browser):
    page, browser, handle = fake_browser
    page.textos["h1"] = "Encabezado"
    page.multi[".item"] = ["uno", "dos"]
    page.atributos[("a.enlace", "href")] = "https://destino.test"

    acciones = [
        {"tipo": "esperar", "selector": "h1"},
        {"tipo": "extraer", "selector": "h1", "formato": "text", "nombre": "titulo"},
        {"tipo": "extraer", "selector": ".item", "formato": "text", "nombre": "items", "multiple": True},
        {"tipo": "extraer", "selector": "a.enlace", "formato": "attr", "atributo": "href", "nombre": "enlace"},
        {"tipo": "extraer", "selector": "#x", "formato": "html", "nombre": "fragmento"},
        {"tipo": "click", "selector": "button"},
        {"tipo": "rellenar", "selector": "input", "valor": "hola"},
        {"tipo": "scroll", "hasta": "bottom"},
        {"tipo": "ejecutar_js", "script": "document.title", "nombre": "titulo_js"},
    ]
    ok, mensaje, resultado = BrowserExecutor.ejecutar(_agente(acciones), {})

    assert ok is True
    assert resultado["error"] is None
    assert resultado["url_final"] == "https://ejemplo.test"
    assert resultado["titulo"] == "Título de prueba"
    assert resultado["html"] == "<html><body>contenido</body></html>"
    assert resultado["datos_extraidos"]["titulo"] == "Encabezado"
    assert resultado["datos_extraidos"]["items"] == ["uno", "dos"]
    assert resultado["datos_extraidos"]["enlace"] == "https://destino.test"
    assert resultado["datos_extraidos"]["titulo_js"] == "Título JS"
    assert all(a["ok"] for a in resultado["acciones_ejecutadas"])
    assert set(resultado.keys()) == {
        "url_final", "titulo", "html", "texto", "datos_extraidos",
        "acciones_ejecutadas", "screenshots", "html_truncado", "error", "duracion",
    }
    # El navegador se cierra siempre
    assert browser.cerrado is True
    assert browser.contexto.cerrado is True
    assert handle.detenido is True


def test_accion_invalida_no_aborta_el_resto(fake_browser):
    page, _, _ = fake_browser
    acciones = [
        {"tipo": "volar"},
        {"tipo": "extraer", "selector": "h1", "nombre": "tras_error"},
    ]

    ok, _, resultado = BrowserExecutor.ejecutar(_agente(acciones), {})

    assert ok is True
    assert resultado["acciones_ejecutadas"][0]["ok"] is False
    assert "no soportada" in resultado["acciones_ejecutadas"][0]["error"]
    assert resultado["acciones_ejecutadas"][1]["ok"] is True
    assert "tras_error" in resultado["datos_extraidos"]


def test_error_en_accion_no_aborta(fake_browser):
    page, _, _ = fake_browser
    page.error_selectors["#malo"] = RuntimeError("selector roto")
    acciones = [
        {"tipo": "esperar", "selector": "#malo"},
        {"tipo": "extraer", "selector": "h1", "nombre": "siguiente"},
    ]

    ok, _, resultado = BrowserExecutor.ejecutar(_agente(acciones), {})

    assert ok is True
    assert resultado["acciones_ejecutadas"][0]["ok"] is False
    assert "selector roto" in resultado["acciones_ejecutadas"][0]["error"]
    assert "siguiente" in resultado["datos_extraidos"]


def test_timeout_en_accion_se_registra(fake_browser):
    page, _, _ = fake_browser
    page.timeout_selectors.add("#lento")

    ok, _, resultado = BrowserExecutor.ejecutar(
        _agente([{"tipo": "esperar", "selector": "#lento", "timeout": 500}]), {}
    )

    assert ok is True
    registro = resultado["acciones_ejecutadas"][0]
    assert registro["ok"] is False
    assert registro["error"].startswith("timeout")
    assert ("wait_for_selector", "#lento", 500) in page.llamadas


def test_timeout_de_carga_no_rompe(fake_browser):
    page, _, _ = fake_browser
    page.timeout_goto_urls.add("https://ejemplo.test")

    ok, _, resultado = BrowserExecutor.ejecutar(_agente(), {})

    assert ok is True
    assert resultado["acciones_ejecutadas"][0]["ok"] is False
    assert "timeout de carga" in resultado["acciones_ejecutadas"][0]["error"]


def test_cancelacion_durante_acciones(fake_browser):
    page, browser, _ = fake_browser
    token = CancellationToken()
    page.al_esperar = token.cancelar  # se cancela al ejecutar la 1ª acción
    acciones = [
        {"tipo": "esperar", "selector": "h1"},
        {"tipo": "extraer", "selector": "h1", "nombre": "no_deberia"},
    ]

    ok, mensaje, resultado = BrowserExecutor.ejecutar(_agente(acciones), {}, token)

    assert ok is False
    assert resultado["error"] == "cancelled"
    assert "no_deberia" not in resultado["datos_extraidos"]
    assert browser.cerrado is True  # se cierra pese a la cancelación


def test_cancelado_antes_de_ejecutar(fake_browser):
    token = CancellationToken()
    token.cancelar()

    ok, _, resultado = BrowserExecutor.ejecutar(_agente(), {}, token)

    assert ok is False
    assert resultado["error"] == "cancelled"


def test_sin_playwright(monkeypatch):
    monkeypatch.setattr(browser_executor, "PLAYWRIGHT_DISPONIBLE", False)

    ok, mensaje, resultado = BrowserExecutor.ejecutar(_agente(), {})

    assert ok is False
    assert resultado["error"] == "browser_unavailable"
    assert "playwright" in mensaje.lower()


def test_url_vacia(fake_browser):
    ok, _, resultado = BrowserExecutor.ejecutar(_agente(url_browser="   "), {})

    assert ok is False
    assert resultado["error"] == "empty_url"


def test_screenshot_en_directorio_de_salida(fake_browser, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    ok, _, resultado = BrowserExecutor.ejecutar(
        _agente([{"tipo": "screenshot", "nombre": "captura"}]),
        {},
    )

    assert ok is True
    assert resultado["screenshots"]
    ruta = resultado["screenshots"][0]
    assert ruta.replace("\\", "/") == "outputs/screenshots/captura.png"
    assert os.path.isfile(ruta)


def test_bloqueo_de_recursos(fake_browser):
    page, browser, _ = fake_browser
    BrowserExecutor.ejecutar(_agente(bloquear_recursos_browser=True), {})

    assert browser.contexto.rutas, "no se registró el handler de bloqueo"
    _, handler = browser.contexto.rutas[0]

    class _Route:
        def __init__(self):
            self.accion = None

        def abort(self):
            self.accion = "abort"

        def continue_(self):
            self.accion = "continue"

    class _Request:
        def __init__(self, tipo):
            self.resource_type = tipo

    img = _Route()
    handler(img, _Request("image"))
    doc = _Route()
    handler(doc, _Request("document"))

    assert img.accion == "abort"
    assert doc.accion == "continue"


def test_url_sin_esquema_recibe_https(fake_browser):
    page, _, _ = fake_browser

    BrowserExecutor.ejecutar(_agente(url_browser="ejemplo.test"), {})

    assert ("goto", "https://ejemplo.test", "load") in page.llamadas


def test_integracion_real_con_file_url(tmp_path, monkeypatch):
    """Integración real con Chromium (se salta si no está instalado)."""
    pytest.importorskip("playwright")
    monkeypatch.chdir(tmp_path)

    html = tmp_path / "pagina.html"
    html.write_text(
        "<!DOCTYPE html><html><head><meta charset='utf-8'><title>Local</title></head>"
        "<body><h1 id='t'>Hola</h1><div class='i'>uno</div><div class='i'>dos</div>"
        "</body></html>",
        encoding="utf-8",
    )

    agente = _agente(
        acciones=[
            {"tipo": "esperar", "selector": "#t"},
            {"tipo": "extraer", "selector": "#t", "formato": "text", "nombre": "titulo"},
            {"tipo": "extraer", "selector": ".i", "formato": "text", "nombre": "items", "multiple": True},
        ],
        url_browser=f"file://{html}",
    )

    try:
        ok, mensaje, resultado = BrowserExecutor.ejecutar(agente, {})
    except Exception as e:  # pragma: no cover - depende del entorno
        pytest.skip(f"Chromium no disponible: {e}")

    if not ok and resultado.get("error") == "browser_unavailable":  # pragma: no cover
        pytest.skip("Playwright no instalado")

    assert ok is True, mensaje
    assert resultado["titulo"] == "Local"
    assert resultado["datos_extraidos"]["titulo"] == "Hola"
    assert resultado["datos_extraidos"]["items"] == ["uno", "dos"]


# ============================================================
# MODO MULTI-URL ('urls_desde')
# ============================================================

def _agente_multi(urls_desde="Buscar.resultados", **kwargs) -> Agente:
    base = dict(
        nombre="NavegarVarias",
        tipo=TipoAgente.BROWSER,
        urls_desde_browser=urls_desde,
        acciones_por_url_browser=[
            {"tipo": "extraer", "selector": "h2", "formato": "text", "nombre": "elementos"},
        ],
    )
    base.update(kwargs)
    return Agente(**base)


def test_urls_desde_lista_de_strings(fake_browser):
    page, browser, _ = fake_browser
    page.textos["h2"] = "Extraído"
    contexto = {"Buscar": {"resultados": ["https://uno.test", "https://dos.test"]}}

    ok, mensaje, resultado = BrowserExecutor.ejecutar(_agente_multi(), contexto)

    assert ok is True
    assert resultado["urls_navegadas"] == 2
    assert resultado["errores"] == []
    assert [r["url"] for r in resultado["resultados_por_url"]] == [
        "https://uno.test", "https://dos.test",
    ]
    assert all(
        r["datos_extraidos"]["elementos"] == "Extraído"
        for r in resultado["resultados_por_url"]
    )
    assert browser.cerrado is True  # se cierra pese a recorrer varias URLs


def test_urls_desde_lista_de_dicts_con_href(fake_browser):
    fake_browser
    contexto = {"Buscar": {"resultados": [
        {"title": "A", "href": "https://a.test/1"},
        {"title": "B", "url": "https://b.test/2"},
        {"title": "C", "link": "https://c.test/3"},
    ]}}

    ok, _, resultado = BrowserExecutor.ejecutar(_agente_multi(), contexto)

    assert ok is True
    assert [r["url"] for r in resultado["resultados_por_url"]] == [
        "https://a.test/1", "https://b.test/2", "https://c.test/3",
    ]


def test_max_urls_limita_las_navegaciones(fake_browser):
    page, _, _ = fake_browser
    contexto = {"Buscar": {"resultados": [f"https://x.test/{i}" for i in range(10)]}}

    ok, _, resultado = BrowserExecutor.ejecutar(_agente_multi(max_urls_browser=3), contexto)

    assert ok is True
    assert resultado["urls_navegadas"] == 3
    assert len(page.llamadas_goto()) == 3


def test_urls_desde_vacio_no_falla(fake_browser):
    contexto = {"Buscar": {"resultados": []}}

    ok, _, resultado = BrowserExecutor.ejecutar(_agente_multi(), contexto)

    assert ok is True
    assert resultado["urls_navegadas"] == 0
    assert resultado["resultados_por_url"] == []
    assert resultado["errores"] == []
    assert resultado["error"] is None


def test_urls_desde_no_resoluble_da_error_claro(fake_browser):
    ok, mensaje, resultado = BrowserExecutor.ejecutar(_agente_multi(), {})

    assert ok is False
    assert resultado["error"] == "urls_desde_not_found"
    assert "no se encontró" in mensaje


def test_una_url_que_falla_no_aborta_el_resto(fake_browser):
    page, _, _ = fake_browser
    page.error_goto_urls["https://falla.test"] = RuntimeError("DNS")
    contexto = {"Buscar": {"resultados": [
        "https://ok1.test", "https://falla.test", "https://ok2.test",
    ]}}

    ok, mensaje, resultado = BrowserExecutor.ejecutar(_agente_multi(), contexto)

    assert ok is True
    assert resultado["urls_navegadas"] == 3
    assert len(resultado["errores"]) == 1
    assert resultado["errores"][0]["url"] == "https://falla.test"
    assert "DNS" in resultado["errores"][0]["error"]
    assert resultado["resultados_por_url"][2]["url"] == "https://ok2.test"


def test_todas_las_urls_fallan(fake_browser):
    page, _, _ = fake_browser
    page.error_goto_urls["https://a.test"] = RuntimeError("boom")
    contexto = {"Buscar": {"resultados": ["https://a.test"]}}

    ok, mensaje, resultado = BrowserExecutor.ejecutar(_agente_multi(), contexto)

    assert ok is False
    assert resultado["error"] == "all_urls_failed"


def test_cancelacion_entre_urls(fake_browser):
    page, _, _ = fake_browser
    token = CancellationToken()
    page.al_navegar = lambda url: token.cancelar()
    contexto = {"Buscar": {"resultados": ["https://uno.test", "https://dos.test"]}}

    ok, mensaje, resultado = BrowserExecutor.ejecutar(_agente_multi(), contexto, token)

    assert ok is False
    assert resultado["error"] == "cancelled"
    assert resultado["urls_navegadas"] == 1  # la segunda ya no se navega


# ============================================================
# EXTRACCIÓN DE TEXTO PRINCIPAL Y FALLBACK
# ============================================================

def _agente_extraer(acciones) -> Agente:
    return Agente(
        nombre="ExtraerContenido",
        tipo=TipoAgente.BROWSER,
        url_browser="https://ejemplo.test",
        acciones_browser=acciones,
    )


def test_extraer_texto_principal(fake_browser):
    page, _, _ = fake_browser
    page.texto_principal = "Titular real\n\nCuerpo de la noticia sin cookies."

    ok, _, resultado = BrowserExecutor.ejecutar(
        _agente_extraer([{"tipo": "extraer", "formato": "texto_principal",
                          "nombre": "contenido"}]), {})

    assert ok is True
    assert resultado["datos_extraidos"]["contenido"].startswith("Titular real")


def test_fallback_a_texto_principal_si_el_selector_no_encuentra(fake_browser):
    page, _, _ = fake_browser
    page.texto_principal = "Contenido principal"
    page.selectores_vacios.add("article")

    ok, _, resultado = BrowserExecutor.ejecutar(
        _agente_extraer([{"tipo": "extraer", "selector": "article",
                          "nombre": "contenido"}]), {})

    assert ok is True
    assert resultado["datos_extraidos"]["contenido"] == "Contenido principal"
    registro = resultado["acciones_ejecutadas"][0]
    assert registro["ok"] is True
    assert registro["fallback"] == "texto_principal"


def test_extraccion_vacia_marca_error_en_la_accion(fake_browser):
    """Si el selector falla Y no hay texto principal, la acción es un error."""
    page, _, _ = fake_browser
    page.texto_principal = ""
    page.selectores_vacios.add("article")

    ok, _, resultado = BrowserExecutor.ejecutar(
        _agente_extraer([{"tipo": "extraer", "selector": "article",
                          "nombre": "contenido"}]), {})

    assert ok is True  # el paso no se rompe
    assert "contenido" not in resultado["datos_extraidos"]
    assert resultado["acciones_ejecutadas"][0]["ok"] is False
    assert "no se pudo extraer" in resultado["acciones_ejecutadas"][0]["error"]


def test_espera_contenido_hasta_que_aparezca_texto(fake_browser):
    """Páginas que pintan el texto con JS: se espera antes de extraer."""
    page, _, _ = fake_browser
    largos = iter([0, 50, 300])

    def _largo():
        try:
            return next(largos)
        except StopIteration:
            return 300

    page.longitud_texto = _largo

    ok, _, resultado = BrowserExecutor.ejecutar(
        _agente_extraer([{"tipo": "extraer", "selector": "h1", "nombre": "titulo"}]), {})

    assert ok is True
    assert ("wait_for_timeout", browser_executor.ESPERA_CONTENIDO_PASO_MS) in page.llamadas
    assert "titulo" in resultado["datos_extraidos"]


def test_multi_url_avisa_si_la_extraccion_queda_vacia(fake_browser):
    page, _, _ = fake_browser
    page.texto_principal = ""
    page.selectores_vacios.add("h2")
    contexto = {"Buscar": {"resultados": ["https://a.test", "https://b.test"]}}

    ok, mensaje, resultado = BrowserExecutor.ejecutar(_agente_multi(), contexto)

    assert ok is True
    assert resultado["extraccion_vacia"] is True
    assert "AVISO" in mensaje
    assert all((r["datos_extraidos"] or {}) == {} for r in resultado["resultados_por_url"])


def test_multi_url_no_avisa_cuando_si_hay_datos(fake_browser):
    page, _, _ = fake_browser
    contexto = {"Buscar": {"resultados": ["https://a.test"]}}

    ok, mensaje, resultado = BrowserExecutor.ejecutar(_agente_multi(), contexto)

    assert ok is True
    assert resultado["extraccion_vacia"] is False
    assert "AVISO" not in mensaje
