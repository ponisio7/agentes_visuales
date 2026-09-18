"""
Tests del inyector de contratos de salida.

Verifican que:
1. Un paso con contrato declarado lo recibe en su prompt.
2. Un paso sin contrato no recibe nada extra.
3. La inyección es idempotente.
"""
import sys
sys.path.insert(0, '.')

from core.problem_solver.builder import (
    _inyectar_contrato_salida,
    CONTRATOS_SALIDA_POR_PASO,
    MARCADOR_CONTRATO,
)


def test_generar_cuento_recibe_contrato():
    prompt = "Escribe un cuento sobre dragones."
    out = _inyectar_contrato_salida(prompt, "GenerarCuento")
    assert MARCADOR_CONTRATO in out
    assert "descripciones_imagenes" in out
    assert '"cuento"' in out
    print("✅ GenerarCuento recibe contrato")


def test_paso_sin_contrato_no_cambia():
    prompt = "Suma 2 + 2."
    out = _inyectar_contrato_salida(prompt, "Sumar")
    assert out == prompt
    print("✅ Paso sin contrato no cambia")


def test_idempotente():
    prompt = "Escribe un cuento."
    out1 = _inyectar_contrato_salida(prompt, "GenerarCuento")
    out2 = _inyectar_contrato_salida(out1, "GenerarCuento")
    assert out1 == out2
    assert out2.count(MARCADOR_CONTRATO) == 1
    print("✅ Inyección idempotente")


def test_todos_los_contratos_son_validos():
    """Cada contrato debe mencionar al menos una clave entre comillas."""
    for nombre, contrato in CONTRATOS_SALIDA_POR_PASO.items():
        assert MARCADOR_CONTRATO in contrato, f"{nombre}: falta marcador"
        assert '"' in contrato, f"{nombre}: sin claves declaradas"
    print(f"✅ {len(CONTRATOS_SALIDA_POR_PASO)} contratos válidos")


if __name__ == "__main__":
    test_generar_cuento_recibe_contrato()
    test_paso_sin_contrato_no_cambia()
    test_idempotente()
    test_todos_los_contratos_son_validos()
    print("\n🎉 Todos los tests pasan")