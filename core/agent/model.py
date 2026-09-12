# core/agent/model.py
"""
Modelo de datos para agentes ejecutables.

Esta clase es el núcleo del agente: solo contiene:
- Campos del dataclass
- Métodos especiales (__post_init__, __eq__, etc.)
- Métodos públicos que delegan en módulos especializados

Para detalles de implementación ver:
- state_machine.py       → transiciones de estado
- validators_by_type.py  → validaciones por tipo
- serialization.py       → clonar, reset, to_dict, from_dict
- presentation.py        → métricas, iconos, tooltips
"""
import uuid
import time
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any, Union, Tuple
from functools import total_ordering

from .enums import EstadoAgente, TipoAgente
from .validator import AgenteValidator
from . import state_machine
from . import validators_by_type
from . import serialization
from . import presentation

logger = logging.getLogger(__name__)


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
    dependencias_ids: List[str] = field(default_factory=list)
    dependencias_nombres: List[str] = field(default_factory=list)
    duracion: float = 5.0
    reintentos: int = 0
    max_reintentos: int = 3

    # ============================================================
    # ESTADO DE EJECUCIÓN (no se persiste)
    # ============================================================
    estado: EstadoAgente = EstadoAgente.PENDIENTE
    progreso: int = 0
    mensaje: str = ""
    tiempo_inicio: Optional[float] = None
    tiempo_fin: Optional[float] = None
    resultado: Optional[Dict[str, Any]] = None
    salida: str = ""
    error: str = ""

    # ============================================================
    # CONFIGURACIÓN ESPECÍFICA POR TIPO
    # ============================================================
    # Python
    codigo_python: str = ""
    funciones_import: List[str] = field(default_factory=list)
    timeout_python: int = 30

    # Shell
    comando_shell: str = ""
    working_dir: str = ""
    timeout_shell: int = 30

    # HTTP
    url_http: str = ""
    metodo_http: str = "GET"
    headers_http: Dict[str, str] = field(default_factory=dict)
    body_http: str = ""
    timeout_http: int = 30

    # File
    archivo_origen: str = ""
    archivo_destino: str = ""
    operacion_file: str = "leer"
    modo_salida_file: str = "auto"

    # LLM
    prompt_llm: str = ""
    modelo_llm: str = "deepseek-v4-flash"
    temperatura_llm: float = 0.7
    max_tokens_llm: int = 4000
    reasoning_effort_llm: str = "low"
    thinking_enabled_llm: bool = False

    # Loop
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
        "reintentos",
    })

    # ============================================================
    # MÉTODOS ESPECIALES
    # ============================================================
    def __post_init__(self):
        if not self.nombre:
            self.nombre = f"Agente_{self.id}"
        if self.duracion <= 0:
            self.duracion = 0.1
        if self.progreso < 0:
            self.progreso = 0
        elif self.progreso > 100:
            self.progreso = 100

    def __eq__(self, other) -> bool:
        if not isinstance(other, Agente):
            return False
        return self.id == other.id

    def __lt__(self, other) -> bool:
        if not isinstance(other, Agente):
            return NotImplemented
        return self.nombre < other.nombre

    def __hash__(self) -> int:
        return hash(self.id)

    def __repr__(self) -> str:
        return f"Agente(id={self.id}, nombre={self.nombre}, tipo={self.tipo.value})"

    # ============================================================
    # CLONACIÓN (delegan en serialization.py)
    # ============================================================
    def clonar(self, nuevo_id: bool = True) -> "Agente":
        return serialization.clonar(self, nuevo_id=nuevo_id)

    def copiar_estado(self, otro: "Agente"):
        serialization.copiar_estado(self, otro)

    def resetear_estado(self):
        serialization.resetear_estado(self)

    # ============================================================
    # TRANSICIONES DE ESTADO (delegan en state_machine.py)
    # ============================================================
    def marcar_como_completado(self, resultado: Dict = None, salida: str = ""):
        self.estado = EstadoAgente.COMPLETADO
        self.progreso = 100
        self.tiempo_fin = time.time()
        self.resultado = resultado or {}
        self.salida = salida
        self.mensaje = "✅ Completado"

    def marcar_como_error(self, error: str, resultado: Dict = None):
        self.estado = EstadoAgente.ERROR
        self.progreso = 100
        self.tiempo_fin = time.time()
        self.error = error
        self.resultado = resultado or {}
        self.mensaje = f"❌ Error: {error[:100]}"

    def marcar_como_cancelado(self):
        self.estado = EstadoAgente.CANCELADO
        self.progreso = 100
        self.tiempo_fin = time.time()
        self.mensaje = "⛔ Cancelado"

    def transicionar_a(self, nuevo_estado: EstadoAgente, razon: str = "") -> bool:
        return state_machine.transicionar_a(self, nuevo_estado, razon)

    def es_terminal(self) -> bool:
        return state_machine.es_terminal(self)

    def puede_ejecutarse(self) -> bool:
        return state_machine.puede_ejecutarse(self)

    def esta_activo(self) -> bool:
        return state_machine.esta_activo(self)

    # ============================================================
    # VALIDACIÓN (delega en validators_by_type.py)
    # ============================================================
    def validar_configuracion(
        self,
        agentes_disponibles: Optional[Dict[str, "Agente"]] = None
    ) -> Tuple[bool, str]:
        # Validaciones comunes
        valido, mensaje = AgenteValidator.validar_nombre(self.nombre)
        if not valido:
            return False, f"Nombre inválido: {mensaje}"

        if not isinstance(self.tipo, TipoAgente):
            return False, f"Tipo inválido: {self.tipo}"

        if agentes_disponibles:
            nombres_agentes = {a.nombre for a in agentes_disponibles.values()}
            for dep_id in self.dependencias_ids:
                if dep_id not in agentes_disponibles:
                    return False, f"Dependencia ID '{dep_id}' no existe"
            for dep_nombre in self.dependencias_nombres:
                if dep_nombre not in nombres_agentes:
                    return False, f"Dependencia nombre '{dep_nombre}' no existe"
        else:
            for dep_id in self.dependencias_ids:
                if not dep_id or not dep_id.strip():
                    return False, "ID de dependencia vacío"
            for dep_nombre in self.dependencias_nombres:
                if not dep_nombre or not dep_nombre.strip():
                    return False, "Nombre de dependencia vacío"

        if self.duracion <= 0:
            return False, "La duración debe ser mayor que 0"
        if self.max_reintentos < 0:
            return False, "Los reintentos no pueden ser negativos"

        # Validaciones específicas por tipo
        if self.tipo == TipoAgente.LOOP:
            return validators_by_type.validar_loop(self, agentes_disponibles)
        elif self.tipo == TipoAgente.PYTHON:
            return validators_by_type.validar_python(self)
        elif self.tipo == TipoAgente.SHELL:
            return validators_by_type.validar_shell(self)
        elif self.tipo == TipoAgente.HTTP:
            return validators_by_type.validar_http(self)
        elif self.tipo == TipoAgente.LLM:
            return validators_by_type.validar_llm(self)
        elif self.tipo == TipoAgente.FILE:
            return validators_by_type.validar_file(self)
        return True, ""

    # ============================================================
    # HELPERS DE TIPO
    # ============================================================
    def es_loop(self) -> bool:
        return self.tipo == TipoAgente.LOOP

    def es_llm(self) -> bool:
        return self.tipo == TipoAgente.LLM

    def es_http(self) -> bool:
        return self.tipo == TipoAgente.HTTP

    def es_python(self) -> bool:
        return self.tipo == TipoAgente.PYTHON

    def tiene_dependencias(self) -> bool:
        return bool(self.dependencias_ids or self.dependencias_nombres)

    def esta_completado(self) -> bool:
        return self.estado == EstadoAgente.COMPLETADO

    def esta_error(self) -> bool:
        return self.estado == EstadoAgente.ERROR

    def esta_ejecutando(self) -> bool:
        return self.estado == EstadoAgente.EJECUTANDO

    # ============================================================
    # HELPERS DE LOOP
    # ============================================================
    def obtener_ruta_completa(self) -> Optional[List[str]]:
        if not self.fuente_items:
            return None
        return [p.strip() for p in self.fuente_items.split('.') if p.strip()]

    def obtener_nombre_dependencia(self) -> Optional[str]:
        ruta = self.obtener_ruta_completa()
        return ruta[0] if ruta else None

    def obtener_clave_items(self) -> Optional[str]:
        ruta = self.obtener_ruta_completa()
        if not ruta or len(ruta) < 2:
            return None
        return '.'.join(ruta[1:])

    def tiene_fuente_items(self) -> bool:
        return self.es_loop() and bool(self.fuente_items)

    def tiene_codigo_por_item(self) -> bool:
        return self.es_loop() and bool(self.codigo_por_item)

    # ============================================================
    # SERIALIZACIÓN (delegan en serialization.py)
    # ============================================================
    def to_dict(self) -> Dict:
        return serialization.to_dict(self)

    def to_json(self) -> str:
        return serialization.to_json(self)

    @classmethod
    def from_dict(cls, data: Dict) -> "Agente":
        return serialization.from_dict(cls, data)

    # ============================================================
    # MÉTRICAS Y UI (delegan en presentation.py)
    # ============================================================
    def obtener_tiempo_ejecucion(self) -> float:
        return presentation.obtener_tiempo_ejecucion(self)

    def obtener_duracion_estimada(self) -> float:
        return presentation.obtener_duracion_estimada(self)

    def obtener_progreso_real(self) -> int:
        return presentation.obtener_progreso_real(self)

    def resumen_corto(self) -> str:
        return presentation.resumen_corto(self)

    def obtener_icono(self) -> str:
        return presentation.obtener_icono(self)

    def obtener_color_estado(self) -> str:
        return presentation.obtener_color_estado(self)

    def obtener_emoji_estado(self) -> str:
        return presentation.obtener_emoji_estado(self)

    def obtener_info_tooltip(self) -> str:
        return presentation.obtener_info_tooltip(self)

    # ============================================================
    # HASH (delegan en serialization.py)
    # ============================================================
    def obtener_hash_configuracion(self) -> str:
        return serialization.obtener_hash_configuracion(self)

    def obtener_firma_ejecucion(self) -> str:
        return serialization.obtener_firma_ejecucion(self)

    # ============================================================
    # MÉTODOS ESTÁTICOS
    # ============================================================
    @staticmethod
    def crear_agente_por_tipo(
        nombre: str,
        tipo: Union[str, TipoAgente],
        **kwargs
    ) -> "Agente":
        if isinstance(tipo, str):
            tipo = TipoAgente.from_string(tipo)

        defaults = {
            TipoAgente.PYTHON: {'timeout_python': 30},
            TipoAgente.SHELL: {'timeout_shell': 30},
            TipoAgente.HTTP: {'metodo_http': 'GET', 'timeout_http': 30},
            TipoAgente.LLM: {
                'modelo_llm': 'deepseek-v4-flash',
                'temperatura_llm': 0.7,
                'max_tokens_llm': 4000,
                'reasoning_effort_llm': 'low',
                'thinking_enabled_llm': False,
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
    def validar_configuracion_completa(
        agentes: List["Agente"]
    ) -> Tuple[bool, List[str]]:
        errores = []
        nombres = {a.nombre for a in agentes}
        ids = {a.id for a in agentes}
        agentes_dict = {a.id: a for a in agentes}

        nombres_vistos = set()
        for agente in agentes:
            if agente.nombre in nombres_vistos:
                errores.append(f"Nombre duplicado: '{agente.nombre}'")
            nombres_vistos.add(agente.nombre)

        for agente in agentes:
            valido, error = agente.validar_configuracion(agentes_dict)
            if not valido:
                errores.append(f"{agente.nombre}: {error}")
            for dep_id in agente.dependencias_ids:
                if dep_id not in ids:
                    errores.append(
                        f"{agente.nombre}: dependencia ID '{dep_id}' no existe"
                    )
            for dep_nombre in agente.dependencias_nombres:
                if dep_nombre not in nombres:
                    errores.append(
                        f"{agente.nombre}: dependencia nombre '{dep_nombre}' no existe"
                    )
        return len(errores) == 0, errores


__all__ = ["Agente"]