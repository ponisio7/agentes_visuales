# core/agent.py - VERSIÓN REFACTORIZADA Y COMPLETA
"""
Modelo de datos para agentes ejecutables.

CARACTERÍSTICAS:
- Tipos seguros con validación en tiempo de ejecución
- Serialización/Deserialización robusta
- Soporte completo para todos los tipos de agentes
- Validación de configuración por tipo (CORREGIDA)
- Métricas y estadísticas de ejecución
- Clonación profunda
- Comparación y hashing
- Representación enriquecida para UI
- Inmutabilidad de campos críticos
- Logging integrado
- CORRECCIONES: Validación de Loop sin dependencia de listas que se vacían
"""

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from functools import total_ordering
from typing import Any

# Configurar logger
logger = logging.getLogger(__name__)


# ============================================================
# ENUMERACIONES
# ============================================================

class EstadoAgente(Enum):
    """Estados posibles de un agente durante su ciclo de vida."""
    
        # ── Estados iniciales ──
    PENDIENTE = "Pendiente"
    EN_COLA = "En cola"              # ← NUEVO: Esperando turno para ejecutar
    ESPERANDO = "Esperando dependencias"
    
    # ── Estados de ejecución ──
    LISTO = "Listo para ejecutar"
    EJECUTANDO = "Ejecutando"
    REINTENTANDO = "Reintentando"    # ← NUEVO: En proceso de reintento
    
    # ── Estados terminales ──
    COMPLETADO = "Completado"
    ERROR = "Error"
    TIMEOUT = "Timeout"              # ← NUEVO: Excedió tiempo límite
    CANCELADO = "Cancelado"
    SALTADO = "Saltado"              # ← NUEVO: Omitido por dependencia fallida
    BLOQUEADO = "Bloqueado"
    
    @classmethod
    def es_terminal(cls, estado: 'EstadoAgente') -> bool:
        """Indica si el estado es terminal (no puede cambiar)."""
        return estado in (
            cls.COMPLETADO, cls.ERROR, cls.TIMEOUT,
            cls.CANCELADO, cls.SALTADO, cls.BLOQUEADO
        )
    
    @classmethod
    def es_activo(cls, estado: 'EstadoAgente') -> bool:
        """Indica si el agente está en ejecución activa."""
        return estado in (cls.LISTO, cls.EJECUTANDO, cls.REINTENTANDO)
    
    @classmethod
    def puede_ejecutarse(cls, estado: 'EstadoAgente') -> bool:
        """Indica si el agente puede ser ejecutado."""
        return estado in (cls.PENDIENTE, cls.EN_COLA, cls.REINTENTANDO)
    
    @classmethod
    def color(cls, estado: 'EstadoAgente') -> str:
        """Retorna el color asociado al estado para UI."""
        colores = {
            cls.PENDIENTE: "#6c757d",
            cls.EN_COLA: "#6c757d",
            cls.ESPERANDO: "#fd7e14",
            cls.LISTO: "#28a745",
            cls.EJECUTANDO: "#007bff",
            cls.REINTENTANDO: "#ffc107",
            cls.COMPLETADO: "#28a745",
            cls.ERROR: "#dc3545",
            cls.TIMEOUT: "#dc3545",
            cls.CANCELADO: "#6c757d",
            cls.SALTADO: "#6c757d",
            cls.BLOQUEADO: "#8b0000",
        }
        return colores.get(estado, "#6c757d")
    
    @classmethod
    def emoji(cls, estado: 'EstadoAgente') -> str:
        """Retorna el emoji asociado al estado para UI."""
        emojis = {
            cls.PENDIENTE: "⏳",
            cls.EN_COLA: "📋",
            cls.ESPERANDO: "🔄",
            cls.LISTO: "✅",
            cls.EJECUTANDO: "⚡",
            cls.REINTENTANDO: "🔄",
            cls.COMPLETADO: "✅",
            cls.ERROR: "❌",
            cls.TIMEOUT: "⏱️",
            cls.CANCELADO: "⛔",
            cls.SALTADO: "⏭️",
            cls.BLOQUEADO: "🚫",
        }
        return emojis.get(estado, "❓")


class TipoAgente(Enum):
    """Tipos de agentes soportados."""
    
    PYTHON = "Python"
    SHELL = "Shell"
    LLM = "LLM"
    HTTP = "HTTP"
    FILE = "File"
    LOOP = "Loop"
   
    @classmethod
    def categoria(cls, tipo: 'TipoAgente') -> str:
        """Retorna la categoría del tipo de agente."""
        categorias = {
            cls.PYTHON: "Programación",
            cls.SHELL: "Sistema",
            cls.LLM: "IA",
            cls.HTTP: "Red",
            cls.FILE: "Archivos",
            cls.LOOP: "Estructura",
        }
        return categorias.get(tipo, "Otro")
    
    @classmethod
    def icono(cls, tipo: 'TipoAgente') -> str:
        """Retorna el icono asociado al tipo para UI."""
        iconos = {
            cls.PYTHON: "🐍",
            cls.SHELL: "💻",
            cls.LLM: "🧠",
            cls.HTTP: "🌐",
            cls.FILE: "📄",
            cls.LOOP: "🔄",
        }
        return iconos.get(tipo, "📦")
    
    @classmethod
    def from_string(cls, value: str) -> 'TipoAgente':
        """Convierte un string a TipoAgente de forma segura."""
        try:
            return cls(value)
        except ValueError:
            # Intentar buscar por nombre sin espacios
            for member in cls:
                if member.value.lower().replace(" ", "") == value.lower().replace(" ", ""):
                    return member
            return cls.PYTHON


# ============================================================
# VALIDADORES
# ============================================================

class AgenteValidator:
    """Valida la configuración de agentes."""
    
    @staticmethod
    def validar_nombre(nombre: str) -> tuple[bool, str]:
        """Valida el nombre del agente."""
        if not nombre or not nombre.strip():
            return False, "El nombre es obligatorio"
        
        nombre = nombre.strip()
        if len(nombre) < 2:
            return False, "El nombre debe tener al menos 2 caracteres"
        if len(nombre) > 100:
            return False, "El nombre no puede tener más de 100 caracteres"
        
        # Caracteres permitidos: letras, números, guiones, guiones bajos, espacios
        if not re.match(r'^[A-Za-z0-9_\-\s]+$', nombre):
            return False, "El nombre solo puede contener letras, números, guiones y espacios"
        
        return True, ""
    
    @staticmethod
    def validar_codigo(codigo: str, max_length: int = 100000) -> tuple[bool, str]:
        """Valida código Python."""
        if not codigo:
            return True, ""  # Opcional
        
        if len(codigo) > max_length:
            return False, f"El código excede el límite de {max_length} caracteres"
        
        # Verificar indentación básica
        lines = codigo.split('\n')
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            
            # Verificar que la indentación sea consistente
            if line.startswith(' '):
                spaces = len(line) - len(line.lstrip(' '))
                if spaces % 4 != 0:
                    return False, "La indentación debe ser múltiplo de 4 espacios"
        
        return True, ""
    
    @staticmethod
    def validar_url(url: str) -> tuple[bool, str]:
        """Valida una URL."""
        if not url or not url.strip():
            return False, "La URL es obligatoria"
        
        url = url.strip()
        if not url.startswith(('http://', 'https://')):
            return False, "La URL debe comenzar con http:// o https://"
        
        if ' ' in url:
            return False, "La URL no puede contener espacios"
        
        # Verificar caracteres no permitidos
        for c in url:
            if ord(c) < 32:
                return False, "La URL contiene caracteres de control"
        
        return True, ""
    
    @staticmethod
    def validar_ruta(ruta: str) -> tuple[bool, str]:
        """Valida una ruta de archivo."""
        if not ruta or not ruta.strip():
            return False, "La ruta es obligatoria"
        
        ruta = ruta.strip()
        if len(ruta) > 1000:
            return False, "La ruta es demasiado larga"
        
        # Verificar path traversal
        import os
        normalized = os.path.normpath(ruta)
        if normalized.startswith('..') or normalized.startswith('/') or normalized.startswith('\\'):
            return False, "La ruta contiene intento de path traversal"
        
        # Verificar caracteres no permitidos
        for c in ruta:
            if ord(c) < 32:
                return False, "La ruta contiene caracteres de control"
        
        return True, ""
    
    @staticmethod
    def validar_dependencias(dependencias: list[str], agentes_existentes: set[str] = None) -> tuple[bool, str]:
        """Valida dependencias."""
        if not dependencias:
            return True, ""
        
        for dep in dependencias:
            if not dep or not dep.strip():
                return False, "Dependencia vacía"
            if len(dep) > 100:
                return False, f"Dependencia '{dep}' es demasiado larga"
        
        if agentes_existentes is not None:
            for dep in dependencias:
                if dep not in agentes_existentes:
                    return False, f"Dependencia '{dep}' no existe"
        
        return True, ""


# ============================================================
# CLASE PRINCIPAL: AGENTE
# ============================================================

@dataclass
@total_ordering
class Agente:
    """
    Modelo de un agente ejecutable.
    
    Un agente representa una unidad de trabajo que puede ser ejecutada
    como parte de un flujo de procesamiento.
    """
    
    # ============================================================
    # IDENTIDAD Y ORQUESTACIÓN
    # ============================================================
    
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    nombre: str = ""
    tipo: TipoAgente = TipoAgente.PYTHON
    descripcion: str = ""
    dependencias_ids: list[str] = field(default_factory=list)
    dependencias_nombres: list[str] = field(default_factory=list)
    duracion: float = 5.0
    reintentos: int = 0
    max_reintentos: int = 3
    
    # ============================================================
    # ESTADO DE EJECUCIÓN (NO se persiste al guardar configuración)
    # ============================================================
    
    estado: EstadoAgente = EstadoAgente.PENDIENTE
    progreso: int = 0
    mensaje: str = ""
    tiempo_inicio: float | None = None
    tiempo_fin: float | None = None
    resultado: dict[str, Any] | None = None
    salida: str = ""
    error: str = ""
    
    # ============================================================
    # CONFIGURACIÓN ESPECÍFICA POR TIPO
    # ============================================================
    
    # ── Python ──
    codigo_python: str = ""
    funciones_import: list[str] = field(default_factory=list)
    timeout_python: int = 30
    memory_limit_mb: int | None = None   # límite RLIMIT_AS del sandbox (None = sin límite)
    
    # ── Shell ──
    comando_shell: str = ""
    working_dir: str = ""
    timeout_shell: int = 30
    
    # ── HTTP ──
    url_http: str = ""
    metodo_http: str = "GET"
    headers_http: dict[str, str] = field(default_factory=dict)
    body_http: str = ""
    timeout_http: int = 30
    
    # ── File ──
    archivo_origen: str = ""
    archivo_destino: str = ""
    operacion_file: str = "leer"
    modo_salida_file: str = "auto"  # ✅ NUEVO: 'auto' | 'contenido' | 'json'
    
        # ── LLM ──
    prompt_llm: str = ""
    modelo_llm: str = "deepseek-v4-flash"
    temperatura_llm: float = 0.7
    max_tokens_llm: int = 4000                  # ← default subido de 2000 a 4000
    reasoning_effort_llm: str = "low"
    thinking_enabled_llm: bool = False          
    prompt_reescrito_id: int = 0        # ✅ FASE 4c: ID de la versión reescrita usada
    
    # ── Loop ──
    fuente_items: str = ""
    codigo_por_item: str = ""
    max_iteraciones: int = 100
    timeout_loop: int = 300
    continuar_en_error: bool = False
    
    # ============================================================
    # CAMPOS DE ESTADO (excluidos al guardar)
    # ============================================================
    
    CAMPOS_RUNTIME = frozenset({
        "estado", "progreso", "mensaje",
        "tiempo_inicio", "tiempo_fin",
        "resultado", "salida", "error",
        "reintentos"
    })
    
    # ============================================================
    # MÉTODOS ESPECIALES
    # ============================================================
    
    def __post_init__(self):
        """Inicialización posterior a la creación."""
        if not self.nombre:
            self.nombre = f"Agente_{self.id}"
        
        # Validación básica
        if self.duracion <= 0:
            self.duracion = 0.1
        
        if self.progreso < 0:
            self.progreso = 0
        elif self.progreso > 100:
            self.progreso = 100
    
    def __eq__(self, other) -> bool:
        """Comparación por ID."""
        if not isinstance(other, Agente):
            return False
        return self.id == other.id
    
    def __lt__(self, other) -> bool:
        """Ordenamiento por nombre."""
        if not isinstance(other, Agente):
            return NotImplemented
        return self.nombre < other.nombre
    
    def __hash__(self) -> int:
        """Hash basado en ID."""
        return hash(self.id)
    
    def __repr__(self) -> str:
        """Representación detallada."""
        return f"Agente(id={self.id}, nombre={self.nombre}, tipo={self.tipo.value})"
    
    # ============================================================
    # MÉTODOS DE CLONACIÓN
    # ============================================================
    
    def clonar(self, nuevo_id: bool = True) -> 'Agente':
        """
        Crea una copia profunda del agente.
        
        Args:
            nuevo_id: Si es True, genera un nuevo ID
            
        Returns:
            Agente: Nueva instancia clonada
        """
        # Crear diccionario con todos los campos
        data = asdict(self)
        
        # Excluir campos de runtime
        for campo in self.CAMPOS_RUNTIME:
            data.pop(campo, None)
        
        # Generar nuevo ID si es necesario
        if nuevo_id:
            data['id'] = str(uuid.uuid4())[:8]
        
        # Crear nueva instancia
        return Agente(**data)
    
    def copiar_estado(self, otro: 'Agente'):
        """
        Copia el estado de ejecución de otro agente.
        
        Args:
            otro: Agente fuente
        """
        self.estado = otro.estado
        self.progreso = otro.progreso
        self.mensaje = otro.mensaje
        self.tiempo_inicio = otro.tiempo_inicio
        self.tiempo_fin = otro.tiempo_fin
        self.resultado = otro.resultado
        self.salida = otro.salida
        self.error = otro.error
        self.reintentos = otro.reintentos
    
    # ============================================================
    # RESET DE ESTADO
    # ============================================================
    
    def resetear_estado(self):
        """Vuelve el agente a su estado inicial, conservando su configuración."""
        self.estado = EstadoAgente.PENDIENTE
        self.progreso = 0
        self.mensaje = ""
        self.tiempo_inicio = None
        self.tiempo_fin = None
        self.resultado = None
        self.salida = ""
        self.error = ""
        self.reintentos = 0
    
    def marcar_como_completado(self, resultado: dict = None, salida: str = ""):
        """Marca el agente como completado exitosamente."""
        self.estado = EstadoAgente.COMPLETADO
        self.progreso = 100
        self.tiempo_fin = time.time()
        self.resultado = resultado or {}
        self.salida = salida
        self.mensaje = "✅ Completado"
    
    def marcar_como_error(self, error: str, resultado: dict = None):
        """Marca el agente como fallido."""
        self.estado = EstadoAgente.ERROR
        self.progreso = 100
        self.tiempo_fin = time.time()
        self.error = error
        self.resultado = resultado or {}
        self.mensaje = f"❌ Error: {error[:100]}"
    
    def marcar_como_cancelado(self):
        """Marca el agente como cancelado."""
        self.estado = EstadoAgente.CANCELADO
        self.progreso = 100
        self.tiempo_fin = time.time()
        self.mensaje = "⛔ Cancelado"
    
    # ============================================================
    # VALIDACIÓN (CORREGIDA)
    # ============================================================
    
    def validar_configuracion(self, agentes_disponibles: dict[str, 'Agente'] | None = None) -> tuple[bool, str]:
        """
        Valida la configuración del agente según su tipo.
        
        Args:
            agentes_disponibles: Diccionario de agentes disponibles para validar dependencias
            
        Returns:
            Tuple[bool, str]: (es_valido, mensaje_error)
        """
        # Validar nombre
        valido, mensaje = AgenteValidator.validar_nombre(self.nombre)
        if not valido:
            return False, f"Nombre inválido: {mensaje}"
        
        # Validar tipo
        if not isinstance(self.tipo, TipoAgente):
            return False, f"Tipo inválido: {self.tipo}"
        
        # Validar dependencias
        if agentes_disponibles:
            nombres_agentes = {a.nombre for a in agentes_disponibles.values()}
            for dep_id in self.dependencias_ids:
                if dep_id not in agentes_disponibles:
                    return False, f"Dependencia ID '{dep_id}' no existe"
            for dep_nombre in self.dependencias_nombres:
                if dep_nombre not in nombres_agentes:
                    return False, f"Dependencia nombre '{dep_nombre}' no existe"
        else:
            # Validación básica sin agentes disponibles
            for dep_id in self.dependencias_ids:
                if not dep_id or not dep_id.strip():
                    return False, "ID de dependencia vacío"
            for dep_nombre in self.dependencias_nombres:
                if not dep_nombre or not dep_nombre.strip():
                    return False, "Nombre de dependencia vacío"
        
        # Validar duración
        if self.duracion <= 0:
            return False, "La duración debe ser mayor que 0"
        
        # Validar reintentos
        if self.max_reintentos < 0:
            return False, "Los reintentos no pueden ser negativos"
        
        # Validar según tipo
        if self.tipo == TipoAgente.LOOP:
            return self._validar_loop(agentes_disponibles)
        
        elif self.tipo == TipoAgente.PYTHON:
            return self._validar_python()
        
        elif self.tipo == TipoAgente.SHELL:
            return self._validar_shell()
        
        elif self.tipo == TipoAgente.HTTP:
            return self._validar_http()
        
        elif self.tipo == TipoAgente.LLM:
            return self._validar_llm()
        
        elif self.tipo == TipoAgente.FILE:
            return self._validar_file()
        
        return True, ""
    
    def _validar_python(self) -> tuple[bool, str]:
        """Valida configuración de Python."""
        if self.timeout_python < 1:
            return False, f"Python: 'timeout_python' debe ser >= 1 (actual: {self.timeout_python})"
        
        # Validar código (opcional)
        if self.codigo_python:
            valido, mensaje = AgenteValidator.validar_codigo(self.codigo_python)
            if not valido:
                return False, f"Python: {mensaje}"
        
        return True, ""
    
    def _validar_shell(self) -> tuple[bool, str]:
        """Valida configuración de Shell."""
        if not self.comando_shell or not self.comando_shell.strip():
            return False, "Shell: 'comando_shell' es obligatorio"
        
        if self.timeout_shell < 1:
            return False, f"Shell: 'timeout_shell' debe ser >= 1 (actual: {self.timeout_shell})"
        
        # Verificar comandos peligrosos (advertencia)
        peligrosos = ['rm -rf', 'dd if=', 'mkfs', '> /', '| /']
        for peligroso in peligrosos:
            if peligroso in self.comando_shell:
                logger.warning(f"Comando shell contiene operación potencialmente peligrosa: {peligroso}")
        
        return True, ""
    
    def _validar_http(self) -> tuple[bool, str]:
        """Valida configuración de HTTP."""
        if not self.url_http or not self.url_http.strip():
            return False, "HTTP: 'url_http' es obligatoria"
        
        valido, mensaje = AgenteValidator.validar_url(self.url_http)
        if not valido:
            return False, f"HTTP: {mensaje}"
        
        if self.timeout_http < 1:
            return False, f"HTTP: 'timeout_http' debe ser >= 1 (actual: {self.timeout_http})"
        
        # Validar método HTTP
        metodos_validos = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
        if self.metodo_http.upper() not in metodos_validos:
            return False, f"HTTP: método '{self.metodo_http}' no soportado"
        
        return True, ""
    
    def _validar_llm(self) -> tuple[bool, str]:
        """Valida configuración de LLM."""
        if not self.prompt_llm or not self.prompt_llm.strip():
            return False, "LLM: 'prompt_llm' es obligatorio"

        if not self.modelo_llm or not self.modelo_llm.strip():
            return False, "LLM: 'modelo_llm' es obligatorio"

        if self.temperatura_llm < 0 or self.temperatura_llm > 2:
            return False, (
                f"LLM: 'temperatura_llm' debe estar entre 0 y 2 "
                f"(actual: {self.temperatura_llm})"
            )

        if self.max_tokens_llm < 1:
            return False, (
                f"LLM: 'max_tokens_llm' debe ser >= 1 "
                f"(actual: {self.max_tokens_llm})"
            )

        if self.reasoning_effort_llm not in ("low", "medium", "high"):
            return False, (
                f"LLM: 'reasoning_effort_llm' debe ser "
                f"low|medium|high (actual: {self.reasoning_effort_llm})"
            )

        # ✅ NUEVO: validar thinking_enabled_llm
        if not isinstance(self.thinking_enabled_llm, bool):
            return False, (
                f"LLM: 'thinking_enabled_llm' debe ser booleano "
                f"(actual: {self.thinking_enabled_llm})"
            )

        return True, ""
    
    def _validar_file(self) -> tuple[bool, str]:
        """Valida configuración de File."""
        operaciones_validas = {"leer", "escribir", "copiar", "mover", "eliminar"}
        if self.operacion_file not in operaciones_validas:
            return False, f"File: operación '{self.operacion_file}' no soportada"

        # ✅ NUEVO: validar modo_salida_file
        modos_validos = {"auto", "contenido", "json", "texto"}
        if self.modo_salida_file not in modos_validos:
            return False, (
                f"File: 'modo_salida_file' inválido: '{self.modo_salida_file}'. "
                f"Opciones: {sorted(modos_validos)}"
            )
        
        if self.operacion_file in ("copiar", "mover") and not self.archivo_origen:
            return False, f"File: 'archivo_origen' es obligatorio para operación '{self.operacion_file}'"
        
        if self.operacion_file in ("escribir", "copiar", "mover") and not self.archivo_destino:
            return False, f"File: 'archivo_destino' es obligatorio para operación '{self.operacion_file}'"
        
        if self.operacion_file in ("leer", "eliminar") and not self.archivo_origen:
            return False, f"File: 'archivo_origen' es obligatorio para operación '{self.operacion_file}'"
        
        # Validar rutas
        if self.archivo_origen:
            valido, mensaje = AgenteValidator.validar_ruta(self.archivo_origen)
            if not valido:
                return False, f"File: {mensaje}"
        
        if self.archivo_destino:
            valido, mensaje = AgenteValidator.validar_ruta(self.archivo_destino)
            if not valido:
                return False, f"File: {mensaje}"
        
        return True, ""
    
    def _validar_loop(self, agentes_disponibles: dict[str, 'Agente'] | None = None) -> tuple[bool, str]:
        """
        Valida configuración de Loop.
        CORREGIDO: No depende de listas que se vacían (dependencias_nombres).
        La validación de dependencias se hace a nivel global.
        """
        fuente = (self.fuente_items or "").strip()
        if not fuente:
            return False, "Loop: 'fuente_items' es obligatorio"
        
        partes = [p.strip() for p in fuente.split('.') if p.strip()]
        if len(partes) < 2:
            return False, f"Loop: 'fuente_items' debe tener formato 'Dependencia.clave' (actual: '{fuente}')"
        
        # Validar que la dependencia existe (si se proporcionan agentes disponibles)
        nombre_dependencia = partes[0]
        if agentes_disponibles:
            nombres_agentes = {a.nombre for a in agentes_disponibles.values()}
            if nombre_dependencia not in nombres_agentes:
                return False, (
                    f"Loop: la fuente '{nombre_dependencia}' no corresponde a un agente disponible. "
                    f"Agentes disponibles: {sorted(nombres_agentes)}"
                )
        
        if not self.codigo_por_item or not self.codigo_por_item.strip():
            return False, "Loop: 'codigo_por_item' es obligatorio"
        
        if self.max_iteraciones < 1:
            return False, f"Loop: 'max_iteraciones' debe ser >= 1 (actual: {self.max_iteraciones})"
        
        if self.timeout_loop < 1:
            return False, f"Loop: 'timeout_loop' debe ser >= 1 (actual: {self.timeout_loop})"
        
        if self.timeout_python < 1:
            return False, f"Loop: 'timeout_python' debe ser >= 1 (actual: {self.timeout_python})"
        
        # Validar que el código sea sintácticamente válido (básico)
        if self.codigo_por_item:
            try:
                # Compilar para verificar sintaxis
                compile(self.codigo_por_item, '<string>', 'exec')
            except SyntaxError as e:
                return False, f"Loop: Error de sintaxis en 'codigo_por_item': {e}"
        
        return True, ""
    
    # ============================================================
    # MÉTODOS DE AYUDA PARA TIPOS ESPECÍFICOS
    # ============================================================
    
    def es_loop(self) -> bool:
        """Retorna True si el agente es de tipo Loop."""
        return self.tipo == TipoAgente.LOOP
    
    def es_llm(self) -> bool:
        """Retorna True si el agente es de tipo LLM."""
        return self.tipo == TipoAgente.LLM
    
    def es_http(self) -> bool:
        """Retorna True si el agente es de tipo HTTP."""
        return self.tipo == TipoAgente.HTTP
    
    # core/agent.py - MÉTODO es_python CORREGIDO

    def es_python(self) -> bool:
        """Retorna True si el agente es de tipo Python."""
        # ✅ CORREGIDO: Comparación directa (no usar 'in' sobre un solo elemento)
        return self.tipo == TipoAgente.PYTHON
    
    def tiene_dependencias(self) -> bool:
        """Retorna True si el agente tiene dependencias."""
        return bool(self.dependencias_ids or self.dependencias_nombres)
    
    def esta_completado(self) -> bool:
        """Retorna True si el agente está completado."""
        return self.estado == EstadoAgente.COMPLETADO
    
    def esta_error(self) -> bool:
        """Retorna True si el agente está en error."""
        return self.estado == EstadoAgente.ERROR
    
    def esta_ejecutando(self) -> bool:
        """Retorna True si el agente está ejecutándose."""
        return self.estado == EstadoAgente.EJECUTANDO
    
    def esta_terminal(self) -> bool:
        """Retorna True si el agente está en estado terminal."""
        return EstadoAgente.es_terminal(self.estado)
    
    # ============================================================
    # MÉTODOS PARA LOOP
    # ============================================================
    
    def obtener_ruta_completa(self) -> list[str] | None:
        """
        Para agentes Loop: retorna la ruta completa de 'fuente_items'
        como lista de partes, o None si no está configurada.
        
        Ej: "ObtenerLista.items" → ["ObtenerLista", "items"]
        """
        if not self.fuente_items:
            return None
        return [p.strip() for p in self.fuente_items.split('.') if p.strip()]
    
    def obtener_nombre_dependencia(self) -> str | None:
        """
        Para agentes Loop: retorna el nombre de la dependencia raíz
        de la que se obtienen los items.
        
        Ej: "ObtenerLista.items" → "ObtenerLista"
        """
        ruta = self.obtener_ruta_completa()
        return ruta[0] if ruta else None
    
    def obtener_clave_items(self) -> str | None:
        """
        Para agentes Loop: retorna la clave dentro del resultado
        de la dependencia donde están los items.
        
        Ej: "ObtenerLista.items" → "items"
        Ej: "Data.results.items" → "results.items"
        """
        ruta = self.obtener_ruta_completa()
        if not ruta or len(ruta) < 2:
            return None
        return '.'.join(ruta[1:])
    
    def tiene_fuente_items(self) -> bool:
        """Verifica si el agente Loop tiene una fuente de items configurada."""
        return self.es_loop() and bool(self.fuente_items)
    
    def tiene_codigo_por_item(self) -> bool:
        """Verifica si el agente Loop tiene código para procesar cada item."""
        return self.es_loop() and bool(self.codigo_por_item)
    
    # ============================================================
    # SERIALIZACIÓN
    # ============================================================
    
    def to_dict(self) -> dict:
        """
        Representación del agente para historial de ejecuciones.
        Incluye solo información relevante para el historial.

        ✅ FASE 1 (feedback): para agentes LLM se persiste también el
        prompt real que se usó (ya con el endurecimiento aplicado). Es la
        materia prima que el FeedbackProcessor reescribirá cuando el usuario
        deje un comentario negativo.
        """
        data = {
            "id": self.id,
            "nombre": self.nombre,
            "tipo": self.tipo.value,
            "descripcion": self.descripcion,
            "dependencias": self.dependencias_ids,
            "duracion": self.duracion,
            "estado": self.estado.value,
            "progreso": self.progreso,
            "mensaje": self.mensaje,
            "resultado": self.resultado,
            "salida": self.salida[:500] if self.salida else "",
            "error": self.error[:500] if self.error else "",
            "tiempo_ejecucion": (self.tiempo_fin - self.tiempo_inicio) if (self.tiempo_inicio and self.tiempo_fin) else 0
        }

        # ✅ FASE 1: persistir el prompt del agente LLM.
        # Truncado a 4000 chars para no inflar la BD si el LLM generó un
        # prompt gigante. El FeedbackProcessor trabajará con este texto tal
        # cual; si se truncara más agresivamente se perdería contexto útil.
        if self.tipo == TipoAgente.LLM:
            data["prompt_usado"] = (self.prompt_llm or "")[:4000]

        return data
    
    def to_json(self) -> str:
        """Serializa a JSON."""
        data = self.to_dict()
        return json.dumps(data, default=str, ensure_ascii=False)
    
    @classmethod
    def from_dict(cls, data: dict) -> 'Agente':
        """
        Reconstruye un Agente desde un diccionario.
        Compatible con formatos nuevos y antiguos.
        """
        # Crear copia para no modificar el original
        kwargs = data.copy()

        # Compatibilidad con to_dict(): emite 'dependencias' (no
        # 'dependencias_ids') y 'prompt_usado' (no 'prompt_llm').
        if 'dependencias' in kwargs and 'dependencias_ids' not in kwargs:
            kwargs['dependencias_ids'] = kwargs.pop('dependencias')
        if 'prompt_usado' in kwargs and 'prompt_llm' not in kwargs:
            kwargs['prompt_llm'] = kwargs.pop('prompt_usado')

        # Limpiar campos que no existen en el dataclass
        valid_fields = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in kwargs.items() if k in valid_fields}
        
        # Convertir tipo
        if 'tipo' in kwargs:
            if isinstance(kwargs['tipo'], str):
                kwargs['tipo'] = TipoAgente.from_string(kwargs['tipo'])
            elif not isinstance(kwargs['tipo'], TipoAgente):
                kwargs['tipo'] = TipoAgente.PYTHON

        # Convertir estado (to_dict lo serializa como string)
        if 'estado' in kwargs:
            if isinstance(kwargs['estado'], str):
                try:
                    kwargs['estado'] = EstadoAgente(kwargs['estado'])
                except ValueError:
                    kwargs['estado'] = EstadoAgente.PENDIENTE
            elif not isinstance(kwargs['estado'], EstadoAgente):
                kwargs['estado'] = EstadoAgente.PENDIENTE

        # Asegurar campos de lista
        for campo in ['dependencias_ids', 'dependencias_nombres', 'funciones_import']:
            valor = kwargs.get(campo)
            if isinstance(valor, str):
                # Un string no es una lista de dependencias: evitar iterar
                # sus caracteres. Se acepta también una lista con un solo
                # nombre separada por comas.
                kwargs[campo] = [p.strip() for p in valor.split(',') if p.strip()]
            elif not isinstance(valor, list):
                kwargs[campo] = []

        return cls(**kwargs)
    
    # ============================================================
    # MÉTRICAS Y ESTADÍSTICAS
    # ============================================================
    
    def obtener_tiempo_ejecucion(self) -> float:
        """Retorna el tiempo de ejecución en segundos."""
        if self.tiempo_inicio and self.tiempo_fin:
            return self.tiempo_fin - self.tiempo_inicio
        elif self.tiempo_inicio:
            return time.time() - self.tiempo_inicio
        return 0.0
    
    def obtener_duracion_estimada(self) -> float:
        """Retorna la duración estimada (configurada o real)."""
        if self.tiempo_inicio:
            return self.obtener_tiempo_ejecucion()
        return self.duracion
    
    def obtener_progreso_real(self) -> int:
        """Retorna el progreso real basado en el estado."""
        if self.estado == EstadoAgente.COMPLETADO:
            return 100
        elif self.estado == EstadoAgente.ERROR:
            return 100
        elif self.estado == EstadoAgente.CANCELADO:
            return 100
        elif self.estado == EstadoAgente.EJECUTANDO:
            return max(self.progreso, 10)
        elif self.estado == EstadoAgente.LISTO:
            return 50
        elif self.estado == EstadoAgente.ESPERANDO:
            return 25
        return self.progreso
    
    # ============================================================
    # REPRESENTACIONES PARA UI
    # ============================================================
    
    def resumen_corto(self) -> str:
        """Retorna un resumen corto para logs y UI."""
        if self.tipo == TipoAgente.LOOP:
            sufijo = ", continúa en error" if self.continuar_en_error else ""
            return f"{self.nombre} (Loop: {self.fuente_items} → {self.max_iteraciones} max{sufijo})"
        elif self.tipo == TipoAgente.LLM:
            return f"{self.nombre} (LLM: {self.modelo_llm})"
        elif self.tipo == TipoAgente.PYTHON:
            return f"{self.nombre} (Python: {len(self.codigo_python)} chars)"
        elif self.tipo == TipoAgente.SHELL:
            return f"{self.nombre} (Shell: {self.comando_shell[:30]}...)"
        elif self.tipo == TipoAgente.HTTP:
            return f"{self.nombre} (HTTP: {self.metodo_http} {self.url_http[:30]}...)"
        elif self.tipo == TipoAgente.FILE:
            return f"{self.nombre} (File: {self.operacion_file})"
        return f"{self.nombre} ({self.tipo.value})"
    
    def obtener_icono(self) -> str:
        """Retorna el icono del agente para UI."""
        return TipoAgente.icono(self.tipo)
    
    def obtener_color_estado(self) -> str:
        """Retorna el color del estado actual."""
        return EstadoAgente.color(self.estado)
    
    def obtener_emoji_estado(self) -> str:
        """Retorna el emoji del estado actual."""
        return EstadoAgente.emoji(self.estado)
    
    def obtener_info_tooltip(self) -> str:
        """Retorna información detallada para tooltip."""
        lines = [
            f"🤖 {self.nombre}",
            f"📌 Tipo: {self.tipo.value}",
            f"📊 Estado: {self.obtener_emoji_estado()} {self.estado.value}",
            f"📝 {self.descripcion or 'Sin descripción'}",
        ]
        
        if self.tiene_dependencias():
            deps = self.dependencias_nombres or self.dependencias_ids
            lines.append(f"🔗 Dependencias: {', '.join(deps)}")
        
        if self.estado == EstadoAgente.EJECUTANDO:
            lines.append(f"⏱ Tiempo: {self.obtener_tiempo_ejecucion():.1f}s")
        
        if self.error:
            lines.append(f"❌ Error: {self.error[:100]}")
        
        if self.resultado:
            lines.append(f"📊 Resultado: {str(self.resultado)[:100]}...")
        
        return "\n".join(lines)
    
    # ============================================================
    # HASH Y FIRMA PARA CACHÉ
    # ============================================================
    
    def obtener_hash_configuracion(self) -> str:
        """
        Calcula un hash de la configuración del agente.
        Útil para detectar cambios en la configuración.
        """
        # Obtener todos los campos excepto los de runtime
        data = asdict(self)
        for campo in self.CAMPOS_RUNTIME:
            data.pop(campo, None)
        
        # Ordenar y serializar
        json_str = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(json_str.encode('utf-8')).hexdigest()[:16]
    
    def obtener_firma_ejecucion(self) -> str:
        """
        Calcula una firma de la ejecución incluyendo el resultado.
        Útil para detectar ejecuciones idénticas.
        """
        data = {
            'id': self.id,
            'nombre': self.nombre,
            'tipo': self.tipo.value,
            'estado': self.estado.value,
            'resultado': self.resultado,
            'error': self.error,
            'tiempo': self.obtener_tiempo_ejecucion()
        }
        json_str = json.dumps(data, sort_keys=True, default=str)
        return hashlib.sha256(json_str.encode('utf-8')).hexdigest()[:16]
    
    # ============================================================
    # MÉTODOS ESTÁTICOS DE UTILIDAD
    # ============================================================
    
    @staticmethod
    def crear_agente_por_tipo(
        nombre: str,
        tipo: str | TipoAgente,
        **kwargs
    ) -> 'Agente':
        """
        Crea un agente con configuración por defecto para el tipo.

        Args:
            nombre: Nombre del agente
            tipo: Tipo del agente (string o TipoAgente)
            **kwargs: Configuración adicional

        Returns:
            Agente: Nueva instancia
        """
        if isinstance(tipo, str):
            tipo = TipoAgente.from_string(tipo)

        # Configuración por defecto según tipo
        defaults = {
            TipoAgente.PYTHON: {'timeout_python': 30},
            TipoAgente.SHELL: {'timeout_shell': 30},
            TipoAgente.HTTP: {'metodo_http': 'GET', 'timeout_http': 30},
            TipoAgente.LLM: {
                'modelo_llm': 'deepseek-v4-flash',
                'temperatura_llm': 0.7,
                'max_tokens_llm': 4000,             # ← subido de 1000 a 4000
                'reasoning_effort_llm': 'low',
                'thinking_enabled_llm': False,      # ← NUEVO
            },
            TipoAgente.FILE: {'operacion_file': 'leer'},
            TipoAgente.LOOP: {
                'max_iteraciones': 100,
                'timeout_loop': 300,
                'continuar_en_error': False,
            },
        }

        base_config = defaults.get(tipo, {})
        base_config.update(kwargs)

        return Agente(nombre=nombre, tipo=tipo, **base_config)
    
    @staticmethod
    def validar_configuracion_completa(agentes: list['Agente']) -> tuple[bool, list[str]]:
        """
        Valida la configuración de múltiples agentes incluyendo dependencias.
        
        Args:
            agentes: Lista de agentes a validar
            
        Returns:
            Tuple[bool, List[str]]: (es_valido, lista_de_errores)
        """
        errores = []
        nombres = {a.nombre for a in agentes}
        ids = {a.id for a in agentes}
        agentes_dict = {a.id: a for a in agentes}
        
        # Validar nombres duplicados
        nombres_vistos = set()
        for agente in agentes:
            if agente.nombre in nombres_vistos:
                errores.append(f"Nombre duplicado: '{agente.nombre}'")
            nombres_vistos.add(agente.nombre)
        
        for agente in agentes:
            # Validar configuración individual con agentes disponibles
            valido, error = agente.validar_configuracion(agentes_dict)
            if not valido:
                errores.append(f"{agente.nombre}: {error}")
            
            # Validar dependencias IDs
            for dep_id in agente.dependencias_ids:
                if dep_id not in ids:
                    errores.append(f"{agente.nombre}: dependencia ID '{dep_id}' no existe")
            
            # Validar dependencias nombres
            for dep_nombre in agente.dependencias_nombres:
                if dep_nombre not in nombres:
                    errores.append(f"{agente.nombre}: dependencia nombre '{dep_nombre}' no existe")
        
        return len(errores) == 0, errores

    # ============================================================
    # MÉTODOS DE TRANSICIÓN DE ESTADO (NUEVA SECCIÓN)
    # ============================================================
    
    def transicionar_a(self, nuevo_estado: EstadoAgente, razon: str = "") -> bool:
        """
        Realiza una transición de estado validada usando la máquina de estados.
        
        Args:
            nuevo_estado: Estado al que se quiere transicionar
            razon: Razón de la transición (para logs)
            
        Returns:
            bool: True si la transición fue exitosa
            
        Raises:
            ValueError: Si la transición no es válida según la máquina de estados
        """
        from .scheduler import TRANSICIONES_VALIDAS
        
        estado_actual = self.estado
        
        # Si ya estamos en el estado destino, no hacer nada
        if estado_actual == nuevo_estado:
            return True
        
        # Verificar si el estado actual tiene transiciones definidas
        if estado_actual not in TRANSICIONES_VALIDAS:
            raise ValueError(
                f"Estado actual '{estado_actual.value}' no tiene transiciones definidas"
            )
        
        # Verificar si la transición es válida
        if nuevo_estado not in TRANSICIONES_VALIDAS[estado_actual]:
            validos = [e.value for e in TRANSICIONES_VALIDAS[estado_actual]]
            raise ValueError(
                f"Transición inválida: {estado_actual.value} → {nuevo_estado.value}\n"
                f"Transiciones válidas desde {estado_actual.value}: {validos}"
            )
        
        # Realizar la transición
        self.estado = nuevo_estado
        if razon:
            self.mensaje = razon
        
        
        return True
    
    def es_terminal(self) -> bool:
        """Indica si el agente está en un estado terminal."""
        return EstadoAgente.es_terminal(self.estado)
    
    def puede_ejecutarse(self) -> bool:
        """Indica si el agente puede ser ejecutado."""
        return EstadoAgente.puede_ejecutarse(self.estado)
    
    def esta_activo(self) -> bool:
        """Indica si el agente está en ejecución activa."""
        return EstadoAgente.es_activo(self.estado)


# ============================================================
# REGISTRO DE TIPOS DE AGENTES (para UI)
# ============================================================

# Mapeo de tipos de agentes a sus configuraciones por defecto
TIPOS_AGENTES_CONFIG = {
    TipoAgente.PYTHON: {
        'descripcion': 'Ejecuta código Python en un sandbox aislado',
        'icono': '🐍',
        'categoria': 'Programación',
        'campos_requeridos': ['codigo_python'],
    },
    TipoAgente.SHELL: {
        'descripcion': 'Ejecuta comandos de shell del sistema',
        'icono': '💻',
        'categoria': 'Sistema',
        'campos_requeridos': ['comando_shell'],
    },
    TipoAgente.LLM: {
        'descripcion': 'Llama a modelos de lenguaje (DeepSeek)',
        'icono': '🧠',
        'categoria': 'IA',
        'campos_requeridos': ['prompt_llm', 'modelo_llm'],
        'campos_opcionales': [
            'temperatura_llm',
            'max_tokens_llm',
            'reasoning_effort_llm',
            'thinking_enabled_llm',
        ],
    },
    TipoAgente.HTTP: {
        'descripcion': 'Realiza peticiones HTTP/HTTPS',
        'icono': '🌐',
        'categoria': 'Red',
        'campos_requeridos': ['url_http'],
    },
    TipoAgente.FILE: {
        'descripcion': 'Operaciones con archivos del sistema',
        'icono': '📄',
        'categoria': 'Archivos',
        'campos_requeridos': ['operacion_file'],
    },
    TipoAgente.LOOP: {
        'descripcion': 'Itera sobre una lista de items',
        'icono': '🔄',
        'categoria': 'Estructura',
        'campos_requeridos': ['fuente_items', 'codigo_por_item'],
    },
}


def obtener_config_tipo(tipo: TipoAgente) -> dict:
    """Obtiene la configuración por defecto para un tipo de agente."""
    return TIPOS_AGENTES_CONFIG.get(tipo, {})


def obtener_tipos_por_categoria(categoria: str) -> list:
    """Obtiene los tipos de agente por categoría."""
    return [
        tipo for tipo, config in TIPOS_AGENTES_CONFIG.items()
        if config.get('categoria') == categoria
    ]


def obtener_categorias() -> list:
    """Obtiene todas las categorías de agentes disponibles."""
    return sorted(set(
        config.get('categoria', 'Otro')
        for config in TIPOS_AGENTES_CONFIG.values()
    ))


# ============================================================
# EXPORTACIONES EXPLÍCITAS
# ============================================================

__all__ = [
    'Agente',
    'EstadoAgente',
    'TipoAgente',
    'AgenteValidator',
    'TIPOS_AGENTES_CONFIG',
    'obtener_config_tipo',
    'obtener_tipos_por_categoria',
    'obtener_categorias',
]
