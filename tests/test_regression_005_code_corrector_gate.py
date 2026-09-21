"""Regresión 005 — el corrector de código no acepta una corrección que no compila.

Bug 1.3 del ROADMAP (punto 3.8):

    ``PythonCodeCorrector`` reescribe código del LLM por spans de texto
    calculados sobre el AST. No había ninguna red de seguridad que garantizase
    que el resultado seguía siendo Python válido: el acta describe el caso
    ``data = contexto if contexto else {}`` convertido en algo como
    ``contexto contexto...``.

El gate añadido compila el resultado (``compile()``, el equivalente en proceso
de ``py_compile``) y, si falla, devuelve el código **original**.
"""
from __future__ import annotations

import logging

import pytest

from core.problem_solver.code_corrector import PythonCodeCorrector

# ---------------------------------------------------------------------------
# 1. El gate revierte una corrección rota
# ---------------------------------------------------------------------------

# ``respuesta`` es un alias resoluble: garantiza que ``corregir`` llegue a
# aplicar reemplazos y, por tanto, que pase por el gate.
CODIGO_ORIGINAL = "resultado = respuesta\n"
CODIGO_CORRUPTO = "contexto contexto if contexto else {}\n"


def test_correccion_que_no_compila_se_revierte(monkeypatch, caplog):
    """El caso histórico del acta: la corrección destruye la sintaxis."""
    monkeypatch.setattr(
        PythonCodeCorrector, "_aplicar_reemplazos",
        staticmethod(lambda codigo, reemplazos: CODIGO_CORRUPTO),
    )
    caplog.set_level(logging.WARNING, logger="core.problem_solver.code_corrector")

    salida = PythonCodeCorrector.corregir(CODIGO_ORIGINAL, ["Dep"])

    assert salida == CODIGO_ORIGINAL, "debe hacer rollback al original"
    assert "Corrección descartada" in caplog.text


def test_correccion_valida_se_aplica(monkeypatch):
    monkeypatch.setattr(
        PythonCodeCorrector, "_aplicar_reemplazos",
        staticmethod(lambda codigo, reemplazos: "resultado = 42\n"),
    )

    salida = PythonCodeCorrector.corregir(CODIGO_ORIGINAL, ["Dep"])

    assert salida == "resultado = 42\n"


def test_el_caso_historico_del_acta_se_revierte(monkeypatch):
    """`data = contexto if contexto else {}` -> `contexto contexto...`."""
    monkeypatch.setattr(
        PythonCodeCorrector, "_aplicar_reemplazos",
        staticmethod(
            lambda codigo, reemplazos:
            "data = contexto contexto if contexto else {}\n"
        ),
    )

    original = "resultado = respuesta\ndata = contexto if contexto else {}\n"
    salida = PythonCodeCorrector.corregir(original, ["Dep"])

    assert salida == original


def test_el_gate_no_revierte_correcciones_correctas():
    """La corrección real de un alias debe seguir aplicándose."""
    salida = PythonCodeCorrector.corregir("resultado = respuesta", ["Dep"])

    assert "dependencia(contexto, 'Dep')" in salida
    compile(salida, "<test>", "exec")  # y sigue compilando


# ---------------------------------------------------------------------------
# 2. Comportamiento previo que NO debe cambiar
# ---------------------------------------------------------------------------

def test_codigo_no_parseable_se_devuelve_intacto():
    roto = "def f(contexto.get('x')):\n"

    assert PythonCodeCorrector.corregir(roto, ["Dep"]) == roto


def test_codigo_vacio_se_devuelve_intacto():
    assert PythonCodeCorrector.corregir("", ["Dep"]) == ""
    assert PythonCodeCorrector.corregir("   ", ["Dep"]) == "   "


def test_sin_reemplazos_no_se_toca_el_codigo():
    codigo = "x = 1\ny = x + 1\n"

    assert PythonCodeCorrector.corregir(codigo, []) == codigo


# ---------------------------------------------------------------------------
# 3. El helper del gate, en aislamiento
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("corregido", [
    "contexto contexto",
    "def f(:",
    "resultado = ",
    "if True\n    pass",
])
def test_validar_o_revertir_revierte_cualquier_sintaxis_rota(corregido):
    original = "x = 1\n"

    assert PythonCodeCorrector._validar_o_revertir(original, corregido) == original


def test_validar_o_revertir_acepta_codigo_valido():
    original = "x = 1\n"
    corregido = "y = 2\n"

    assert PythonCodeCorrector._validar_o_revertir(original, corregido) == corregido
