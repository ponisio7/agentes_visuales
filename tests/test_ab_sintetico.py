#!/usr/bin/env python3
"""
Test sintético del A/B testing de reescrituras.

Crea escenarios controlados en la BD y verifica que PromptABEvaluator
toma las decisiones correctas:
  - Candidato mejor → PROMOVIDO
  - Candidato peor  → DESCARTADO
  - Candidato igual → ESPERA (o DESCARTA si supera MAX_USOS_SIN_DECISION)
"""
import sys
from pathlib import Path

# Añadir la raíz del proyecto al sys.path
_raiz = Path(__file__).resolve().parent.parent  # tmp/ → raíz
if str(_raiz) not in sys.path:
    sys.path.insert(0, str(_raiz))
    
import sqlite3
from datetime import datetime
from pathlib import Path

# Asegurar import del proyecto
sys.path.insert(0, str(Path(__file__).resolve().parent))

from learning.prompt_ab_evaluator import (
    MARGEN_PROMOCION,
    MAX_USOS_SIN_DECISION,
    MIN_USOS_ACTIVO_PARA_COMPARAR,
    MIN_USOS_PARA_DECIDIR,
    PromptABEvaluator,
)

DB = "agent_history.db"


def _crear_escenario(nombre, firma, activo_score, candidato_scores):
    """
    Crea un escenario limpio:
      - Un activo con N usos todos con activo_score.
      - Un candidato con len(candidato_scores) usos con los scores dados.

    Devuelve (activo_id, candidato_id).
    """
    with sqlite3.connect(DB, timeout=10) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")

        # Reutilizar un feedback existente para satisfacer la FK
        cur = conn.execute("SELECT id FROM feedback_usuario LIMIT 1")
        row = cur.fetchone()
        if row is None:
            raise RuntimeError("No hay feedback_usuario en la DB; el test necesita al menos uno")
        feedback_id = row[0]

        # Limpiar versiones de esta firma
        conn.execute("DELETE FROM prompts_reescritos WHERE firma = ?", (firma,))

        # Crear activo (usa feedback_id real)
        cur = conn.execute(
            """INSERT INTO prompts_reescritos
               (firma, prompt_original, prompt_nuevo, feedback_id, razon,
                fecha, activo, estado, n_usos)
               VALUES (?, ?, ?, ?, ?, ?, 1, 'activo', 0)""",
            (firma, f"[TEST] prompt original {nombre}", f"[TEST] activo {nombre}",
             feedback_id, "sintético", datetime.now().isoformat()),
        )
        activo_id = cur.lastrowid

        # Crear candidato
        cur = conn.execute(
            """INSERT INTO prompts_reescritos
               (firma, prompt_original, prompt_nuevo, feedback_id, razon,
                fecha, activo, estado, n_usos)
               VALUES (?, ?, ?, ?, ?, ?, 0, 'candidato', 0)""",
            (firma, f"[TEST] prompt original {nombre}", f"[TEST] candidato {nombre}",
             feedback_id, "sintético", datetime.now().isoformat()),
        )
        candidato_id = cur.lastrowid

        # Simular usos del activo
        for i in range(MIN_USOS_ACTIVO_PARA_COMPARAR):
            conn.execute(
                """INSERT INTO prompt_reescrito_usos
                   (prompt_reescrito_id, ejecucion_id, score, fecha)
                   VALUES (?, ?, ?, ?)""",
                (activo_id, 900000 + i, activo_score, datetime.now().isoformat()),
            )

        # Simular usos del candidato
        for i, sc in enumerate(candidato_scores):
            conn.execute(
                """INSERT INTO prompt_reescrito_usos
                   (prompt_reescrito_id, ejecucion_id, score, fecha)
                   VALUES (?, ?, ?, ?)""",
                (candidato_id, 800000 + i, sc, datetime.now().isoformat()),
            )

        # Actualizar contadores n_usos
        conn.execute(
            "UPDATE prompts_reescritos SET n_usos = ("
            "  SELECT COUNT(*) FROM prompt_reescrito_usos WHERE prompt_reescrito_id = ?"
            ") WHERE id = ?",
            (activo_id, activo_id),
        )
        conn.execute(
            "UPDATE prompts_reescritos SET n_usos = ("
            "  SELECT COUNT(*) FROM prompt_reescrito_usos WHERE prompt_reescrito_id = ?"
            ") WHERE id = ?",
            (candidato_id, candidato_id),
        )

        conn.commit()
        return activo_id, candidato_id


def _leer_estado(firma):
    with sqlite3.connect(DB, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, estado, n_usos FROM prompts_reescritos
               WHERE firma = ? ORDER BY id""",
            (firma,),
        ).fetchall()
        return [dict(r) for r in rows]


def _limpiar(firma):
    with sqlite3.connect(DB, timeout=10) as conn:
        conn.execute("DELETE FROM prompts_reescritos WHERE firma = ?", (firma,))
        conn.commit()


# ────────────────────────────────────────────────────────────
# ESCENARIO 1: candidato mejor → PROMOVIDO
# ────────────────────────────────────────────────────────────
def test_promocion():
    firma = "test_firma_promocion_" + datetime.now().strftime("%H%M%S")
    print("=" * 60)
    print("ESCENARIO 1: candidato mejor → PROMOVIDO")
    print("=" * 60)

    # Activo: 0.60. Candidato: 0.80 (mejora de 0.20 ≥ 0.05)
    activo_id, candidato_id = _crear_escenario(
        "promocion",
        firma,
        activo_score=0.60,
        candidato_scores=[0.80] * MIN_USOS_PARA_DECIDIR,
    )

    estado_antes = _leer_estado(firma)
    print("Estado ANTES:")
    for r in estado_antes:
        print(f"  id={r['id']} estado={r['estado']} n_usos={r['n_usos']}")

    ab = PromptABEvaluator(DB)
    decision = ab.evaluar_candidato(firma)
    print(f"\nDecisión: {decision}")

    estado_despues = _leer_estado(firma)
    print("Estado DESPUÉS:")
    for r in estado_despues:
        print(f"  id={r['id']} estado={r['estado']} n_usos={r['n_usos']}")

    # Verificar
    activo = next(r for r in estado_despues if r["id"] == activo_id)
    candidato = next(r for r in estado_despues if r["id"] == candidato_id)

    ok = (
        decision == "promovido"
        and activo["estado"] == "descartado"
        and candidato["estado"] == "activo"
    )
    print(f"\n{'✅ PASS' if ok else '❌ FAIL'}")
    _limpiar(firma)
    return ok


# ────────────────────────────────────────────────────────────
# ESCENARIO 2: candidato peor → DESCARTADO
# ────────────────────────────────────────────────────────────
def test_descarte():
    firma = "test_firma_descarte_" + datetime.now().strftime("%H%M%S")
    print()
    print("=" * 60)
    print("ESCENARIO 2: candidato peor → DESCARTADO")
    print("=" * 60)

    # Activo: 0.70. Candidato: 0.40 (empeora 0.30 ≥ 0.05)
    activo_id, candidato_id = _crear_escenario(
        "descarte",
        firma,
        activo_score=0.70,
        candidato_scores=[0.40] * MIN_USOS_PARA_DECIDIR,
    )

    estado_antes = _leer_estado(firma)
    print("Estado ANTES:")
    for r in estado_antes:
        print(f"  id={r['id']} estado={r['estado']} n_usos={r['n_usos']}")

    ab = PromptABEvaluator(DB)
    decision = ab.evaluar_candidato(firma)
    print(f"\nDecisión: {decision}")

    estado_despues = _leer_estado(firma)
    print("Estado DESPUÉS:")
    for r in estado_despues:
        print(f"  id={r['id']} estado={r['estado']} n_usos={r['n_usos']}")

    activo = next(r for r in estado_despues if r["id"] == activo_id)
    candidato = next(r for r in estado_despues if r["id"] == candidato_id)

    ok = (
        decision == "descartado"
        and activo["estado"] == "activo"     # sigue activo
        and candidato["estado"] == "descartado"
    )
    print(f"\n{'✅ PASS' if ok else '❌ FAIL'}")
    _limpiar(firma)
    return ok


# ────────────────────────────────────────────────────────────
# ESCENARIO 3: candidato igual con pocos usos → ESPERA
# ────────────────────────────────────────────────────────────
def test_empate_espera():
    firma = "test_firma_empate_" + datetime.now().strftime("%H%M%S")
    print()
    print("=" * 60)
    print("ESCENARIO 3: candidato igual (pocos usos) → ESPERA")
    print("=" * 60)

    # Activo: 0.60. Candidato: 0.62 (dentro del margen)
    activo_id, candidato_id = _crear_escenario(
        "empate",
        firma,
        activo_score=0.60,
        candidato_scores=[0.62] * MIN_USOS_PARA_DECIDIR,
    )

    estado_antes = _leer_estado(firma)
    print("Estado ANTES:")
    for r in estado_antes:
        print(f"  id={r['id']} estado={r['estado']} n_usos={r['n_usos']}")

    ab = PromptABEvaluator(DB)
    decision = ab.evaluar_candidato(firma)
    print(f"\nDecisión: {decision}")

    estado_despues = _leer_estado(firma)
    print("Estado DESPUÉS:")
    for r in estado_despues:
        print(f"  id={r['id']} estado={r['estado']} n_usos={r['n_usos']}")

    activo = next(r for r in estado_despues if r["id"] == activo_id)
    candidato = next(r for r in estado_despues if r["id"] == candidato_id)

    ok = (
        decision == "espera"
        and activo["estado"] == "activo"
        and candidato["estado"] == "candidato"
    )
    print(f"\n{'✅ PASS' if ok else '❌ FAIL'}")
    _limpiar(firma)
    return ok


# ────────────────────────────────────────────────────────────
# ESCENARIO 4: candidato igual con MUCHOS usos → DESCARTA
# ────────────────────────────────────────────────────────────
def test_empate_descarte():
    firma = "test_firma_empate_max_" + datetime.now().strftime("%H%M%S")
    print()
    print("=" * 60)
    print("ESCENARIO 4: candidato igual (muchos usos) → DESCARTA")
    print("=" * 60)

    # Activo: 0.60. Candidato: 0.62 con MAX_USOS_SIN_DECISION usos
    activo_id, candidato_id = _crear_escenario(
        "empate_max",
        firma,
        activo_score=0.60,
        candidato_scores=[0.62] * MAX_USOS_SIN_DECISION,
    )

    ab = PromptABEvaluator(DB)
    decision = ab.evaluar_candidato(firma)
    print(f"Decisión: {decision}")

    estado_despues = _leer_estado(firma)
    candidato = next(r for r in estado_despues if r["id"] == candidato_id)

    ok = (
        decision == "descartado"
        and candidato["estado"] == "descartado"
    )
    print(f"\n{'✅ PASS' if ok else '❌ FAIL'}")
    _limpiar(firma)
    return ok


# ────────────────────────────────────────────────────────────
# MAIN
# ────────────────────────────────────────────────────────────
def main():
    print("\n🧪 TEST SINTÉTICO DEL A/B TESTING\n")
    print("Parámetros:")
    print(f"  MIN_USOS_PARA_DECIDIR         = {MIN_USOS_PARA_DECIDIR}")
    print(f"  MIN_USOS_ACTIVO_PARA_COMPARAR = {MIN_USOS_ACTIVO_PARA_COMPARAR}")
    print(f"  MARGEN_PROMOCION              = {MARGEN_PROMOCION}")
    print(f"  MAX_USOS_SIN_DECISION         = {MAX_USOS_SIN_DECISION}")
    print()

    resultados = []
    resultados.append(("Promoción (mejora ≥ 0.05)", test_promocion()))
    resultados.append(("Descarte (empeora ≥ 0.05)", test_descarte()))
    resultados.append(("Empate con pocos usos → espera", test_empate_espera()))
    resultados.append(("Empate con muchos usos → descarta", test_empate_descarte()))

    print()
    print("=" * 60)
    print("RESUMEN")
    print("=" * 60)
    todos_ok = True
    for nombre, ok in resultados:
        marca = "✅ PASS" if ok else "❌ FAIL"
        print(f"  {marca}  {nombre}")
        if not ok:
            todos_ok = False

    print()
    if todos_ok:
        print("🎉 TODOS LOS ESCENARIOS PASAN")
        return 0
    else:
        print("⚠️  ALGUNOS ESCENARIOS FALLAN")
        return 1


if __name__ == "__main__":
    sys.exit(main())
