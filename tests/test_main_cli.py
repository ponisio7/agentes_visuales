"""Pruebas de la CLI headless de ``main.py``.

Cubren los errores corregidos en esta rama y el contrato de ``run``:

- El ``--timeout`` global que el subcomando ``run`` pisaba con su default
  (``agentes_visuales --timeout 30 run`` acababa con ``timeout=None``).
- ``list-agents``, que importaba ``core.agents_registry`` (inexistente) y
  caía a un glob de ``core/agents/`` (también inexistente), devolviendo
  ``{"ok": true, "agentes": []}`` con exit 0: un fallo silencioso.

Ninguna prueba toca la red, la BD ni la GUI: el pipeline real se inyecta
con ``monkeypatch``.
"""
import io
import json
import sys
from argparse import Namespace

import pytest

import main

# ---------------------------------------------------------------------------
# Parser: --timeout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv, esperado",
    [
        (["--timeout", "30", "run", "--prompt", "x"], 30.0),
        (["run", "--timeout", "10", "--prompt", "x"], 10.0),
        (["run", "--prompt", "x"], None),
        (["--check-env"], None),
        (["--timeout", "15", "--check-env"], 15.0),
    ],
)
def test_timeout_global_no_lo_pisa_el_subcomando(argv, esperado):
    args = main._construir_parser().parse_args(argv)
    assert args.timeout == esperado


def test_timeout_debe_ser_positivo():
    with pytest.raises(SystemExit):
        main._construir_parser().parse_args(["--timeout", "0"])


def test_max_pasos_debe_ser_positivo():
    with pytest.raises(SystemExit):
        main._construir_parser().parse_args(["run", "--prompt", "x", "--max-pasos", "0"])


def test_max_pasos_ausente_si_no_se_indica():
    args = main._construir_parser().parse_args(["run", "--prompt", "x"])
    assert getattr(args, "max_pasos", None) is None
    con_flag = main._construir_parser().parse_args(["run", "--prompt", "x", "--max-pasos", "5"])
    assert con_flag.max_pasos == 5


def test_serve_valores_por_defecto():
    args = main._construir_parser().parse_args(["serve"])
    assert args.host == "127.0.0.1"
    assert args.port == 8765


# ---------------------------------------------------------------------------
# list-agents
# ---------------------------------------------------------------------------


def test_listar_agentes_usa_el_catalogo_real():
    agentes = main._listar_agentes()
    nombres = {a["nombre"] for a in agentes}
    assert {"Python", "Shell", "LLM", "HTTP", "File", "Loop"} <= nombres
    for agente in agentes:
        assert agente["categoria"]
        assert isinstance(agente["campos_requeridos"], list)


def test_list_agents_json_no_es_vacio(capsys):
    codigo = main._ejecutar_list_agents(Namespace(json=True))
    assert codigo == main.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert len(payload["agentes"]) >= 8


def test_list_agents_texto(capsys):
    codigo = main._ejecutar_list_agents(Namespace(json=False))
    assert codigo == main.EXIT_OK
    salida = capsys.readouterr().out
    assert "Python" in salida
    assert "Programación" in salida


# ---------------------------------------------------------------------------
# Carga de tarea
# ---------------------------------------------------------------------------


def _args_tarea(**kw):
    base = {"stdin": False, "file": None, "prompt": None}
    base.update(kw)
    return Namespace(**base)


def test_cargar_tarea_prompt():
    assert main._cargar_tarea(_args_tarea(prompt="hola")) == {"prompt": "hola"}


def test_cargar_tarea_fichero(tmp_path):
    fichero = tmp_path / "tarea.json"
    fichero.write_text('{"prompt": "desde fichero"}', encoding="utf-8")
    assert main._cargar_tarea(_args_tarea(file=fichero))["prompt"] == "desde fichero"


def test_cargar_tarea_stdin(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"prompt": "desde stdin"}'))
    assert main._cargar_tarea(_args_tarea(stdin=True))["prompt"] == "desde stdin"


def test_cargar_tarea_stdin_tiene_prioridad(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"prompt": "stdin"}'))
    tarea = main._cargar_tarea(_args_tarea(stdin=True, prompt="prompt"))
    assert tarea["prompt"] == "stdin"


@pytest.mark.parametrize("crudo", ["no soy json", "[1, 2]", "   "])
def test_cargar_tarea_stdin_invalido(monkeypatch, crudo):
    monkeypatch.setattr(sys, "stdin", io.StringIO(crudo))
    with pytest.raises(main._TareaInvalida):
        main._cargar_tarea(_args_tarea(stdin=True))


def test_cargar_tarea_fichero_inexistente(tmp_path):
    with pytest.raises(main._TareaInvalida):
        main._cargar_tarea(_args_tarea(file=tmp_path / "no_existe.json"))


def test_cargar_tarea_sin_fuente():
    with pytest.raises(main._TareaInvalida):
        main._cargar_tarea(_args_tarea())


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _args_run(**kw):
    base = {
        "prompt": "hola", "file": None, "stdin": False, "agent": None,
        "output": None, "json": False, "quiet": True,
        "no_aprender": True, "timeout": None,
    }
    base.update(kw)
    return Namespace(**base)


def _pipeline_falso(resultado=None, capturado=None):
    salida = resultado or {"ok": True, "resultado": "hecho", "agentes": [], "advertencias": []}

    def _falso(problema, **kw):
        if capturado is not None:
            capturado["problema"] = problema
            capturado.update(kw)
        return salida

    return _falso


def test_run_sin_prompt_devuelve_bad_args():
    assert main._ejecutar_run(_args_run(prompt=None)) == main.EXIT_BAD_ARGS


def test_run_json_inyectando_pipeline(monkeypatch, capsys):
    capturado: dict = {}
    monkeypatch.setattr(main, "_ejecutar_pipeline", _pipeline_falso(capturado=capturado))

    codigo = main._ejecutar_run(
        _args_run(json=True, max_pasos=4, agent="Paso1", no_aprender=True)
    )

    assert codigo == main.EXIT_OK
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert capturado["problema"] == "hola"
    assert capturado["max_pasos"] == 4
    assert capturado["agente"] == "Paso1"
    assert capturado["aprender"] is False


def test_run_aprende_por_defecto_salvo_no_aprender(monkeypatch):
    capturado: dict = {}
    monkeypatch.setattr(main, "_ejecutar_pipeline", _pipeline_falso(capturado=capturado))

    main._ejecutar_run(_args_run(no_aprender=False))
    assert capturado["aprender"] is True

    capturado.clear()
    main._ejecutar_run(_args_run(no_aprender=True))
    assert capturado["aprender"] is False


def test_run_usa_max_pasos_de_la_tarea_si_la_cli_no_lo_indica(monkeypatch, tmp_path):
    capturado: dict = {}
    monkeypatch.setattr(main, "_ejecutar_pipeline", _pipeline_falso(capturado=capturado))

    fichero = tmp_path / "tarea.json"
    fichero.write_text('{"prompt": "x", "max_pasos": 9}', encoding="utf-8")
    main._ejecutar_run(_args_run(prompt=None, file=fichero))
    assert capturado["max_pasos"] == 9


def test_run_max_pasos_de_la_cli_gana_a_la_tarea(monkeypatch, tmp_path):
    capturado: dict = {}
    monkeypatch.setattr(main, "_ejecutar_pipeline", _pipeline_falso(capturado=capturado))

    fichero = tmp_path / "tarea.json"
    fichero.write_text('{"prompt": "x", "max_pasos": 9}', encoding="utf-8")
    main._ejecutar_run(_args_run(prompt=None, file=fichero, max_pasos=3))
    assert capturado["max_pasos"] == 3


def test_run_max_pasos_por_defecto(monkeypatch):
    capturado: dict = {}
    monkeypatch.setattr(main, "_ejecutar_pipeline", _pipeline_falso(capturado=capturado))
    main._ejecutar_run(_args_run())
    assert capturado["max_pasos"] == main.MAX_PASOS_DEFAULT


def test_run_fallo_devuelve_exit_run_error(monkeypatch, capsys):
    def _explota(problema, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(main, "_ejecutar_pipeline", _explota)
    codigo = main._ejecutar_run(_args_run(json=True))
    assert codigo == main.EXIT_RUN_ERROR
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "boom" in payload["error"]


def test_run_no_ok_devuelve_exit_run_error(monkeypatch, capsys):
    salida = {"ok": False, "resultado": "", "agentes": [{"ok": False, "estado": "Error"}],
              "advertencias": []}
    monkeypatch.setattr(main, "_ejecutar_pipeline", _pipeline_falso(resultado=salida))
    assert main._ejecutar_run(_args_run(json=True)) == main.EXIT_RUN_ERROR


def test_run_texto_imprime_solo_el_resultado(monkeypatch, capsys):
    monkeypatch.setattr(main, "_ejecutar_pipeline", _pipeline_falso())
    codigo = main._ejecutar_run(_args_run(json=False))
    assert codigo == main.EXIT_OK
    capturado = capsys.readouterr()
    assert capturado.out.strip() == "hecho"
    assert capturado.err == ""


def test_run_texto_reporta_agentes_fallidos_por_stderr(monkeypatch, capsys):
    salida = {
        "ok": False,
        "resultado": "",
        "advertencias": ["algo raro"],
        "agentes": [{"nombre": "Paso1", "tipo": "Python", "ok": False,
                     "estado": "Error", "error": "fallo de red"}],
    }
    monkeypatch.setattr(main, "_ejecutar_pipeline", _pipeline_falso(resultado=salida))
    codigo = main._ejecutar_run(_args_run(json=False))
    assert codigo == main.EXIT_RUN_ERROR
    capturado = capsys.readouterr()
    assert "algo raro" in capturado.err
    assert "fallo de red" in capturado.err


def test_run_output_escribe_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(main, "_ejecutar_pipeline", _pipeline_falso())
    destino = tmp_path / "out.json"
    codigo = main._ejecutar_run(_args_run(json=True, output=destino))
    assert codigo == main.EXIT_OK
    assert json.loads(destino.read_text(encoding="utf-8"))["resultado"] == "hecho"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "resultado, esperado",
    [
        ("hola", "hola"),
        ({"resultado": "hola"}, "hola"),
        (None, "null"),
    ],
)
def test_extraer_texto(resultado, esperado):
    assert main._extraer_texto(resultado) == esperado


def test_extraer_texto_dict_multiclave_es_json():
    texto = main._extraer_texto({"a": 1, "b": 2})
    assert json.loads(texto) == {"a": 1, "b": 2}


@pytest.mark.parametrize(
    "valor, esperado",
    [(5, 5), ("7", 7), (0, 6), (-1, 6), ("x", 6), (None, 6)],
)
def test_entero_o_defecto(valor, esperado):
    assert main._entero_o_defecto(valor, 6) == esperado


def test_json_limpio_serializa_objetos_raros():
    class Raro:
        def __str__(self):
            return "raro"

    limpio = main._json_limpio({"obj": Raro()})
    assert limpio == {"obj": "raro"}
