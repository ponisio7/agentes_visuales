# core/plan_repairs.py
"""
Parches de planes: correcciones deterministas aplicadas a planes generados
por el LLM cuando éste no cumple un requisito explícito del problema.

⚠️ DEUDA TÉCNICA RECONOCIDA:
Este módulo existe porque el LLM no siempre incluye pasos que el problema
pide explícitamente (ej: "con imágenes", "con gráficos", "con tablas").
La estrategia de resolución tiene dos niveles:

  1. REGENERACIÓN: el ProblemSolver reintenta pedir el plan al LLM con
     una instrucción forzada. Esto es lo preferido porque el LLM sigue
     siendo el autor del plan.

  2. PARCHE (este módulo): si tras la regeneración el plan sigue sin
     cumplir el requisito, se aplica un parche determinista que inyecta
     el paso que falta. Cada parche queda registrado en el LearningEngine
     para reforzar la lección correspondiente.

Cuando el LearningEngine acumule suficientes lecciones y el LLM aprenda
a incluir estos pasos por sí mismo, este módulo puede desaparecer.
"""

from __future__ import annotations

import logging
import re
from typing import List, Optional

logger = logging.getLogger(__name__)


# ============================================================
# CONSTANTES — todo configurable aquí, nada inline
# ============================================================

# Palabras clave que activan la detección de "el problema pide imágenes"
IMAGE_PROBLEM_KEYWORDS = (
    "imagen", "imág", "ilustracion", "ilustración",
    "dibujo", "foto", "con dibujos",
)

# Número por defecto de imágenes si el problema no especifica uno
DEFAULT_IMAGE_COUNT = 2

# Proveedor de imágenes por defecto (público, sin auth)
DEFAULT_IMAGE_PROVIDER_URL = "https://picsum.photos/800/600"

# Nombre del paso que se inyecta cuando falta el generador
GENERADOR_URLS_NOMBRE = "GenerarURLsImagenes"

# Nombres que indican que ya hay un generador de imágenes en el plan
GENERADOR_URLS_KEYWORDS_EN_NOMBRE = ("imagen", "url")

# Extensiones de documento que soportan imágenes embebidas
EXTENSIONES_DOC_CON_IMAGENES = (".docx",)


# ============================================================
# API PÚBLICA
# ============================================================

def detectar_requisitos_no_cumplidos(problema: str, plan) -> List[str]:
    """
    Analiza el problema y el plan y devuelve la lista de requisitos
    explícitos del problema que el plan NO cumple.

    Devuelve nombres simbólicos (ej: "imagenes") que el ProblemSolver
    usa para decidir si regenerar o parchear.
    """
    faltantes = []

    if _problema_pide_imagenes(problema) and not _plan_tiene_generador_imagenes(plan):
        faltantes.append("imagenes")

    # Aquí se añadirían otros requisitos detectables:
    # - "graficos" si el problema pide gráficos y no hay paso de matplotlib
    # - "tablas" si pide tablas y no hay paso que las genere
    # - "audio" / "video" / etc.

    return faltantes


def aplicar_parche(problema: str, plan, requisito: str) -> Optional[str]:
    """
    Aplica un parche determinista al plan para cubrir un requisito faltante.

    Devuelve el nombre del parche aplicado (ej: "GenerarURLsImagenes"),
    o None si no se pudo aplicar.
    """
    if requisito == "imagenes":
        return _parche_imagenes(problema, plan)
    # Aquí se añadirían otros parches según el requisito
    return None


def construir_instruccion_regeneracion(requisitos: List[str]) -> str:
    """
    Construye la instrucción extra que se añade al user_prompt en la
    segunda llamada al LLM, forzando los requisitos que faltaron.
    """
    if not requisitos:
        return ""

    instrucciones = []
    for req in requisitos:
        if req == "imagenes":
            instrucciones.append(
                "**REQUISITO OBLIGATORIO FALTANTE**: El plan anterior NO "
                "incluía un paso para generar las URLs de las imágenes que "
                "el problema pide explícitamente. Debes incluir un paso "
                "Python llamado 'GenerarURLs' que devuelva "
                "`resultado = {'urls': ['https://...', ...]}` con N URLs "
                "de imágenes reales (usa picsum.photos). Y el paso final "
                "debe ser un File .docx que reciba un dict con 'imagenes'."
            )
        # Aquí se añadirían instrucciones para otros requisitos
    return "\n\n".join(instrucciones)


# ============================================================
# DETECCIÓN
# ============================================================

def _problema_pide_imagenes(problema: str) -> bool:
    problema_lower = problema.lower()
    return any(k in problema_lower for k in IMAGE_PROBLEM_KEYWORDS)


def _plan_tiene_generador_imagenes(plan) -> bool:
    """¿El plan ya tiene un paso que produce URLs de imágenes?"""
    for p in plan.pasos:
        nombre_lower = p.nombre.lower()
        if any(k in nombre_lower for k in GENERADOR_URLS_KEYWORDS_EN_NOMBRE):
            return True
    return False


def _plan_tiene_doc_con_imagenes(plan):
    """Devuelve el paso File de .docx (donde se pueden insertar imágenes) o None."""
    for p in plan.pasos:
        if p.tipo_agente != "File":
            continue
        op = p.configuracion.get("operacion")
        if op not in ("escribir", "escribir_docx"):
            continue
        destino = p.configuracion.get("archivo_destino", "")
        if any(destino.endswith(ext) for ext in EXTENSIONES_DOC_CON_IMAGENES):
            return p
    return None


def _extraer_numero_imagenes(problema: str) -> int:
    """Extrae 'N' de 'N imágenes' del problema. Default: DEFAULT_IMAGE_COUNT."""
    match = re.search(
        r'(\d+)\s*(imagen|imág|ilustracion|ilustración|dibujo|foto)',
        problema.lower(),
    )
    return int(match.group(1)) if match else DEFAULT_IMAGE_COUNT


# ============================================================
# PARCHE: IMÁGENES
# ============================================================

def _parche_imagenes(problema: str, plan) -> Optional[str]:
    """
    Inyecta un paso GenerarURLsImagenes y adapta el paso de preparación
    para que combine el contenido con las URLs.

    Devuelve el nombre del parche aplicado, o None si no aplica.
    """
    from core.problem_solver import StepPlan  # import local para evitar ciclo

    # 1. Localizar el paso File de .docx
    paso_docx = _plan_tiene_doc_con_imagenes(plan)
    if paso_docx is None:
        logger.warning(
            "PARCHE_IMAGENES: el plan no tiene un paso File .docx; no se aplica."
        )
        return None

    # 2. Número de imágenes
    n_imagenes = _extraer_numero_imagenes(problema)

    # 3. Localizar el paso que produce el contenido (LLM)
    paso_contenido = None
    for p in plan.pasos:
        if p.tipo_agente == "LLM":
            paso_contenido = p
            break
    nombre_contenido = paso_contenido.nombre if paso_contenido else None

    # 4. Localizar el paso "PrepararDocumento" (Python justo antes del docx)
    idx_docx = plan.pasos.index(paso_docx)
    paso_prep = None
    for p in reversed(plan.pasos[:idx_docx]):
        if p.tipo_agente == "Python" and "documento" in p.nombre.lower():
            paso_prep = p
            break

    # 5. Crear el paso GenerarURLsImagenes
    paso_urls = StepPlan(
        nombre=GENERADOR_URLS_NOMBRE,
        descripcion=f"Genera {n_imagenes} URLs de imágenes reales (picsum.photos)",
        tipo_agente="Python",
        dependencia_ids=[],
        configuracion={
            "codigo": _codigo_generador_urls(n_imagenes),
            "timeout": 10,
        },
        justificacion="Provee URLs de imágenes reales para el documento",
    )

    plan.pasos.insert(idx_docx, paso_urls)
    idx_docx = plan.pasos.index(paso_docx)  # recalcular tras insertar

    # 6. Reescribir (o crear) el paso de preparación
    deps_prep = []
    if nombre_contenido:
        deps_prep.append(nombre_contenido)
    deps_prep.append(GENERADOR_URLS_NOMBRE)

    codigo_prep = _codigo_preparador_documento(nombre_contenido)

    if paso_prep is not None:
        paso_prep.dependencia_ids = deps_prep
        paso_prep.configuracion = {"codigo": codigo_prep, "timeout": 30}
        paso_prep.descripcion = "Combina contenido + URLs de imágenes"
        nombre_prep = paso_prep.nombre
    else:
        paso_prep = StepPlan(
            nombre="PrepararDocumentoConImagenes",
            descripcion="Combina contenido + URLs de imágenes",
            tipo_agente="Python",
            dependencia_ids=deps_prep,
            configuracion={"codigo": codigo_prep, "timeout": 30},
            justificacion="Prepara el dict con imagenes para el documento",
        )
        plan.pasos.insert(idx_docx, paso_prep)
        nombre_prep = paso_prep.nombre

    # 7. Asegurar que el docx depende del preparador
    if nombre_prep not in paso_docx.dependencia_ids:
        paso_docx.dependencia_ids = [nombre_prep]

    return GENERADOR_URLS_NOMBRE


def _codigo_generador_urls(n_imagenes: int) -> str:
    """
    Código Python que genera N URLs de imágenes.
    Todo configurable desde constantes del módulo.
    """
    return (
        f"resultado = {{'urls': ["
        f"f'{DEFAULT_IMAGE_PROVIDER_URL}?random={{i}}' "
        f"for i in range({n_imagenes})"
        f"]}}"
    )


def _codigo_preparador_documento(nombre_contenido: Optional[str]) -> str:
    """
    Código Python que combina el contenido (si existe) con las URLs
    de imágenes, produciendo el dict que el FileExecutor de .docx espera.
    """
    lineas = []

    if nombre_contenido:
        lineas.extend([
            f"contenido_data = contexto.get('{nombre_contenido}', {{}})",
            "contenido_json = contenido_data.get('json') or contenido_data",
            "cuento = contenido_json.get('cuento', '') or contenido_json.get('texto', '')",
            "descripciones = contenido_json.get('descripciones_imagenes', [])",
        ])
    else:
        lineas.extend([
            "cuento = ''",
            "descripciones = []",
        ])

    lineas.extend([
        f"urls_data = contexto.get('{GENERADOR_URLS_NOMBRE}', {{}})",
        "urls = urls_data.get('urls') or []",
        "if not urls and isinstance(urls_data.get('resultado'), dict):",
        "    urls = urls_data['resultado'].get('urls', [])",
        "imagenes = []",
        "for i, url in enumerate(urls):",
        "    desc = descripciones[i] if i < len(descripciones) else ''",
        "    imagenes.append({'url': url, 'descripcion': desc})",
        "resultado = {",
        "    'titulo': 'Documento',",
        "    'cuento': cuento,",
        "    'imagenes': imagenes,",
        "}",
    ])

    return "\n".join(lineas)