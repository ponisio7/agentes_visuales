"""Regresión 012 — la mitigación del SIGSEGV (aislamiento por proceso) se mantiene.

Bug 1.13 del ROADMAP (punto 3.4):

    ``python -m pytest -q`` (suite completa en un solo proceso) muere con
    ``SIGSEGV`` de forma intermitente. Causa: ``fork()`` en un proceso con
    hilos es inseguro en CPython 3.13; el Scheduler lanza subprocesos del
    sandbox desde su ``ThreadPoolExecutor``.

Decisión (documentada en ``DECISIONES.md``): la mitigación operativa
—``tools/run_tests.sh``, que ejecuta cada test en un proceso hijo con
``pytest-forked``— es la definitiva mientras la base sea CPython 3.13, y **no
se desactiva ninguna prueba**.

Estos tests protegen esa decisión: sin ellos, borrar el ``--forked`` del runner
dejaría la suite expuesta al SIGSEGV sin que nada avisara.
"""
from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
RUNNER = RAIZ / "tools" / "run_tests.sh"
DECISIONES = RAIZ / "DECISIONES.md"
PYTEST_INI = RAIZ / "pytest.ini"


def test_el_runner_aislado_sigue_usando_forked():
    contenido = RUNNER.read_text(encoding="utf-8")

    assert "--forked" in contenido, (
        "tools/run_tests.sh debe seguir aislando por proceso: sin --forked la "
        "suite completa muere con SIGSEGV (ver DECISIONES.md)"
    )
    assert "pytest_forked" in contenido, "el runner comprueba la dependencia"


def test_no_se_desactiva_ninguna_prueba_en_el_runner():
    """La mitigación aísla, no omite: nada de ``--deselect`` ni ``-k``."""
    contenido = RUNNER.read_text(encoding="utf-8")

    assert "--deselect" not in contenido
    assert "-k " not in contenido


def test_pytest_forked_esta_disponible():
    import pytest_forked  # noqa: F401  (la importación es la aserción)


def test_la_decision_esta_documentada():
    texto = DECISIONES.read_text(encoding="utf-8")

    assert "SIGSEGV" in texto
    assert "--forked" in texto
    # Y con el camino de reversión explícito, para que no sea un callejón.
    assert "3.14" in texto


def test_el_aislamiento_vive_en_el_runner_y_no_en_pytest_ini():
    """Intención de diseño: ``pytest fichero.py`` sigue siendo rápido.

    El aislamiento se aplica a la suite completa (donde aparece la carrera), no
    a cada invocación de pytest: forzarlo en ``addopts`` encarecería también
    correr un solo fichero sin necesidad.
    """
    addopts = ""
    for linea in PYTEST_INI.read_text(encoding="utf-8").splitlines():
        if linea.strip().startswith("addopts"):
            addopts = linea

    assert "--forked" not in addopts
