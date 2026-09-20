#!/usr/bin/env bash
# tools/pre_tag_check.sh — Verificación pre-tag v3.0
set -uo pipefail

ROJO='\033[31m'; VERDE='\033[32m'; AMAR='\033[33m'; CYAN='\033[36m'; NC='\033[0m'
FALLOS=0

ok()   { echo -e "${VERDE}✅ $1${NC}"; }
fail() { echo -e "${ROJO}❌ $1${NC}"; FALLOS=$((FALLOS+1)); }
warn() { echo -e "${AMAR}⚠️  $1${NC}"; }
info() { echo -e "${CYAN}▸ $1${NC}"; }

echo "════════════════════════════════════════════════════════════"
echo "  VERIFICACIÓN PRE-TAG v3.0.1 — Agentes Visuales"
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

# ── 4. No hay tags v3.x previos ───────────────────────────────
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
    if ! python3 -m py_compile "$f" 2>/dev/null; then
        fail "SyntaxError en: $f"
        ERR_SINTAXIS=$((ERR_SINTAXIS+1))
    fi
done < <(find . -name "*.py" \
    -not -path "./.git/*" \
    -not -path "./.venv/*" \
    -not -path "./venv/*" \
    -not -path "./env/*" \
    -not -path "*/__pycache__/*")
[ "$ERR_SINTAXIS" -eq 0 ] && ok "Todos los .py compilan"

# ── 7. pyflakes (si está instalado) ───────────────────────────
info "7. pyflakes (errores reales, no estilo)"
if command -v pyflakes >/dev/null 2>&1; then
    PYFLAKES_OUT=$(pyflakes . 2>&1 | grep -v "__pycache__" | grep -v ".venv" || true)
    if [ -n "$PYFLAKES_OUT" ]; then
        warn "pyflakes reporta issues (revisar, no bloquean):"
        echo "$PYFLAKES_OUT" | head -30
    else
        ok "pyflakes limpio"
    fi
else
    warn "pyflakes no instalado (pip install pyflakes)"
fi

# ── 8. No hay prints de debug obvios ──────────────────────────
info "8. Prints de debug olvidados"
PRINTS=$(grep -rn "print(" --include="*.py" \
    --exclude-dir=.git --exclude-dir=__pycache__ \
    --exclude-dir=.venv --exclude-dir=venv --exclude-dir=env \
    --exclude-dir=tests --exclude-dir=tools \
    | grep -v "if __name__" | grep -v "print(f\"\[TERMINADA\]" | grep -v "file=sys.stderr" || true)
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
    --exclude-dir=.venv --exclude-dir=venv --exclude-dir=env \
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
[ -f "CHANGELOG.md" ] && ok "CHANGELOG.md" || warn "Falta CHANGELOG.md (recomendado para v3.0)"

# ── 11. Smoke Test (ProblemSolver) ────────────────────────────
info "11. Smoke test de ProblemSolver"
if python3 -c "
from core.problem_solver import ProblemSolver
from core.llm_client import obtener_llm_client_compartido
solver = ProblemSolver(obtener_llm_client_compartido())
plan = solver.resolver_problema('Genera un archivo saludo.txt con Hola Mundo', max_pasos=3)
print(f'Plan: {plan.titulo} | Pasos: {len(plan.pasos)} | Agentes: {len(plan.agentes_generados)}')
for p in plan.pasos:
    print(f'  {p.orden}. [{p.tipo_agente}] {p.nombre}')
" 2>/dev/null; then
    ok "Smoke test completado exitosamente"
else
    warn "El smoke test no pudo ejecutarse correctamente"
fi

echo ""
echo "════════════════════════════════════════════════════════════"
if [ "$FALLOS" -eq 0 ]; then
    echo -e "${VERDE}✅ LISTO PARA TAGGEAR (revisa los ⚠️  manualmente)${NC}"
    echo ""
    echo "  Siguiente paso:"
    echo "    git tag -a v3.0 -m 'Release v3.0.1: DeepSeek Harness integration'"
    echo "    git push origin v3.0"
else
    echo -e "${ROJO}❌ $FALLOS fallos bloqueantes. Corrige antes de taggear.${NC}"
fi
echo "════════════════════════════════════════════════════════════"

exit $FALLOS