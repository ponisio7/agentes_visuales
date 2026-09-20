# core/ia_config.py
"""Configuración persistente de la IA (API key, modelo, base URL, proxies).

Fuente única de verdad del archivo de secretos:

    ~/.config/agentes_visuales/env      (directorio 700, archivo 600)

Reglas de seguridad (inviolables):

* La API key **nunca** se guarda en SQLite, ni en logs, ni en el repositorio,
  ni se imprime. Solo se escribe en el archivo de configuración con permisos
  ``600`` dentro de un directorio ``700``.
* Los valores leídos se devuelven a quien los pida, pero este módulo jamás los
  registra en el log: solo se registran los **nombres** de las variables.

El archivo usa el formato ``export VAR="valor"`` que ya entendían
``core/llm_client`` y ``core/env_checker``.
"""
from __future__ import annotations

import logging
import os
import stat
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


# ============================================================
# RUTAS Y VARIABLES SOPORTADAS
# ============================================================

RUTA_ENV_PRINCIPAL = Path.home() / ".config" / "agentes_visuales" / "env"

# Rutas de búsqueda en orden de prioridad (compatibilidad con versiones
# anteriores). El primer archivo con al menos una variable soportada gana.
_FALLBACK_ENV_PATHS = [
    RUTA_ENV_PRINCIPAL,
    Path.home() / ".config" / "deepseek.env",
    Path.home() / ".deepseek_key",
]

VARIABLES_SOPORTADAS = frozenset({
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_MODEL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
})

# Orden en el que se escriben las variables en el archivo.
_ORDEN_ESCRITURA = (
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_MODEL",
    "DEEPSEEK_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
)

# ============================================================
# MODELOS
# ============================================================

MODELO_PRO = "deepseek-v4-pro"
MODELO_FLASH = "deepseek-v4-flash"
DEFAULT_MODEL = MODELO_PRO

# Alias aceptados en DEEPSEEK_MODEL o en la GUI.
_ALIAS_MODELOS = {
    "pro": MODELO_PRO,
    "flash": MODELO_FLASH,
    "deepseek-v4-pro": MODELO_PRO,
    "deepseek-v4-flash": MODELO_FLASH,
    "deepseek-chat": MODELO_FLASH,
    "deepseek-reasoner": MODELO_PRO,
}

ETIQUETAS_MODELO = {
    MODELO_PRO: "Pro",
    MODELO_FLASH: "Flash",
}


def normalizar_modelo(nombre: str | None) -> str:
    """Devuelve el nombre canónico del modelo ('pro'/'flash'/alias)."""
    if not nombre or not str(nombre).strip():
        return DEFAULT_MODEL
    clave = str(nombre).strip().lower()
    return _ALIAS_MODELOS.get(clave, str(nombre).strip())


def es_modelo_pro(nombre: str | None) -> bool:
    return normalizar_modelo(nombre) == MODELO_PRO


# ============================================================
# LECTURA DEL ARCHIVO
# ============================================================

def parsear_linea_env(linea: str) -> tuple[str, str] | None:
    """Parsea ``export VAR="valor"`` o ``VAR=valor``. ``None`` si no aplica."""
    linea = linea.strip()
    if not linea or linea.startswith("#"):
        return None

    if linea.startswith("export "):
        linea = linea[7:].strip()

    if "=" not in linea:
        return None

    clave, _, valor = linea.partition("=")
    clave = clave.strip()
    valor = valor.strip()

    # Comentario al final (solo si va precedido de espacio).
    if " #" in valor and not valor.startswith("#"):
        valor = valor.split(" #", 1)[0].strip()

    if len(valor) >= 2:
        if (valor[0] == '"' and valor[-1] == '"') or \
           (valor[0] == "'" and valor[-1] == "'"):
            valor = valor[1:-1]

    return clave, valor


def _verificar_permisos_archivo(path: Path) -> None:
    """Avisa (sin exponer valores) si el archivo es demasiado abierto."""
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            logger.warning(
                f"⚠️ {path} tiene permisos demasiado abiertos "
                f"({oct(mode & 0o777)}). Recomendado: chmod 600"
            )
    except OSError as e:
        logger.debug(f"No se pudieron verificar permisos de {path}: {e}")


def cargar_entorno_desde_archivos() -> dict[str, str]:
    """Variables soportadas del primer archivo de configuración con datos.

    No registra valores en el log: solo los nombres de las variables cargadas.
    """
    for path in _FALLBACK_ENV_PATHS:
        if not path.exists() or not path.is_file():
            continue

        _verificar_permisos_archivo(path)

        encontradas: dict[str, str] = {}
        try:
            with open(path, encoding="utf-8") as f:
                for num_linea, linea in enumerate(f, 1):
                    resultado = parsear_linea_env(linea)
                    if resultado is None:
                        continue
                    clave, valor = resultado
                    if clave in VARIABLES_SOPORTADAS and valor:
                        encontradas[clave] = valor
                    elif clave in VARIABLES_SOPORTADAS:
                        logger.warning(f"⚠️ {path}:{num_linea} — '{clave}' está vacía")
        except OSError as e:
            logger.warning(f"No se pudo leer {path}: {e}")
            continue

        if encontradas:
            logger.debug(
                f"✅ Variables cargadas desde {path}: {sorted(encontradas.keys())}"
            )
            return encontradas

    return {}


def modelo_por_defecto() -> str:
    """Modelo configurado: variable de entorno > archivo > DEFAULT_MODEL."""
    valor = os.environ.get("DEEPSEEK_MODEL")
    if valor and valor.strip():
        return normalizar_modelo(valor)
    archivo = cargar_entorno_desde_archivos().get("DEEPSEEK_MODEL")
    if archivo and archivo.strip():
        return normalizar_modelo(archivo)
    return DEFAULT_MODEL


def resolver_api_key(
    api_key: str | None = None,
    env_file: dict[str, str] | None = None,
) -> tuple[str | None, str | None]:
    """Resuelve la API key y su origen: arg > entorno > archivo.

    ``env_file`` permite inyectar el resultado de
    ``cargar_entorno_desde_archivos`` (lo usa ``LLMClient`` para que los tests
    puedan neutralizar el archivo real). Si es ``None`` se lee el archivo.

    Devuelve ``(key, origen)``. Nunca registra la key.
    """
    if api_key:
        return api_key, "argumento"

    key_env = os.environ.get("DEEPSEEK_API_KEY")
    if key_env:
        return key_env, "variable de entorno"

    datos = env_file if env_file is not None else cargar_entorno_desde_archivos()
    key_archivo = datos.get("DEEPSEEK_API_KEY")
    if key_archivo:
        return key_archivo, "archivo de configuración"

    return None, None


def enmascarar_key(key: str | None) -> str:
    """Representación segura de una key (solo extremos)."""
    if not key:
        return ""
    if len(key) <= 12:
        return key[:4] + "…"
    return f"{key[:8]}…{key[-4:]}"


def leer_configuracion() -> dict:
    """Configuración efectiva, con la key ya enmascarada.

    Pensado para la GUI/CLI: es seguro mostrarlo o loguearlo porque no
    contiene la API key completa.
    """
    key_env = os.environ.get("DEEPSEEK_API_KEY")
    archivo = cargar_entorno_desde_archivos()

    key, origen = resolver_api_key()
    return {
        "api_key_enmascarada": enmascarar_key(key),
        "api_key_configurada": bool(key),
        "api_key_origen": origen,
        "api_key_en_entorno": bool(key_env),
        "modelo": modelo_por_defecto(),
        "base_url": (
            os.environ.get("DEEPSEEK_BASE_URL")
            or archivo.get("DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com"
        ),
        "http_proxy": os.environ.get("HTTP_PROXY") or archivo.get("HTTP_PROXY") or "",
        "https_proxy": os.environ.get("HTTPS_PROXY") or archivo.get("HTTPS_PROXY") or "",
        "no_proxy": os.environ.get("NO_PROXY") or archivo.get("NO_PROXY") or "",
        "ruta_archivo": str(RUTA_ENV_PRINCIPAL),
    }


# ============================================================
# ESCRITURA SEGURA DEL ARCHIVO
# ============================================================

def _asegurar_directorio(directorio: Path) -> None:
    """Crea el directorio con permisos 700."""
    directorio.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directorio, 0o700)
    except OSError as e:
        logger.warning(f"No se pudieron fijar permisos 700 en {directorio}: {e}")


def _escribir_lineas(path: Path, lineas: list[str]) -> None:
    """Escritura atómica con permisos 600 (temp en el mismo directorio)."""
    _asegurar_directorio(path.parent)

    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("\n".join(lineas) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _formatear_linea(clave: str, valor: str) -> str:
    escapado = str(valor).replace("\\", "\\\\").replace('"', '\\"')
    return f'export {clave}="{escapado}"'


def guardar_configuracion(
    *,
    api_key: str | None = None,
    modelo: str | None = None,
    base_url: str | None = None,
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
) -> dict:
    """Guarda la configuración en ``~/.config/agentes_visuales/env``.

    - Los valores ``None`` no se escriben (se conservan los actuales).
    - Un valor vacío ("") **elimina** la variable.
    - El archivo queda en ``600`` y su directorio en ``700``.
    - ``api_key`` vacía elimina la clave; no se escribe nunca en claro en logs.

    Devuelve la configuración efectiva resultante (con la key enmascarada).
    """
    actuales = cargar_entorno_desde_archivos()

    entrantes = {
        "DEEPSEEK_API_KEY": api_key,
        "DEEPSEEK_MODEL": normalizar_modelo(modelo) if modelo else None,
        "DEEPSEEK_BASE_URL": base_url,
        "HTTP_PROXY": http_proxy,
        "HTTPS_PROXY": https_proxy,
        "NO_PROXY": no_proxy,
    }

    for clave, valor in entrantes.items():
        if valor is None:
            continue
        valor = str(valor).strip()
        if valor == "":
            actuales.pop(clave, None)
        else:
            actuales[clave] = valor

    lineas = [
        "# Configuración de Agentes Visuales — generada por la GUI.",
        "# NO compartir este archivo: contiene la API key.",
    ]
    for clave in _ORDEN_ESCRITURA:
        valor = actuales.get(clave)
        if valor:
            lineas.append(_formatear_linea(clave, valor))

    _escribir_lineas(RUTA_ENV_PRINCIPAL, lineas)
    logger.info(f"🔐 Configuración de IA guardada en {RUTA_ENV_PRINCIPAL}")

    # Reflejar en el entorno del proceso para que el cliente lo vea sin reiniciar.
    for clave, valor in actuales.items():
        if clave in VARIABLES_SOPORTADAS:
            os.environ[clave] = valor
    for clave, valor in entrantes.items():
        if valor is not None and str(valor).strip() == "":
            os.environ.pop(clave, None)

    return leer_configuracion()


def borrar_api_key() -> dict:
    """Elimina solo la API key del archivo, conservando el resto."""
    return guardar_configuracion(api_key="")


# ============================================================
# PRUEBA DE CONEXIÓN
# ============================================================

def probar_conexion(
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """Prueba la conexión autenticada contra ``{base_url}/v1/models``.

    Devuelve ``(ok, mensaje)``. No registra la key ni la respuesta cruda.
    """
    key, _origen = resolver_api_key(api_key)
    if not key:
        return False, "No hay API key configurada."

    url_base = (base_url or leer_configuracion()["base_url"]).rstrip("/")
    # Acepta tanto la raíz del proveedor (https://api.deepseek.com) como una
    # base ya versionada (http://localhost:8000/v1) de un servidor local.
    if url_base.endswith("/v1"):
        url = f"{url_base}/models"
    else:
        url = f"{url_base}/v1/models"

    try:
        import requests
    except ImportError:
        return False, "El módulo 'requests' no está instalado."

    try:
        resp = requests.get(
            url,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=timeout,
        )
    except requests.exceptions.Timeout:
        return False, f"Timeout tras {timeout:.0f}s conectando a {url}"
    except requests.exceptions.SSLError as e:
        return False, f"Error SSL: {str(e)[:120]}"
    except requests.exceptions.ProxyError as e:
        return False, f"Error de proxy: {str(e)[:120]}"
    except requests.exceptions.RequestException as e:
        return False, f"No se pudo conectar: {str(e)[:120]}"

    if 200 <= resp.status_code < 300:
        return True, f"Conexión correcta (HTTP {resp.status_code})."
    if resp.status_code in (401, 403):
        return False, f"La key fue rechazada (HTTP {resp.status_code})."
    if resp.status_code == 404:
        # Algunos proveedores compatibles no exponen /v1/models: la red y la
        # autenticación no se pueden confirmar, pero tampoco negar.
        return True, "Servidor alcanzado (HTTP 404 en /v1/models)."
    if resp.status_code == 429:
        return False, "Rate limit excedido (HTTP 429)."
    if resp.status_code >= 500:
        return False, f"El servidor respondió con error {resp.status_code}."
    return False, f"HTTP {resp.status_code} inesperado."
