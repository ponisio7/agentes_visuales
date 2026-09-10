#!/bin/bash
# ============================================================
# Lanzador de Agentes Visuales
# ============================================================
#
# Garantiza que la aplicación tenga el mismo entorno que
# cuando se ejecuta desde una terminal interactiva.
#
# Uso:
#   ./launch.sh                    # Arranca la GUI
#   ./launch.sh --check-env        # Diagnóstico de IA
#   ./launch.sh --check-env --timeout 15
#
# ============================================================

set -e  # Abortar ante cualquier error

# ── Rutas del proyecto ──
PROJECT_DIR="/home/christian/proyectosPython/agentes_visuales"
VENV_DIR="$PROJECT_DIR/.venv"

# ── 1. Cargar entorno del usuario ──
# Se cargan ambos porque algunos usuarios definen variables
# en .profile y otros en .bashrc. Silenciamos errores por si
# el shell no es interactivo y hace 'return' temprano.
[ -f "$HOME/.profile" ] && source "$HOME/.profile" 2>/dev/null || true
[ -f "$HOME/.bashrc" ]  && source "$HOME/.bashrc"  2>/dev/null || true

# Red de seguridad: archivo dedicado a secretos de la app
if [ -f "$HOME/.config/agentes_visuales/env" ]; then
    set -a
    source "$HOME/.config/agentes_visuales/env" 2>/dev/null || true
    set +a
fi

# ── 2. Configurar entorno virtual sin 'activate' ──
# Esto deja el venv funcionalmente activo (VIRTUAL_ENV + PATH)
# sin ejecutar el script 'activate', que a veces interfiere
# con scripts no interactivos.
export VIRTUAL_ENV="$VENV_DIR"
export PATH="$VENV_DIR/bin:$PATH"

# ── 3. Ir al directorio del proyecto ──
cd "$PROJECT_DIR" || {
    echo "ERROR: no se pudo entrar a $PROJECT_DIR" >&2
    exit 1
}

# ── 4. (Opcional) Proxy ──
# Descomenta y ajusta si tu red requiere proxy:
# export HTTP_PROXY="http://proxy.tu-red:puerto"
# export HTTPS_PROXY="http://proxy.tu-red:puerto"
# export NO_PROXY="localhost,127.0.0.1"

# ── 5. Lanzar la app ──
exec "$VENV_DIR/bin/python" "$PROJECT_DIR/main.py" "$@"
