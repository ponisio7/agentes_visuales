#!/usr/bin/env python3
"""
Test sintético del A/B testing de reescrituras.

Crea escenarios controlados en la BD y verifica que PromptABEvaluator
toma las decisiones correctas:
  - Candidato mejor → PROMOVIDO
  - Candidato peor  → DESCARTADO
  - Candidato igual → ESPERA (o DESCARTA si supera MAX_USOS_SIN_DECISION)

Aislamiento (Regla 2 del harness): este test NUNCA toca
``agent_history.db`` (la BD de producción). Trabaja siempre sobre una
copia temporal cuyo esquema se construye con ``storage.database.Database``
dentro de un directorio de pytest (``tmp_path``). La comprobación
``_validar_db_no_produccion()`` aborta el test si alguien intentara
apuntarlo a la BD real.

También se puede ejecutar como script::

    python tests/test_ab_sintetico.py
"""
import sys
import tempfile
from pathlib import Path

# Añadir la raíz del proyecto al sys.path
_raiz = Path(__file__).resolve().parent.parent  # tests/ → raíz
if str(_raiz) not in sys.path:
    sys.path.insert(0, str(_raiz))

import shutil
import sqlite3
from datetime import datetime

import pytest

from learning.prompt_ab_evaluator import (
    MARGEN_PROMOCION,
    MAX_USOS_SIN_DECISION,
    MIN_USOS_ACTIVO_PARA_COMPARAR,
    MIN_USOS_PARA_DECIDIR,
    PromptABEvaluator,
)

# Ruta de la BD de producción: PROHIBIDO escribir aquí desde los tests.
DB_PRODUCCION = (_raiz / "agent_history.db").resolve()


def _validar_db_no_produccion(db_path) -> None:
    """Aborta si ``db_path`` apunta a la BD de producción.

    Es una red de seguridad: protege contra futuras ediciones que
    vuelvan a hardcodear la ruta real (bug B3 de v3.0.1).
    """
    if Path(db_path).resolve() == DB_PRODUCCION:
        raise AssertionError(
            "test_ab_sintetico no puede escribir en la BD de producción "
            f"({DB_PRODUCCION}). Usa una BD temporal (tmp_path)."
        )


def _crear_db_temporal(db_path) -> str:
    """Crea un esquema completo (base + learning) en ``db_path``.

    Añade un ``feedback_usuario`` mínimo para satisfacer la FK de
    ``prompts_reescritos.feedback_id``. Devuelve la ruta como ``str``.
    """
    _validar_db_no_produccion(db_path)

    from storage.database import Database

    db = Database(str(db_path))
    db.close()

    # La FK feedback_usuario.ejecucion_id → ejecuciones(id) se satisface
    # sin fila padre porque sqlite3 tiene foreign_keys=OFF por defecto en
    # esta conexión auxiliar; lo que importa es que exista la fila de
    # feedback para la FK de prompts_reescritos (que sí se activa abajo).
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO feedback_usuario (ejecucion_id, score, fecha) "
            "VALUES (?, ?, ?)",
            (1, 1.0, datetime.now().isoformat()),
        )
        conn.commit()

    return str(db_path)


@pytest.fixture(scope="module")
def db(tmp_path_factory):
    """BD temporal aislada (nunca la de producción)."""
    carpeta = tmp_path_factory.mktemp("ab_sintetico")
    return _crear_db_temporal(carpeta / "agent_history_test.db")


def _crear_escenario(db, nombre, firma, activo_score, candidato_scores):
    """
    Crea un escenario limpio:
      - Un activo con N usos todos con activo_score.
      - Un candidato con len(candidato_scores) usos con los scores dados.

    Devuelve (activo_id, candidato_id).
    """
    _validar_db_no_produccion(db)

    with sqlite3.connect(db, timeout=10) as conn:
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


def _leer_estado(db, firma):
    _validar_db_no_produccion(db)

    with sqlite3.connect(db, timeout=10) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, estado, n_usos FROM prompts_reescritos
               WHERE firma = ? ORDER BY id""",
            (firma,),
        ).fetchall()
        return [dict(r) for r in rows]


def _limpiar(db, firma):
    _validar_db_no_produccion(db)

    with sqlite3.connect(db, timeout=10) as conn:
        conn.execute("DELETE FROM prompts_reescritos WHERE firma = ?", (firma,))
        conn.commit()


# ────────────────────────────────────────────────────────────
# ESCENARIO 1: candidato mejor → PROMOVIDO
# ────────────────────────────────────────────────────────────
def _escenario_promocion(db):
    firma = "test_firma_promocion_" + datetime.now().strftime("%H%M%S")
    print("=" * 60)
    print("ESCENARIO 1: candidato mejor → PROMOVIDO")
    print("=" * 60)

    # Activo: 0.60. Candidato: 0.80 (mejora de 0.20 ≥ 0.05)
    activo_id, candidato_id = _crear_escenario(
        db,
        "promocion",
        firma,
        activo_score=0.60,
        candidato_scores=[0.80] * MIN_USOS_PARA_DECIDIR,
    )

    estado_antes = _leer_estado(db, firma)
    print("Estado ANTES:")
    for r in estado_antes:
        print(f"  id={r['id']} estado={r['estado']} n_usos={r['n_usos']}")

    ab = PromptABEvaluator(db)
    decision = ab.evaluar_candidato(firma)
    print(f"\nDecisión: {decision}")

    estado_despues = _leer_estado(db, firma)
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
    _limpiar(db, firma)
    return ok


# ────────────────────────────────────────────────────────────
# ESCENARIO 2: candidato peor → DESCARTADO
# ────────────────────────────────────────────────────────────
def _escenario_descarte(db):
    firma = "test_firma_descarte_" + datetime.now().strftime("%H%M%S")
    print()
    print("=" * 60)
    print("ESCENARIO 2: candidato peor → DESCARTADO")
    print("=" * 60)

    # Activo: 0.70. Candidato: 0.40 (empeora 0.30 ≥ 0.05)
    activo_id, candidato_id = _crear_escenario(
        db,
        "descarte",
        firma,
        activo_score=0.70,
        candidato_scores=[0.40] * MIN_USOS_PARA_DECIDIR,
    )

    estado_antes = _leer_estado(db, firma)
    print("Estado ANTES:")
    for r in estado_antes:
        print(f"  id={r['id']} estado={r['estado']} n_usos={r['n_usos']}")

    ab = PromptABEvaluator(db)
    decision = ab.evaluar_candidato(firma)
    print(f"\nDecisión: {decision}")

    estado_despues = _leer_estado(db, firma)
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
    _limpiar(db, firma)
    return ok


# ────────────────────────────────────────────────────────────
# ESCENARIO 3: candidato igual con pocos usos → ESPERA
# ────────────────────────────────────────────────────────────
def _escenario_empate_espera(db):
    firma = "test_firma_empate_" + datetime.now().strftime("%H%M%S")
    print()
    print("=" * 60)
    print("ESCENARIO 3: candidato igual (pocos usos) → ESPERA")
    print("=" * 60)

    # Activo: 0.60. Candidato: 0.62 (dentro del margen)
    activo_id, candidato_id = _crear_escenario(
        db,
        "empate",
        firma,
        activo_score=0.60,
        candidato_scores=[0.62] * MIN_USOS_PARA_DECIDIR,
    )

    estado_antes = _leer_estado(db, firma)
    print("Estado ANTES:")
    for r in estado_antes:
        print(f"  id={r['id']} estado={r['estado']} n_usos={r['n_usos']}")

    ab = PromptABEvaluator(db)
    decision = ab.evaluar_candidato(firma)
    print(f"\nDecisión: {decision}")

    estado_despues = _leer_estado(db, firma)
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
    _limpiar(db, firma)
    return ok


# ────────────────────────────────────────────────────────────
# ESCENARIO 4: candidato igual con MUCHOS usos → DESCARTA
# ────────────────────────────────────────────────────────────
def _escenario_empate_descarte(db):
    firma = "test_firma_empate_max_" + datetime.now().strftime("%H%M%S")
    print()
    print("=" * 60)
    print("ESCENARIO 4: candidato igual (muchos usos) → DESCARTA")
    print("=" * 60)

    # Activo: 0.60. Candidato: 0.62 con MAX_USOS_SIN_DECISION usos
    activo_id, candidato_id = _crear_escenario(
        db,
        "empate_max",
        firma,
        activo_score=0.60,
        candidato_scores=[0.62] * MAX_USOS_SIN_DECISION,
    )

    ab = PromptABEvaluator(db)
    decision = ab.evaluar_candidato(firma)
    print(f"Decisión: {decision}")

    estado_despues = _leer_estado(db, firma)
    candidato = next(r for r in estado_despues if r["id"] == candidato_id)

    ok = (
        decision == "descartado"
        and candidato["estado"] == "descartado"
    )
    print(f"\n{'✅ PASS' if ok else '❌ FAIL'}")
    _limpiar(db, firma)
    return ok


# ────────────────────────────────────────────────────────────
# TESTS PYTEST (assert real: antes devolvían bool y pytest los
# daba por buenos siempre — regresión corregida)
# ────────────────────────────────────────────────────────────
def test_promocion(db):
    assert _escenario_promocion(db) is True


def test_descarte(db):
    assert _escenario_descarte(db) is True


def test_empate_espera(db):
    assert _escenario_empate_espera(db) is True


def test_empate_descarte(db):
    assert _escenario_empate_descarte(db) is True


def _imprimir_parametros():
    print("\n🧪 TEST SINTÉTICO DEL A/B TESTING\n")
    print("Parámetros:")
    print(f"  MIN_USOS_PARA_DECIDIR         = {MIN_USOS_PARA_DECIDIR}")
    print(f"  MIN_USOS_ACTIVO_PARA_COMPARAR = {MIN_USOS_ACTIVO_PARA_COMPARAR}")
    print(f"  MARGEN_PROMOCION              = {MARGEN_PROMOCION}")
    print(f"  MAX_USOS_SIN_DECISION         = {MAX_USOS_SIN_DECISION}")
    print()


def main():
    _imprimir_parametros()

    carpeta = tempfile.mkdtemp(prefix="ab_sintetico_")
    try:
        db = _crear_db_temporal(Path(carpeta) / "agent_history_test.db")
        resultados = [
            ("Promoción (mejora ≥ 0.05)", _escenario_promocion(db)),
            ("Descarte (empeora ≥ 0.05)", _escenario_descarte(db)),
            ("Empate con pocos usos → espera", _escenario_empate_espera(db)),
            ("Empate con muchos usos → descarta", _escenario_empate_descarte(db)),
        ]
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)

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
