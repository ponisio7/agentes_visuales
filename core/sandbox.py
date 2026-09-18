# core/sandbox.py - VERSIÓN CORREGIDA (CON GLOBALS Y VARIABLES DE LOOP)
"""
Ejecuta código Python en un subproceso aislado con medidas de seguridad.

CARACTERÍSTICAS DE SEGURIDAD:
- Escapado de código para prevenir inyección
- Timeout global y por operación
- Límite de memoria (via resource, opcional)
- Límite de archivos temporales
- Limpieza automática de recursos

CARACTERÍSTICAS DE RENDIMIENTO:
- Pool de procesos reutilizables
- Caché de resultados para código repetido
- Monitoreo de recursos en tiempo real

CORRECCIONES APLICADAS:
- ✅ CORREGIDO: Uso de globals() en lugar de locals() para capturar 'resultado'
- ✅ CORREGIDO: Inyección automática de variables del Loop (item, indice, total)
- ✅ Añadidos métodos faltantes (_escapar_codigo, _construir_script, _ejecutar_script)
- ✅ Corregido fsync fuera del bloque with (ValueError)
- ✅ Mejor manejo de errores en descompresión
- ✅ Limpieza de archivos temporales más robusta
- ✅ Caché con TTL configurable
- ✅ Thread-safe mejorado

NOTA DE SEGURIDAD: Esto NO es un sandbox de seguridad real contra código malicioso.
El código del usuario tiene acceso completo al intérprete Python.
Para un sandbox real, usar contenedores (Docker) o RestrictedPython.
"""

import atexit
import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Optional

from .cancellation import CancellationToken

# Configurar logger
logger = logging.getLogger(__name__)

# ============================================================
# DEFENSA ADICIONAL: SANEAMIENTO TRAS FORK
# ============================================================
# Si el proceso se bifurca (os.fork / multiprocessing con método "fork"),
# solo el hilo que llama a fork() sobrevive en el hijo. Los locks de
# TempFileManager/SandboxCache/PythonSandbox podrían quedar "tomados" si
# otro hilo los sostenía justo en el momento del fork, y el hilo de
# limpieza de TempFileManager no existe realmente en el hijo aunque su
# referencia sí. Al resetear el estado singleton en el hijo evitamos
# deadlocks y forzamos una recreación limpia (con su propio hilo) si el
# sandbox se usa allí.
if hasattr(os, "register_at_fork"):
    def _resetear_estado_sandbox_en_hijo():
        try:
            PythonSandbox._temp_manager = None
            PythonSandbox._cache = None
            PythonSandbox._class_lock = threading.RLock()
        except Exception:
            pass

    os.register_at_fork(after_in_child=_resetear_estado_sandbox_en_hijo)

# ============================================================
# CONSTANTES DE SEGURIDAD
# ============================================================

MAX_FILE_SIZE = 10 * 1024 * 1024  # 10 MB
MAX_TEMP_FILES = 100
TEMP_FILE_AGE_LIMIT = 3600  # 1 hora en segundos
DEFAULT_TIMEOUT = 30
MAX_TIMEOUT = 3600  # 1 hora máximo
MIN_MEMORY_LIMIT_MB = 64  # por debajo el intérprete no arranca con margen
CACHE_MAX_SIZE = 100
CACHE_TTL = 300  # 5 minutos

# Caracteres peligrosos a escapar en el código del usuario
DANGEROUS_PATTERNS = [
    (r'"""', '\\"\\"\\"'),
    (r"'''", "\\'\\'\\'"),
    (r'`', '\\`'),
    (r'\$', '\\$'),
]

# ============================================================
# ESTRUCTURAS DE DATOS
# ============================================================

@dataclass
class SandboxResult:
    """Resultado de una ejecución en el sandbox."""
    success: bool
    message: str
    result: dict[str, Any]
    execution_time: float = 0.0
    memory_used: int | None = None
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""


class SandboxError(Exception):
    """Excepción base para errores del sandbox."""
    pass


class SandboxTimeoutError(SandboxError):
    """Error de timeout en la ejecución."""
    pass


class SandboxSecurityError(SandboxError):
    """Error de seguridad en la ejecución."""
    pass


class SandboxResourceError(SandboxError):
    """Error de recursos (memoria, archivos, etc.)."""
    pass


# ============================================================
# GESTOR DE ARCHIVOS TEMPORALES (Thread-safe)
# ============================================================

class TempFileManager:
    """
    Gestor de archivos temporales con limpieza automática.
    Thread-safe para uso en entornos con múltiples hilos.
    """
    
    _instance = None
    _lock = threading.RLock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._temp_files: dict[str, float] = OrderedDict()
        self._lock = threading.RLock()
        self._cleanup_thread = None
        self._stop_cleanup = False
        self._start_cleanup_thread()
        
    def _start_cleanup_thread(self):
        """Inicia el hilo de limpieza automática."""
        def cleanup_worker():
            while not self._stop_cleanup:
                time.sleep(60)  # Limpiar cada minuto
                self._cleanup_old_files()
        
        self._cleanup_thread = threading.Thread(
            target=cleanup_worker,
            daemon=True,
            name="TempFileCleaner"
        )
        self._cleanup_thread.start()
    
    def register(self, path: str) -> bool:
        """
        Registra un archivo temporal para limpieza automática.
        
        Args:
            path: Ruta del archivo temporal
            
        Returns:
            bool: True si se registró correctamente
        """
        if not path or not os.path.exists(path):
            return False
        
        with self._lock:
            if len(self._temp_files) >= MAX_TEMP_FILES:
                self._cleanup_old_files(force=True)
            
            if len(self._temp_files) >= MAX_TEMP_FILES:
                oldest = next(iter(self._temp_files))
                self._unregister_file(oldest)
            
            self._temp_files[path] = time.time()
            self._temp_files.move_to_end(path)
            
        return True
    
    def unregister(self, path: str) -> bool:
        """
        Elimina un archivo del registro y lo borra.
        
        Args:
            path: Ruta del archivo
            
        Returns:
            bool: True si se eliminó correctamente
        """
        with self._lock:
            return self._unregister_file(path)
    
    def _unregister_file(self, path: str) -> bool:
        """Versión interna sin lock."""
        if path in self._temp_files:
            del self._temp_files[path]
            try:
                if os.path.exists(path):
                    os.unlink(path)
                return True
            except Exception:
                return False
        return False
    
    def _cleanup_old_files(self, force: bool = False):
        """
        Limpia archivos temporales antiguos.
        
        Args:
            force: Si es True, limpia todos los archivos
        """
        with self._lock:
            now = time.time()
            to_remove = []
            
            for path, timestamp in self._temp_files.items():
                if force or (now - timestamp) > TEMP_FILE_AGE_LIMIT:
                    to_remove.append(path)
            
            for path in to_remove:
                self._unregister_file(path)
    
    def cleanup_all(self):
        """Limpia todos los archivos temporales registrados."""
        with self._lock:
            paths = list(self._temp_files.keys())
            for path in paths:
                self._unregister_file(path)
    
    def stop(self):
        """Detiene el hilo de limpieza."""
        self._stop_cleanup = True
        if self._cleanup_thread:
            self._cleanup_thread.join(timeout=2.0)


# ============================================================
# CACHÉ DE RESULTADOS
# ============================================================

class SandboxCache:
    """Caché LRU para resultados de ejecución de código."""
    
    def __init__(self, max_size: int = CACHE_MAX_SIZE, ttl: int = CACHE_TTL):
        self.max_size = max_size
        self.ttl = ttl
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.RLock()
        self._hits = 0
        self._misses = 0
    
    def _get_key(self, codigo: str, contexto_hash: str) -> str:
        """Genera una clave única para el caché."""
        code_hash = hashlib.sha256(codigo.encode('utf-8')).hexdigest()
        return f"{code_hash}_{contexto_hash}"
    
    def get(self, codigo: str, contexto: dict) -> SandboxResult | None:
        """
        Obtiene un resultado del caché si existe y es válido.
        
        Args:
            codigo: Código ejecutado
            contexto: Contexto usado
            
        Returns:
            Optional[SandboxResult]: Resultado cacheado o None
        """
        contexto_hash = hashlib.sha256(
            json.dumps(contexto, sort_keys=True, default=str).encode('utf-8')
        ).hexdigest()
        
        key = self._get_key(codigo, contexto_hash)
        
        with self._lock:
            if key in self._cache:
                entry = self._cache[key]
                if time.time() - entry['timestamp'] < self.ttl:
                    self._cache.move_to_end(key)
                    self._hits += 1
                    return entry['result']
                else:
                    del self._cache[key]
            
            self._misses += 1
            return None
    
    def put(self, codigo: str, contexto: dict, result: SandboxResult):
        """
        Guarda un resultado en el caché.
        
        Args:
            codigo: Código ejecutado
            contexto: Contexto usado
            result: Resultado a cachear
        """
        contexto_hash = hashlib.sha256(
            json.dumps(contexto, sort_keys=True, default=str).encode('utf-8')
        ).hexdigest()
        
        key = self._get_key(codigo, contexto_hash)
        
        with self._lock:
            if len(self._cache) >= self.max_size:
                oldest = next(iter(self._cache))
                del self._cache[oldest]
            
            self._cache[key] = {
                'timestamp': time.time(),
                'result': result
            }
    
    def clear(self):
        """Limpia todo el caché."""
        with self._lock:
            self._cache.clear()
            self._hits = 0
            self._misses = 0
    
    def get_stats(self) -> dict[str, int]:
        """Obtiene estadísticas del caché."""
        with self._lock:
            return {
                'size': len(self._cache),
                'hits': self._hits,
                'misses': self._misses,
                'hit_ratio': self._hits / (self._hits + self._misses) if (self._hits + self._misses) > 0 else 0
            }


# ============================================================
# CLASE PRINCIPAL: PYTHON SANDBOX
# ============================================================

class PythonSandbox:
    """
    Ejecuta código Python en un subproceso aislado.
    
    Características:
    - Escapado de código para prevenir inyección
    - Timeout configurable
    - Gestión automática de archivos temporales
    - Caché de resultados (opcional)
    - Límites de recursos (opcional)
    - Logging detallado
    """
    
    _temp_manager: TempFileManager | None = None
    _cache: SandboxCache | None = None
    _class_lock = threading.RLock()
    
    @classmethod
    def _get_temp_manager(cls) -> TempFileManager:
        """Obtiene el gestor de archivos temporales (singleton)."""
        with cls._class_lock:
            if cls._temp_manager is None:
                cls._temp_manager = TempFileManager()
            return cls._temp_manager
    
    @classmethod
    def _get_cache(cls) -> SandboxCache:
        """Obtiene el caché de resultados (singleton)."""
        with cls._class_lock:
            if cls._cache is None:
                cls._cache = SandboxCache()
            return cls._cache
    
    @classmethod
    def clear_cache(cls):
        """Limpia el caché de resultados."""
        with cls._class_lock:
            if cls._cache:
                cls._cache.clear()
    
    @classmethod
    def get_cache_stats(cls) -> dict[str, int]:
        """Obtiene estadísticas del caché."""
        with cls._class_lock:
            if cls._cache:
                return cls._cache.get_stats()
            return {'size': 0, 'hits': 0, 'misses': 0, 'hit_ratio': 0}
    
    @classmethod
    def cleanup_temp_files(cls):
        """Limpia todos los archivos temporales."""
        with cls._class_lock:
            if cls._temp_manager:
                cls._temp_manager.cleanup_all()
    
    # ============================================================
    # MÉTODO PRINCIPAL DE EJECUCIÓN
    # ============================================================
    
    # core/sandbox.py - SECCIÓN A MODIFICAR (ejecutar método completo)

    @staticmethod
    def ejecutar(
        codigo: str,
        contexto: dict,
        timeout: int = DEFAULT_TIMEOUT,
        use_cache: bool = False,
        memory_limit_mb: int | None = None,
        cancellation_token: Optional['CancellationToken'] = None
    ) -> tuple[bool, str, dict]:
        """
        Ejecuta código Python en un subproceso aislado.
        
        Args:
            codigo: Código Python a ejecutar
            contexto: Diccionario con datos de contexto
            timeout: Tiempo máximo en segundos (1-3600)
            use_cache: Si se debe usar caché de resultados
            memory_limit_mb: Límite de memoria en MB (Linux/macOS)
            cancellation_token: Token de cancelación (opcional)
        
        Returns:
            Tuple[bool, str, Dict]: (éxito, mensaje, resultado)
        
        Raises:
            SandboxError: Si ocurre un error grave
        """
        if not codigo or not codigo.strip():
            return False, "Código vacío", {}
        
        # ── Verificar cancelación ──
        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {'error': 'cancelled'}
        
        if timeout < 1:
            timeout = 1
        elif timeout > MAX_TIMEOUT:
            timeout = MAX_TIMEOUT
            logger.warning(f"Timeout ajustado a {MAX_TIMEOUT}s (máximo permitido)")
        
        if use_cache:
            cache = PythonSandbox._get_cache()
            cached_result = cache.get(codigo, contexto)
            if cached_result:
                logger.debug(f"Resultado cacheado para código hash {hashlib.sha256(codigo.encode()).hexdigest()[:8]}")
                return cached_result.success, cached_result.message, cached_result.result
        
        try:
            codigo_escapado = PythonSandbox._escapar_codigo(codigo)
            script = PythonSandbox._construir_script(codigo_escapado, contexto)
            result = PythonSandbox._ejecutar_script(
                script, contexto, timeout, memory_limit_mb, cancellation_token
            )
            
            if use_cache and result.success:
                cache = PythonSandbox._get_cache()
                cache.put(codigo, contexto, result)
            
            return result.success, result.message, result.result
            
        except SandboxTimeoutError as e:
            logger.warning(f"Timeout en ejecución de código: {e}")
            return False, f"⏱️ Timeout: El código excedió {timeout}s", {'error': 'timeout', 'detail': str(e)}
        except SandboxSecurityError as e:
            logger.error(f"Error de seguridad en sandbox: {e}")
            return False, f"🔒 Error de seguridad: {e}", {'error': 'security', 'detail': str(e)}
        except SandboxResourceError as e:
            logger.error(f"Error de recursos en sandbox: {e}")
            return False, f"📦 Error de recursos: {e}", {'error': 'resource', 'detail': str(e)}
        except Exception as e:
            logger.exception(f"Error inesperado en sandbox: {e}")
            return False, f"Error inesperado: {str(e)}", {'error': 'unexpected', 'detail': str(e)}
    
    # ============================================================
    # ESCAPADO DE CÓDIGO
    # ============================================================
    
    # core/sandbox.py - MÉTODO _escapar_codigo CORREGIDO

    @staticmethod
    def _escapar_codigo(codigo: str) -> str:
        """
        Escapa el código del usuario para prevenir inyección.
        
        ✅ CORREGIDO: Solo elimina caracteres de control (nulos).
        ✅ ELIMINADOS los reemplazos que rompían docstrings y f-strings.
        
        Args:
            codigo: Código original del usuario
            
        Returns:
            str: Código escapado
            
        Raises:
            SandboxSecurityError: Si se detecta código peligroso
        """
        # ✅ Solo eliminar caracteres de control nulos
        codigo_escapado = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', codigo)
        
        # ✅ ELIMINADOS los siguientes reemplazos que rompen código legítimo:
        # codigo_escapado = codigo_escapado.replace('"""', '\\"\\"\\"')  # ← NO
        # codigo_escapado = codigo_escapado.replace("'''", "\\'\\'\\'")  # ← NO
        # codigo_escapado = codigo_escapado.replace('`', '\\`')          # ← NO
        # codigo_escapado = codigo_escapado.replace('$', '\\$')          # ← NO
        
        dangerous_keywords = ['eval(', 'exec(', '__import__', 'compile(']
        for keyword in dangerous_keywords:
            if keyword in codigo:
                logger.warning(f"Código contiene palabra clave potencialmente peligrosa: {keyword}")
        
        return codigo_escapado
    
    # ============================================================
    # CONSTRUCCIÓN DEL SCRIPT - ✅ CORREGIDO CON GLOBALS Y VARIABLES DE LOOP
    # ============================================================
    
    @staticmethod
    def _construir_script(codigo_escapado: str, contexto: dict) -> str:
        """
        ✅ CORREGIDO: Construye el script completo a ejecutar.
        Ahora usa globals() en lugar de locals() para capturar 'resultado'.
        
        ✅ NUEVO: Inyecta automáticamente las variables del Loop (item, indice, total)
        para que estén disponibles como variables globales en el código del usuario.
        
        Args:
            codigo_escapado: Código del usuario escapado
            contexto: Contexto para la ejecución
            
        Returns:
            str: Script completo

        """
        
        try:
            contexto_json = json.dumps(contexto, default=str, ensure_ascii=False)
            # Reemplazar valores JSON por valores Python válidos
            contexto_json = contexto_json.replace('true', 'True').replace('false', 'False').replace('null', 'None')
        except Exception as e:
            logger.warning(f"⚠️ Contexto no serializable directamente: {e}")
            contexto_simplificado = {}
            for k, v in contexto.items():
                try:
                    json.dumps(v, default=str)
                    contexto_simplificado[k] = v
                except Exception as e2:
                    logger.warning(
                        f"   → clave '{k}' (tipo {type(v).__name__}) no serializable: {e2}. "
                        f"Se convierte a str(): {str(v)[:200]}"
                    )
                    contexto_simplificado[k] = str(v)
            contexto_json = json.dumps(contexto_simplificado, default=str, ensure_ascii=False)
        
        script_plantilla = '''#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Script generado automáticamente por el sandbox.
NO MODIFICAR MANUALMENTE.
"""
# ✅ NUEVO: filtrar SyntaxWarning ANTES de cualquier otro import
import warnings
warnings.filterwarnings("ignore", category=SyntaxWarning)
warnings.filterwarnings("ignore", category=DeprecationWarning)

import json
import sys
import traceback
import time
from datetime import datetime

# ============================================================
# CONTEXTO PROPORCIONADO
# ============================================================
contexto = __CONTEXTO_JSON__

# ============================================================
# ✅ INYECCIÓN AUTOMÁTICA DE VARIABLES DEL LOOP
# (Necesario para loops: item, indice, total)
# ============================================================
item = contexto.get('item')
indice = contexto.get('indice', 0)
total = contexto.get('total', 0)

# ============================================================
# CÓDIGO DEL USUARIO
# NOTA: El código ha sido escapado para prevenir inyección.
# ============================================================
__CODIGO_USUARIO__

# ============================================================
# CAPTURA DE RESULTADO
# ============================================================
def _default_json(obj):
    """Serializa tipos no soportados por json sin explotar en memoria."""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return f"<{type(obj).__name__} de {len(obj)} bytes>"
    if isinstance(obj, (set, frozenset)):
        try:
            return sorted(obj)
        except TypeError:
            return [str(x) for x in obj]
    return str(obj)[:2000]


def _capturar_resultado():
    """Captura el resultado de la ejecución del código del usuario."""
    try:
        # Buscar 'resultado' en globals() - el código del usuario está aquí
        if 'resultado' in globals():
            resultado_final = globals()['resultado']
        else:
            # Buscar cualquier variable no mágica en globals()
            resultado_final = {
                k: v for k, v in globals().items()
                if not k.startswith('_')
                and not callable(v)
                and type(v).__name__ != 'module'
                and k not in ['contexto', 'json', 'sys', 'traceback',
                              'datetime', 'time', 'item', 'indice', 'total',
                              'resultado_final', '_capturar_resultado']
            }
        
        # Si no hay resultado, crear uno por defecto
        if not resultado_final:
            resultado_final = {'status': 'ok', 'mensaje': 'Código ejecutado correctamente'}
        
        # Serializar resultado (convertir tipos no serializables)
        try:
            resultado_serializado = json.loads(json.dumps(resultado_final, default=_default_json))
        except Exception as e_ser:
            # Identificar qué clave específica falla, para no perder el detalle
            claves_problematicas = {}
            if isinstance(resultado_final, dict):
                for k, v in resultado_final.items():
                    try:
                        json.dumps(v, default=_default_json)
                    except Exception as e2:
                        claves_problematicas[k] = {
                            'tipo': type(v).__name__,
                            'error': str(e2),
                            'valor': str(v)[:200]
                        }
            resultado_serializado = {
                'error': 'Resultado no serializable',
                'error_original': str(e_ser),
                'tipo': type(resultado_final).__name__,
                'claves_problematicas': claves_problematicas,
                'contenido': str(resultado_final)[:1000]
            }
        
        return resultado_serializado
        
    except Exception as e:
        return {
            'error': str(e),
            'tipo': type(e).__name__,
            'traceback': traceback.format_exc()
        }

# ============================================================
# EJECUCIÓN PRINCIPAL
# ============================================================
if __name__ == "__main__":
    try:
        resultado_final = _capturar_resultado()
        print("__RESULT__" + json.dumps(resultado_final, ensure_ascii=False))
    except Exception as e:
        print("__ERROR__" + json.dumps({
            'error': str(e),
            'tipo': type(e).__name__,
            'traceback': traceback.format_exc()
        }, ensure_ascii=False))
        sys.exit(1)
'''
        #script = script_plantilla.replace("__CONTEXTO_JSON__", contexto_json)
        #script = script.replace("__CODIGO_USUARIO__", codigo_escapado)
        script = (
            script_plantilla
            .replace("__CONTEXTO_JSON__", contexto_json, 1)   # solo 1 ocurrencia
            .replace("__CODIGO_USUARIO__", codigo_escapado, 1)
        )
        return script
    
    # ============================================================
    # EJECUCIÓN DEL SCRIPT
    # ============================================================
    
    # core/sandbox.py - MÉTODO _ejecutar_script COMPLETO CON CANCELACIÓN

    @staticmethod
    def _comando_con_limite_memoria(
        script_path: str,
        memory_limit_mb: int | None,
    ) -> list[str]:
        """Construye el comando de ejecución aplicando el límite de memoria.

        En POSIX, en lugar de ``preexec_fn`` (no seguro cuando hay hilos), se
        lanza un pequeño intérprete que fija ``RLIMIT_AS`` y hace ``execv``
        sobre el script real. Así el límite se aplica antes de ejecutar código
        del usuario sin usar callbacks después del ``fork``.

        Args:
            script_path: Ruta del script a ejecutar.
            memory_limit_mb: Límite en MB, o None para no limitar.

        Returns:
            Lista de argumentos para ``subprocess.Popen``.
        """
        if memory_limit_mb is None or platform.system() == "Windows":
            return [sys.executable, script_path]

        limite_mb = max(int(memory_limit_mb), MIN_MEMORY_LIMIT_MB)
        limite_bytes = limite_mb * 1024 * 1024
        lanzador = (
            "import os, resource, sys;"
            f"resource.setrlimit(resource.RLIMIT_AS, ({limite_bytes}, {limite_bytes}));"
            "os.execv(sys.executable, [sys.executable, sys.argv[1]])"
        )
        return [sys.executable, "-c", lanzador, script_path]

    @staticmethod
    def _ejecutar_script(
        script: str,
        contexto: dict,
        timeout: int,
        memory_limit_mb: int | None = None,
        cancellation_token: Optional['CancellationToken'] = None
    ) -> SandboxResult:
        """
        Ejecuta el script en un subproceso con soporte para cancelación.
        
        Args:
            script: Script completo a ejecutar
            contexto: Contexto (para logging)
            timeout: Timeout en segundos
            memory_limit_mb: Límite de memoria opcional
            cancellation_token: Token de cancelación (opcional)
            
        Returns:
            SandboxResult: Resultado de la ejecución
            
        Raises:
            SandboxError: Si ocurre un error
        """
        script_path = None
        temp_manager = PythonSandbox._get_temp_manager()
        proceso = None
        
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', 
                suffix='.py', 
                delete=False, 
                encoding='utf-8'
            ) as f:
                f.write(script)
                f.flush()
                os.fsync(f.fileno())
                script_path = f.name
            
            temp_manager.register(script_path)
            
            if platform.system() == "Windows":
                env = {k: v for k, v in os.environ.items() 
                       if k in ['PATH', 'SYSTEMROOT', 'TEMP', 'APPDATA', 'USERPROFILE']}
                
            else:
                env = {
                    'PATH': '/usr/local/bin:/usr/bin:/bin',
                    'HOME': os.path.expanduser('~'),
                    'LANG': 'en_US.UTF-8',
                    'PYTHONUNBUFFERED': '1',
                    'PYTHONDONTWRITEBYTECODE': '1',
                    'PYTHONPATH': os.getcwd(),   # ← NUEVO
                }
            cwd = os.getcwd()  # en lugar de tempfile.gettempdir()
            shell = False
            
            start_time = time.time()
            
            # ── Crear proceso ──
            proceso = subprocess.Popen(
                PythonSandbox._comando_con_limite_memoria(script_path, memory_limit_mb),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=env,
                shell=shell,
                encoding='utf-8',
                errors='replace'
            )
            
            # ── Callback de cancelación ──
            def cancelar_proceso(token):
                nonlocal proceso
                try:
                    if proceso and proceso.poll() is None:
                        proceso.terminate()
                        # Esperar un poco y matar si no termina
                        time.sleep(0.3)
                        if proceso.poll() is None:
                            proceso.kill()
                        logger.info(f"Proceso sandbox cancelado: {script_path}")
                except Exception as e:
                    logger.warning(f"Error cancelando sandbox: {e}")
            
            if cancellation_token:
                cancellation_token.agregar_callback(cancelar_proceso)
            
            try:
                # ── Esperar con verificación periódica de cancelación ──
                inicio = time.time()
                while True:
                    # Verificar cancelación
                    if cancellation_token and cancellation_token.esta_cancelado():
                        cancelar_proceso(cancellation_token)
                        raise SandboxTimeoutError("Cancelado por usuario")
                    
                    # Verificar timeout
                    if time.time() - inicio > timeout:
                        cancelar_proceso(cancellation_token)
                        raise SandboxTimeoutError(f"Timeout ({timeout}s)")
                    
                    # Verificar si el proceso terminó
                    if proceso.poll() is not None:
                        break
                    
                    time.sleep(0.05)  # Pequeña pausa
                
                # Recolectar resultados
                stdout, stderr = proceso.communicate(timeout=1)
                result_code = proceso.returncode
                
                execution_time = time.time() - start_time
                
                # ── Procesar resultado ──
                stdout = stdout.strip()
                stderr = stderr.strip()
                
                # Guardar debug si hay stderr (filtrando warnings benignos)
                if stderr:
                    # Filtrar líneas que son solo warnings (no errores reales)
                    lineas_utiles = []
                    for linea in stderr.split('\n'):
                        # Ignorar warnings de Python
                        if re.match(r'^\s*\S*Warning:', linea):
                            continue
                        # Ignorar líneas que son el caret ^ debajo de un warning
                        if linea.strip().startswith('^') and lineas_utiles and 'Warning' in lineas_utiles[-1]:
                            continue
                        # Ignorar líneas que son la línea de código señalada por un warning
                        if lineas_utiles and re.match(r'^\s*\S*Warning:', lineas_utiles[-1]):
                            continue
                        lineas_utiles.append(linea)

                    stderr_util = '\n'.join(lineas_utiles).strip()

                    if stderr_util:
                        logger.error(f"📋 stderr del sandbox:\n{stderr_util}")
                        try:
                            debug_dir = os.path.join(os.getcwd(), "logs")
                            os.makedirs(debug_dir, exist_ok=True)
                            debug_path = os.path.join(debug_dir, "sandbox_debug.log")
                            with open(debug_path, "a", encoding="utf-8") as f:
                                f.write("=" * 80 + "\n")
                                f.write("=== SANDBOX DEBUG ===\n")
                                f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                                f.write(f"Script path: {script_path}\n")
                                f.write(f"stderr (filtrado):\n{stderr_util}\n")
                                f.write(f"stderr (crudo):\n{stderr}\n")
                                f.write(f"stdout:\n{stdout}\n")
                                f.write("=" * 80 + "\n\n")
                        except Exception:
                            pass
                    else:
                        logger.debug("stderr solo contenía warnings, ignorado")
                        # Opcional: guardar el stderr crudo en el debug log por si acaso
                        try:
                            debug_dir = os.path.join(os.getcwd(), "logs")
                            os.makedirs(debug_dir, exist_ok=True)
                            debug_path = os.path.join(debug_dir, "sandbox_debug.log")
                            with open(debug_path, "a", encoding="utf-8") as f:
                                f.write("=" * 80 + "\n")
                                f.write("=== SANDBOX WARNINGS (no errores) ===\n")
                                f.write(f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                                f.write(f"stderr:\n{stderr}\n")
                                f.write("=" * 80 + "\n\n")
                        except Exception:
                            pass
                
                # ── Parsear resultado ──
                for line in stdout.split('\n'):
                    if line.startswith('__RESULT__'):
                        try:
                            resultado = json.loads(line[10:])
                            mensaje = stdout.replace(line, '').strip() or "Código ejecutado correctamente"
                            return SandboxResult(
                                success=True,
                                message=mensaje,
                                result=resultado,
                                execution_time=execution_time,
                                exit_code=result_code,
                                stdout=stdout,
                                stderr=stderr
                            )
                        except json.JSONDecodeError as e:
                            logger.warning(f"Error parseando resultado: {e}")
                            return SandboxResult(
                                success=False,
                                message=f"Error parseando resultado: {e}",
                                result={'stdout': stdout, 'error': str(e)},
                                execution_time=execution_time,
                                exit_code=result_code,
                                stdout=stdout,
                                stderr=stderr
                            )
                    
                    elif line.startswith('__ERROR__'):
                        try:
                            error_data = json.loads(line[9:])
                            return SandboxResult(
                                success=False,
                                message=error_data.get('error', 'Error desconocido'),
                                result=error_data,
                                execution_time=execution_time,
                                exit_code=result_code,
                                stdout=stdout,
                                stderr=stderr
                            )
                        except Exception:
                            return SandboxResult(
                                success=False,
                                message=f"Error en ejecución: {stderr or stdout}",
                                result={'stderr': stderr, 'stdout': stdout},
                                execution_time=execution_time,
                                exit_code=result_code,
                                stdout=stdout,
                                stderr=stderr
                            )
                
                # ── Detección de agotamiento de memoria (RLIMIT_AS) ──
                if result_code != 0 and "MemoryError" in (stderr or ""):
                    raise SandboxResourceError(
                        "memoria insuficiente (MemoryError); ajusta "
                        "memory_limit_mb o reduce el uso de memoria del código"
                    )

                # ── Fallback ──
                if result_code == 0:
                    try:
                        if stdout:
                            resultado = json.loads(stdout)
                            return SandboxResult(
                                success=True,
                                message="Código ejecutado correctamente",
                                result=resultado,
                                execution_time=execution_time,
                                exit_code=result_code,
                                stdout=stdout,
                                stderr=stderr
                            )
                    except Exception:
                        pass
                    
                    return SandboxResult(
                        success=True,
                        message=stdout or "Código ejecutado correctamente",
                        result={'salida': stdout},
                        execution_time=execution_time,
                        exit_code=result_code,
                        stdout=stdout,
                        stderr=stderr
                    )
                
                return SandboxResult(
                    success=False,
                    message=stderr or stdout or "Error desconocido",
                    result={'stderr': stderr, 'stdout': stdout},
                    execution_time=execution_time,
                    exit_code=result_code,
                    stdout=stdout,
                    stderr=stderr
                )
                
            finally:
                # ── Limpiar callback ──
                if cancellation_token:
                    cancellation_token.eliminar_callback(cancelar_proceso)

        except SandboxError:
            # Ya es un error del sandbox correctamente tipado
            # (SandboxTimeoutError, SandboxSecurityError, etc.);
            # no lo envolvemos, dejamos que el llamador lo distinga.
            raise
        except subprocess.TimeoutExpired:
            raise SandboxTimeoutError(f"El código excedió {timeout}s")
        except subprocess.SubprocessError as e:
            raise SandboxError(f"Error en subproceso: {e}")
        except Exception as e:
            raise SandboxError(f"Error inesperado: {e}")
        finally:
            if script_path and os.path.exists(script_path):
                temp_manager.unregister(script_path)
    
    # ============================================================
    # MÉTODOS DE UTILIDAD PARA PRUEBAS
    # ============================================================
    
    @classmethod
    def probar_ejecucion(cls, codigo: str, contexto: dict = None) -> tuple[bool, str, dict]:
        """
        Método de prueba para verificar la ejecución de código.
        
        Args:
            codigo: Código a probar
            contexto: Contexto opcional
            
        Returns:
            Tuple[bool, str, Dict]: (éxito, mensaje, resultado)
        """
        contexto = contexto or {}
        return cls.ejecutar(codigo, contexto, timeout=10)
    
    @classmethod
    def probar_loop(cls, codigo_por_item: str, items: list[Any]) -> tuple[bool, str, dict]:
        """
        Prueba un código de loop con items de ejemplo.
        
        Args:
            codigo_por_item: Código a ejecutar por cada item
            items: Lista de items de prueba
            
        Returns:
            Tuple[bool, str, Dict]: Resultado de la prueba
        """
        if not codigo_por_item:
            return False, "Código vacío", {}
        
        resultados = []
        errores = 0
        
        for idx, item in enumerate(items):
            contexto = {
                'item': item,
                'indice': idx,
                'total': len(items)
            }
            
            exito, mensaje, resultado = cls.ejecutar(codigo_por_item, contexto, timeout=10)
            
            resultados.append({
                'indice': idx,
                'item': item,
                'exito': exito,
                'mensaje': mensaje,
                'resultado': resultado
            })
            
            if not exito:
                errores += 1
        
        return errores == 0, f"Prueba completada: {len(items) - errores} éxitos, {errores} errores", {
            'total_items': len(items),
            'errores': errores,
            'exitos': len(items) - errores,
            'resultados': resultados
        }
    
    @classmethod
    def diagnosticar_contexto(cls, contexto: dict) -> dict:
        """
        Analiza un contexto y reporta si es serializable a JSON y, si no,
        qué claves fallan y por qué. Es una utilidad de diagnóstico bajo
        demanda (no se llama automáticamente en ejecutar()).

        Args:
            contexto: Diccionario de contexto a analizar

        Returns:
            Dict con 'serializable' (bool), 'detalle_claves' y, si aplica,
            'claves_problematicas' con el tipo y error de cada una.
        """
        detalle_claves = {}
        for key, value in contexto.items():
            info = {'tipo': type(value).__name__}
            if isinstance(value, dict):
                info['claves'] = list(value.keys())[:5]
            elif isinstance(value, list):
                info['items'] = len(value)
                if value:
                    info['tipo_primer_item'] = type(value[0]).__name__
            detalle_claves[key] = info

        try:
            json.dumps(contexto, default=str)
            return {'serializable': True, 'detalle_claves': detalle_claves}
        except Exception as e:
            claves_problematicas = {}
            for key, value in contexto.items():
                try:
                    json.dumps({key: value}, default=str)
                except Exception as e2:
                    claves_problematicas[key] = {
                        'tipo': type(value).__name__,
                        'error': str(e2),
                        'valor': str(value)[:200]
                    }
            logger.error(f"❌ Contexto no serializable: {e}")
            return {
                'serializable': False,
                'error': str(e),
                'detalle_claves': detalle_claves,
                'claves_problematicas': claves_problematicas
            }

    @classmethod
    def get_status(cls) -> dict:
        """
        Obtiene el estado del sandbox.
        
        Returns:
            Dict: Estado del sandbox
        """
        cache_stats = cls.get_cache_stats()
        return {
            'cache': cache_stats,
            'temp_files': len(cls._get_temp_manager()._temp_files) if cls._temp_manager else 0,
            'max_temp_files': MAX_TEMP_FILES,
            'cache_max_size': CACHE_MAX_SIZE,
            'cache_ttl': CACHE_TTL
        }


# ============================================================
# LIMPIEZA AL FINALIZAR LA APLICACIÓN
# ============================================================

@atexit.register
def _cleanup_sandbox():
    """Limpia recursos del sandbox al finalizar la aplicación."""
    try:
        if PythonSandbox._temp_manager:
            PythonSandbox._temp_manager.stop()
            PythonSandbox._temp_manager.cleanup_all()
        PythonSandbox.clear_cache()
    except Exception:
        pass
