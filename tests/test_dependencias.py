# tests/test_dependencias.py
"""Dependencias reales y estabilidad de la suite (H3).

Comprueba que ``requirements.txt`` declara las dependencias que el código
importa de verdad (sentence-transformers/torch para el matcher y el
retrieval, pypdf para la verificación de PDF, ddgs para Search) y que el
verificador de dependencias las detecta instaladas.
"""
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def test_requirements_declara_dependencias_reales():
    texto = (RAIZ / "requirements.txt").read_text(encoding="utf-8")
    for paquete in (
        "sentence-transformers",
        "torch",
        "pypdf",
        "ddgs",
        "scikit-learn",
        "playwright",
        "flask",
        "PyQt6",
        "openai",
    ):
        assert paquete.lower() in texto.lower(), f"falta '{paquete}'"


def test_requirements_dev_declara_pytest_forked():
    texto = (RAIZ / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "pytest-forked" in texto


def test_el_codigo_importa_lo_que_declara():
    """sentence_transformers/torch se importan realmente desde el matcher."""
    matcher = (RAIZ / "learning" / "embedding_matcher.py").read_text(encoding="utf-8")
    assert "from sentence_transformers import SentenceTransformer" in matcher
    requirements = (RAIZ / "requirements.txt").read_text(encoding="utf-8")
    assert "torch>=2.0.0" in requirements
    assert "sentence-transformers>=3.0.0" in requirements


def test_verificador_de_dependencias_detecta_instaladas():
    from tools.verificar_dependencias import comprobar

    filas = comprobar()
    por_modulo = {f["modulo"]: f for f in filas}

    # El entorno del proyecto tiene estas instaladas.
    for modulo in ("numpy", "sklearn", "sentence_transformers", "torch", "pypdf"):
        assert por_modulo[modulo]["instalado"] is True, modulo

    assert all({"modulo", "paquete", "obligatorio", "instalado"} <= set(f) for f in filas)


def test_hay_un_runner_de_tests_aislado():
    runner = RAIZ / "tools" / "run_tests.sh"
    assert runner.exists()
    contenido = runner.read_text(encoding="utf-8")
    assert "--forked" in contenido
    assert "pytest-forked" in contenido
