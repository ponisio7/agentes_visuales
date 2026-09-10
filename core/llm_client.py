# core/llm_client.py
"""
Cliente LLM para consultas a DeepSeek usando la API oficial.
Documentación: https://api-docs.deepseek.com/

CARACTERÍSTICAS:
- ✅ Configuración correcta de DeepSeek (stream=False, reasoning_effort, thinking)
- ✅ Reintentos automáticos con modelos alternativos
- ✅ Logging detallado para depuración
- ✅ Manejo robusto de errores
- ✅ Soporte para diferentes niveles de razonamiento
- ✅ Timeouts configurables
- ✅ Compatible con la API OpenAI
- ✅ NUEVO: Carga la API key desde archivos de configuración de fallback
        (~/.config/agentes_visuales/env, ~/.config/deepseek.env, ~/.deepseek_key)
        para que funcione igual desde terminal, menú, cron, systemd, etc.
"""

import os
import stat
import logging
import time
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
import json

# Intentar importar OpenAI
try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

logger = logging.getLogger(__name__)


# ============================================================
# ✅ CARGA DE ENTORNO DESDE ARCHIVOS DE CONFIGURACIÓN
# ============================================================

# Rutas de búsqueda en orden de prioridad (el primer archivo con
# al menos una variable soportada gana).
_FALLBACK_ENV_PATHS = [
    Path.home() / ".config" / "agentes_visuales" / "env",
    Path.home() / ".config" / "deepseek.env",
    Path.home() / ".deepseek_key",
]

# Variables reconocidas en el archivo
_VARIABLES_SOPORTADAS = frozenset({
    "DEEPSEEK_API_KEY",
    "DEEPSEEK_BASE_URL",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
})


def _parsear_linea_env(linea: str) -> Optional[Tuple[str, str]]:
    """
    Parsea una línea tipo 'export VAR="valor"' o 'VAR=valor'.
    Devuelve (clave, valor) o None si no es una asignación válida.
    """
    linea = linea.strip()
    if not linea or linea.startswith("#"):
        return None

    # Quitar 'export ' si está
    if linea.startswith("export "):
        linea = linea[7:].strip()

    if "=" not in linea:
        return None

    clave, _, valor = linea.partition("=")
    clave = clave.strip()
    valor = valor.strip()

    # Quitar comentarios al final del valor (solo si van precedidos de espacio)
    if " #" in valor and not valor.startswith("#"):
        valor = valor.split(" #", 1)[0].strip()

    # Quitar comillas envolventes
    if len(valor) >= 2:
        if (valor[0] == '"' and valor[-1] == '"') or \
           (valor[0] == "'" and valor[-1] == "'"):
            valor = valor[1:-1]

    return clave, valor


def _verificar_permisos_archivo(path: Path) -> None:
    """Advierte si un archivo de secretos tiene permisos demasiado abiertos."""
    try:
        mode = path.stat().st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            permisos = oct(mode & 0o777)
            logger.warning(
                f"⚠️ {path} tiene permisos demasiado abiertos ({permisos}). "
                f"Recomendado: chmod 600 {path}"
            )
    except OSError as e:
        logger.debug(f"No se pudieron verificar permisos de {path}: {e}")


def cargar_entorno_desde_archivos() -> Dict[str, str]:
    """
    Carga variables de entorno desde archivos de configuración de fallback.

    Busca en rutas comunes (~/.config/agentes_visuales/env, etc.) y devuelve
    un dict con las variables encontradas. Solo considera variables declaradas
    en _VARIABLES_SOPORTADAS.

    La búsqueda para en el primer archivo que contenga al menos una variable
    soportada, para evitar que un archivo antiguo sobrescriba a uno nuevo.

    Returns:
        Dict[str, str]: Variables encontradas (puede estar vacío).
    """
    for path in _FALLBACK_ENV_PATHS:
        if not path.exists() or not path.is_file():
            continue

        _verificar_permisos_archivo(path)

        encontradas: Dict[str, str] = {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                for num_linea, linea in enumerate(f, 1):
                    resultado = _parsear_linea_env(linea)
                    if resultado is None:
                        continue
                    clave, valor = resultado
                    if clave in _VARIABLES_SOPORTADAS and valor:
                        encontradas[clave] = valor
                    elif clave in _VARIABLES_SOPORTADAS and not valor:
                        logger.warning(
                            f"⚠️ {path}:{num_linea} — '{clave}' está vacía"
                        )
        except OSError as e:
            logger.warning(f"No se pudo leer {path}: {e}")
            continue

        if encontradas:
            logger.debug(
                f"✅ Variables cargadas desde {path}: {sorted(encontradas.keys())}"
            )
            return encontradas

    return {}


# ============================================================
# CONSTANTES
# ============================================================

DEFAULT_MODEL = "deepseek-v4-pro"
FALLBACK_MODELS = ["deepseek-v4-flash"]
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_TIMEOUT = 120.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_REASONING_EFFORT = "high"
DEFAULT_THINKING_ENABLED = True
VALID_REASONING_EFFORTS = {"low", "medium", "high"}


# ============================================================
# EXCEPCIONES PERSONALIZADAS
# ============================================================

class LLMError(Exception):
    """Excepción base para errores del LLM."""
    pass


class LLMConnectionError(LLMError):
    """Error de conexión con el LLM."""
    pass


class LLMResponseError(LLMError):
    """Error en la respuesta del LLM."""
    pass


class LLMConfigurationError(LLMError):
    """Error de configuración del LLM."""
    pass


# ============================================================
# CLASE PRINCIPAL: LLMClient
# ============================================================

class LLMClient:
    """
    Cliente para interactuar con modelos LLM de DeepSeek.

    Atributos:
        api_key: API key de DeepSeek
        base_url: URL base de la API
        default_model: Modelo por defecto
        timeout: Timeout en segundos para las peticiones
        max_retries: Número máximo de reintentos
        reasoning_effort: Nivel de razonamiento por defecto
        thinking_enabled: Si el modo thinking está habilitado por defecto
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
        default_model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        thinking_enabled: bool = DEFAULT_THINKING_ENABLED
    ):
        """
        Inicializa el cliente LLM de DeepSeek.

        Args:
            api_key: API key de DeepSeek (si no se proporciona, usa DEEPSEEK_API_KEY
                     o el archivo de fallback)
            base_url: URL base de la API
            default_model: Modelo por defecto
            timeout: Timeout en segundos
            max_retries: Número máximo de reintentos
            reasoning_effort: Nivel de razonamiento ("low", "medium", "high")
            thinking_enabled: Si el modo thinking está habilitado

        Raises:
            LLMConfigurationError: Si openai no está instalado o no hay API key
        """
        # Verificar que OpenAI está instalado
        if OpenAI is None:
            raise LLMConfigurationError(
                "openai no instalado. Ejecuta: pip install openai"
            )

        # ── 1. Cargar entorno de fallback ──
        env_file = cargar_entorno_desde_archivos()

        # ── 2. Resolver API key: arg > env var > archivo ──
        origen_key = "argumento"
        self.api_key = api_key

        if not self.api_key:
            self.api_key = os.environ.get("DEEPSEEK_API_KEY")
            if self.api_key:
                origen_key = "variable de entorno"

        if not self.api_key:
            self.api_key = env_file.get("DEEPSEEK_API_KEY")
            if self.api_key:
                origen_key = "archivo de configuración"

        if not self.api_key:
            raise LLMConfigurationError(
                "No se encontró DEEPSEEK_API_KEY. Opciones:\n"
                "  1. Exportar la variable de entorno:\n"
                "       export DEEPSEEK_API_KEY='tu-api-key'\n"
                "  2. Crear ~/.config/agentes_visuales/env con:\n"
                "       export DEEPSEEK_API_KEY=\"tu-api-key\"\n"
                "     y darle permisos: chmod 600 ~/.config/agentes_visuales/env"
            )

        # ── 3. Resolver base_url: arg > env var > archivo > default ──
        self.base_url = (
            base_url
            or os.environ.get("DEEPSEEK_BASE_URL")
            or env_file.get("DEEPSEEK_BASE_URL")
            or DEFAULT_BASE_URL
        )

        # ── 4. Aplicar proxies al entorno del proceso si vienen del archivo ──
        #    La librería requests (usada por openai) lee HTTP_PROXY/HTTPS_PROXY
        #    del entorno, no de argumentos.
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
            valor = env_file.get(var)
            if valor and not os.environ.get(var):
                os.environ[var] = valor
                logger.debug(f"Proxy aplicado desde archivo: {var}")

        # ── 5. Resto de la configuración ──
        self.default_model = default_model
        self.timeout = timeout
        self.max_retries = max_retries

        # Validar reasoning_effort
        if reasoning_effort not in VALID_REASONING_EFFORTS:
            logger.warning(
                f"⚠️ reasoning_effort '{reasoning_effort}' no válido. "
                f"Usando '{DEFAULT_REASONING_EFFORT}'"
            )
            reasoning_effort = DEFAULT_REASONING_EFFORT
        self.reasoning_effort = reasoning_effort
        self.thinking_enabled = thinking_enabled

        # ── 6. Crear cliente OpenAI ──
        try:
            self._client = OpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                max_retries=self.max_retries
            )
            logger.info(
                f"✅ Cliente DeepSeek inicializado\n"
                f"   Modelo: {self.default_model}\n"
                f"   Base URL: {self.base_url}\n"
                f"   Reasoning: {self.reasoning_effort}\n"
                f"   Thinking: {'enabled' if self.thinking_enabled else 'disabled'}\n"
                f"   API key origen: {origen_key}"
            )
        except Exception as e:
            raise LLMConnectionError(f"Error inicializando cliente DeepSeek: {e}")

    # ============================================================
    # PROPIEDADES
    # ============================================================

    @property
    def disponible(self) -> bool:
        """Verifica si el cliente está disponible y configurado."""
        return self._client is not None

    @property
    def modelo_actual(self) -> str:
        """Retorna el modelo actual."""
        return self.default_model

    # ============================================================
    # MÉTODO PRINCIPAL
    # ============================================================

    def chat(
        self,
        prompt: str,
        system_prompt: str = "",
        model: Optional[str] = None,
        temperature: float = 0.2,
        max_tokens: int = 2000,
        reasoning_effort: Optional[str] = None,
        thinking_enabled: Optional[bool] = None,
        timeout: Optional[float] = None,
        extra_body: Optional[Dict[str, Any]] = None
    ) -> str:
        """
        Envía una consulta al LLM de DeepSeek y devuelve la respuesta.

        Args:
            prompt: Mensaje del usuario
            system_prompt: Instrucciones del sistema (opcional)
            model: Modelo a usar (por defecto usa el modelo por defecto)
            temperature: Temperatura (0.0-1.0)
            max_tokens: Máximo de tokens a generar
            reasoning_effort: Nivel de razonamiento ("low", "medium", "high")
            thinking_enabled: Si se debe habilitar el modo thinking
            timeout: Timeout en segundos para esta petición
            extra_body: Parámetros adicionales para la API

        Returns:
            str: Respuesta del modelo

        Raises:
            LLMConnectionError: Si el cliente no está disponible
            LLMResponseError: Si la respuesta está vacía o mal formada
            LLMError: Para otros errores
        """
        if not self.disponible:
            raise LLMConnectionError(
                "Cliente LLM no disponible. Verifica la configuración."
            )

        model = model or self.default_model
        reasoning = reasoning_effort or self.reasoning_effort
        thinking = thinking_enabled if thinking_enabled is not None else self.thinking_enabled
        timeout = timeout or self.timeout

        # ── Construir mensajes ──
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        # ── Construir extra_body ──
        extra = {"thinking": {"type": "enabled" if thinking else "disabled"}}
        if extra_body:
            extra.update(extra_body)

        # ── Log de la consulta ──
        logger.debug(f"📤 Consultando modelo: {model}")
        logger.debug(f"   Temperatura: {temperature}, Max tokens: {max_tokens}")
        logger.debug(
            f"   Razonamiento: {reasoning}, "
            f"Thinking: {'enabled' if thinking else 'disabled'}"
        )
        logger.debug(f"   Prompt: {prompt[:200]}...")

        # ── Ejecutar con reintentos ──
        last_error = None
        for attempt in range(self.max_retries + 1):
            try:
                if attempt > 0:
                    wait_time = 2 ** attempt
                    logger.info(
                        f"🔄 Reintento {attempt} de {self.max_retries} "
                        f"en {wait_time}s"
                    )
                    time.sleep(wait_time)

                response = self._client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stream=False,
                    reasoning_effort=reasoning,
                    extra_body=extra,
                    timeout=timeout
                )

                # ── Verificar respuesta ──
                if not response or not response.choices:
                    raise LLMResponseError("La respuesta está vacía o sin choices")

                content = response.choices[0].message.content

                if content is None or not content.strip():
                    if hasattr(response.choices[0].message, 'reasoning_content'):
                        content = response.choices[0].message.reasoning_content
                    if not content or not content.strip():
                        raise LLMResponseError(
                            "El contenido de la respuesta está vacío"
                        )

                # ── Log del éxito ──
                logger.info(f"✅ Respuesta recibida ({len(content)} caracteres)")
                if hasattr(response, 'usage') and response.usage:
                    logger.debug(
                        f"   Tokens: {response.usage.total_tokens} total"
                    )
                    logger.debug(
                        f"   Prompt: {response.usage.prompt_tokens}, "
                        f"Completion: {response.usage.completion_tokens}"
                    )
                logger.debug(f"   Primeros 200 chars: {content[:200]}...")

                return content

            except Exception as e:
                last_error = e
                logger.warning(f"❌ Intento {attempt + 1} falló: {e}")

                # Reintentar ante errores de conexión/timeout/rate limit
                err_str = str(e).lower()
                if any(k in err_str for k in ("connection", "timeout", "rate_limit", "overloaded")):
                    continue
                break

        raise LLMError(f"Todos los intentos fallaron. Último error: {last_error}")

    # ============================================================
    # MÉTODOS DE CONVENIENCIA
    # ============================================================

    def chat_simple(
        self,
        prompt: str,
        temperature: float = 0.2,
        max_tokens: int = 1000
    ) -> str:
        """Versión simplificada para consultas rápidas."""
        return self.chat(
            prompt=prompt,
            system_prompt="Eres un asistente útil y preciso.",
            temperature=temperature,
            max_tokens=max_tokens
        )

    def chat_con_json(
        self,
        prompt: str,
        system_prompt: str = "",
        temperature: float = 0.2,
        max_tokens: int = 2000
    ) -> Dict[str, Any]:
        """
        Consulta al LLM y espera una respuesta en formato JSON.

        Raises:
            LLMResponseError: Si la respuesta no es JSON válido
        """
        from core.utils import extraer_json_de_llm

        system = system_prompt + "\n\nResponde ÚNICAMENTE con un objeto JSON válido."

        respuesta = self.chat(
            prompt=prompt,
            system_prompt=system,
            temperature=temperature,
            max_tokens=max_tokens
        )

        # ── 1. Intentar con la función unificada ──
        data = extraer_json_de_llm(respuesta)
        if data is not None:
            return data

        # ── 2. Fallback: limpiar manualmente ──
        import re

        limpio = re.sub(r'^```json\s*', '', respuesta, flags=re.MULTILINE)
        limpio = re.sub(r'^```\s*', '', limpio, flags=re.MULTILINE)
        limpio = re.sub(r'\s*```$', '', limpio, flags=re.MULTILINE)
        limpio = limpio.strip()

        inicio = limpio.find('{')
        fin = limpio.rfind('}')
        if inicio != -1 and fin != -1 and fin > inicio:
            limpio = limpio[inicio:fin + 1]

        try:
            return json.loads(limpio)
        except json.JSONDecodeError as e:
            # ── 3. Último fallback: reparar ──
            try:
                reparado = re.sub(r"([{,])\s*'([^']*)'\s*:", r'\1"\2":', limpio)
                reparado = re.sub(r":\s*'([^']*)'", r': "\1"', reparado)
                reparado = re.sub(
                    r'([{,])\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*:',
                    r'\1"\2":',
                    reparado
                )
                reparado = re.sub(r',\s*}', '}', reparado)
                reparado = re.sub(r',\s*]', ']', reparado)

                if reparado.startswith('{') and reparado.endswith('}'):
                    return json.loads(reparado)
            except json.JSONDecodeError:
                pass

            raise LLMResponseError(f"La respuesta no es JSON válido: {e}")

    def chat_con_modelo_alternativo(
        self,
        prompt: str,
        system_prompt: str = "",
        temperatura: float = 0.2,
        max_tokens: int = 2000
    ) -> str:
        """Intenta con varios modelos en orden hasta obtener una respuesta."""
        modelos_a_probar = [self.default_model] + FALLBACK_MODELS

        for modelo in modelos_a_probar:
            try:
                logger.info(f"🔄 Intentando con modelo: {modelo}")
                return self.chat(
                    prompt=prompt,
                    system_prompt=system_prompt,
                    model=modelo,
                    temperature=temperatura,
                    max_tokens=max_tokens
                )
            except Exception as e:
                logger.warning(f"❌ Falló con modelo {modelo}: {e}")
                continue

        raise LLMError("Todos los modelos fallaron")

    # ============================================================
    # CONFIGURACIÓN
    # ============================================================

    def set_modelo(self, modelo: str):
        """Cambia el modelo por defecto."""
        self.default_model = modelo
        logger.info(f"📌 Modelo cambiado a: {modelo}")

    def set_reasoning(self, esfuerzo: str):
        """Cambia el nivel de razonamiento."""
        if esfuerzo not in VALID_REASONING_EFFORTS:
            raise ValueError(
                f"Esfuerzo inválido: {esfuerzo}. "
                f"Opciones: {VALID_REASONING_EFFORTS}"
            )
        self.reasoning_effort = esfuerzo
        logger.info(f"📌 Razonamiento cambiado a: {esfuerzo}")

    def set_thinking(self, habilitado: bool):
        """Habilita o deshabilita el modo thinking."""
        self.thinking_enabled = habilitado
        logger.info(
            f"📌 Thinking: {'enabled' if habilitado else 'disabled'}"
        )

    # ============================================================
    # ESTADÍSTICAS Y DIAGNÓSTICO
    # ============================================================

    def obtener_estado(self) -> Dict[str, Any]:
        """Obtiene el estado actual del cliente."""
        return {
            'disponible': self.disponible,
            'modelo': self.default_model,
            'base_url': self.base_url,
            'timeout': self.timeout,
            'max_retries': self.max_retries,
            'reasoning_effort': self.reasoning_effort,
            'thinking_enabled': self.thinking_enabled,
            'api_key_configurada': bool(self.api_key),
        }


# ============================================================
# FUNCIÓN DE AYUDA PARA USO RÁPIDO
# ============================================================

def crear_cliente(
    api_key: Optional[str] = None,
    modelo: str = DEFAULT_MODEL
) -> LLMClient:
    """Función rápida para crear un cliente LLM."""
    return LLMClient(api_key=api_key, default_model=modelo)


# ============================================================
# EJECUCIÓN DE PRUEBA
# ============================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("=" * 70)
    print("🧪 PRUEBA DE CLIENTE LLM (DeepSeek)")
    print("=" * 70)

    try:
        print("\n📦 Creando cliente LLM...")
        client = LLMClient()

        print(f"\n📊 Estado del cliente:")
        estado = client.obtener_estado()
        for key, value in estado.items():
            print(f"   {key}: {value}")

        print("\n🔍 Probando consulta simple...")
        respuesta = client.chat_simple("Di 'Hola mundo' en español")
        print(f"\n📝 Respuesta: {respuesta}")

        print("\n✅ Pruebas completadas con éxito.")

    except Exception as e:
        print(f"\n❌ Error en la prueba: {e}")
        import traceback
        traceback.print_exc()