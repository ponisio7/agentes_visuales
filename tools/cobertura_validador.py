#!/usr/bin/env python3
"""Mide la cobertura del validador de Fase 6 sobre el corpus real de ``logs/``.

Punto 3.10 del ROADMAP (bug 1.12): la cobertura del validador se había
comprobado «solo con el caso probado a mano». Esto la mide contra los planes
reales que el LLM generó y quedaron guardados en ``logs/llm_response_*.txt``.

Qué informa:
  - planes legibles / ilegibles (el corpus incluye respuestas que no son JSON),
  - pasos con código Python y cuántos detecta el validador,
  - desglose por REGLA (la primera palabra significativa del mensaje),
  - un ejemplo de plan por regla, para poder convertirlo en test.

Uso:
    python tools/cobertura_validador.py                 # todo el corpus
    python tools/cobertura_validador.py --limite 50     # solo 50 ficheros
    python tools/cobertura_validador.py --solo-inexistentes
    python tools/cobertura_validador.py --json
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from typing import Any

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))

from core.problem_solver.validator import PlanValidator  # noqa: E402

logger = logging.getLogger("cobertura")

_RE_ETIQUETA_REGLAS = re.compile(
    r"^BLOQUEANTE: [^:]+: (?P<detalle>.+)$", re.DOTALL
)


def _clasificar(mensaje: str) -> str:
    """Etiqueta corta de la regla que disparó el mensaje."""
    coincidencia = _RE_ETIQUETA_REGLAS.match(mensaje)
    detalle = coincidencia.group("detalle") if coincidencia else mensaje
    detalle = detalle.lower()
    if "relleno" in detalle:
        return "contenido de relleno (regla 8)"
    if "syntaxerror" in detalle:
        return "SyntaxError"
    if "json.loads con placeholder" in detalle:
        return "json.loads con placeholder"
    if "placeholder literal" in detalle:
        return "placeholder literal sin sustituir"
    if "sin definirla" in detalle:
        return "nombre libre sin definir"
    if "como variable python" in detalle:
        return "agente usado como variable"
    if "de otro tipo de agente" in detalle:
        return "clave de dependencia de otro tipo"
    if "sintaxis de plantilla" in detalle:
        return "placeholder {{...}} en string"
    if "contrato" in detalle:
        return "contrato de aceptación"
    return detalle.split(".")[0][:60]


def _extraer_json(texto: str) -> dict | None:
    """Extrae el plan del log.

    El log NO es JSON puro: lleva cabecera ``===`` delante y una línea
    ``LONGITUD: ...`` detrás, así que un ``json.loads`` desde la primera llave
    falla. Se usa el extractor balanceado del propio proyecto.
    """
    from core.utils import extraer_json_balanceado

    crudo = extraer_json_balanceado(texto)
    if not crudo:
        return None
    try:
        plan = json.loads(crudo)
    except json.JSONDecodeError:
        return None
    return plan if isinstance(plan, dict) else None


def iterar_planes(directorio: Path, limite: int | None = None) -> Iterator[tuple[str, dict]]:
    for i, ruta in enumerate(sorted(directorio.glob("llm_response_*.txt"))):
        if limite is not None and i >= limite:
            return
        try:
            texto = ruta.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        plan = _extraer_json(texto)
        if isinstance(plan, dict):
            yield ruta.name, plan


def analizar(directorio: Path, limite: int | None = None) -> dict[str, Any]:
    validador = PlanValidator(logger)

    total_ficheros = 0
    legibles = 0
    pasos_con_codigo = 0
    pasos_detectados = 0
    por_regla: Counter[str] = Counter()
    ejemplos: dict[str, dict[str, str]] = {}
    sin_detectar: list[dict[str, str]] = []

    for nombre_fichero, plan in iterar_planes(directorio, limite):
        total_ficheros += 1
        legibles += 1
        pasos = plan.get("pasos") or []
        nombres = {p.get("nombre") for p in pasos if isinstance(p, dict)}
        nombres.discard(None)
        tipos = {
            p.get("nombre"): p.get("tipo")
            for p in pasos
            if isinstance(p, dict) and p.get("nombre")
        }

        for paso in pasos:
            if not isinstance(paso, dict):
                continue
            config = paso.get("configuracion") or {}
            codigo = config.get("codigo")
            if not isinstance(codigo, str) or not codigo.strip():
                continue
            pasos_con_codigo += 1
            errores = validador._validar_codigo_python_ast(
                codigo=codigo,
                nombre=str(paso.get("nombre") or "?"),
                nombres_agentes=nombres,
                tipos_agentes=tipos,
            )
            if errores:
                pasos_detectados += 1
                regla = _clasificar(errores[0])
                por_regla[regla] += 1
                ejemplos.setdefault(regla, {
                    "fichero": nombre_fichero,
                    "paso": str(paso.get("nombre")),
                    "mensaje": errores[0][:200],
                })
            elif len(sin_detectar) < 5:
                sin_detectar.append({
                    "fichero": nombre_fichero,
                    "paso": str(paso.get("nombre")),
                    "codigo": codigo[:200],
                })

    return {
        "ficheros_analizados": total_ficheros,
        "ficheros_legibles": legibles,
        "pasos_con_codigo": pasos_con_codigo,
        "pasos_detectados": pasos_detectados,
        "cobertura": (
            round(pasos_detectados / pasos_con_codigo, 4)
            if pasos_con_codigo else 0.0
        ),
        "por_regla": dict(por_regla.most_common()),
        "ejemplos": ejemplos,
        "muestra_no_detectada": sin_detectar,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", type=Path, default=RAIZ / "logs")
    parser.add_argument("--limite", type=int, default=None,
                        help="Analiza solo los primeros N ficheros.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    informes = analizar(args.dir, args.limite)

    if args.json:
        print(json.dumps(informes, ensure_ascii=False, indent=2))
        return 0

    print("Cobertura del validador de Fase 6 sobre logs/")
    print(f"  Ficheros analizados : {informes['ficheros_analizados']}")
    print(f"  Pasos con código    : {informes['pasos_con_codigo']}")
    print(f"  Detectados          : {informes['pasos_detectados']}"
          f"  ({informes['cobertura'] * 100:.1f} %)")
    print("\n  Desglose por regla:")
    for regla, cuenta in informes["por_regla"].items():
        print(f"    {cuenta:5d}  {regla}")
        ejemplo = informes["ejemplos"].get(regla)
        if ejemplo:
            print(f"           p. ej. {ejemplo['fichero']} :: {ejemplo['paso']}")

    if informes["muestra_no_detectada"]:
        print("\n  Muestra NO detectada (candidata a hueco de cobertura):")
        for caso in informes["muestra_no_detectada"]:
            print(f"    - {caso['fichero']} :: {caso['paso']}")
            print(f"      {caso['codigo'][:120]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
