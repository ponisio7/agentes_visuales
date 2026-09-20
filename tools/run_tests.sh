#!/usr/bin/env bash
# tools/run_tests.sh — suite completa reproducible en CPython 3.13.
#
# CONTEXTO (H3):
#   La suite completa sufre un SIGSEGV intermitente y PRE-EXISTENTE en
#   CPython 3.13: core/sandbox.py lanza subprocesos (fork+exec) desde hilos
#   del ThreadPoolExecutor del Scheduler. Con muchas pruebas acumuladas en un
#   mismo proceso, algún fork coincide con un hilo que cierra un fichero
#   temporal y el intérprete muere. No es un fallo de la lógica de la app.
#
#   Solución EXPLÍCITA (no se desactiva ninguna prueba): ejecutar cada test en
#   un proceso hijo con pytest-forked. El proceso padre recolecta el resultado
#   y un SIGSEGV en un hijo se reporta como fallo de ESE test, no tumba la
#   suite.
#
# Uso:
#   tools/run_tests.sh                 # suite completa aislada
#   tools/run_tests.sh tests/test_x.py # solo esos ficheros
#   PYTHON=.venv/bin/python tools/run_tests.sh
#
# Requiere: pip install -r requirements-dev.txt  (pytest-forked)
set -u

cd "$(dirname "$0")/.." || exit 1
PYTHON="${PYTHON:-.venv/bin/python}"

if ! "$PYTHON" -c "import pytest_forked" >/dev/null 2>&1; then
    echo "❌ Falta pytest-forked. Instala: pip install -r requirements-dev.txt" >&2
    exit 2
fi

if [ "$#" -eq 0 ]; then
    set -- tests/
fi

echo "▶ pytest aislado por proceso (--forked) sobre: $*"
exec "$PYTHON" -m pytest "$@" \
    -p no:cacheprovider \
    -o addopts="" \
    --forked
