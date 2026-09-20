# tests/test_version.py
"""La versión del proyecto debe tener una única fuente de verdad.

Regresión (B5 de la auditoría v3.0.1): ``main.py`` declaraba
``__version__ = "1.1.0"``, el tag era v3.0.1 y el CHANGELOG documentaba
v3.0. Ahora ``[project].version`` de pyproject.toml es la única fuente y
main.py la lee con tomllib.
"""
import tomllib
from pathlib import Path

import main


def test_version_coincide_con_pyproject():
    raiz = Path(main.__file__).resolve().parent
    with open(raiz / "pyproject.toml", "rb") as f:
        version_pyproject = tomllib.load(f)["project"]["version"]

    assert main.__version__ == version_pyproject


def test_version_no_es_la_historica_1_1_0():
    # Valor que quedó desincronizado durante varias releases.
    assert main.__version__ != "1.1.0"


def test_changelog_documenta_la_version_actual():
    raiz = Path(main.__file__).resolve().parent
    changelog = (raiz / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"v{main.__version__}" in changelog
