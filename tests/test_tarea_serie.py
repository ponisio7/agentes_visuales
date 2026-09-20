# tests/test_tarea_serie.py
"""Cliente de tareas en serie (H10).

La validación de cada tarea depende del VerificationEngine: ``ok`` exige que
la aceptación de la salida haya pasado. No se lanza ningún subproceso real:
``ejecutar_tarea`` se sustituye por un doble controlado.
"""
from pathlib import Path

import tools.enviar_tarea_subproceso as cli


def _salida_ok(artefacto: str = "documento.docx") -> dict:
    return {
        "ok": True,
        "estado": "completada",
        "duracion": 1.2,
        "ejecucion_id": 7,
        "titulo": "Documento",
        "resultado": "contenido del documento",
        "agentes": [{"nombre": "Escribir", "ok": True, "estado": "Completado"}],
        "aceptacion": {
            "aceptada": True,
            "verificada": True,
            "motivos": [],
            "pasos": [{
                "nombre": "Escribir",
                "aceptado": True,
                "criterios_comprobados": [f"archivo:{artefacto}", "resultado_no_vacio"],
                "criterios_fallidos": [],
            }],
        },
    }


def _salida_fallida() -> dict:
    return {
        "ok": False,
        "estado": "fallida",
        "duracion": 2.0,
        "ejecucion_id": 8,
        "titulo": "Documento",
        "resultado": "",
        "agentes": [{"nombre": "Escribir", "ok": False, "estado": "Error",
                     "error": "Aceptación fallida: 0 imágenes raster"}],
        "aceptacion": {
            "aceptada": False,
            "verificada": True,
            "motivos": ["'Escribir': 0 imágenes raster (mínimo 1)"],
            "pasos": [{
                "nombre": "Escribir",
                "aceptado": False,
                "criterios_comprobados": ["archivo:documento.docx"],
                "criterios_fallidos": ["imagenes_documento:documento.docx"],
            }],
        },
    }


def test_resumen_verificacion_extrae_artefactos_y_motivos():
    resumen = cli.resumen_verificacion(_salida_fallida())

    assert resumen["verificada"] is True
    assert resumen["aceptada"] is False
    assert resumen["artefactos"] == ["documento.docx"]
    assert resumen["criterios_fallidos"] == ["imagenes_documento:documento.docx"]
    assert resumen["motivos"]


def test_resumen_verificacion_sin_contrato():
    resumen = cli.resumen_verificacion({"aceptacion": {"verificada": False}})
    assert resumen["verificada"] is False
    assert resumen["artefactos"] == []


def test_resumen_tarea_incluye_estado_errores_y_ejecucion():
    informe = cli.resumen_tarea({
        "ok": False, "returncode": 4, "salida": _salida_fallida(), "aviso": "",
    })
    assert informe["ok"] is False
    assert informe["estado"] == "fallida"
    assert informe["ejecucion_id"] == 8
    assert informe["errores"][0]["nombre"] == "Escribir"
    assert informe["verificacion"]["aceptada"] is False


def test_procesar_lista_se_detiene_en_el_primer_fallo(monkeypatch, capsys):
    llamadas = []

    def _fake(prompt, **kwargs):
        llamadas.append(prompt)
        salida = _salida_fallida() if prompt == "falla" else _salida_ok()
        return {"ok": salida["ok"], "returncode": 4, "salida": salida, "aviso": ""}

    monkeypatch.setattr(cli, "ejecutar_tarea", _fake)
    codigo = cli.procesar_lista(
        ["ok1", "falla", "ok2"],
        cwd=Path("."), aprender=False, max_pasos=3, timeout=10,
        ver_logs=False,
    )

    assert codigo == 1
    assert llamadas == ["ok1", "falla"]
    assert "se detiene la lista" in capsys.readouterr().out


def test_procesar_lista_continue_on_error_sigue(monkeypatch, capsys):
    llamadas = []

    def _fake(prompt, **kwargs):
        llamadas.append(prompt)
        salida = _salida_fallida() if prompt == "falla" else _salida_ok()
        return {"ok": salida["ok"], "returncode": 4, "salida": salida, "aviso": ""}

    monkeypatch.setattr(cli, "ejecutar_tarea", _fake)
    codigo = cli.procesar_lista(
        ["ok1", "falla", "ok2"],
        cwd=Path("."), aprender=False, max_pasos=3, timeout=10,
        ver_logs=False, continue_on_error=True,
    )

    assert codigo == 1
    assert llamadas == ["ok1", "falla", "ok2"]
    assert "continue-on-error" in capsys.readouterr().out


def test_procesar_lista_todo_ok(monkeypatch):
    monkeypatch.setattr(
        cli, "ejecutar_tarea",
        lambda prompt, **kwargs: {"ok": True, "returncode": 0,
                                  "salida": _salida_ok(), "aviso": ""},
    )
    assert cli.procesar_lista(
        ["a", "b"], cwd=Path("."), aprender=False, max_pasos=3, timeout=10,
        ver_logs=False,
    ) == 0
