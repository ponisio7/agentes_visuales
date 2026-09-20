# tests/test_sandbox_contract.py
"""Contrato de runtime del sandbox y corrección semántica de alias.

Cubre la causa raíz del fallo "cuento vacío":
  * ``respuesta`` ya no se convierte en ``contexto.get('Dep', {}).get('body', '{}')``
    (forma HTTP que devolvía el literal ``'{}'``).
  * ``dependencia(contexto, 'Dep')`` devuelve el valor útil real.
  * El validador rechaza ANTES de ejecutar nombres libres sin definir, claves
    que el tipo del productor no puede emitir y pasos ``File`` imposibles.

No se hardcodea ningún caso del problema: los datos de prueba son genéricos.
"""
import pytest

from core.problem_solver.code_corrector import PythonCodeCorrector
from core.problem_solver.validator import PlanValidator
from core.sandbox_contract import (
    NOMBRES_INYECTADOS,
    dependencia,
    formato_imagen_real,
    preparar_imagen,
)


# ============================================================
# Corrector: resolución por nombre semántico
# ============================================================

@pytest.mark.parametrize(
    "codigo, deps, esperado",
    [
        ("texto = respuesta", ["GenerarCuento"], "texto = dependencia(contexto, 'GenerarCuento')"),
        ("data = respuesta", ["Resumir"], "data = dependencia(contexto, 'Resumir')"),
        (
            "result = json.loads(respuesta)\nprint(result)",
            ["Resumir"],
            "result = dependencia(contexto, 'Resumir', 'json')\nprint(result)",
        ),
    ],
)
def test_corrector_resuelve_alias_por_dependencia(codigo, deps, esperado):
    assert PythonCodeCorrector.corregir(codigo, deps) == esperado


def test_corrector_no_inventa_si_no_hay_dependencia():
    """Sin dependencia no hay fuente semántica: se deja intacto para que el
    validador lo bloquee (antes emitía un ``contexto`` colado)."""
    assert PythonCodeCorrector.corregir("texto = respuesta", []) == "texto = respuesta"


@pytest.mark.parametrize(
    "codigo, deps",
    [
        ("def f(respuesta):\n    return respuesta", []),
        ("d = {'respuesta': 1}\nprint(d['respuesta'])", []),
        ("respuesta = 'hola'\nprint(respuesta)", ["Dep"]),
        ("resultado = {'respuesta': respuesta_local}\nrespuesta_local = 1", ["Dep"]),
    ],
)
def test_corrector_respeta_nombres_ligados(codigo, deps):
    """Parámetros, claves de dict y variables ya asignadas NO se tocan."""
    assert PythonCodeCorrector.corregir(codigo, deps) == codigo


def test_corrector_es_idempotente():
    una = PythonCodeCorrector.corregir("texto = respuesta", ["Dep"])
    dos = PythonCodeCorrector.corregir(una, ["Dep"])
    assert una == dos


# ============================================================
# dependencia(): valor principal según contrato de salida
# ============================================================

def test_dependencia_prefiere_json_y_desenvuelve_texto():
    """Un resultado LLM con JSON anidado devuelve el texto útil, no el dict."""
    resultado_llm = {
        "modelo": "m",
        "respuesta": "crudo",
        "respuesta_limpia": "crudo",
        "json": {"titulo": "T", "cuento": "x" * 3000},
    }
    valor = dependencia({"GenerarCuento": resultado_llm}, "GenerarCuento")
    assert isinstance(valor, str)
    assert len(valor) == 3000


def test_dependencia_devuelve_string_mas_largo_sin_claves_de_contrato():
    resultado = {"cuento": "y" * 500, "otra": "corta"}
    assert dependencia({"Generar": resultado}, "Generar") == "y" * 500


def test_dependencia_clave_explicita_y_json():
    resultado = {"json": {"total": 3}, "respuesta_limpia": "hola"}
    assert dependencia({"Dep": resultado}, "Dep", "json") == {"total": 3}
    assert dependencia({"Dep": resultado}, "Dep", "texto") == "hola"
    assert dependencia({"Dep": resultado}, "Dep", "total") == 3


def test_dependencia_clave_inexistente_devuelve_none():
    assert dependencia({"Dep": {"respuesta": "hola"}}, "Dep", "body") is None


def test_prelude_expone_los_nombres_documentados():
    for nombre in ("dependencia", "preparar_imagen", "formato_imagen_real"):
        assert nombre in NOMBRES_INYECTADOS


# ============================================================
# Contrato de imagen (bytes reales, no extensión)
# ============================================================

def test_formato_real_ignora_la_extension(tmp_path):
    from PIL import Image

    mal_nombrado = tmp_path / "foto.png"  # contenido JPEG
    Image.new("RGB", (16, 16), "red").save(mal_nombrado, format="JPEG")
    assert formato_imagen_real(str(mal_nombrado)) == "JPEG"


def test_preparar_imagen_rechaza_no_raster(tmp_path):
    svg = tmp_path / "dibujo.png"  # nombre .png, contenido SVG
    svg.write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')

    with pytest.raises(ValueError) as excinfo:
        preparar_imagen(str(svg))
    assert "no contiene una imagen raster" in str(excinfo.value)


def test_preparar_imagen_acepta_raster_mal_nombrado(tmp_path):
    from PIL import Image

    mal_nombrado = tmp_path / "imagen.png"  # contenido JPEG
    Image.new("RGB", (16, 16), "red").save(mal_nombrado, format="JPEG")
    assert preparar_imagen(str(mal_nombrado)) == str(mal_nombrado)


def test_preparar_imagen_convierte_formato_no_nativo(tmp_path):
    from PIL import Image

    webp = tmp_path / "imagen.webp"
    Image.new("RGB", (16, 16), "red").save(webp, format="WEBP")
    convertida = preparar_imagen(str(webp))
    assert convertida != str(webp)
    assert formato_imagen_real(convertida) == "PNG"


# ============================================================
# Validador: reglas que bloquean ANTES de ejecutar
# ============================================================

NOMBRES = {"GenerarCuento", "Procesar"}
TIPOS = {"GenerarCuento": "LLM", "Procesar": "Python"}


def test_validador_bloquea_nombre_libre_sin_dependencia():
    errores = PlanValidator._validar_codigo_python_ast(
        codigo="texto = respuesta",
        nombre="Paso",
        nombres_agentes=set(),
    )
    assert len(errores) == 1
    assert errores[0].startswith("BLOQUEANTE:")
    assert "respuesta" in errores[0]


def test_validador_bloquea_clave_de_otro_tipo():
    """El bug original: leer 'body' (HTTP) de un agente LLM."""
    errores = PlanValidator._validar_codigo_python_ast(
        codigo="texto = contexto.get('GenerarCuento', {}).get('body', '{}')",
        nombre="Paso",
        nombres_agentes=NOMBRES,
        tipos_agentes=TIPOS,
    )
    assert any("body" in e and "BLOQUEANTE" in e for e in errores)


def test_validador_acepta_clave_valida_de_su_tipo():
    errores = PlanValidator._validar_codigo_python_ast(
        codigo="texto = contexto.get('GenerarCuento', {}).get('respuesta_limpia', '')",
        nombre="Paso",
        nombres_agentes=NOMBRES,
        tipos_agentes=TIPOS,
    )
    assert errores == []


def test_validador_acepta_el_helper_dependencia():
    errores = PlanValidator._validar_codigo_python_ast(
        codigo="texto = dependencia(contexto, 'GenerarCuento')",
        nombre="Paso",
        nombres_agentes=NOMBRES,
        tipos_agentes=TIPOS,
    )
    assert errores == []


def test_validador_no_bloquea_clave_desconocida_de_python():
    """De un productor Python no se sabe qué claves devuelve: no se adivina."""
    errores = PlanValidator._validar_codigo_python_ast(
        codigo="dato = contexto.get('Procesar', {}).get('body')",
        nombre="Paso",
        nombres_agentes=NOMBRES,
        tipos_agentes=TIPOS,
    )
    assert errores == []
