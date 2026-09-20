# core/problem_solver/models.py
"""
Modelos de datos de ProblemSolver: StepPlan, ExecutionPlan.
Extraído literalmente de core/problem_solver.py (monolito) — Paso 2.
"""
import uuid
from dataclasses import dataclass, field
from typing import Any

from core.agent import Agente

from .constants import PlanStatus


def _lista_str(valor: Any) -> list[str]:
    """Normaliza un valor del LLM a una lista de strings no vacíos."""
    if valor is None:
        return []
    if isinstance(valor, str):
        return [valor] if valor.strip() else []
    if isinstance(valor, (list, tuple, set)):
        return [
            str(v) for v in valor
            if v is not None and str(v).strip()
        ]
    return []


def _a_int_seguro(valor: Any, default: int = 0) -> int:
    """Convierte a int de forma segura (el LLM puede mandar null/texto)."""
    try:
        if valor is None:
            return default
        return int(float(valor))
    except (TypeError, ValueError):
        return default


@dataclass
class ContratoAceptacion:
    """Qué debe cumplir la salida de un paso para considerarse aceptada.

    Es un contrato **declarativo y determinista**: el verificador
    (``core/verification.py``) lo comprueba en disco/bytes, nunca en lo que
    el LLM dice haber generado. Todos los campos son opcionales; un contrato
    vacío no verifica nada (y por tanto no se considera «verificado»).
    """
    # Artefactos que deben existir en disco y no estar vacíos.
    archivos: list[str] = field(default_factory=list)
    # Rutas que deben ser imágenes raster reales (no basta la extensión).
    imagenes: list[str] = field(default_factory=list)
    # Formato real exigido a 'imagenes' (p. ej. 'PNG', 'JPEG'); None = cualquiera.
    formato_imagen: str | None = None
    # Tamaño mínimo en bytes de cada archivo de 'archivos'.
    min_bytes: int = 0
    # Longitud mínima de texto del resultado.
    min_caracteres: int = 0
    # El JSON (archivo .json declarado o clave 'json' del resultado) debe parsear.
    json_parseable: bool = False
    # Claves que deben estar presentes en el resultado (o en el JSON del archivo).
    claves_requeridas: list[str] = field(default_factory=list)
    # Un documento ofimático (.docx/.pptx/.xlsx/.odt) debe llevar imágenes.
    requiere_imagen: bool = False
    min_imagenes: int = 0
    # Número mínimo de items (resultado de un Loop / lista).
    min_items: int = 0
    # Número máximo de errores tolerados en el resultado (None = sin límite).
    max_errores: int | None = None
    # Directorio que debe existir y contener al menos 'min_archivos' ficheros.
    directorio: str = ""
    min_archivos: int = 0
    # Nombres de archivo que deben estar dentro de 'directorio'.
    archivos_esperados: list[str] = field(default_factory=list)
    # Validar la estructura interna del contenedor (docx/xlsx/pptx/odt/zip/pdf).
    validar_contenedor: bool = False

    def __post_init__(self):
        self.archivos = _lista_str(self.archivos)
        self.imagenes = _lista_str(self.imagenes)
        self.claves_requeridas = _lista_str(self.claves_requeridas)
        self.archivos_esperados = _lista_str(self.archivos_esperados)
        if self.formato_imagen is not None:
            self.formato_imagen = str(self.formato_imagen).upper().strip() or None
        self.min_bytes = _a_int_seguro(self.min_bytes, 0)
        self.min_caracteres = _a_int_seguro(self.min_caracteres, 0)
        self.min_imagenes = _a_int_seguro(self.min_imagenes, 0)
        self.min_items = _a_int_seguro(self.min_items, 0)
        self.min_archivos = _a_int_seguro(self.min_archivos, 0)
        self.json_parseable = bool(self.json_parseable)
        self.requiere_imagen = bool(self.requiere_imagen)
        self.validar_contenedor = bool(self.validar_contenedor)
        self.directorio = str(self.directorio or "").strip()
        if self.max_errores is not None:
            self.max_errores = _a_int_seguro(self.max_errores, 0)

    def es_vacio(self) -> bool:
        """True si no hay ninguna comprobación real que hacer."""
        return not (
            self.archivos
            or self.imagenes
            or self.min_bytes > 0
            or self.min_caracteres > 0
            or self.json_parseable
            or self.claves_requeridas
            or self.requiere_imagen
            or self.min_imagenes > 0
            or self.min_items > 0
            or self.max_errores is not None
            or self.directorio
            or self.min_archivos > 0
            or self.archivos_esperados
            or self.validar_contenedor
        )

    def to_dict(self) -> dict:
        return {
            'archivos': list(self.archivos),
            'imagenes': list(self.imagenes),
            'formato_imagen': self.formato_imagen,
            'min_bytes': self.min_bytes,
            'min_caracteres': self.min_caracteres,
            'json_parseable': self.json_parseable,
            'claves_requeridas': list(self.claves_requeridas),
            'requiere_imagen': self.requiere_imagen,
            'min_imagenes': self.min_imagenes,
            'min_items': self.min_items,
            'max_errores': self.max_errores,
            'directorio': self.directorio,
            'min_archivos': self.min_archivos,
            'archivos_esperados': list(self.archivos_esperados),
            'validar_contenedor': self.validar_contenedor,
        }

    @classmethod
    def from_dict(cls, data: Any) -> 'ContratoAceptacion | None':
        """Construye el contrato desde el dict del LLM (tolerante a basura).

        Acepta alias frecuentes (``ficheros``, ``salidas``, ``archivo``,
        ``imagen``, ``min_imagen``...) para no perder invariantes por un
        nombre distinto. Devuelve ``None`` si no hay nada que verificar.
        """
        if data is None:
            return None
        if isinstance(data, ContratoAceptacion):
            return data
        if not isinstance(data, dict):
            return None

        archivos = data.get('archivos', data.get('ficheros', data.get('salidas')))
        if archivos is None and data.get('archivo'):
            archivos = [data['archivo']]
        imagenes = data.get('imagenes', data.get('imagenes_requeridas'))
        if imagenes is None and data.get('imagen'):
            imagenes = [data['imagen']]
        min_imagenes = data.get('min_imagenes', data.get('min_imagen'))
        # 'requiere_imagen' implica al menos una imagen.
        requiere = bool(data.get('requiere_imagen', False))
        if min_imagenes is not None and _a_int_seguro(min_imagenes, 0) > 0:
            requiere = True

        contrato = cls(
            archivos=_lista_str(archivos),
            imagenes=_lista_str(imagenes),
            formato_imagen=data.get('formato_imagen', data.get('mime')),
            min_bytes=_a_int_seguro(data.get('min_bytes', data.get('tamano_minimo')), 0),
            min_caracteres=_a_int_seguro(
                data.get('min_caracteres', data.get('caracteres_minimos')), 0
            ),
            json_parseable=bool(data.get('json_parseable', data.get('json_valido', False))),
            claves_requeridas=_lista_str(
                data.get('claves_requeridas', data.get('claves'))
            ),
            requiere_imagen=requiere,
            min_imagenes=_a_int_seguro(min_imagenes, 0),
            min_items=_a_int_seguro(data.get('min_items', data.get('items_minimos')), 0),
            max_errores=data.get('max_errores'),
            directorio=data.get('directorio', data.get('carpeta', '')),
            min_archivos=_a_int_seguro(
                data.get('min_archivos', data.get('archivos_minimos')), 0
            ),
            archivos_esperados=_lista_str(
                data.get('archivos_esperados', data.get('contenido_directorio'))
            ),
            validar_contenedor=bool(
                data.get('validar_contenedor', data.get('contenedor_valido', False))
            ),
        )
        return None if contrato.es_vacio() else contrato


@dataclass
class StepPlan:
    """Un paso individual del plan descompuesto."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    orden: int = 0
    nombre: str = ""
    descripcion: str = ""
    tipo_agente: str = "Python"
    dependencia_ids: list[str] = field(default_factory=list)
    configuracion: dict[str, Any] = field(default_factory=dict)
    justificacion: str = ""
    es_critico: bool = False
    aceptacion: ContratoAceptacion | None = None

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'orden': self.orden,
            'nombre': self.nombre,
            'descripcion': self.descripcion,
            'tipo_agente': self.tipo_agente,
            'dependencia_ids': self.dependencia_ids,
            'configuracion': self.configuracion,
            'justificacion': self.justificacion,
            'es_critico': self.es_critico,
            'aceptacion': self.aceptacion.to_dict() if self.aceptacion else None,
        }


@dataclass
class ExecutionPlan:
    """Plan completo de ejecución generado por el solver."""
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    problema_original: str = ""
    titulo: str = ""
    analisis: str = ""
    pasos: list[StepPlan] = field(default_factory=list)
    status: PlanStatus = PlanStatus.DRAFT
    estimacion_tiempo: float = 0.0
    agentes_generados: list[Agente] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)
    metadatos: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'id': self.id,
            'problema_original': self.problema_original,
            'titulo': self.titulo,
            'analisis': self.analisis,
            'pasos': [p.to_dict() for p in self.pasos],
            'status': self.status.value,
            'estimacion_tiempo': self.estimacion_tiempo,
            'advertencias': self.advertencias,
            'metadatos': self.metadatos,
        }

    def obtener_agentes_por_nombre(self) -> dict[str, Agente]:
        return {a.nombre: a for a in self.agentes_generados}

    def obtener_pasos_por_nombre(self) -> dict[str, StepPlan]:
        return {p.nombre: p for p in self.pasos}
