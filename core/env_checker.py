# core/env_checker.py
"""
Diagnóstico del entorno de configuración de IA.

Verifica:
  1. De dónde se carga la API key (argumento / env / archivo).
  2. Permisos del archivo de secretos.
  3. Conectividad HTTP real contra https://api.deepseek.com.
  4. Autenticación con la API key cargada.

Uso:
    from core.env_checker import ejecutar_check_env
    codigo_salida = ejecutar_check_env()
"""

import os
import platform
import stat
import sys
import threading
import time
from typing import Any

logger_name = __name__


# ============================================================
# COLORES ANSI (con detección de TTY)
# ============================================================

class _Colores:
    """Colores ANSI, deshabilitados si no es TTY o NO_COLOR está definido."""

    def __init__(self, habilitado: bool):
        self.habilitado = habilitado

    def _wrap(self, codigo: str, texto: str) -> str:
        if not self.habilitado:
            return texto
        return f"\033[{codigo}m{texto}\033[0m"

    def verde(self, t):    return self._wrap("32", t)
    def rojo(self, t):     return self._wrap("31", t)
    def amarillo(self, t): return self._wrap("33", t)
    def azul(self, t):     return self._wrap("34", t)
    def magenta(self, t):  return self._wrap("35", t)
    def cyan(self, t):     return self._wrap("36", t)
    def gris(self, t):     return self._wrap("90", t)
    def negrita(self, t):  return self._wrap("1", t)


def _detectar_colores() -> _Colores:
    """
    Detecta si debemos usar colores.
    - Windows moderno: habilita VT processing.
    - NO_COLOR env var: deshabilita.
    - stdout no es TTY: deshabilita.
    """
    if os.environ.get("NO_COLOR"):
        return _Colores(False)

    if not sys.stdout.isatty():
        return _Colores(False)

    if platform.system() == "Windows":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            return _Colores(False)

    return _Colores(True)


# ============================================================
# CÓDIGOS DE SALIDA
# ============================================================

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
EXIT_NETWORK_ERROR = 2
EXIT_UNEXPECTED = 3


# ============================================================
# HELPERS DE PRESENTACIÓN
# ============================================================

def _titulo(c: _Colores, texto: str) -> str:
    linea = "═" * 60
    return f"\n{c.cyan(linea)}\n{c.negrita(c.cyan(texto))}\n{c.cyan(linea)}"


def _seccion(c: _Colores, texto: str) -> str:
    return f"\n{c.negrita(c.azul('▸ ' + texto))}"


def _ok(c: _Colores, texto: str) -> str:
    return f"  {c.verde('✅')} {texto}"


def _fail(c: _Colores, texto: str) -> str:
    return f"  {c.rojo('❌')} {texto}"


def _warn(c: _Colores, texto: str) -> str:
    return f"  {c.amarillo('⚠️ ')} {texto}"


def _info(c: _Colores, texto: str) -> str:
    return f"  {c.gris('ℹ️ ')} {texto}"


def _bullet(c: _Colores, texto: str) -> str:
    return f"    {c.gris('•')} {texto}"


# ============================================================
# VERIFICACIONES INDIVIDUALES
# ============================================================

def _verificar_api_key(c: _Colores) -> tuple[str | None, str | None, list[str]]:
    """
    Determina de dónde viene la API key.

    Returns:
        (api_key, origen, mensajes) donde origen ∈ {'env', 'archivo', None}
    """
    mensajes: list[str] = []

    # 1. Variable de entorno
    key_env = os.environ.get("DEEPSEEK_API_KEY")
    if key_env:
        return key_env, "env", mensajes

    # 2. Archivo de fallback
    from core.llm_client import _FALLBACK_ENV_PATHS, _parsear_linea_env

    for path in _FALLBACK_ENV_PATHS:
        if not path.exists() or not path.is_file():
            continue

        # Verificar permisos
        try:
            mode = path.stat().st_mode
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                permisos = oct(mode & 0o777)
                mensajes.append(
                    f"Archivo {path} tiene permisos {permisos} "
                    f"(recomendado 600)"
                )
        except OSError:
            pass

        # Intentar leer la key
        try:
            with open(path, encoding="utf-8") as f:
                for linea in f:
                    resultado = _parsear_linea_env(linea)
                    if resultado is None:
                        continue
                    clave, valor = resultado
                    if clave == "DEEPSEEK_API_KEY":
                        if valor:
                            return valor, "archivo", mensajes
                        else:
                            mensajes.append(f"{path}: DEEPSEEK_API_KEY está vacía")
        except OSError as e:
            mensajes.append(f"No se pudo leer {path}: {e}")

    return None, None, mensajes


def _enmascarar_key(key: str) -> str:
    """Muestra solo los primeros 8 y últimos 4 caracteres."""
    if len(key) <= 12:
        return key[:4] + "…"
    return f"{key[:8]}…{key[-4:]}"


def _verificar_proxies(c: _Colores) -> list[str]:
    """Devuelve las variables de proxy configuradas."""
    proxies = []
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        valor = os.environ.get(var) or os.environ.get(var.lower())
        if valor:
            proxies.append(f"{var}={valor}")
    return proxies


def _ping_http_deepseek(
    c: _Colores,
    api_key: str,
    timeout: float = 5.0
) -> tuple[bool, str, dict[str, Any]]:
    """Ping con presupuesto GLOBAL de reloj real (ver 3.11).

    El ``timeout`` que se pasa a ``requests`` solo acota las operaciones de
    socket (connect + read). La **resolución DNS** queda fuera y puede bloquear
    ~20 s con el resolver del sistema, así que ``--timeout 2`` tardaba >20 s.

    Un ``signal.setitimer`` tampoco lo resuelve: el handler de Python no corre
    mientras ``getaddrinfo`` está bloqueado en C (se midió: 17 s). Por eso el
    ping se ejecuta en un **hilo del que se puede desistir**: se espera como
    mucho ``timeout`` segundos y, si no ha terminado, se informa del timeout y
    se sigue. El hilo es daemon: el proceso de diagnóstico termina igualmente.
    """
    try:
        limite = float(timeout)
    except (TypeError, ValueError):
        limite = 0.0

    if limite <= 0:
        # Se deja pasar el valor tal cual a requests para que lance su
        # ValueError; hay un test de regresión que depende de ese camino.
        return _ping_http_deepseek_interno(c, api_key, timeout=timeout)

    resultado: list[tuple[bool, str, dict[str, Any]]] = []
    fallo: list[BaseException] = []

    def _trabajo():
        try:
            resultado.append(
                _ping_http_deepseek_interno(c, api_key, timeout=limite)
            )
        except BaseException as e:  # se re-lanza en el hilo principal
            fallo.append(e)

    hilo = threading.Thread(target=_trabajo, name="check-env-ping", daemon=True)
    hilo.start()
    hilo.join(limite)

    if hilo.is_alive():
        return (
            False,
            f"Presupuesto de {limite:.0f}s agotado: el diagnóstico no puede "
            f"exceder ese tiempo (¿red o DNS lentos?)",
            {"error_tipo": "timeout"},
        )

    if fallo:
        raise fallo[0]

    if not resultado:
        return False, "El ping no devolvió resultado", {"error_tipo": "desconocido"}

    return resultado[0]


def _ping_http_deepseek_interno(
    c: _Colores,
    api_key: str,
    timeout: float = 5.0
) -> tuple[bool, str, dict[str, Any]]:
    """
    Hace un ping HTTP real a la API de DeepSeek.

    Intenta primero /v1/models (requiere auth). Si falla por 401/403,
    la red funciona pero la key es inválida. Si falla por timeout/DNS,
    es problema de red.

    Returns:
        (exito, mensaje, detalles) donde detalles puede tener:
          - status_code
          - latency_ms
          - endpoint
          - error_tipo
    """
    detalles: dict[str, Any] = {}

    try:
        import requests
    except ImportError:
        return False, "El módulo 'requests' no está instalado", {"error_tipo": "import"}

    # Endpoint ligero y autenticado
    endpoints = [
        ("https://api.deepseek.com/v1/models", True),   # requiere auth
        ("https://api.deepseek.com/", False),            # solo para probar conectividad
    ]

    headers = {
        "Authorization": f"Bearer {api_key}",
        "User-Agent": "agentes-visuales/1.0 (env-check)",
        "Accept": "application/json",
    }

    ultimo_error = None

    # 3.11: ``timeout`` de requests es POR OPERACIÓN (connect + read), no acota
    # el comando. Con dos endpoints y redirecciones, ``--timeout 5`` podía
    # tardar >10 s (se midió 10949 ms con el endpoint en 200). Aquí se impone un
    # presupuesto GLOBAL: cada petición recibe solo el tiempo que queda y, si se
    # agota, no se prueba el siguiente endpoint.
    try:
        presupuesto = float(timeout)
    except (TypeError, ValueError):
        presupuesto = 0.0
    # Con ``timeout <= 0`` se deja pasar el valor a requests para que lance su
    # ValueError; hay un test de regresión que depende de ese camino.
    deadline = time.monotonic() + presupuesto if presupuesto > 0 else None

    for url, requiere_auth in endpoints:
        detalles["endpoint"] = url

        if deadline is None:
            restante = timeout
        else:
            restante = deadline - time.monotonic()
            if restante <= 0:
                detalles["error_tipo"] = "timeout"
                return (
                    False,
                    f"Presupuesto de {presupuesto:.0f}s agotado antes de {url}",
                    detalles,
                )

        inicio = time.time()

        try:
            # ``allow_redirects=False``: seguir una cadena de redirecciones
            # multiplicaría el gasto del presupuesto (cada salto es otra
            # petición). Una redirección ya demuestra que la red funciona, así
            # que se trata como éxito más abajo.
            resp = requests.get(
                url,
                headers=headers if requiere_auth else {},
                timeout=restante,
                allow_redirects=False,
            )
            latencia_ms = (time.time() - inicio) * 1000
            detalles["status_code"] = resp.status_code
            detalles["latency_ms"] = latencia_ms

            # ── 2xx: todo OK ──
            if 200 <= resp.status_code < 300:
                return True, f"Respuesta {resp.status_code} en {latencia_ms:.0f} ms", detalles

            # ── 3xx: la red funciona (no se siguen redirecciones para no salir
            #         del presupuesto global; ver 3.11) ──
            if 300 <= resp.status_code < 400:
                return (
                    True,
                    f"Respuesta {resp.status_code} en {latencia_ms:.0f} ms (redirección)",
                    detalles,
                )

            # ── 401/403: la red funciona pero la key es inválida ──
            if resp.status_code in (401, 403):
                detalles["error_tipo"] = "auth"
                return (
                    False,
                    f"La key fue rechazada (HTTP {resp.status_code}) — red OK, key inválida",
                    detalles,
                )

            # ── 404 en /v1/models: algunos providers no lo exponen. Probamos el root. ──
            if resp.status_code == 404:
                ultimo_error = f"HTTP 404 en {url}"
                continue

            # ── 429: rate limit ──
            if resp.status_code == 429:
                detalles["error_tipo"] = "rate_limit"
                return False, "Rate limit excedido (HTTP 429)", detalles

            # ── 5xx: el servidor tiene problemas, pero la red funciona ──
            if resp.status_code >= 500:
                detalles["error_tipo"] = "server"
                return (
                    False,
                    f"El servidor respondió con error {resp.status_code}",
                    detalles,
                )

            # ── Otros ──
            ultimo_error = f"HTTP {resp.status_code} inesperado en {url}"
            continue

        except requests.exceptions.Timeout:
            detalles["error_tipo"] = "timeout"
            return False, f"Timeout tras {timeout}s conectando a {url}", detalles

        except requests.exceptions.SSLError as e:
            detalles["error_tipo"] = "ssl"
            return False, f"Error SSL en {url}: {str(e)[:120]}", detalles

        except requests.exceptions.ProxyError as e:
            detalles["error_tipo"] = "proxy"
            return False, f"Error de proxy: {str(e)[:120]}", detalles

        except requests.exceptions.ConnectionError as e:
            detalles["error_tipo"] = "connection"
            return False, f"No se pudo conectar a {url}: {str(e)[:120]}", detalles

        except ValueError as e:
            # p. ej. timeout <= 0: requests lanza ValueError, que NO hereda
            # de RequestException y escaparía hasta main().
            detalles["error_tipo"] = "valor_invalido"
            return False, f"Parámetro inválido en {url}: {str(e)[:120]}", detalles

        except requests.exceptions.RequestException as e:
            detalles["error_tipo"] = "request"
            return False, f"Error de petición: {str(e)[:120]}", detalles

    return False, ultimo_error or "Sin respuesta válida de ningún endpoint", detalles


# ============================================================
# FUNCIÓN PRINCIPAL
# ============================================================

def ejecutar_check_env(timeout: float = 5.0) -> int:
    """
    Ejecuta el diagnóstico completo y devuelve un código de salida:

        0 → Todo OK
        1 → Error de configuración (falta key, key vacía, etc.)
        2 → Error de red (timeout, DNS, proxy)
        3 → Error inesperado
    """
    c = _detectar_colores()

    print(_titulo(c, "🔍 Diagnóstico de entorno — Agentes Visuales"))

    # ── 1. Información del sistema ──
    print(_seccion(c, "Sistema"))
    print(_info(c, f"Python: {sys.version.split()[0]}"))
    print(_info(c, f"Plataforma: {platform.platform()}"))
    print(_info(c, f"Ejecutable: {sys.executable}"))
    print(_info(c, f"Directorio: {os.getcwd()}"))

    # ── 2. API key ──
    print(_seccion(c, "API key de DeepSeek"))

    key, origen, mensajes = _verificar_api_key(c)

    if not key:
        print(_fail(c, "No se encontró DEEPSEEK_API_KEY en ninguna fuente"))
        for m in mensajes:
            print(_warn(c, m))
        print()
        print(_info(c, "Cómo solucionarlo:"))
        print(_bullet(c, "Opción A — variable de entorno (temporal):"))
        print(_bullet(c, "    export DEEPSEEK_API_KEY=\"sk-tu-key\""))
        print(_bullet(c, "Opción B — archivo persistente (recomendado):"))
        print(_bullet(c, "    mkdir -p ~/.config/agentes_visuales"))
        print(_bullet(c, "    echo 'export DEEPSEEK_API_KEY=\"sk-tu-key\"' \\"))
        print(_bullet(c, "        > ~/.config/agentes_visuales/env"))
        print(_bullet(c, "    chmod 600 ~/.config/agentes_visuales/env"))
        print()
        print(c.rojo(c.negrita("RESULTADO: ❌ Error de configuración")))
        return EXIT_CONFIG_ERROR

    # Key encontrada
    print(_ok(c, f"API key encontrada: {c.negrita(_enmascarar_key(key))}"))
    print(_info(c, f"Origen: {origen}"))

    # Advertencias sobre permisos, etc.
    for m in mensajes:
        print(_warn(c, m))

    # Validación de formato mínima
    if not key.startswith("sk-"):
        print(_warn(c, "La key no empieza por 'sk-'. ¿Es correcta?"))

    # ── 2b. Modelo configurado (H1) ──
    print(_seccion(c, "Modelo"))
    try:
        from core.ia_config import ETIQUETAS_MODELO, modelo_por_defecto

        modelo = modelo_por_defecto()
        etiqueta = ETIQUETAS_MODELO.get(modelo, "")
        print(_ok(c, f"Modelo: {modelo}" + (f" ({etiqueta})" if etiqueta else "")))
        print(_info(c, "Se cambia con DEEPSEEK_MODEL o con ⚙ Configuración en la GUI"))
    except Exception as e:
        print(_warn(c, f"No se pudo resolver el modelo: {e}"))

    # ── 3. Proxies ──
    print(_seccion(c, "Configuración de red"))
    proxies = _verificar_proxies(c)
    if proxies:
        for p in proxies:
            print(_info(c, f"Proxy: {p}"))
    else:
        print(_info(c, "Sin proxies configurados (conexión directa)"))

    # ── 4. Ping HTTP real ──
    print(_seccion(c, f"Ping a api.deepseek.com (timeout {timeout:.0f}s)"))

    exito, mensaje, detalles = _ping_http_deepseek(c, key, timeout=timeout)

    if exito:
        print(_ok(c, mensaje))
        if detalles.get("endpoint"):
            print(_bullet(c, f"Endpoint: {detalles['endpoint']}"))
        if detalles.get("status_code"):
            print(_bullet(c, f"Status: {detalles['status_code']}"))
        if detalles.get("latency_ms"):
            print(_bullet(c, f"Latencia: {detalles['latency_ms']:.0f} ms"))
        print()
        print(c.verde(c.negrita("RESULTADO: ✅ Todo OK")))
        return EXIT_OK

    # Falló el ping: distinguir red vs auth
    error_tipo = detalles.get("error_tipo", "desconocido")

    print(_fail(c, mensaje))
    if detalles.get("endpoint"):
        print(_bullet(c, f"Endpoint: {detalles['endpoint']}"))
    if detalles.get("status_code"):
        print(_bullet(c, f"Status: {detalles['status_code']}"))

    print()

    if error_tipo == "auth":
        print(_info(c, "Diagnóstico: la red funciona, pero la API key no es válida."))
        print(_info(c, "Sugerencias:"))
        print(_bullet(c, "Verifica que la key no tenga espacios ni comillas extra."))
        print(_bullet(c, "Regenera la key en https://platform.deepseek.com/api_keys"))
        print()
        print(c.rojo(c.negrita("RESULTADO: ❌ Error de configuración (key inválida)")))
        return EXIT_CONFIG_ERROR

    if error_tipo in ("timeout", "connection", "ssl", "proxy"):
        print(_info(c, "Diagnóstico: problema de red."))
        print(_info(c, "Sugerencias:"))
        print(_bullet(c, "Comprueba tu conexión a internet."))
        print(_bullet(c, "Verifica si estás detrás de un proxy/firewall."))
        print(_bullet(c, "Prueba: curl -I https://api.deepseek.com"))
        print(_bullet(c, "Aumenta el timeout: agentes_visuales --check-env --timeout 15"))
        print()
        print(c.rojo(c.negrita("RESULTADO: ❌ Error de red")))
        return EXIT_NETWORK_ERROR

    if error_tipo == "server":
        print(_info(c, "Diagnóstico: DeepSeek está teniendo problemas."))
        print(_bullet(c, "Revisa https://status.deepseek.com si existe."))
        print()
        print(c.amarillo(c.negrita("RESULTADO: ⚠️  Error del servidor remoto")))
        return EXIT_NETWORK_ERROR

    print(_info(c, f"Diagnóstico: error desconocido ({error_tipo})."))
    print()
    print(c.rojo(c.negrita("RESULTADO: ❌ Error inesperado")))
    return EXIT_UNEXPECTED
