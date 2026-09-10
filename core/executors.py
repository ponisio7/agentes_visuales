# core/executors.py - VERSIÓN REFACTORIZADA v4 (2026-09) - FIX JSON PARSEADO
"""
Ejecutores de agentes con soporte para todos los tipos.

CARACTERÍSTICAS PRINCIPALES:
- ✅ Validación robusta de entrada
- ✅ Timeouts por tipo de operación
- ✅ Sustitución de variables inteligente (extrae contenido relevante)
- ✅ Extracción automática de contenido de agentes File, LLM, HTTP, Shell, Python
- ✅ Extracción RECURSIVA de contenido (busca en cualquier nivel del dict)
- ✅ Protección contra estructuras cíclicas en la extracción
- ✅ File: FALLA en vez de inventar contenido fallback
- ✅ Caché para resultados HTTP (LRU con TTL)
- ✅ Rate limiting para APIs externas
- ✅ Progreso en tiempo real con señales
- ✅ Logging estructurado
- ✅ Manejo de errores granular
- ✅ Soporte para todos los tipos de agentes
- ✅ Cancelación cooperativa vía CancellationToken

TIPOS DE AGENTES SOPORTADOS:
- Python / Python Script
- Shell
- HTTP
- File
- LLM (DeepSeek)
- Loop
"""

import json
import os
import shlex          # ✅ NUEVO (para _extraer_primer_comando y shlex.quote)
import shutil         # ✅ NUEVO (para shutil.which)
import re
import time
import logging
import hashlib
import threading
import subprocess
import platform
import shutil
import tempfile
import atexit
from typing import Dict, Any, Optional, Tuple, List, Union, Set
from collections import OrderedDict
from urllib.parse import urlparse

import requests

from core.agent import Agente, TipoAgente
from core.sandbox import PythonSandbox, SandboxError
from .cancellation import obtener_gestor_cancelacion, CancellationToken


# ============================================================
# CONFIGURACIÓN DE LOGGING
# ============================================================

logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTES DE SEGURIDAD Y CONFIGURACIÓN
# ============================================================

# Límites de seguridad
MAX_BYTES_LECTURA_ARCHIVO = 10 * 1024 * 1024      # 10 MB
MAX_HTTP_BODY_SIZE = 50 * 1024 * 1024             # 50 MB
MAX_SHELL_COMMAND_LENGTH = 10000                  # 10k caracteres
MAX_FILE_PATH_LENGTH = 1000                       # 1k caracteres
MAX_CODIGO_LENGTH = 100000                        # 100 KB

# Caché HTTP
HTTP_CACHE_SIZE = 100
HTTP_CACHE_TTL = 300                              # 5 minutos
DEFAULT_USER_AGENT = "Agentes-Visuales/1.0"

# Métodos HTTP soportados
ALLOWED_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

# Variables reservadas del sistema
_VARIABLES_RESERVADAS = frozenset(
    ("contexto", "resultado", "nombre", "descripcion", "id", "fecha", "hora")
)

# Comandos shell peligrosos (solo advertencia)
DANGEROUS_SHELL_COMMANDS = frozenset([
    'rm -rf', 'dd if=', 'mkfs', 'chmod 777', 'chown', 'sudo',
    ':(){ :|:& };:', 'mkfs', 'dd', '>/dev/sda'
])

# Directorios peligrosos para eliminar
DANGEROUS_DIRS = frozenset(['/', 'C:\\', '/home', '/root', '/etc', '/var'])

# ============================================================
# DETECCIÓN DE COMANDOS QUE REQUIEREN PRIVILEGIOS ROOT
# ============================================================
# Conjunto de comandos base que típicamente requieren privilegios
# de root. Se usa para decidir si aplicamos 'pkexec' automáticamente.
#
# Solo se comprueba el PRIMER token del comando (ignorando flags,
# operadores y palabras reservadas del shell).
# ============================================================
COMANDOS_PRIVILEGIADOS = frozenset({
    # Gestores de paquetes
    'apt', 'apt-get', 'aptitude', 'dpkg', 'snap', 'yum', 'dnf',
    'pacman', 'zypper', 'apk', 'rpm',

    # Servicios y sistema
    'systemctl', 'service', 'init', 'shutdown', 'reboot', 'halt',

    # Montaje y discos
    'mount', 'umount', 'fdisk', 'parted', 'mkfs', 'fsck',
    'cryptsetup', 'losetup',

    # Redes y firewall
    'iptables', 'ip6tables', 'nft', 'ufw', 'firewall-cmd',

    # Usuarios y permisos
    'useradd', 'userdel', 'usermod', 'groupadd', 'groupdel',
    'passwd', 'chpasswd', 'chsh', 'visudo',

    # Modificación de sistema
    'modprobe', 'insmod', 'rmmod', 'sysctl',

    # Otros
    'docker', 'podman',  # suelen requerir root o grupo docker
})

# Claves prioritarias para extracción de contenido (fuente única de verdad) - v5
#
# ORDEN IMPORTANTE:
#   - 'html' y 'markdown' van PRIMERO porque son "salidas finales" típicas de
#     agentes de generación. Si los pones detrás de 'json', y el resultado del
#     agente es {'html': '...', 'json': {...metadata...}'}, el extractor
#     cogería el JSON de metadatos en vez del HTML final.
_CLAVES_CONTENIDO_PRIORITARIAS = (
    'html',         # ✅ Generación de páginas web
    'markdown',     # ✅ Generación de documentos md
    'json',         # JSON parseado (LLM + HTTP)
    'contenido',    # File / Python explícito
    'respuesta_limpia',  # texto limpio sin fences
    'respuesta',    # LLM crudo
    'body',         # HTTP (crudo)
    'stdout',       # Shell
    'items',        # Loop
    'resultado',    # Python genérico
    'data',         # Genérico
    'texto',        # Genérico
    'output',       # Genérico
    '_av_json',     # fallback reservado
    '_av_respuesta_limpia',
    '_av_respuesta',
)


# Profundidad máxima de recursión al extraer contenido
_MAX_PROFUNDIDAD_EXTRACCION = 5


# ============================================================
# HELPERS v4: LIMPIEZA Y PARSEO ROBUSTO DE LLM
# ============================================================

def _limpiar_fences_markdown(texto: str) -> str:
    """Elimina ```json ... ``` y espacios."""
    if not texto:
        return ""
    t = texto.strip()
    # quita fence inicial
    t = re.sub(r'^```[a-zA-Z]*\s*', '', t)
    # quita fence final
    t = re.sub(r'\s*```\s*$', '', t)
    return t.strip()

def _parsear_json_robusto(texto: str) -> Optional[Any]:
    """Intenta parsear JSON con múltiples estrategias."""
    if not texto:
        return None
    # 1. directo
    try:
        return json.loads(texto)
    except:
        pass
    # 2. buscar primer { ... último }
    try:
        ini = texto.find('{')
        fin = texto.rfind('}')
        if ini != -1 and fin != -1 and fin > ini:
            candidato = texto[ini:fin+1]
            return json.loads(candidato)
    except:
        pass
    # 3. buscar primer [ ... último ]
    try:
        ini = texto.find('[')
        fin = texto.rfind(']')
        if ini != -1 and fin != -1 and fin > ini:
            candidato = texto[ini:fin+1]
            return json.loads(candidato)
    except:
        pass
    # 4. regex extract
    try:
        m = re.search(r'\{.*\}', texto, re.DOTALL)
        if m:
            return json.loads(m.group(0))
    except:
        pass
    return None

def _es_resultado_sospechoso(resultado: Any) -> Tuple[bool, str]:
    """Detecta resultados vacíos que antes pasaban silenciosos."""
    if resultado is None:
        return True, "resultado es None"
    if resultado == {} or resultado == [] or resultado == "":
        return True, "resultado vacío"
    if isinstance(resultado, dict):
        vacios = (None, '', [], {}, 'N/A', 'null')
        # si TODOS los valores son vacíos
        if all(v in vacios for v in resultado.values()):
            return True, f"todos los valores vacíos: {list(resultado.keys())}"
        # caso típico del bug: {'productos': []} o {'texto': ''}
        for k, v in resultado.items():
            if isinstance(v, dict) and not v:
                return True, f"clave '{k}' es dict vacío"
    return False, ""



# ============================================================
# CACHÉ HTTP (LRU con TTL)
# ============================================================

class HTTPCache:
    """
    Caché LRU para resultados de peticiones HTTP.
    Thread-safe con TTL configurable.
    """

    def __init__(self, max_size: int = HTTP_CACHE_SIZE, ttl: int = HTTP_CACHE_TTL):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.RLock()
        self._stats = {"hits": 0, "misses": 0}

    def _generate_key(
        self,
        url: str,
        method: str,
        headers: Dict,
        body: Optional[Any]
    ) -> str:
        """Genera una clave única para la petición."""
        headers_normalized = {k.lower(): v for k, v in (headers or {}).items()}
        headers_json = json.dumps(headers_normalized, sort_keys=True)

        body_str = ""
        if body is not None:
            try:
                body_str = json.dumps(body, sort_keys=True, default=str)
            except (TypeError, ValueError):
                body_str = str(body)

        key_str = f"{method.upper()}|{url}|{headers_json}|{body_str}"
        return hashlib.sha256(key_str.encode('utf-8')).hexdigest()

    def get(
        self,
        url: str,
        method: str,
        headers: Dict,
        body: Optional[Any]
    ) -> Optional[Dict]:
        """Obtiene un resultado del caché."""
        key = self._generate_key(url, method, headers, body)

        with self._lock:
            if key not in self._cache:
                self._stats["misses"] += 1
                return None

            entry = self._cache[key]
            if time.time() - entry['timestamp'] > self.ttl:
                del self._cache[key]
                self._stats["misses"] += 1
                return None

            self._cache.move_to_end(key)
            self._stats["hits"] += 1
            return entry['result']

    def put(
        self,
        url: str,
        method: str,
        headers: Dict,
        body: Optional[Any],
        result: Dict
    ):
        """Guarda un resultado en el caché."""
        key = self._generate_key(url, method, headers, body)

        with self._lock:
            if len(self._cache) >= self.max_size:
                oldest = next(iter(self._cache))
                del self._cache[oldest]

            self._cache[key] = {
                'timestamp': time.time(),
                'result': result
            }

    def clear(self):
        """Limpia el caché."""
        with self._lock:
            self._cache.clear()
            self._stats = {"hits": 0, "misses": 0}

    def get_stats(self) -> Dict[str, Union[int, float]]:
        """Obtiene estadísticas del caché."""
        with self._lock:
            total = self._stats["hits"] + self._stats["misses"]
            return {
                'size': len(self._cache),
                'hits': self._stats["hits"],
                'misses': self._stats["misses"],
                'hit_ratio': self._stats["hits"] / total if total > 0 else 0
            }


# ============================================================
# RATE LIMITER
# ============================================================

class RateLimiter:
    """
    Rate limiter simple para APIs externas.
    Thread-safe.
    """

    def __init__(self, calls_per_second: float = 10):
        self.calls_per_second = calls_per_second
        self._last_call = 0.0
        self._lock = threading.RLock()

    def wait(self):
        """Espera si es necesario para respetar el rate limit."""
        with self._lock:
            now = time.time()
            elapsed = now - self._last_call
            min_interval = 1.0 / self.calls_per_second

            if elapsed < min_interval:
                time.sleep(min_interval - elapsed)

            self._last_call = time.time()


# ============================================================
# EJECUTOR PRINCIPAL
# ============================================================

class AgentExecutor:
    """
    Ejecuta agentes de diferentes tipos con funcionalidad real.
    """

    # ============================================================
    # RECURSOS COMPARTIDOS (Singletons)
    # ============================================================

    _http_cache: Optional[HTTPCache] = None
    _rate_limiter: Optional[RateLimiter] = None
    _class_lock = threading.RLock()

    @classmethod
    def _get_http_cache(cls) -> HTTPCache:
        with cls._class_lock:
            if cls._http_cache is None:
                cls._http_cache = HTTPCache()
            return cls._http_cache

    @classmethod
    def _get_rate_limiter(cls) -> RateLimiter:
        with cls._class_lock:
            if cls._rate_limiter is None:
                cls._rate_limiter = RateLimiter()
            return cls._rate_limiter

    @classmethod
    def clear_http_cache(cls):
        """Limpia el caché HTTP."""
        with cls._class_lock:
            if cls._http_cache is not None:
                cls._http_cache.clear()

    @classmethod
    def get_http_cache_stats(cls) -> Dict:
        """Obtiene estadísticas del caché HTTP."""
        with cls._class_lock:
            if cls._http_cache is not None:
                return cls._http_cache.get_stats()
            return {'size': 0, 'hits': 0, 'misses': 0, 'hit_ratio': 0}

    # ============================================================
    # ACTUALIZACIÓN DE PROGRESO
    # ============================================================

    @staticmethod
    def _actualizar_progreso(agente: Agente, progreso: int, mensaje: str = ""):
        """
        Actualiza el progreso del agente y emite señal a través del bridge.
        """
        agente.progreso = min(100, max(0, progreso))
        if mensaje:
            agente.mensaje = mensaje

        if hasattr(agente, '_bridge') and agente._bridge is not None:
            try:
                agente._bridge.agente_actualizado.emit(agente.id)
            except Exception as e:
                logger.debug(f"Error emitiendo señal de progreso: {e}")

    # ============================================================
    # EXTRACCIÓN INTELIGENTE DE CONTENIDO (RECURSIVA)
    # ============================================================

    @staticmethod
    def _extraer_contenido_relevante(
        valor: Any,
        _profundidad: int = 0,
        _visitados: Optional[Set[int]] = None
    ) -> Any:
        """
        Extrae el contenido más relevante de un resultado de agente.

        BÚSQUEDA RECURSIVA:
        Antes buscaba solo en el primer nivel del diccionario. Ahora busca
        en cualquier nivel de anidación (con límite de profundidad y
        protección contra estructuras cíclicas).

        ESTRATEGIA (en orden de prioridad):
        1. Si 'valor' no es un dict → devolverlo tal cual.
        2. Si 'valor' ya tiene una clave de contenido directa
           ('contenido', 'respuesta', 'json', 'body', 'resultado',
            'stdout', 'items') → devolver ese valor.
        3. Si 'valor' tiene un diccionario hijo → recursar en él.
        4. Si no se encuentra nada útil → devolver el dict original.

        CASOS RESUELTOS:
        - {'contenido': 'texto'} → 'texto'
        - {'FormatearContenido': {'contenido': 'texto'}} → 'texto'
        - {'Dep': {'resultado': {'contenido': 'texto'}}} → 'texto'
        - {'Dep': {'json': {'a': 1}, 'contenido': 'x'}} → 'x' (prioridad)
        - {'foo': 'bar'} → {'foo': 'bar'} (nada útil, devuelve el dict)

        Args:
            valor: Valor a procesar (puede ser dict, lista, str, etc.).
            _profundidad: Nivel actual de recursión (uso interno).
            _visitados: Set de IDs de objetos ya visitados (anti-ciclos).

        Returns:
            Any: Contenido extraído, o el valor original si no hay nada.
        """
        # ── Guarda: evitar recursión infinita ──
        if _profundidad > _MAX_PROFUNDIDAD_EXTRACCION:
            logger.debug(
                f"Extracción de contenido: profundidad máxima "
                f"({_MAX_PROFUNDIDAD_EXTRACCION}) alcanzada, devolviendo valor"
            )
            return valor

        # ── Casos base: tipos primitivos y listas ──
        if valor is None:
            return None
        if isinstance(valor, (str, int, float, bool)):
            return valor
        if isinstance(valor, list):
            return valor

        # ── Si no es un dict, devolver representación de string ──
        if not isinstance(valor, dict):
            return str(valor)

        # ── Protección contra estructuras cíclicas ──
        if _visitados is None:
            _visitados = set()
        valor_id = id(valor)
        if valor_id in _visitados:
            logger.debug("Extracción de contenido: ciclo detectado, saltando")
            return valor
        _visitados.add(valor_id)

        # ── 1. Buscar clave prioritaria en el nivel actual ──
        for clave in _CLAVES_CONTENIDO_PRIORITARIAS:
            if clave not in valor:
                continue
            encontrado = valor[clave]

            # Ignorar valores "vacíos"
            if encontrado is None:
                continue
            if isinstance(encontrado, str) and not encontrado.strip():
                continue
            if isinstance(encontrado, (list, dict)) and not encontrado:
                continue

            # Primitivo → devolver directamente
            if isinstance(encontrado, (str, int, float, bool)):
                return encontrado

            # Dict anidado → recursar
            if isinstance(encontrado, dict):
                resultado_recursivo = AgentExecutor._extraer_contenido_relevante(
                    encontrado,
                    _profundidad + 1,
                    _visitados
                )
                if resultado_recursivo is not encontrado:
                    return resultado_recursivo

            # Lista → devolver directamente
            if isinstance(encontrado, list):
                return encontrado

        # ── 2. Buscar en dicts hijos de forma recursiva ──
        # Cubre: {'FormatearContenido': {'contenido': 'texto'}}
        for clave, sub_valor in valor.items():
            if isinstance(sub_valor, dict) and sub_valor:
                resultado_recursivo = AgentExecutor._extraer_contenido_relevante(
                    sub_valor,
                    _profundidad + 1,
                    _visitados
                )
                if resultado_recursivo is not sub_valor:
                    return resultado_recursivo

        # ── 3. Fallback: nada útil encontrado ──
        return valor

    # ============================================================
    # VARIABLES Y SUSTITUCIÓN
    # ============================================================

    @staticmethod
    def _variables_disponibles(agente: Agente, contexto: Dict) -> Dict[str, str]:
        """
        Construye el diccionario de variables sustituibles.

        Incluye:
        - Variables reservadas (contexto, resultado, nombre, descripcion, id, fecha, hora)
        - Una entrada por cada dependencia en el contexto
        - Extracción automática de contenido relevante
        """
        contexto = contexto or {}
        ahora = time.localtime()

        variables = {
            "contexto": json.dumps(contexto, indent=2, default=str) if contexto else "",
            "resultado": json.dumps(contexto, indent=2, default=str) if contexto else "",
            "nombre": agente.nombre,
            "descripcion": agente.descripcion,
            "id": agente.id,
            "fecha": time.strftime("%Y-%m-%d", ahora),
            "hora": time.strftime("%H:%M:%S", ahora),
        }

        for clave, valor in contexto.items():
            if clave in variables:
                continue

            valor_extraido = AgentExecutor._extraer_contenido_relevante(valor)

            if isinstance(valor_extraido, str):
                variables[clave] = valor_extraido
            elif isinstance(valor_extraido, (dict, list)):
                try:
                    variables[clave] = json.dumps(
                        valor_extraido, indent=2, default=str, ensure_ascii=False
                    )
                except (TypeError, ValueError):
                    variables[clave] = str(valor_extraido)
            else:
                variables[clave] = str(valor_extraido)

        return variables

    @staticmethod
    def _sustituir_variables(texto: str, variables: Dict[str, str]) -> str:
        """Sustituye variables en un texto usando el patrón {variable}."""
        if not texto or "{" not in texto or not variables:
            return texto

        pattern = re.compile(
            r"\{\s*(" + "|".join(re.escape(k) for k in variables.keys()) + r")\s*\}"
        )
        return pattern.sub(lambda m: variables.get(m.group(1), m.group(0)), texto)

    # ============================================================
    # VALIDADORES
    # ============================================================

    @staticmethod
    def _validar_ruta_archivo(ruta: str) -> bool:
        """Valida una ruta de archivo."""
        if not ruta:
            return False
        if len(ruta) > MAX_FILE_PATH_LENGTH:
            return False

        normalized = os.path.normpath(ruta)
        if normalized.startswith(('..', '/', '\\')):
            return False

        basename = os.path.basename(normalized)
        if basename.startswith('.') and basename not in ('.', '..'):
            return False

        return True

    # ============================================================
    # DETECCIÓN DE COMANDOS PRIVILEGIADOS
    # ============================================================

    @staticmethod
    def _extraer_primer_comando(comando: str) -> Optional[str]:
        """
        Extrae el primer comando "real" de una cadena shell.

        Ignora:
        - Palabras reservadas al inicio: if, then, else, fi, while, do,
            done, for, in, echo (no, echo sí es comando)
        - Operadores: &&, ||, |, ;, &
        - Flags que empiezan por '-'
        - Asignaciones tipo VAR=valor

        Devuelve el nombre base del comando (sin ruta), o None si no
        se pudo determinar.

        Ejemplos:
            "apt-get update"           → "apt-get"
            "sudo apt-get update"      → "apt-get"
            "if [ -f x ]; then apt-get update; fi"  → "apt-get"
            "FOO=bar apt-get update"   → "apt-get"
            "ls -la | grep foo"        → "ls"
        """
        if not comando or not comando.strip():
            return None

        # Quitar 'sudo' o 'pkexec' iniciales si ya los tiene
        # (para evitar doble elevación)
        import shlex
        try:
            tokens = shlex.split(comando)
        except ValueError:
            # Comando mal formado → fallback simple
            tokens = comando.split()

        # Palabras reservadas del shell que se ignoran al inicio
        PALABRAS_RESERVADAS = frozenset({
            'if', 'then', 'else', 'elif', 'fi', 'while', 'do', 'done',
            'for', 'in', 'case', 'esac', 'function', 'return',
        })

        for token in tokens:
            # Ignorar vacíos
            if not token:
                continue
            # Ignorar operadores del shell
            if token in ('&&', '||', '|', ';', '&', '(', ')', '{', '}'):
                continue
            # Ignorar flags
            if token.startswith('-'):
                continue
            # Ignorar asignaciones VAR=valor
            if '=' in token and not token.startswith('/') and not token.startswith('./'):
                # Podría ser una asignación → verificar que no sea una ruta
                parte_izq, _, _ = token.partition('=')
                if parte_izq.isidentifier():
                    continue
            # Ignorar palabras reservadas
            if token in PALABRAS_RESERVADAS:
                continue

            # Este es el primer comando real → devolver su basename
            return os.path.basename(token)

        return None


    # ============================================================
    # DETECCIÓN DE COMANDOS PRIVILEGIADOS
    # ============================================================

    @staticmethod
    def _extraer_primer_comando(comando: str) -> Optional[str]:
        """
        Extrae el primer comando "real" de una cadena shell.

        Ignora:
        - Palabras reservadas al inicio: if, then, else, fi, while, do,
            done, for, in, echo (no, echo sí es comando)
        - Operadores: &&, ||, |, ;, &
        - Flags que empiezan por '-'
        - Asignaciones tipo VAR=valor

        Devuelve el nombre base del comando (sin ruta), o None si no
        se pudo determinar.

        Ejemplos:
            "apt-get update"           → "apt-get"
            "sudo apt-get update"      → "apt-get"
            "if [ -f x ]; then apt-get update; fi"  → "apt-get"
            "FOO=bar apt-get update"   → "apt-get"
            "ls -la | grep foo"        → "ls"
        """
        if not comando or not comando.strip():
            return None

        # Quitar 'sudo' o 'pkexec' iniciales si ya los tiene
        # (para evitar doble elevación)
        import shlex
        try:
            tokens = shlex.split(comando)
        except ValueError:
            # Comando mal formado → fallback simple
            tokens = comando.split()

        # Palabras reservadas del shell que se ignoran al inicio
        PALABRAS_RESERVADAS = frozenset({
            'if', 'then', 'else', 'elif', 'fi', 'while', 'do', 'done',
            'for', 'in', 'case', 'esac', 'function', 'return',
        })

        for token in tokens:
            # Ignorar vacíos
            if not token:
                continue
            # Ignorar operadores del shell
            if token in ('&&', '||', '|', ';', '&', '(', ')', '{', '}'):
                continue
            # Ignorar flags
            if token.startswith('-'):
                continue
            # Ignorar asignaciones VAR=valor
            if '=' in token and not token.startswith('/') and not token.startswith('./'):
                # Podría ser una asignación → verificar que no sea una ruta
                parte_izq, _, _ = token.partition('=')
                if parte_izq.isidentifier():
                    continue
            # Ignorar palabras reservadas
            if token in PALABRAS_RESERVADAS:
                continue

            # Este es el primer comando real → devolver su basename
            return os.path.basename(token)

        return None


    @staticmethod
    def _comando_requiere_root(comando: str) -> bool:
        """
        Detecta si un comando shell requiere privilegios de root.

        Estrategia:
        1. Si ya empieza por 'sudo ' o 'pkexec ', NO requiere elevación
            adicional (ya la tiene).
        2. Extrae el primer comando real (ignorando if/then, flags, etc.).
        3. Lo compara con COMANDOS_PRIVILEGIADOS.
        """
        if not comando:
            return False

        comando_stripped = comando.strip()

        # Ya está elevado
        if comando_stripped.startswith(('sudo ', 'pkexec ', 'doas ')):
            return False

        primer_comando = AgentExecutor._extraer_primer_comando(comando_stripped)
        if not primer_comando:
            return False

        return primer_comando in COMANDOS_PRIVILEGIADOS

    @staticmethod
    def _validar_url(url: str) -> bool:
        """Valida una URL HTTP/HTTPS."""
        if not url:
            return False
        if len(url) > 2000:
            return False

        try:
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https'):
                return False
            if not parsed.netloc:
                return False
            if any(c in url for c in ('\n', '\r', '\t')):
                return False
            return True
        except Exception:
            return False

    @staticmethod
    def _es_lista_valida(items: Any) -> bool:
        """Verifica si un valor es una lista válida."""
        return isinstance(items, list)

    @staticmethod
    def _resolver_ruta_en_contexto(contexto: Dict, ruta: str) -> Any:
        """Resuelve una ruta en el contexto (ej: 'Dependencia.clave')."""
        if not ruta:
            return None

        partes = ruta.split('.')
        valor = contexto.get(partes[0])

        for parte in partes[1:]:
            if isinstance(valor, dict):
                valor = valor.get(parte)
            else:
                return None

        return valor

    # ============================================================
    # MÉTODO PRINCIPAL DE EJECUCIÓN
    # ============================================================

    @staticmethod
    def ejecutar(
        agente: Agente,
        contexto: Dict = None,
        cancellation_token: Optional['CancellationToken'] = None
    ) -> Tuple[bool, str, Dict]:
        """
        Ejecuta un agente según su tipo con soporte para cancelación.

        Args:
            agente: Agente a ejecutar
            contexto: Contexto con resultados de dependencias
            cancellation_token: Token de cancelación (opcional)

        Returns:
            Tuple[bool, str, Dict]: (éxito, mensaje, resultado)
        """
        contexto = contexto or {}
        start_time = time.time()

        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de iniciar", {'error': 'cancelled'}

        try:
            es_valido, mensaje_error = agente.validar_configuracion()
            if not es_valido:
                return False, f"Configuración inválida: {mensaje_error}", {
                    'error': mensaje_error
                }

            tipo = agente.tipo
            if tipo == TipoAgente.PYTHON:
                return AgentExecutor._ejecutar_python(agente, contexto, cancellation_token)
            elif tipo == TipoAgente.SHELL:
                return AgentExecutor._ejecutar_shell(agente, contexto, cancellation_token)
            elif tipo == TipoAgente.HTTP:
                return AgentExecutor._ejecutar_http(agente, contexto, cancellation_token)
            elif tipo == TipoAgente.FILE:
                return AgentExecutor._ejecutar_file(agente, contexto, cancellation_token)
            elif tipo == TipoAgente.LLM:
                return AgentExecutor._ejecutar_llm(agente, contexto, cancellation_token)
            elif tipo == TipoAgente.LOOP:
                return AgentExecutor._ejecutar_loop(agente, contexto, cancellation_token)
            else:
                return False, f"Tipo de agente no soportado: {tipo}", {}

        except Exception as e:
            execution_time = time.time() - start_time
            logger.exception(f"Error en ejecución de {agente.nombre}")
            return False, f"Error crítico: {str(e)}", {
                'error': str(e),
                'tipo': type(e).__name__,
                'duracion': execution_time
            }

    # ============================================================
    # 1. EJECUTOR PYTHON
    # ============================================================

    @staticmethod
    def _ejecutar_python(
        agente: Agente,
        contexto: Dict,
        cancellation_token: Optional['CancellationToken'] = None
    ) -> Tuple[bool, str, Dict]:
        """Ejecuta código Python en un subproceso aislado."""

        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {'error': 'cancelled'}

        AgentExecutor._actualizar_progreso(agente, 20, "Preparando entorno Python...")

        codigo = agente.codigo_python or ""

        if not codigo.strip():
            AgentExecutor._actualizar_progreso(agente, 50, "Simulando ejecución...")
            duracion = max(0.01, float(agente.duracion or 0.5))
            for _ in range(int(min(duracion, 5) * 10)):
                if cancellation_token and cancellation_token.esta_cancelado():
                    return False, "Cancelado durante simulación", {'error': 'cancelled'}
                time.sleep(0.1)
            AgentExecutor._actualizar_progreso(agente, 100, "Simulación completada")
            return True, f"Simulado {duracion:.2f}s", {
                "simulado": True,
                "duracion": duracion,
                "nombre": agente.nombre
            }

        if len(codigo) > MAX_CODIGO_LENGTH:
            AgentExecutor._actualizar_progreso(agente, 100, "Código demasiado largo")
            return False, f"Código demasiado largo (máx {MAX_CODIGO_LENGTH//1024}KB)", {
                'error': 'code_too_long'
            }

        timeout = getattr(agente, 'timeout_python', 30)
        AgentExecutor._actualizar_progreso(agente, 40, "Ejecutando código Python...")

        try:
            exito, mensaje, resultado = PythonSandbox.ejecutar(
                codigo,
                contexto,
                timeout=timeout,
                cancellation_token=cancellation_token
            )

            # v4: detector de resultados vacíos silenciosos
            if exito:
                sospechoso, razon = _es_resultado_sospechoso(resultado)
                if sospechoso:
                    logger.warning(f"[{agente.nombre}] Resultado sospechoso Python: {razon} -> {str(resultado)[:200]}")
                    # no falla, pero el mensaje avisa
                    mensaje += f" (⚠️ sospechoso: {razon})"

            AgentExecutor._actualizar_progreso(
                agente, 100,
                "Código ejecutado correctamente" if exito else f"Error: {mensaje[:50]}"
            )

            return exito, mensaje, resultado

        except SandboxError as e:
            AgentExecutor._actualizar_progreso(
                agente, 100, f"Error en sandbox: {str(e)[:50]}"
            )
            return False, f"Error en sandbox: {str(e)}", {'error': str(e)}

    # ============================================================
    # 2. EJECUTOR SHELL
    # ============================================================


    @staticmethod
    def _ejecutar_shell(
    agente: Agente,
    contexto: Dict,
    cancellation_token: Optional['CancellationToken'] = None
) -> Tuple[bool, str, Dict]:
        """
        Ejecuta un comando de shell con sustitución de variables.

        ✅ NUEVO: Detección automática de comandos que requieren root.
        Si el comando requiere privilegios y 'pkexec' está disponible,
        se antepone automáticamente para que el usuario introduzca su
        contraseña mediante el diálogo nativo del sistema.
        """

        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {'error': 'cancelled'}

        AgentExecutor._actualizar_progreso(agente, 20, "Preparando comando shell...")

        variables = AgentExecutor._variables_disponibles(agente, contexto)
        comando = AgentExecutor._sustituir_variables(agente.comando_shell, variables)
        working_dir = AgentExecutor._sustituir_variables(agente.working_dir, variables)

        if not comando:
            AgentExecutor._actualizar_progreso(agente, 100, "Comando vacío")
            return False, "No hay comando shell definido", {}

        if len(comando) > MAX_SHELL_COMMAND_LENGTH:
            AgentExecutor._actualizar_progreso(agente, 100, "Comando demasiado largo")
            return False, (
                f"Comando demasiado largo (máx {MAX_SHELL_COMMAND_LENGTH} caracteres)"
            ), {}

        for dangerous in DANGEROUS_SHELL_COMMANDS:
            if dangerous in comando:
                logger.warning(
                    f"Comando shell contiene operación potencialmente peligrosa: "
                    f"{dangerous}"
                )

        # ══════════════════════════════════════════════════════════
        # ✅ NUEVO: Detección de privilegios + aplicación de pkexec
        # ══════════════════════════════════════════════════════════
        comando_original = comando
        comando_ejecutable = comando
        usar_pkexec = False

        if AgentExecutor._comando_requiere_root(comando):
            pkexec_path = shutil.which('pkexec')

            if pkexec_path:
                # pkexec ejecuta el comando en un proceso elevado y muestra
                # el diálogo nativo de autenticación de PolicyKit.
                # ⚠️ NO usamos shell=True con pkexec porque no lo necesita
                #    y complica el paso de argumentos.
                comando_ejecutable = f"{pkexec_path} /bin/sh -c {shlex.quote(comando)}"
                usar_pkexec = True
                logger.info(
                    f"[{agente.nombre}] Comando requiere root → aplicando pkexec: "
                    f"{comando[:60]}..."
                )
                AgentExecutor._actualizar_progreso(
                    agente, 35, "🔒 Solicitando privilegios (pkexec)..."
                )
            else:
                # No hay pkexec → fallar rápido con mensaje accionable
                mensaje_error = (
                    f"🔒 El comando requiere privilegios de root, pero 'pkexec' "
                    f"no está instalado.\n\n"
                    f"Comando: {comando[:120]}\n\n"
                    f"Opciones:\n"
                    f"  1. Instala policykit:\n"
                    f"       sudo apt install policykit-1   (Debian/Ubuntu)\n"
                    f"       sudo dnf install polkit        (Fedora)\n"
                    f"  2. Edita el agente y antepón 'sudo' manualmente al comando.\n"
                    f"  3. Configura NOPASSWD en /etc/sudoers.d/ para este comando."
                )
                logger.warning(f"[{agente.nombre}] {mensaje_error}")
                AgentExecutor._actualizar_progreso(
                    agente, 100, "❌ Falta pkexec"
                )
                return False, mensaje_error, {
                    'error': 'requires_root_no_pkexec',
                    'comando': comando[:200],
                    'requiere_root': True,
                    'pkexec_disponible': False,
                }

        AgentExecutor._actualizar_progreso(
            agente, 50, f"Ejecutando: {comando[:50]}..."
        )

        # ══════════════════════════════════════════════════════════
        # EJECUCIÓN
        # ══════════════════════════════════════════════════════════
        try:
            cwd = None
            if working_dir and AgentExecutor._validar_ruta_archivo(working_dir):
                if os.path.exists(working_dir) and os.path.isdir(working_dir):
                    cwd = working_dir

            env = os.environ.copy()
            safe_env_vars = ['PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'TMPDIR']
            if platform.system() == "Windows":
                safe_env_vars.extend(['SYSTEMROOT', 'TEMP', 'APPDATA'])

            clean_env = {k: env.get(k, '') for k in safe_env_vars if k in env}
            clean_env['PYTHONUNBUFFERED'] = '1'

            timeout = getattr(agente, 'timeout_shell', 30)

            # ✅ Si usamos pkexec, el diálogo puede tardar en aparecer,
            # así que añadimos un margen al timeout para la autenticación.
            timeout_efectivo = timeout + 30 if usar_pkexec else timeout

            # ✅ Construir el comando final para Popen
            if usar_pkexec:
                # Ya viene con '/bin/sh -c' → no necesita shell=True
                args = comando_ejecutable
                usar_shell = True  # Necesario para que sh -c se interprete
            else:
                args = comando_ejecutable
                usar_shell = True

            proceso = subprocess.Popen(
                args,
                shell=usar_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=clean_env,
                encoding='utf-8',
                errors='replace'
            )

            def cancelar_proceso(token):
                nonlocal proceso
                try:
                    if proceso and proceso.poll() is None:
                        proceso.terminate()
                        time.sleep(0.3)
                        if proceso.poll() is None:
                            proceso.kill()
                        logger.info(f"Proceso shell cancelado: {comando[:50]}...")
                except Exception as e:
                    logger.warning(f"Error cancelando proceso shell: {e}")

            if cancellation_token:
                cancellation_token.agregar_callback(cancelar_proceso)

            try:
                inicio = time.time()
                while True:
                    if cancellation_token and cancellation_token.esta_cancelado():
                        cancelar_proceso(cancellation_token)
                        return False, "Cancelado por usuario", {
                            'error': 'cancelled', 'comando': comando[:100]
                        }

                    if time.time() - inicio > timeout_efectivo:
                        cancelar_proceso(cancellation_token)
                        return False, f"Timeout ({timeout_efectivo}s)", {
                            'error': 'timeout', 'comando': comando[:100]
                        }

                    if proceso.poll() is not None:
                        break

                    time.sleep(0.05)

                stdout, stderr = proceso.communicate(timeout=1)
                codigo = proceso.returncode

            finally:
                if cancellation_token:
                    cancellation_token.eliminar_callback(cancelar_proceso)

            AgentExecutor._actualizar_progreso(agente, 90, "Procesando resultado...")

            stdout = stdout.strip()
            stderr = stderr.strip()

            if len(stdout) > 10000:
                stdout = stdout[:10000] + "\n... (truncado)"
            if len(stderr) > 10000:
                stderr = stderr[:10000] + "\n... (truncado)"

            AgentExecutor._actualizar_progreso(agente, 100, "Comando completado")

            resultado_dict = {
                "codigo": codigo,
                "returncode": codigo,       # ← alias para compatibilidad con LLMs
                "exit_code": codigo,        # ← alias
                "codigo_salida": codigo,    # ← alias
                "stdout": stdout,
                "stderr": stderr,
                "comando_original": comando_original,
                "usado_pkexec": usar_pkexec,
            }

            # ✅ NUEVO: Detección de errores de permisos en el resultado
            if codigo != 0:
                error_lower = (stdout + " " + stderr).lower()
                patrones_permisos = (
                    "permission denied",
                    "permiso denegado",
                    "operation not permitted",
                    "are you root",
                    "must be root",
                    "access denied",
                    "no se pudo abrir el fichero de bloqueo",
                )
                if any(p in error_lower for p in patrones_permisos):
                    resultado_dict['error_permisos'] = True
                    resultado_dict['sugerencia'] = (
                        "El comando falló por permisos. Verifica que:\n"
                        "  1. 'pkexec' está correctamente instalado y configurado\n"
                        "  2. Tu usuario está en el grupo adecuado\n"
                        "  3. El comando no está bloqueado por Polkit"
                    )

            if codigo == 0:
                mensaje = f"Comando ejecutado correctamente (código: {codigo})"
                if usar_pkexec:
                    mensaje += " [con pkexec]"
                if stdout:
                    mensaje += f"\nSalida: {stdout[:200]}..."
                return True, mensaje, resultado_dict
            else:
                mensaje = f"Comando falló (código: {codigo})"
                if usar_pkexec:
                    mensaje += " [con pkexec]"
                if stderr:
                    mensaje += f"\nError: {stderr[:200]}..."
                elif stdout:
                    mensaje += f"\nSalida: {stdout[:200]}..."
                return False, mensaje, resultado_dict

        except subprocess.TimeoutExpired:
            AgentExecutor._actualizar_progreso(agente, 100, "Timeout")
            return False, f"Comando excedió el tiempo límite ({timeout}s)", {}
        except subprocess.SubprocessError as e:
            AgentExecutor._actualizar_progreso(agente, 100, f"Error: {str(e)[:50]}")
            return False, f"Error en subproceso: {str(e)}", {}
        except Exception as e:
            AgentExecutor._actualizar_progreso(agente, 100, f"Error: {str(e)[:50]}")
            return False, f"Error en comando shell: {str(e)}", {}    
    # ============================================================
    # 3. EJECUTOR HTTP
    # ============================================================

    @staticmethod
    def _ejecutar_http(
        agente: Agente,
        contexto: Dict,
        cancellation_token: Optional['CancellationToken'] = None
    ) -> Tuple[bool, str, Dict]:
        """
        Realiza una petición HTTP con sustitución de variables.

        ✅ CARACTERÍSTICAS:
        - Sustitución de variables en URL, headers y body
        - Caché LRU con TTL configurable
        - Rate limiting para APIs externas
        - Soporte para todos los métodos HTTP
        - Cancelación en tiempo real (cierra la sesión)
        - Formato de salida SIEMPRE consistente
        - Manejo granular de errores
        - Timeout configurable por agente
        """
        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {
                'error': 'cancelled', 'cancelled': True,
                'status_code': None, 'headers': {}, 'body': '',
                'json': None, 'url': '', 'elapsed': 0
            }

        AgentExecutor._actualizar_progreso(agente, 10, "Preparando petición HTTP...")

        variables = AgentExecutor._variables_disponibles(agente, contexto)

        url_raw = agente.url_http or ""
        url = AgentExecutor._sustituir_variables(url_raw, variables)

        if not url or not url.strip():
            AgentExecutor._actualizar_progreso(agente, 100, "URL vacía")
            return False, "No hay URL definida", {
                'error': 'empty_url', 'status_code': None, 'headers': {},
                'body': '', 'json': None, 'url': '', 'elapsed': 0
            }

        if not AgentExecutor._validar_url(url):
            AgentExecutor._actualizar_progreso(agente, 100, "URL inválida")
            return False, f"URL inválida: {url}", {
                'error': 'invalid_url', 'status_code': None, 'headers': {},
                'body': '', 'json': None, 'url': url[:100], 'elapsed': 0
            }

        metodo = agente.metodo_http.upper() if agente.metodo_http else "GET"
        if metodo not in ALLOWED_HTTP_METHODS:
            AgentExecutor._actualizar_progreso(
                agente, 100, f"Método no soportado: {metodo}"
            )
            return False, f"Método HTTP no soportado: {metodo}", {
                'error': 'unsupported_method', 'status_code': None,
                'headers': {}, 'body': '', 'json': None,
                'url': url[:100], 'elapsed': 0
            }

        headers = {}
        if agente.headers_http:
            for k, v in agente.headers_http.items():
                if k and v:
                    try:
                        headers[k] = AgentExecutor._sustituir_variables(str(v), variables)
                    except Exception as e:
                        logger.warning(f"Error sustituyendo header '{k}': {e}")
                        headers[k] = str(v)

        if 'User-Agent' not in headers:
            headers['User-Agent'] = DEFAULT_USER_AGENT
        if 'Accept' not in headers:
            headers['Accept'] = 'application/json, */*'

        body_texto_raw = agente.body_http or ""
        body_texto = AgentExecutor._sustituir_variables(body_texto_raw, variables)

        body = None
        content_type = headers.get('Content-Type', '')

        if body_texto and metodo in ("POST", "PUT", "PATCH"):
            if 'application/json' in content_type.lower() or body_texto.strip().startswith(('{', '[')):
                try:
                    body = json.loads(body_texto)
                except json.JSONDecodeError:
                    body = body_texto
                    if 'Content-Type' not in headers:
                        headers['Content-Type'] = 'text/plain'
            else:
                body = body_texto
                if 'Content-Type' not in headers:
                    headers['Content-Type'] = 'text/plain'

        timeout = getattr(agente, 'timeout_http', 30)
        if timeout < 1:
            timeout = 30
            logger.warning(f"Timeout HTTP inválido ({agente.timeout_http}), usando 30s")

        AgentExecutor._actualizar_progreso(
            agente, 30, f"{metodo} {url[:60]}..."
        )

        http_cache = AgentExecutor._get_http_cache()
        cached_result = http_cache.get(url, metodo, headers, body)
        if cached_result is not None:
            AgentExecutor._actualizar_progreso(agente, 100, "✅ Respuesta desde caché")
            logger.debug(f"HTTP caché hit: {metodo} {url[:60]}...")
            return True, "Respuesta desde caché", cached_result

        rate_limiter = AgentExecutor._get_rate_limiter()
        rate_limiter.wait()

        AgentExecutor._actualizar_progreso(agente, 50, "Enviando petición...")

        import threading
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        session = None
        response = None
        error = None
        completed = threading.Event()
        cancelar_peticion = None

        try:
            session = requests.Session()
            retry = Retry(
                total=2,
                backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"]
            )
            adapter = HTTPAdapter(max_retries=retry)
            session.mount('http://', adapter)
            session.mount('https://', adapter)

            def cancelar_peticion(token):
                nonlocal session
                try:
                    if session:
                        session.close()
                        logger.info(f"🛑 Petición HTTP cancelada: {url[:60]}...")
                except Exception as e:
                    logger.warning(f"Error cancelando petición HTTP: {e}")

            if cancellation_token:
                cancellation_token.agregar_callback(cancelar_peticion)

            def hacer_peticion():
                nonlocal response, error
                try:
                    kwargs = {
                        'headers': headers,
                        'timeout': timeout,
                        'allow_redirects': True,
                        'verify': True,
                    }

                    if metodo in ("POST", "PUT", "PATCH"):
                        if isinstance(body, dict):
                            kwargs['json'] = body
                        else:
                            kwargs['data'] = body

                    if metodo == "GET":
                        response = session.get(url, **kwargs)
                    elif metodo == "POST":
                        response = session.post(url, **kwargs)
                    elif metodo == "PUT":
                        response = session.put(url, **kwargs)
                    elif metodo == "DELETE":
                        response = session.delete(url, **kwargs)
                    elif metodo == "PATCH":
                        response = session.patch(url, **kwargs)
                    elif metodo == "HEAD":
                        response = session.head(url, **kwargs)
                    elif metodo == "OPTIONS":
                        response = session.options(url, **kwargs)

                except requests.exceptions.Timeout as e:
                    error = e
                except requests.exceptions.ConnectionError as e:
                    error = e
                except requests.exceptions.SSLError as e:
                    error = e
                except requests.exceptions.TooManyRedirects as e:
                    error = e
                except requests.exceptions.RequestException as e:
                    error = e
                except Exception as e:
                    error = e
                finally:
                    completed.set()

            thread = threading.Thread(target=hacer_peticion, daemon=True)
            thread.start()

            inicio = time.time()
            while not completed.is_set():
                if cancellation_token and cancellation_token.esta_cancelado():
                    session.close()
                    AgentExecutor._actualizar_progreso(
                        agente, 100, "⛔ Cancelado por usuario"
                    )
                    return False, "Cancelado por usuario", {
                        'error': 'cancelled', 'cancelled': True,
                        'status_code': None, 'headers': {}, 'body': '',
                        'json': None, 'url': url[:100],
                        'elapsed': time.time() - inicio
                    }

                if time.time() - inicio > timeout + 5:
                    session.close()
                    AgentExecutor._actualizar_progreso(agente, 100, "⏱️ Timeout")
                    return False, f"Timeout en petición HTTP ({timeout}s)", {
                        'error': 'timeout', 'status_code': None, 'headers': {},
                        'body': '', 'json': None, 'url': url[:100],
                        'elapsed': timeout
                    }

                completed.wait(0.05)

            if error:
                raise error

            if response is None:
                raise requests.exceptions.RequestException("No se recibió respuesta")

        except requests.exceptions.Timeout:
            AgentExecutor._actualizar_progreso(agente, 100, "⏱️ Timeout")
            return False, f"Timeout en petición HTTP ({timeout}s)", {
                'error': 'timeout', 'status_code': None, 'headers': {},
                'body': '', 'json': None, 'url': url[:100], 'elapsed': timeout
            }

        except requests.exceptions.ConnectionError as e:
            AgentExecutor._actualizar_progreso(agente, 100, "❌ Error de conexión")
            return False, f"Error de conexión: {url}", {
                'error': 'connection_error', 'status_code': None,
                'headers': {}, 'body': '', 'json': None,
                'url': url[:100], 'elapsed': 0, 'detail': str(e)
            }

        except requests.exceptions.SSLError as e:
            AgentExecutor._actualizar_progreso(agente, 100, "❌ Error SSL")
            return False, f"Error SSL en: {url}", {
                'error': 'ssl_error', 'status_code': None, 'headers': {},
                'body': '', 'json': None, 'url': url[:100],
                'elapsed': 0, 'detail': str(e)
            }

        except requests.exceptions.TooManyRedirects as e:
            AgentExecutor._actualizar_progreso(
                agente, 100, "❌ Demasiadas redirecciones"
            )
            return False, f"Demasiadas redirecciones en: {url}", {
                'error': 'too_many_redirects', 'status_code': None,
                'headers': {}, 'body': '', 'json': None,
                'url': url[:100], 'elapsed': 0, 'detail': str(e)
            }

        except requests.exceptions.RequestException as e:
            AgentExecutor._actualizar_progreso(
                agente, 100, f"❌ Error: {str(e)[:50]}"
            )
            return False, f"Error en petición HTTP: {str(e)}", {
                'error': 'request_exception', 'status_code': None,
                'headers': {}, 'body': '', 'json': None,
                'url': url[:100], 'elapsed': 0, 'detail': str(e)
            }

        except Exception as e:
            AgentExecutor._actualizar_progreso(
                agente, 100, f"❌ Error inesperado: {str(e)[:50]}"
            )
            logger.exception(f"Error inesperado en HTTP: {e}")
            return False, f"Error inesperado en HTTP: {str(e)}", {
                'error': 'unexpected', 'status_code': None, 'headers': {},
                'body': '', 'json': None, 'url': url[:100],
                'elapsed': 0, 'detail': str(e)
            }

        finally:
            if cancellation_token and cancelar_peticion:
                cancellation_token.eliminar_callback(cancelar_peticion)

        AgentExecutor._actualizar_progreso(
            agente, 80, f"📥 Procesando respuesta {response.status_code}..."
        )

        body_size = len(response.content)
        body_truncated = body_size > MAX_HTTP_BODY_SIZE

        try:
            body_text = response.text
        except UnicodeDecodeError:
            import base64
            body_text = (
                f"[BINARY DATA: "
                f"{base64.b64encode(response.content).decode('ascii')[:100]}...]"
            )

        if body_truncated:
            body_text = body_text[:10000] + "\n... (truncado)"

        json_data = None
        if response.headers.get('Content-Type', '').startswith('application/json'):
            try:
                json_data = response.json()
            except (ValueError, json.JSONDecodeError):
                try:
                    json_data = json.loads(body_text[:10000])
                except (ValueError, json.JSONDecodeError):
                    pass

        resultado = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": body_text,
            "body_truncated": body_truncated,
            "body_size": body_size,
            "json": json_data,
            "url": response.url,
            "elapsed": response.elapsed.total_seconds(),
            "error": None,
        }

        if 200 <= response.status_code < 400:
            http_cache.put(url, metodo, headers, body, resultado)
            logger.debug(
                f"HTTP cacheado: {metodo} {url[:60]}... "
                f"(status: {response.status_code})"
            )
        else:
            logger.debug(
                f"HTTP no cacheado: {metodo} {url[:60]}... "
                f"(status: {response.status_code})"
            )

        AgentExecutor._actualizar_progreso(
            agente, 100, f"✅ Completado (status: {response.status_code})"
        )

        if 200 <= response.status_code < 300:
            mensaje = f"Petición exitosa (status: {response.status_code})"
            if json_data is not None:
                if isinstance(json_data, dict):
                    claves = list(json_data.keys())[:3]
                    mensaje += f" - JSON claves: {claves}"
                elif isinstance(json_data, list):
                    mensaje += f" - JSON array: {len(json_data)} items"
            return True, mensaje, resultado
        else:
            mensaje = f"Petición falló (status: {response.status_code})"
            if json_data is not None:
                if isinstance(json_data, dict):
                    error_msg = (
                        json_data.get('error') or
                        json_data.get('message') or
                        json_data.get('detail')
                    )
                    if error_msg:
                        mensaje += f" - {error_msg[:100]}"
            return False, mensaje, resultado

    # ============================================================
    # 4. EJECUTOR FILE (CON FIX: FALLA EN VEZ DE INVENTAR)
    # ============================================================

    @staticmethod
    def _ejecutar_file(
        agente: Agente,
        contexto: Dict,
        cancellation_token: Optional['CancellationToken'] = None
    ) -> Tuple[bool, str, Dict]:
        """
        Realiza operaciones con archivos con sustitución de variables y
        soporte para cancelación.

        MEJORAS IMPLEMENTADAS:
        - ✅ Extracción inteligente de contenido (no serializa todo a JSON)
        - ✅ Prioriza 'contenido' y 'resultado' explícitos
        - ✅ Si hay una sola dependencia, extrae su contenido relevante
        - ✅ Si hay múltiples, busca el más útil por prioridad:
              respuesta (LLM) > contenido (File) > json (HTTP) > body (HTTP) > resultado (Python)
        - ✅ Strings se escriben directamente SIN comillas ni escapes
        - ✅ Solo dicts y lists se serializan como JSON (cuando corresponde)
        - ✅ Logging detallado para diagnóstico
        - ✅ FIX v3: FALLA en vez de inventar contenido fallback
        - ✅ FIX v3: extracción recursiva con anti-ciclos

        Operaciones soportadas:
        - leer: Lee un archivo y retorna su contenido
        - escribir: Escribe contenido en un archivo
        - copiar: Copia un archivo
        - mover: Mueve/renombra un archivo
        - eliminar: Elimina un archivo o directorio
        """
        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {'error': 'cancelled'}

        AgentExecutor._actualizar_progreso(
            agente, 20, "Preparando operación de archivo..."
        )

        # ── Sustituir variables ──
        variables = AgentExecutor._variables_disponibles(agente, contexto)
        origen = AgentExecutor._sustituir_variables(agente.archivo_origen, variables)
        destino = AgentExecutor._sustituir_variables(agente.archivo_destino, variables)
        operacion = agente.operacion_file or "leer"

        AgentExecutor._actualizar_progreso(agente, 40, f"Operación: {operacion}")

        # ── Validar según operación ──
        if operacion in ("leer", "eliminar"):
            ruta_archivo = origen
            if not ruta_archivo:
                AgentExecutor._actualizar_progreso(
                    agente, 100, "Ruta no especificada"
                )
                return False, (
                    f"No se especificó ruta para operación: {operacion}"
                ), {}
            if not AgentExecutor._validar_ruta_archivo(ruta_archivo):
                AgentExecutor._actualizar_progreso(agente, 100, "Ruta inválida")
                return False, f"Ruta inválida: {ruta_archivo}", {}

        elif operacion in ("escribir", "copiar", "mover"):
            if not destino:
                AgentExecutor._actualizar_progreso(
                    agente, 100, "Destino no especificado"
                )
                return False, (
                    f"No se especificó destino para operación: {operacion}"
                ), {}
            if not AgentExecutor._validar_ruta_archivo(destino):
                AgentExecutor._actualizar_progreso(
                    agente, 100, "Ruta destino inválida"
                )
                return False, f"Ruta destino inválida: {destino}", {}

            if operacion in ("copiar", "mover") and origen:
                if not AgentExecutor._validar_ruta_archivo(origen):
                    AgentExecutor._actualizar_progreso(
                        agente, 100, "Ruta origen inválida"
                    )
                    return False, f"Ruta origen inválida: {origen}", {}
                ruta_archivo = origen
            else:
                ruta_archivo = destino
        else:
            AgentExecutor._actualizar_progreso(
                agente, 100, f"Operación no soportada: {operacion}"
            )
            return False, f"Operación de archivo no soportada: {operacion}", {}

        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de operación", {'error': 'cancelled'}

        try:
            # ══════════════════════════════════════════════════════════
            # LEER
            # ══════════════════════════════════════════════════════════
            if operacion == "leer":
                if not os.path.exists(ruta_archivo):
                    AgentExecutor._actualizar_progreso(
                        agente, 100, "Archivo no encontrado"
                    )
                    return False, f"Archivo no encontrado: {ruta_archivo}", {}

                if not os.path.isfile(ruta_archivo):
                    AgentExecutor._actualizar_progreso(
                        agente, 100, "No es un archivo"
                    )
                    return False, f"No es un archivo: {ruta_archivo}", {}

                tamaño = os.path.getsize(ruta_archivo)
                if tamaño > MAX_BYTES_LECTURA_ARCHIVO:
                    AgentExecutor._actualizar_progreso(
                        agente, 100, "Archivo demasiado grande"
                    )
                    return False, (
                        f"Archivo demasiado grande ({tamaño} bytes > "
                        f"{MAX_BYTES_LECTURA_ARCHIVO} límite)"
                    ), {"archivo": ruta_archivo, "tamaño": tamaño}

                AgentExecutor._actualizar_progreso(
                    agente, 70, "Leyendo archivo..."
                )

                contenido = ""
                chunk_size = 8192
                with open(ruta_archivo, 'r', encoding='utf-8', errors='replace') as f:
                    while True:
                        if cancellation_token and cancellation_token.esta_cancelado():
                            return False, "Cancelado durante lectura", {
                                'error': 'cancelled',
                                'archivo': ruta_archivo,
                                'bytes_leidos': len(contenido)
                            }
                        chunk = f.read(chunk_size)
                        if not chunk:
                            break
                        contenido += chunk

                AgentExecutor._actualizar_progreso(agente, 100, "Archivo leído")

                resultado = {
                    "archivo": ruta_archivo,
                    "tamaño": tamaño,
                    "contenido": contenido,
                    "total_caracteres": len(contenido)
                }

                if ruta_archivo.endswith('.json'):
                    try:
                        resultado["json"] = json.loads(contenido)
                    except json.JSONDecodeError:
                        pass

                # Aplicar el modo de salida elegido por el usuario
                modo_salida = getattr(agente, 'modo_salida_file', 'auto') or 'auto'
                resultado = AgentExecutor._aplicar_modo_salida_file(
                    resultado, modo_salida, contenido
                )

                return True, (
                    f"Archivo leído: {ruta_archivo} "
                    f"({len(contenido)} caracteres)"
                ), resultado

            # ══════════════════════════════════════════════════════════
            # ESCRIBIR (REFACTORIZADO — FALLA EN VEZ DE INVENTAR)
            # ══════════════════════════════════════════════════════════
            elif operacion == "escribir":
                # ── Obtener contenido a escribir (extracción inteligente) ──
                contenido = None

                # Prioridad 1: 'contenido' explícito en el contexto raíz
                if 'contenido' in contexto:
                    contenido = contexto['contenido']
                    logger.debug(
                        "File: usando 'contenido' explícito del contexto"
                    )

                # Prioridad 2: 'resultado' explícito en el contexto raíz
                elif 'resultado' in contexto:
                    contenido = contexto['resultado']
                    logger.debug(
                        "File: usando 'resultado' explícito del contexto"
                    )

                # Prioridad 3: Extracción inteligente desde las dependencias
                elif contexto:
                    if len(contexto) == 1:
                        # Una sola dependencia → extraer su contenido
                        valor_unico = next(iter(contexto.values()))
                        contenido = AgentExecutor._extraer_contenido_relevante(
                            valor_unico
                        )
                        logger.debug(
                            f"File: extraído contenido relevante de la única "
                            f"dependencia (tipo: {type(valor_unico).__name__})"
                        )
                    else:
                        # Múltiples dependencias → buscar por prioridad
                        for clave, valor in contexto.items():
                            if not isinstance(valor, dict):
                                continue
                            # Delegar a la extracción recursiva
                            candidato = AgentExecutor._extraer_contenido_relevante(
                                valor
                            )
                            if candidato is not valor:
                                contenido = candidato
                                logger.debug(
                                    f"File: extraído contenido relevante del "
                                    f"agente '{clave}'"
                                )
                                break

                        # Fallback: extracción del primer valor disponible
                        if contenido is None:
                            primer_valor = next(iter(contexto.values()))
                            contenido = AgentExecutor._extraer_contenido_relevante(
                                primer_valor
                            )
                            logger.debug(
                                "File: fallback con extracción inteligente "
                                "del primer valor"
                            )

                # ── Prioridad 4: SIN CONTENIDO → ERROR, NO FALLBACK ──
                if contenido is None:
                    claves_contexto = list(contexto.keys()) if contexto else []
                    mensaje_error = (
                        f"File: no se encontró contenido para escribir en "
                        f"'{ruta_archivo}'. El agente '{agente.nombre}' no "
                        f"recibió datos útiles de sus dependencias. "
                        f"Claves del contexto: "
                        f"{claves_contexto if claves_contexto else '(vacío)'}"
                    )
                    logger.error(mensaje_error)
                    AgentExecutor._actualizar_progreso(
                        agente, 100, "❌ Sin contenido"
                    )

                    return False, mensaje_error, {
                        'error': 'no_content',
                        'archivo': ruta_archivo,
                        'operacion': operacion,
                        'contexto_claves': claves_contexto,
                        'contexto_preview': (
                            str(contexto)[:500] if contexto else ''
                        ),
                    }

                # ── Convertir a string (respetando el tipo original) ──
                if isinstance(contenido, str):
                    # ✅ String → usar directamente SIN comillas ni escapes
                    contenido_str = contenido
                elif isinstance(contenido, dict):
                    contenido_str = json.dumps(
                        contenido, indent=2, default=str, ensure_ascii=False
                    )
                elif isinstance(contenido, list):
                    contenido_str = json.dumps(
                        contenido, indent=2, default=str, ensure_ascii=False
                    )
                elif isinstance(contenido, (int, float, bool)):
                    contenido_str = str(contenido)
                else:
                    contenido_str = str(contenido)

                logger.debug(
                    f"File: contenido final = {len(contenido_str)} caracteres"
                )

                # ── Crear directorio si es necesario ──
                directorio = os.path.dirname(ruta_archivo)
                if directorio:
                    os.makedirs(directorio, exist_ok=True)

                AgentExecutor._actualizar_progreso(
                    agente, 70, "Escribiendo archivo..."
                )

                # ── Escribir con verificación de cancelación ──
                chunk_size = 8192
                with open(ruta_archivo, 'w', encoding='utf-8') as f:
                    for i in range(0, len(contenido_str), chunk_size):
                        if cancellation_token and cancellation_token.esta_cancelado():
                            return False, "Cancelado durante escritura", {
                                'error': 'cancelled',
                                'archivo': ruta_archivo,
                                'caracteres_escritos': i
                            }
                        f.write(contenido_str[i:i + chunk_size])

                AgentExecutor._actualizar_progreso(
                    agente, 100, "Archivo escrito"
                )

                resultado = {
                    "archivo": ruta_archivo,
                    "caracteres_escritos": len(contenido_str),
                    "contenido_preview": (
                        contenido_str[:200]
                        if len(contenido_str) > 200
                        else contenido_str
                    )
                }

                return True, (
                    f"Archivo escrito: {ruta_archivo} "
                    f"({len(contenido_str)} caracteres)"
                ), resultado

            # ══════════════════════════════════════════════════════════
            # COPIAR
            # ══════════════════════════════════════════════════════════
            elif operacion == "copiar":
                if not os.path.exists(origen):
                    AgentExecutor._actualizar_progreso(
                        agente, 100, "Origen no encontrado"
                    )
                    return False, f"Archivo origen no encontrado: {origen}", {}

                if not os.path.isfile(origen):
                    AgentExecutor._actualizar_progreso(
                        agente, 100, "No es un archivo"
                    )
                    return False, f"No es un archivo: {origen}", {}

                directorio_destino = os.path.dirname(destino)
                if directorio_destino:
                    os.makedirs(directorio_destino, exist_ok=True)

                AgentExecutor._actualizar_progreso(agente, 70, "Copiando archivo...")
                shutil.copy2(origen, destino)

                AgentExecutor._actualizar_progreso(agente, 100, "Archivo copiado")

                resultado = {
                    "origen": origen,
                    "destino": destino,
                    "tamaño": os.path.getsize(destino)
                }

                return True, f"Archivo copiado: {origen} → {destino}", resultado

            # ══════════════════════════════════════════════════════════
            # MOVER
            # ══════════════════════════════════════════════════════════
            elif operacion == "mover":
                if not os.path.exists(origen):
                    AgentExecutor._actualizar_progreso(
                        agente, 100, "Origen no encontrado"
                    )
                    return False, f"Archivo origen no encontrado: {origen}", {}

                if not os.path.isfile(origen):
                    AgentExecutor._actualizar_progreso(
                        agente, 100, "No es un archivo"
                    )
                    return False, f"No es un archivo: {origen}", {}

                directorio_destino = os.path.dirname(destino)
                if directorio_destino:
                    os.makedirs(directorio_destino, exist_ok=True)

                AgentExecutor._actualizar_progreso(agente, 70, "Moviendo archivo...")
                shutil.move(origen, destino)

                AgentExecutor._actualizar_progreso(agente, 100, "Archivo movido")

                resultado = {
                    "origen": origen,
                    "destino": destino,
                    "tamaño": (
                        os.path.getsize(destino)
                        if os.path.exists(destino)
                        else None
                    )
                }

                return True, f"Archivo movido: {origen} → {destino}", resultado

            # ══════════════════════════════════════════════════════════
            # ELIMINAR
            # ══════════════════════════════════════════════════════════
            elif operacion == "eliminar":
                if not os.path.exists(ruta_archivo):
                    AgentExecutor._actualizar_progreso(
                        agente, 100, "Archivo no encontrado"
                    )
                    return False, f"Archivo no encontrado: {ruta_archivo}", {}

                if os.path.isdir(ruta_archivo):
                    if (ruta_archivo in DANGEROUS_DIRS or
                            os.path.dirname(ruta_archivo) in DANGEROUS_DIRS):
                        AgentExecutor._actualizar_progreso(
                            agente, 100, "No se permite eliminar"
                        )
                        return False, (
                            f"No se permite eliminar directorios del sistema: "
                            f"{ruta_archivo}"
                        ), {}

                    AgentExecutor._actualizar_progreso(
                        agente, 70, "Eliminando directorio..."
                    )
                    shutil.rmtree(ruta_archivo)
                    mensaje = f"Directorio eliminado: {ruta_archivo}"
                else:
                    AgentExecutor._actualizar_progreso(
                        agente, 70, "Eliminando archivo..."
                    )
                    os.remove(ruta_archivo)
                    mensaje = f"Archivo eliminado: {ruta_archivo}"

                AgentExecutor._actualizar_progreso(agente, 100, "Eliminado")
                return True, mensaje, {"archivo": ruta_archivo, "eliminado": True}

            else:
                AgentExecutor._actualizar_progreso(
                    agente, 100, f"Operación no soportada: {operacion}"
                )
                return False, (
                    f"Operación de archivo no soportada: {operacion}"
                ), {}

        except PermissionError as e:
            AgentExecutor._actualizar_progreso(agente, 100, "Permiso denegado")
            return False, (
                f"Permiso denegado para operación '{operacion}' en: "
                f"{ruta_archivo} - {e}"
            ), {
                'error': 'permission_denied',
                'archivo': ruta_archivo,
                'operacion': operacion
            }
        except OSError as e:
            AgentExecutor._actualizar_progreso(agente, 100, f"Error: {str(e)[:50]}")
            return False, f"Error en operación de archivo: {str(e)}", {
                'error': 'os_error',
                'archivo': ruta_archivo,
                'operacion': operacion,
                'detalle': str(e)
            }
        except Exception as e:
            AgentExecutor._actualizar_progreso(agente, 100, f"Error: {str(e)[:50]}")
            return False, f"Error inesperado en operación de archivo: {str(e)}", {
                'error': 'unexpected',
                'archivo': ruta_archivo,
                'operacion': operacion,
                'detalle': str(e)
            }

    @staticmethod
    def _aplicar_modo_salida_file(
        resultado: Dict,
        modo: str,
        contenido_texto: str
    ) -> Dict:
        """
        Ajusta el resultado devuelto por un agente File según el modo elegido
        por el usuario. Controla qué "ve" el siguiente agente cuando consume
        el resultado a través de la extracción inteligente.

        Modos:
        - "auto"      → comportamiento actual (heurística de extracción)
        - "contenido" → solo expone `contenido` como string crudo
        - "texto"     → alias de "contenido"
        - "json"      → solo expone `json` (parseado); oculta `contenido`
        """
        modo = (modo or "auto").lower()

        if modo == "auto":
            return resultado

        if modo in ("contenido", "texto"):
            return {
                "archivo": resultado.get("archivo", ""),
                "tamaño": resultado.get("tamaño", len(contenido_texto)),
                "contenido": contenido_texto,
                "total_caracteres": len(contenido_texto),
                "modo_salida": "contenido",
            }

        if modo == "json":
            json_data = resultado.get("json")
            if json_data is None:
                try:
                    json_data = json.loads(contenido_texto)
                except (json.JSONDecodeError, ValueError):
                    json_data = None

            return {
                "archivo": resultado.get("archivo", ""),
                "tamaño": resultado.get("tamaño", len(contenido_texto)),
                "json": json_data,
                "modo_salida": "json",
            }

        return resultado

        # ============================================================
    # 5. EJECUTOR LLM (DeepSeek)
    # ============================================================

    @staticmethod
    def _ejecutar_llm(
        agente: Agente,
        contexto: Dict,
        cancellation_token: Optional['CancellationToken'] = None
    ) -> Tuple[bool, str, Dict]:
        """Llama a la API de DeepSeek con el prompt del agente."""

        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {'error': 'cancelled'}

        AgentExecutor._actualizar_progreso(agente, 20, "Preparando prompt LLM...")

        if not agente.prompt_llm:
            AgentExecutor._actualizar_progreso(agente, 100, "Prompt vacío")
            return False, "No hay prompt definido", {}

        try:
            import openai
        except ImportError:
            AgentExecutor._actualizar_progreso(agente, 100, "openai no instalado")
            return False, "openai no instalado. Ejecuta: pip install openai", {}

        variables = AgentExecutor._variables_disponibles(agente, contexto)
        prompt_procesado = AgentExecutor._sustituir_variables(
            agente.prompt_llm, variables
        )
        se_sustituyo_algo = prompt_procesado != agente.prompt_llm

        try:
            from core.llm_client import LLMClient
            _cliente_llm = LLMClient()
            api_key = _cliente_llm.api_key
            base_url = _cliente_llm.base_url
        except Exception as e:
            AgentExecutor._actualizar_progreso(agente, 100, "Error cargando config LLM")
            return False, (
                f"No se pudo inicializar el cliente LLM: {e}\n"
                f"Verifica la configuración de DEEPSEEK_API_KEY.\n"
                f"Prueba: python main.py --check-env"
            ), {'error': 'llm_client_init', 'detalle': str(e)}

        if not api_key:
            AgentExecutor._actualizar_progreso(agente, 100, "Falta DEEPSEEK_API_KEY")
            return False, (
                "No se encontró DEEPSEEK_API_KEY en ninguna fuente.\n"
                "Prueba: python main.py --check-env"
            ), {'error': 'missing_api_key'}

        modelo = getattr(agente, 'modelo_llm', 'deepseek-v4-flash')
        reasoning_effort = getattr(agente, 'reasoning_effort_llm', 'low') or 'low'

        # ✅ NUEVO: control explícito de thinking. Desactivado por defecto
        # para no consumir tokens del presupuesto de respuesta.
        thinking_enabled = bool(getattr(agente, 'thinking_enabled_llm', False))

        # ✅ Blindaje: el thinking mode consume tokens del presupuesto
        MIN_TOKENS_SEGUROS = 4000
        max_tokens_efectivos = int(getattr(agente, 'max_tokens_llm', MIN_TOKENS_SEGUROS) or MIN_TOKENS_SEGUROS)
        if max_tokens_efectivos < MIN_TOKENS_SEGUROS:
            logger.warning(
                f"⚠️ [{agente.nombre}] max_tokens={max_tokens_efectivos} es bajo "
                f"para un modelo con thinking. Subiendo a {MIN_TOKENS_SEGUROS}."
            )
            max_tokens_efectivos = MIN_TOKENS_SEGUROS

        AgentExecutor._actualizar_progreso(agente, 40, f"Consultando {modelo}...")

        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de llamar a la API", {'error': 'cancelled'}

        try:
            rate_limiter = AgentExecutor._get_rate_limiter()
            rate_limiter.wait()

            client = openai.OpenAI(api_key=api_key, base_url=base_url)

            messages = [
                {
                    "role": "system",
                    "content": (
                        "Eres un asistente útil y preciso. "
                        "Responde directamente con lo que se te pide, sin preámbulos."
                    )
                },
                {"role": "user", "content": prompt_procesado}
            ]

            if contexto and not se_sustituyo_algo:
                contexto_visible = {
                    k: v for k, v in contexto.items()
                    if k not in ('llm_base_url', 'llm_api_key', 'openai_api_key')
                }
                if contexto_visible:
                    messages[1]["content"] += (
                        "\n\nContexto adicional:\n" +
                        json.dumps(
                            contexto_visible, indent=2, default=str,
                            ensure_ascii=False
                        )
                    )

            AgentExecutor._actualizar_progreso(
                agente, 60, "Esperando respuesta..."
            )

            start_time = time.time()

            import threading
            response = None
            error = None
            completed = threading.Event()

            def hacer_llamada():
                nonlocal response, error
                try:
                    response = client.chat.completions.create(
                        model=modelo,
                        messages=messages,
                        temperature=agente.temperatura_llm,
                        max_tokens=max_tokens_efectivos,
                        reasoning_effort=reasoning_effort,
                        stream=False,
                        extra_body={
                            "thinking": {
                                "type": "enabled" if thinking_enabled else "disabled"
                            }
                        },
                        timeout=60
                    )
                except Exception as e:
                    error = e
                finally:
                    completed.set()

            thread = threading.Thread(target=hacer_llamada, daemon=True)
            thread.start()

            timeout = 60
            inicio = time.time()
            while not completed.is_set():
                if cancellation_token and cancellation_token.esta_cancelado():
                    AgentExecutor._actualizar_progreso(
                        agente, 100, "⛔ Cancelado por usuario"
                    )
                    return False, (
                        "Cancelado por usuario durante la llamada LLM"
                    ), {
                        'error': 'cancelled',
                        'modelo': modelo,
                        'tiempo_espera': time.time() - inicio
                    }

                if time.time() - inicio > timeout + 5:
                    AgentExecutor._actualizar_progreso(agente, 100, "⏱️ Timeout")
                    return False, f"Timeout en llamada LLM ({timeout}s)", {
                        'error': 'timeout',
                        'modelo': modelo,
                        'tiempo_espera': timeout
                    }

                completed.wait(0.1)

            if error:
                raise error

            if response is None or not response.choices:
                raise openai.APIError("No se recibió respuesta de la API")

            elapsed_time = time.time() - start_time

            respuesta = response.choices[0].message.content if response.choices else ""

            if not respuesta or not respuesta.strip():
                reasoning = getattr(
                    response.choices[0].message, 'reasoning_content', None
                )
                if reasoning:
                    tokens_info = {}
                    if response.usage:
                        tokens_info = {
                            'prompt': response.usage.prompt_tokens,
                            'completion': response.usage.completion_tokens,
                            'total': response.usage.total_tokens
                        }
                    return False, (
                        f"⚠️ LLM solo devolvió razonamiento (no respuesta final). "
                        f"Aumenta max_tokens (actual: {agente.max_tokens_llm}). "
                        f"Tokens usados: {tokens_info.get('total', '?')}"
                    ), {
                        'error': 'only_reasoning',
                        'modelo': modelo,
                        'razonamiento_preview': reasoning[:200],
                        'tokens_uso': tokens_info
                    }
                else:
                    return False, (
                        f"LLM devolvió respuesta vacía (modelo: {modelo}, "
                        f"tokens: {response.usage.total_tokens if response.usage else '?'})"
                    ), {
                        'error': 'empty_response',
                        'modelo': modelo
                    }

            prompt_lower = prompt_procesado.lower()
            pide_generar = any(
                kw in prompt_lower for kw in (
                    "genera", "escribe", "redacta", "crea", "mensaje",
                    "texto", "resumen", "explica", "describe", "elabora",
                    "lista", "enumera", "traduce"
                )
            )

            if pide_generar and len(respuesta.strip()) < 3:
                tokens_usados = response.usage.total_tokens if response.usage else None
                return False, (
                    f"⚠️ El LLM devolvió una respuesta truncada: '{respuesta}'\n"
                    f"Esto ocurre cuando max_tokens es demasiado bajo "
                    f"(actual: {agente.max_tokens_llm}) o el modelo gastó "
                    f"tokens en razonamiento.\n"
                    f"Sugerencia: aumenta max_tokens a 500 o más."
                ), {
                    'error': 'truncated_response',
                    'modelo': modelo,
                    'respuesta_truncada': respuesta,
                    'max_tokens': agente.max_tokens_llm,
                    'tokens_usados': tokens_usados
                }

            palabras_thinking = [
                "We need", "The user", "I need to", "Let me", "First,",
                "Okay,", "Alright,", "Hmm,", "So,", "Now,"
            ]
            respuesta_lower = respuesta.strip()[:50].lower()
            es_razonamiento = any(
                respuesta_lower.startswith(p.lower()) for p in palabras_thinking
            )

            if es_razonamiento and len(respuesta) > 300:
                logger.warning(
                    f"⚠️ LLM parece haber devuelto razonamiento en lugar "
                    f"de respuesta. Modelo: {modelo}, Longitud: {len(respuesta)}, "
                    f"Inicio: {respuesta[:100]}"
                )

            AgentExecutor._actualizar_progreso(
                agente, 90, "Procesando respuesta..."
            )

            # ── v5 FIX: parsear JSON y exponer respuesta_limpia ──
            respuesta_limpia = _limpiar_fences_markdown(respuesta)
            json_auto = _parsear_json_robusto(respuesta_limpia)

            # ✅ NUEVO: Detección de JSON truncado.
            # Si la respuesta parece JSON ({...} o [...]) pero no se pudo
            # parsear, es probable que esté cortada por max_tokens insuficiente.
            # Fallar explícitamente en vez de devolver un resultado parcial.
            if json_auto is None and respuesta_limpia.strip().startswith(('{', '[')):
                error_msg = (
                    f"La respuesta del LLM parece ser un JSON truncado "
                    f"({len(respuesta_limpia)} caracteres recibidos). "
                    f"Aumenta 'max_tokens_llm' (actual: {agente.max_tokens_llm}) "
                    f"o desactiva el modo 'thinking' en este agente."
                )
                logger.error(f"[{agente.nombre}] {error_msg}")
                AgentExecutor._actualizar_progreso(agente, 100, "❌ JSON truncado")
                return False, error_msg, {
                    'error': 'truncated_json',
                    'modelo': modelo,
                    'raw_response': respuesta[:500],
                    'total_length': len(respuesta),
                    'max_tokens_actual': agente.max_tokens_llm,
                }

            # log para debug si falla el parse pero parece JSON
            if json_auto is None and ('{' in respuesta_limpia or '[' in respuesta_limpia):
                logger.debug(
                    f"[{agente.nombre}] No se pudo parsear JSON auto, "
                    f"respuesta_limpia[:200]={respuesta_limpia[:200]}"
                )

            resultado = {
                "modelo": modelo,
                "prompt": prompt_procesado,
                "respuesta": respuesta,
                "respuesta_limpia": respuesta_limpia,
                "json": json_auto,
                "_av_respuesta": respuesta,
                "_av_respuesta_limpia": respuesta_limpia,
                "_av_json": json_auto,
                "tiempo_respuesta": elapsed_time,
                "tokens_uso": {
                    "prompt": response.usage.prompt_tokens if response.usage else 0,
                    "completion": response.usage.completion_tokens if response.usage else 0,
                    "total": response.usage.total_tokens if response.usage else 0
                }
            }

            # detector de resultados sospechosos (warning, no error)
            es_sospechoso, razon = _es_resultado_sospechoso(
                json_auto if json_auto is not None else respuesta_limpia
            )
            if es_sospechoso and len(respuesta_limpia) < 20:
                logger.warning(f"[{agente.nombre}] Resultado sospechoso: {razon}")

            AgentExecutor._actualizar_progreso(agente, 100, "LLM respondió")
            preview = respuesta_limpia[:200] if len(respuesta_limpia) > 20 else respuesta[:200]
            return True, f"DeepSeek respondió: {preview}...", resultado

        except openai.APIError as e:
            AgentExecutor._actualizar_progreso(
                agente, 100, f"Error API: {str(e)[:50]}"
            )
            return False, f"Error de API de DeepSeek: {str(e)}", {
                'error': 'api_error', 'detalle': str(e)
            }
        except openai.APIConnectionError as e:
            AgentExecutor._actualizar_progreso(agente, 100, "Error de conexión")
            return False, f"Error de conexión con DeepSeek: {str(e)}", {
                'error': 'connection_error', 'detalle': str(e)
            }
        except openai.APITimeoutError as e:
            AgentExecutor._actualizar_progreso(agente, 100, "Timeout")
            return False, f"Timeout en DeepSeek: {str(e)}", {
                'error': 'timeout', 'detalle': str(e)
            }
        except openai.RateLimitError as e:
            AgentExecutor._actualizar_progreso(agente, 100, "Rate limit")
            return False, f"Rate limit excedido en DeepSeek: {str(e)}", {
                'error': 'rate_limit', 'detalle': str(e)
            }
        except Exception as e:
            AgentExecutor._actualizar_progreso(agente, 100, f"Error: {str(e)[:50]}")
            return False, f"Error en DeepSeek: {str(e)}", {
                'error': 'unexpected', 'detalle': str(e)
            }

    # ============================================================
    # 6. EJECUTOR LOOP
    # ============================================================

    @staticmethod
    def _ejecutar_loop(
        agente: Agente,
        contexto: Dict,
        cancellation_token: Optional['CancellationToken'] = None
    ) -> Tuple[bool, str, Dict]:
        """
        Ejecuta un bucle sobre una lista de items.

        COMPORTAMIENTO:
        - ✅ Si continuar_en_error=False, se DETIENE en el primer error
        - ✅ Si continuar_en_error=True, continúa procesando todos los items
        - ✅ Soporte para cancelación durante la ejecución
        """
        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {'error': 'cancelled'}

        AgentExecutor._actualizar_progreso(agente, 10, "Preparando loop...")

        if not agente.fuente_items:
            AgentExecutor._actualizar_progreso(agente, 100, "Fuente de items vacía")
            return False, "Loop: 'fuente_items' no está configurado", {}

        if not agente.codigo_por_item or not agente.codigo_por_item.strip():
            AgentExecutor._actualizar_progreso(agente, 100, "Código por item vacío")
            return False, "Loop: 'codigo_por_item' está vacío", {}

        items = AgentExecutor._resolver_ruta_en_contexto(
            contexto, agente.fuente_items
        )

        if not AgentExecutor._es_lista_valida(items):
            AgentExecutor._actualizar_progreso(
                agente, 100, "Fuente no es una lista"
            )
            return False, (
                f"Loop: '{agente.fuente_items}' no resolvió a una lista válida "
                f"(obtuve: {type(items).__name__})"
            ), {"fuente": agente.fuente_items, "valor": items}

        total_items = len(items)
        if total_items == 0:
            AgentExecutor._actualizar_progreso(agente, 100, "Lista vacía")
            return True, (
                "Loop: La lista está vacía, no hay items que procesar"
            ), {
                'total_items': 0,
                'items_procesados': 0,
                'exitos': 0,
                'errores': 0,
                'no_ejecutados': 0
            }

        if total_items > agente.max_iteraciones:
            AgentExecutor._actualizar_progreso(
                agente, 100, "Excede máx. iteraciones"
            )
            return False, (
                f"Loop: La lista tiene {total_items} items, "
                f"excede el máximo de {agente.max_iteraciones}"
            ), {"total_items": total_items, "max_iteraciones": agente.max_iteraciones}

        resultados = []
        errores = 0
        exitos = 0
        tiempo_inicio = time.time()
        timeout_total = agente.timeout_loop or 300
        timeout_item = agente.timeout_python or 30

        AgentExecutor._actualizar_progreso(
            agente, 15, f"Procesando {total_items} items..."
        )

        for idx, item in enumerate(items):
            if cancellation_token and cancellation_token.esta_cancelado():
                AgentExecutor._actualizar_progreso(
                    agente, 100, "⛔ Cancelado por usuario"
                )
                return False, f"Loop cancelado en item {idx+1}/{total_items}", {
                    'error': 'cancelled',
                    'items_procesados': idx,
                    'total_items': total_items,
                    'errores': errores,
                    'exitos': exitos,
                    'no_ejecutados': total_items - idx,
                    'resultados_parciales': resultados
                }

            elapsed = time.time() - tiempo_inicio
            if elapsed > timeout_total:
                AgentExecutor._actualizar_progreso(
                    agente, 100, "Timeout global"
                )
                return False, (
                    f"Loop: Timeout global excedido ({timeout_total}s) "
                    f"después de {idx} items"
                ), {
                    "items_procesados": idx,
                    "total_items": total_items,
                    "errores": errores,
                    "exitos": exitos,
                    "no_ejecutados": total_items - idx,
                    "resultados_parciales": resultados
                }

            progreso_actual = 15 + int((idx / total_items) * 75)
            AgentExecutor._actualizar_progreso(
                agente,
                progreso_actual,
                f"Item {idx+1}/{total_items}: {str(item)[:30]}..."
            )

            ctx_item = dict(contexto)
            ctx_item['item'] = item
            ctx_item['indice'] = idx
            ctx_item['total'] = total_items

            try:
                exito, msg, res = PythonSandbox.ejecutar(
                    agente.codigo_por_item,
                    ctx_item,
                    timeout=timeout_item,
                    cancellation_token=cancellation_token
                )

                resultados.append({
                    'indice': idx,
                    'item': item,
                    'exito': exito,
                    'mensaje': msg,
                    'resultado': res
                })

                if exito:
                    exitos += 1
                else:
                    errores += 1
                    if not agente.continuar_en_error:
                        duracion_total = time.time() - tiempo_inicio
                        no_ejecutados = total_items - len(resultados)

                        AgentExecutor._actualizar_progreso(
                            agente, 100,
                            f"⛔ Detenido en item {idx+1} por error"
                        )

                        resultado_final = {
                            'total_items': total_items,
                            'items_procesados': len(resultados),
                            'exitos': exitos,
                            'errores': errores,
                            'no_ejecutados': no_ejecutados,
                            'duracion_total': duracion_total,
                            'detenido_en_indice': idx,
                            'detenido_por_error': True,
                            'continuar_en_error': False,
                            'mensaje_error': msg[:500],
                            'items': resultados
                        }

                        return False, (
                            f"Loop detenido en item {idx+1} por error: {msg[:200]}"
                        ), resultado_final

            except Exception as e:
                errores += 1
                resultados.append({
                    'indice': idx,
                    'item': item,
                    'exito': False,
                    'mensaje': f"Error crítico: {str(e)}",
                    'resultado': {'error': str(e)}
                })

                if not agente.continuar_en_error:
                    duracion_total = time.time() - tiempo_inicio
                    no_ejecutados = total_items - len(resultados)

                    AgentExecutor._actualizar_progreso(
                        agente, 100,
                        f"⛔ Detenido en item {idx+1} por error crítico"
                    )

                    resultado_final = {
                        'total_items': total_items,
                        'items_procesados': len(resultados),
                        'exitos': exitos,
                        'errores': errores,
                        'no_ejecutados': no_ejecutados,
                        'duracion_total': duracion_total,
                        'detenido_en_indice': idx,
                        'detenido_por_error': True,
                        'continuar_en_error': False,
                        'mensaje_error': str(e)[:500],
                        'items': resultados
                    }

                    return False, (
                        f"Loop detenido en item {idx+1} por error crítico: {e}"
                    ), resultado_final

        duracion_total = time.time() - tiempo_inicio

        resultado_final = {
            'total_items': total_items,
            'items_procesados': len(resultados),
            'exitos': exitos,
            'errores': errores,
            'no_ejecutados': 0,
            'duracion_total': duracion_total,
            'detenido_por_error': False,
            'continuar_en_error': agente.continuar_en_error,
            'items': resultados
        }

        mensaje = (
            f"Loop completado: {total_items} items, "
            f"{exitos} éxitos, {errores} errores, "
            f"duración: {duracion_total:.2f}s"
        )

        if errores > 0 and agente.continuar_en_error:
            mensaje += " (continuar_en_error activo: se ignoraron los fallos individuales)"

        AgentExecutor._actualizar_progreso(agente, 100, "Loop completado")

        exito_general = errores == 0 or agente.continuar_en_error
        return exito_general, mensaje, resultado_final

    # ============================================================
    # 7. MÉTODO DE PRUEBA PARA LOOP
    # ============================================================

    @staticmethod
    def probar_loop(agente: Agente, items: List[Any]) -> Tuple[bool, str, Dict]:
        """Método de prueba para verificar la configuración de un agente LOOP."""
        if agente.tipo != TipoAgente.LOOP:
            return False, "El agente no es de tipo LOOP", {}

        es_valido, mensaje = agente.validar_configuracion()
        if not es_valido:
            return False, f"Configuración inválida: {mensaje}", {}

        partes = agente.fuente_items.split('.')
        nombre_dep = partes[0]
        clave_items = '.'.join(partes[1:]) if len(partes) > 1 else 'items'

        contexto = {}
        dep_data = {}
        dep_data[clave_items] = items
        contexto[nombre_dep] = dep_data
        contexto['items_prueba'] = items

        return AgentExecutor._ejecutar_loop(agente, contexto)

    # ============================================================
    # 8. UTILIDADES ADICIONALES
    # ============================================================

    @classmethod
    def get_status(cls) -> Dict:
        """Obtiene el estado del ejecutor."""
        return {
            'http_cache': cls.get_http_cache_stats(),
            'rate_limiter': 'active',
        }


# ============================================================
# LIMPIEZA AL FINALIZAR
# ============================================================

@atexit.register
def _cleanup_executor():
    """Limpia recursos al finalizar la aplicación."""
    try:
        AgentExecutor.clear_http_cache()
        logger.debug("Executor resources cleaned up")
    except Exception:
        pass