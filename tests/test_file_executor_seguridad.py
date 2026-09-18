# tests/test_file_executor_seguridad.py
"""Pruebas de seguridad del ``FileExecutor`` al eliminar.

Además de ``validar_ruta_archivo``, la operación ``eliminar`` comprueba que
la ruta real resuelta esté dentro del directorio de trabajo. Esto cubre
enlaces simbólicos que apuntan fuera del proyecto y evita un borrado
destructivo (p. ej. ``shutil.rmtree(".")``).
"""

import os

import pytest

from core.agent import Agente, TipoAgente
from core.executors.file_executor import FileExecutor


def _agente_eliminar(ruta: str) -> Agente:
    return Agente(
        nombre="Borrar",
        tipo=TipoAgente.FILE,
        operacion_file="eliminar",
        archivo_origen=ruta,
    )


@pytest.fixture(autouse=True)
def _en_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


class TestRutasRechazadas:
    @pytest.mark.parametrize("ruta", [".", "..", "../archivo.txt", "~/archivo"])
    def test_rechaza_rutas_peligrosas(self, ruta):
        exito, mensaje, _ = FileExecutor.ejecutar(_agente_eliminar(ruta), {})
        assert exito is False
        assert "inválida" in mensaje.lower() or "no se permite" in mensaje.lower()

    def test_rechaza_symlink_que_apunta_fuera(self, tmp_path):
        fuera = tmp_path.parent / (tmp_path.name + "_fuera")
        fuera.mkdir()
        (fuera / "importante.txt").write_text("no borrar", encoding="utf-8")
        enlace = tmp_path / "enlace"
        os.symlink(fuera, enlace)

        exito, mensaje, _ = FileExecutor.ejecutar(_agente_eliminar("enlace"), {})

        assert exito is False
        assert "fuera del directorio de trabajo" in mensaje
        assert (fuera / "importante.txt").exists()


class TestRutasPermitidas:
    def test_elimina_archivo_dentro_del_cwd(self, tmp_path):
        archivo = tmp_path / "a.txt"
        archivo.write_text("contenido", encoding="utf-8")

        exito, mensaje, resultado = FileExecutor.ejecutar(_agente_eliminar("a.txt"), {})

        assert exito is True
        assert resultado.get("eliminado") is True
        assert not archivo.exists()

    def test_elimina_directorio_dentro_del_cwd(self, tmp_path):
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        (subdir / "x.txt").write_text("x", encoding="utf-8")

        exito, _, _ = FileExecutor.ejecutar(_agente_eliminar("subdir"), {})

        assert exito is True
        assert not subdir.exists()
