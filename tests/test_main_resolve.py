# tests/test_main_resolve.py
"""
Tests del subcomando `resolve` (V4.0 «Resolver tarea»).

Cubren dos capas:

  1. **Handler de CLI** (`_ejecutar_resolve`): parseo, salida JSON/texto, código
     de salida y escritura de `--output`. El resolver real se inyecta.
  2. **Cableado** (`_resolver_objetivo`): que el `GoalResolver` reciba de verdad
     el planificador (`ProblemSolver`) y el ejecutor (`_ejecutar_plan`), y que
     el bucle re-planifique con la evidencia cuando la aceptación falla.

Ninguna prueba toca red, BD, GUI ni LLM.
"""
from __future__ import annotations

import json
from argparse import Namespace

import pytest

import main
from core.goal_resolver import (
    PARADA_INTENTOS,
    PARADA_VERIFICADO,
    IntentoResolucion,
    ResultadoResolucion,
)
from core.problem_solver.models import ContratoAceptacion, ExecutionPlan, StepPlan


# ------------------------------------------------------------
# 1. Parser
# ------------------------------------------------------------
def test_parser_resolve_valores_por_defecto():
    args = main._construir_parser().parse_args(["resolve", "haz", "un", "informe"])
    assert args.comando == "resolve"
    assert args.objetivo == ["haz", "un", "informe"]
    assert args.max_intentos == main.MAX_INTENTOS_DEFAULT
    assert args.max_pasos == main.MAX_PASOS_DEFAULT
    assert args.json is False
    assert args.sin_exigir_verificacion is False


def test_parser_resolve_flags():
    args = main._construir_parser().parse_args([
        "resolve", "--max-intentos", "3", "--max-pasos", "9",
        "--json", "--no-aprender", "--sin-exigir-verificacion",
        "--timeout", "42", "objetivo único",
    ])
    assert args.max_intentos == 3
    assert args.max_pasos == 9
    assert args.json is True
    assert args.no_aprender is True
    assert args.sin_exigir_verificacion is True
    assert args.timeout == 42.0


def test_parser_resolve_exige_objetivo():
    with pytest.raises(SystemExit):
        main._construir_parser().parse_args(["resolve"])


def test_max_intentos_no_diverge_del_core():
    """El espejo de main.py y la constante real no pueden separarse."""
    from core.goal_resolver import MAX_INTENTOS_DEFAULT

    assert main.MAX_INTENTOS_DEFAULT == MAX_INTENTOS_DEFAULT


# ------------------------------------------------------------
# 2. Handler
# ------------------------------------------------------------
def _resultado(resuelto: bool, *, resultado_texto: str = "informe listo"):
    return ResultadoResolucion(
        resuelto=resuelto,
        objetivo="haz un informe",
        motivo="el artefacto cumple el contrato" if resuelto else "falta salida.docx",
        parada=PARADA_VERIFICADO if resuelto else PARADA_INTENTOS,
        intentos=[
            IntentoResolucion(
                numero=1, plan_id="p1", plan_titulo="Plan 1",
                verificable=True, ejecutado=True, exito=resuelto,
                motivo="aceptado" if resuelto else "falta salida.docx",
                aceptacion={"aceptada": resuelto, "motivos": [] if resuelto else ["falta salida.docx"]},
            )
        ],
        salida_final={"resultado": resultado_texto} if resuelto else {},
    )


def _args(**kwargs):
    base = dict(
        objetivo=["haz", "un", "informe"],
        max_intentos=2, max_pasos=6, timeout=None,
        json=False, no_aprender=False, sin_exigir_verificacion=False,
        output=None,
    )
    base.update(kwargs)
    return Namespace(**base)


def test_resolve_json_inyectando_el_resolver(monkeypatch, capsys):
    monkeypatch.setattr(main, "_resolver_objetivo", lambda *a, **k: _resultado(True))

    rc = main._ejecutar_resolve(_args(json=True))

    assert rc == main.EXIT_OK
    datos = json.loads(capsys.readouterr().out)
    assert datos["resuelto"] is True
    assert datos["ok"] is True
    assert datos["estado"] == "completada"
    assert datos["parada"] == PARADA_VERIFICADO
    assert datos["n_intentos"] == 1


def test_resolve_texto_imprime_el_resultado(monkeypatch, capsys):
    monkeypatch.setattr(main, "_resolver_objetivo", lambda *a, **k: _resultado(True))

    rc = main._ejecutar_resolve(_args())

    salida = capsys.readouterr()
    assert rc == main.EXIT_OK
    assert "informe listo" in salida.out
    assert "intento 1" in salida.err


def test_resolve_no_resuelto_devuelve_exit_run_error(monkeypatch, capsys):
    monkeypatch.setattr(main, "_resolver_objetivo", lambda *a, **k: _resultado(False))

    rc = main._ejecutar_resolve(_args())

    assert rc == main.EXIT_RUN_ERROR
    assert "No resuelto" in capsys.readouterr().err


def test_resolve_objetivo_vacio_devuelve_bad_args(capsys):
    rc = main._ejecutar_resolve(_args(objetivo=[]))
    assert rc == main.EXIT_BAD_ARGS
    assert "objetivo" in capsys.readouterr().err


def test_resolve_error_del_resolver_devuelve_exit_run_error(monkeypatch, capsys):
    def _explota(*a, **k):
        raise RuntimeError("sin API key")

    monkeypatch.setattr(main, "_resolver_objetivo", _explota)
    rc = main._ejecutar_resolve(_args(json=True))

    assert rc == main.EXIT_RUN_ERROR
    datos = json.loads(capsys.readouterr().out)
    assert datos["ok"] is False
    assert "sin API key" in datos["error"]


def test_resolve_pasa_los_flags_al_resolver(monkeypatch):
    capturado = {}

    def _falso(objetivo, **kwargs):
        capturado["objetivo"] = objetivo
        capturado.update(kwargs)
        return _resultado(True)

    monkeypatch.setattr(main, "_resolver_objetivo", _falso)
    main._ejecutar_resolve(_args(
        objetivo=["informe", "bitcoin"], max_intentos=4, max_pasos=8,
        no_aprender=True, sin_exigir_verificacion=True,
    ))

    assert capturado["objetivo"] == "informe bitcoin"
    assert capturado["max_intentos"] == 4
    assert capturado["max_pasos"] == 8
    assert capturado["aprender"] is False
    assert capturado["exigir_verificacion"] is False


def test_resolve_escribe_output(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(main, "_resolver_objetivo", lambda *a, **k: _resultado(True))
    destino = tmp_path / "resolucion.json"

    rc = main._ejecutar_resolve(_args(json=True, output=destino))

    assert rc == main.EXIT_OK
    datos = json.loads(destino.read_text(encoding="utf-8"))
    assert datos["resuelto"] is True
    assert datos["objetivo"] == "haz un informe"


# ------------------------------------------------------------
# 3. Cableado real del GoalResolver
# ------------------------------------------------------------
def _plan_con_contrato(titulo: str = "A") -> ExecutionPlan:
    return ExecutionPlan(
        id=titulo,
        titulo=titulo,
        pasos=[
            StepPlan(
                orden=1, nombre="Producir", tipo_agente="Python", es_critico=True,
                aceptacion=ContratoAceptacion(archivos=["salida.docx"]),
            )
        ],
    )


def test_resolver_objetivo_replanifica_con_la_evidencia(monkeypatch):
    """Sin LLM ni Qt: el bucle real re-planifica y usa el motivo del fallo."""
    import core.llm_client as llm_mod
    import core.problem_solver as ps_mod

    llamadas_plan: list[tuple[str, str]] = []
    salidas = [
        {
            "ok": False,
            "aceptacion": {"aceptada": False, "motivos": ["falta salida.docx"]},
            "agentes": [{"nombre": "Escribir", "ok": True}],
        },
        {
            "ok": True,
            "aceptacion": {"aceptada": True, "motivos": []},
            "agentes": [{"nombre": "Escribir", "ok": True}],
            "resultado": "informe generado",
        },
    ]

    class _SolverFalso:
        def __init__(self, llm):
            self.llm = llm

        def resolver_problema(self, objetivo, max_pasos=10, _instruccion_extra=""):
            llamadas_plan.append((objetivo, _instruccion_extra))
            return _plan_con_contrato(f"P{len(llamadas_plan)}")

    class _LLMFalso:
        disponible = True

    monkeypatch.setattr(llm_mod, "obtener_llm_client_compartido", lambda: _LLMFalso())
    monkeypatch.setattr(ps_mod, "ProblemSolver", _SolverFalso)
    monkeypatch.setattr(main, "_asegurar_qt", lambda: None)

    def _ejecutar_falso(plan, **kwargs):
        assert kwargs.get("presupuesto") is not None   # presupuesto compartido
        return salidas[min(len(llamadas_plan), len(salidas)) - 1]

    monkeypatch.setattr(main, "_ejecutar_plan", _ejecutar_falso)

    resultado = main._resolver_objetivo("haz un informe", max_intentos=2, max_pasos=5)

    assert resultado.resuelto is True
    assert resultado.n_intentos == 2
    assert llamadas_plan[0][1] == ""                       # primer intento limpio
    assert "falta salida.docx" in llamadas_plan[1][1]      # evidencia concreta
    assert resultado.salida_final.get("resultado") == "informe generado"
