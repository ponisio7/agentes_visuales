# tests/test_file_executor_escritura.py
"""Regresión: escritura de dicts/listas en extensiones de texto.

Origen del bug (regresión de 903f94a): para las extensiones de
``EXTENSIONES_TEXTO_PLANO`` el executor intentaba desenvolver el dict
buscando una clave de contenido conocida; si no la encontraba, fallaba el
agente con ``no_known_text_keys``. Eso rompía la cadena natural
HTTP → Python (dict) → File ``.txt`` y se perdía el resultado, dejando a
los agentes dependientes bloqueados (lo detectó
``test_integration.py::test_flujo_completo_con_http_mock``).

Comportamiento fijado aquí:
  * ``.txt`` / ``.log`` con dict sin claves reconocidas → se escribe el
    JSON (formato de texto libre, cualquier contenido es válido).
  * ``.html`` con dict sin claves reconocidas → sigue fallando con
    ``no_known_text_keys`` (un JSON no es HTML válido).
  * dict con clave reconocida (p. ej. ``contenido``) → se desenvuelve el
    texto, sin JSON.

Nota: el FileExecutor rechaza rutas absolutas por seguridad, así que los
tests trabajan con rutas relativas dentro de un cwd temporal.
"""
import json
from pathlib import Path

import pytest

from core.agent import Agente, TipoAgente
from core.executors.file_executor import FileExecutor


@pytest.fixture
def cwd_temporal(tmp_path, monkeypatch):
    """cwd aislado: el FileExecutor sólo acepta rutas relativas."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _agente(destino: str) -> Agente:
    return Agente(
        nombre="Escritor",
        tipo=TipoAgente.FILE,
        operacion_file="escribir",
        archivo_destino=destino,
    )


DICT_SIN_CLAVES_CONOCIDAS = {
    "url": "https://example.com",
    "procesado": True,
    "timestamp": "2026-09-20T10:00:00",
    "status": "ok",
}


def test_dict_sin_claves_en_txt_se_serializa_a_json(cwd_temporal):
    exito, mensaje, resultado = FileExecutor.ejecutar(
        _agente("reporte.txt"),
        contexto={"Procesador": DICT_SIN_CLAVES_CONOCIDAS},
    )

    assert exito is True, mensaje
    contenido = Path("reporte.txt").read_text(encoding="utf-8")
    assert json.loads(contenido) == DICT_SIN_CLAVES_CONOCIDAS
    assert resultado["archivo"] == "reporte.txt"


def test_dict_sin_claves_en_log_se_serializa_a_json(cwd_temporal):
    exito, mensaje, _ = FileExecutor.ejecutar(
        _agente("traza.log"),
        contexto={"Procesador": DICT_SIN_CLAVES_CONOCIDAS},
    )

    assert exito is True, mensaje
    assert json.loads(Path("traza.log").read_text(encoding="utf-8")) == DICT_SIN_CLAVES_CONOCIDAS


def test_dict_sin_claves_en_html_sigue_fallando(cwd_temporal):
    exito, mensaje, resultado = FileExecutor.ejecutar(
        _agente("pagina.html"),
        contexto={"Procesador": DICT_SIN_CLAVES_CONOCIDAS},
    )

    assert exito is False
    assert resultado["error"] == "no_known_text_keys"
    assert not Path("pagina.html").exists()


def test_dict_con_clave_conocida_se_desenvuelve_sin_json(cwd_temporal):
    texto = "Línea uno\nLínea dos"
    exito, mensaje, _ = FileExecutor.ejecutar(
        _agente("salida.txt"),
        contexto={"contenido": {"contenido": texto}},
    )

    assert exito is True, mensaje
    assert Path("salida.txt").read_text(encoding="utf-8") == texto
