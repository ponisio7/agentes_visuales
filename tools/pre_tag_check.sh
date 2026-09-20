#!/usr/bin/env bash
# tools/pre_tag_check.sh — Verificación pre-tag de Agentes Visuales
#
# Puerta de calidad previa al tag. Comprueba estado de git, sintaxis, lint
# acotado al código del proyecto, smoke test de ProblemSolver y (por
# defecto) la suite completa de tests.
#
# Uso:
#   ./tools/pre_tag_check.sh              # verificación completa (incluye tests)
#   ./tools/pre_tag_check.sh --no-tests   # omite la suite (solo comprobaciones rápidas)
#   PRETAG_SKIP_TESTS=1 ./tools/pre_tag_check.sh
set -uo pipefail

EJECUTAR_TESTS=1
for arg in "$@"; do
    case "$arg" in
        --no-tests) EJECUTAR_TESTS=0 ;;
        -h|--help)
            sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *)
            echo "Opción desconocida: $arg (usa --help)"
            exit 2
            ;;
    esac
done
[ "${PRETAG_SKIP_TESTS:-0}" = "1" ] && EJECUTAR_TESTS=0

ROJO='\033[31m'; VERDE='\033[32m'; AMAR='\033[33m'; CYAN='\033[36m'; NC='\033[0m'
FALLOS=0

ok()   { echo -e "${VERDE}✅ $1${NC}"; }
fail() { echo -e "${ROJO}❌ $1${NC}"; FALLOS=$((FALLOS+1)); }
warn() { echo -e "${AMAR}⚠️  $1${NC}"; }
info() { echo -e "${CYAN}▸ $1${NC}"; }

# ── Raíz del proyecto e intérprete ────────────────────────────
RAIZ="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$RAIZ"

# Preferir el venv del proyecto: python3 del sistema no tiene las deps.
PY="python3"
[ -x ".venv/bin/python" ] && PY=".venv/bin/python"

# Única fuente de verdad de la versión (ver pyproject.toml / main.py).
VERSION="$("$PY" -c 'import tomllib;print(tomllib.load(open("pyproject.toml","rb"))["project"]["version"])' 2>/dev/null || echo "desconocida")"

echo "════════════════════════════════════════════════════════════"
echo "  VERIFICACIÓN PRE-TAG v${VERSION} — Agentes Visuales"
echo "════════════════════════════════════════════════════════════"

# ── 1. Git limpio ─────────────────────────────────────────────
info "1. Estado de git"
if [ -n "$(git status --porcelain)" ]; then
    fail "Hay cambios sin commitear:"
    git status --short
else
    ok "Working tree limpio"
fi

# ── 2. Rama correcta ──────────────────────────────────────────
info "2. Rama actual"
RAMA=$(git rev-parse --abbrev-ref HEAD)
if [ "$RAMA" != "main" ] && [ "$RAMA" != "master" ]; then
    warn "Estás en '$RAMA', no en main/master. ¿Seguro?"
else
    ok "Rama: $RAMA"
fi

# ── 3. Sincronizado con remoto ────────────────────────────────
info "3. Sincronía con remoto"
git fetch --quiet origin 2>/dev/null || warn "No se pudo hacer fetch"
LOCAL=$(git rev-parse HEAD)
REMOTO=$(git rev-parse "@{u}" 2>/dev/null || echo "sin-upstream")
if [ "$LOCAL" = "$REMOTO" ]; then
    ok "Local == remoto"
elif [ "$REMOTO" = "sin-upstream" ]; then
    warn "Sin upstream configurado"
else
    fail "Local ($LOCAL) != remoto ($REMOTO). Haz push/pull primero."
fi

# ── 4. Tags v3.x previos ──────────────────────────────────────
info "4. Tags existentes v3.x"
TAGS_V3=$(git tag -l "v3.*")
if [ -n "$TAGS_V3" ]; then
    warn "Ya existen tags v3.x: $TAGS_V3"
else
    ok "No hay tags v3.x previos"
fi

# ── 5. Los archivos críticos existen ──────────────────────────
info "5. Archivos críticos"
CRITICOS=(
    "main.py"
    "core/sandbox.py"
    "core/scheduler.py"
    "core/llm_client.py"
    "storage/database.py"
    "learning/engine.py"
)
for f in "${CRITICOS[@]}"; do
    [ -f "$f" ] && ok "Existe: $f" || fail "FALTA: $f"
done

# ── 6. Sintaxis de todos los .py ──────────────────────────────
info "6. Compilación de sintaxis (todos los .py)"
ERR_SINTAXIS=0
while IFS= read -r f; do
    if ! "$PY" -m py_compile "$f" 2>/dev/null; then
        fail "SyntaxError en: $f"
        ERR_SINTAXIS=$((ERR_SINTAXIS+1))
    fi
done < <(find . -name "*.py" \
    -not -path "./.git/*" \
    -not -path "./.venv/*" \
    -not -path "./.env/*" \
    -not -path "./venv/*" \
    -not -path "./env/*" \
    -not -path "./tmp/*" \
    -not -path "./backup/*" \
    -not -path "./.backups_fix_*/*" \
    -not -path "*/__pycache__/*")
[ "$ERR_SINTAXIS" -eq 0 ] && ok "Todos los .py compilan"

# ── 7. Lint (acotado al código del proyecto) ──────────────────
# El escaneo debe acotarse SIEMPRE a los directorios del proyecto: un
# `pyflakes .` recorre .venv/.env y muere con RecursionError sin analizar
# nada del código propio (bug B4 de v3.0.1).
info "7. Lint (ruff/pyflakes, solo código del proyecto)"
OBJETIVOS_LINT=(core learning ui storage export tools tests main.py run_all_tests.py)

RUFF=""
for cand in ".venv/bin/ruff" "$(command -v ruff 2>/dev/null || true)"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then RUFF="$cand"; break; fi
done

if [ -n "$RUFF" ]; then
    if "$RUFF" check --no-cache "${OBJETIVOS_LINT[@]}"; then
        ok "ruff limpio"
    else
        fail "ruff reporta problemas (ver arriba)"
    fi
elif "$PY" -m pyflakes --version >/dev/null 2>&1; then
    if "$PY" -m pyflakes "${OBJETIVOS_LINT[@]}"; then
        ok "pyflakes limpio"
    else
        fail "pyflakes reporta problemas (ver arriba)"
    fi
else
    warn "Ni ruff ni pyflakes instalados (pip install ruff)"
fi

# ── 8. Prints de debug obvios ─────────────────────────────────
info "8. Prints de debug olvidados"
PRINTS=$(grep -rn "print(" --include="*.py" \
    --exclude-dir=.git --exclude-dir=__pycache__ \
    --exclude-dir=.venv --exclude-dir=.env --exclude-dir=venv --exclude-dir=env \
    --exclude-dir=tests --exclude-dir=tools \
    | grep -v "^run_all_tests.py:" \
    | grep -v "if __name__" | grep -v "file=sys.stderr" || true)
if [ -n "$PRINTS" ]; then
    warn "Posibles prints de debug (revisar):"
    echo "$PRINTS" | head -15
else
    ok "Sin prints sospechosos"
fi

# ── 9. Emojis de debug sospechosos ────────────────────────────
info "9. Mensajes de debug sospechosos"
SOSPECHOSOS=$(grep -rn "🥶\|FIXME\|XXX\|HACK\|DEBUG:" --include="*.py" \
    --exclude-dir=.git --exclude-dir=__pycache__ \
    --exclude-dir=.venv --exclude-dir=.env --exclude-dir=venv --exclude-dir=env \
    --exclude-dir=tests || true)
if [ -n "$SOSPECHOSOS" ]; then
    warn "Encontrados marcadores de debug:"
    echo "$SOSPECHOSOS" | head -20
else
    ok "Sin marcadores de debug"
fi

# ── 10. README y CHANGELOG ────────────────────────────────────
info "10. Documentación"
[ -f "README.md" ]   && ok "README.md"   || warn "Falta README.md"
if [ -f "CHANGELOG.md" ]; then
    if grep -q "v${VERSION}" CHANGELOG.md; then
        ok "CHANGELOG.md menciona v${VERSION}"
    else
        fail "CHANGELOG.md no menciona la versión v${VERSION}"
    fi
else
    warn "Falta CHANGELOG.md (recomendado)"
fi

# ── 11. Smoke test (ProblemSolver) ────────────────────────────
# Puerta real: si el pipeline ProblemSolver → LLM → plan falla, el tag no
# debe salir. Antes este paso solo avisaba y ocultaba el stderr.
info "11. Smoke test de ProblemSolver"
if "$PY" - <<'PYEOF'
from core.llm_client import obtener_llm_client_compartido
from core.problem_solver import ProblemSolver

solver = ProblemSolver(obtener_llm_client_compartido())
plan = solver.resolver_problema(
    "Genera un archivo saludo.txt con Hola Mundo", max_pasos=3
)
n_pasos = len(plan.pasos)
print(f"Plan: {plan.titulo} | Pasos: {n_pasos} | Agentes: {len(plan.agentes_generados)}")
for p in plan.pasos:
    print(f"  {p.orden}. [{p.tipo_agente}] {p.nombre}")
assert n_pasos >= 2, f"el plan tiene {n_pasos} pasos; se esperaban >= 2"
assert plan.agentes_generados, "el plan no generó agentes"
PYEOF
then
    ok "Smoke test completado (plan con >= 2 pasos)"
else
    fail "El smoke test falló (¿red o API key?); revisa la salida de arriba"
fi

# ── 12. Suite de tests ────────────────────────────────────────
if [ "$EJECUTAR_TESTS" -eq 1 ]; then
    info "12. Suite de tests completa (run_all_tests.py)"
    mkdir -p logs
    LOG_SUITE="logs/pretag_suite.log"
    if "$PY" run_all_tests.py > "$LOG_SUITE" 2>&1; then
        ok "Suite de tests: todos los grupos OK"
    else
        fail "La suite de tests falló (detalle en $LOG_SUITE):"
        tail -25 "$LOG_SUITE"
    fi
else
    warn "Suite de tests OMITIDA (--no-tests / PRETAG_SKIP_TESTS=1)"
fi

echo ""
echo "════════════════════════════════════════════════════════════"
if [ "$FALLOS" -eq 0 ]; then
    echo -e "${VERDE}✅ LISTO PARA TAGGEAR (revisa los ⚠️  manualmente)${NC}"
    echo ""
    echo "  Siguiente paso:"
    echo "    git tag -a v${VERSION} -m 'Release v${VERSION}'"
    echo "    git push origin v${VERSION}"
else
    echo -e "${ROJO}❌ $FALLOS fallos bloqueantes. Corrige antes de taggear.${NC}"
fi
echo "════════════════════════════════════════════════════════════"

exit "$FALLOS"
