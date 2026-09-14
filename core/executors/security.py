# core/executors/security.py
"""
Constantes de seguridad y validadores estáticos para los ejecutores.

Este módulo NO debe importar nada del proyecto para evitar dependencias
circulares. Solo stdlib.
"""

import os
import logging
from urllib.parse import urlparse
from typing import Any

logger = logging.getLogger(__name__)


# ============================================================
# LÍMITES DE SEGURIDAD
# ============================================================

MAX_BYTES_LECTURA_ARCHIVO = 10 * 1024 * 1024      # 10 MB
MAX_HTTP_BODY_SIZE = 50 * 1024 * 1024             # 50 MB
MAX_SHELL_COMMAND_LENGTH = 10000                  # 10k caracteres
MAX_FILE_PATH_LENGTH = 1000                       # 1k caracteres
MAX_CODIGO_LENGTH = 100000                        # 100 KB


# ============================================================
# MÉTODOS HTTP SOPORTADOS
# ============================================================

ALLOWED_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}


# ============================================================
# COMANDOS PELIGROSOS (solo advertencia)
# ============================================================

DANGEROUS_SHELL_COMMANDS = frozenset([
    'rm -rf', 'dd if=', 'mkfs', 'chmod 777', 'chown', 'sudo',
    ':(){ :|:& };:', 'mkfs', 'dd', '>/dev/sda'
])

DANGEROUS_DIRS = frozenset(['/', 'C:\\', '/home', '/root', '/etc', '/var'])


# ============================================================
# COMANDOS QUE REQUIEREN ROOT
# ============================================================

COMANDOS_PRIVILEGIADOS = frozenset({
    # Gestores de paquetes
    'apt', 'apt-get', 'aptitude', 'dpkg', 'snap', 'yum', 'dnf',
    'pacman', 'zypper', 'apk', 'rpm',

    # Servicios y sistema
    'systemctl', 'service', 'init', 'shutdown', 'reboot', 'halt',

    # Montaje y discos
    'mount', 'umount', 'fdisk', 'parted', 'mkfs', 'fsck',
    'cryptsetup', 'losetup',

    # Redes y firewall
    'iptables', 'ip6tables', 'nft', 'ufw', 'firewall-cmd',

    # Usuarios y permisos
    'useradd', 'userdel', 'usermod', 'groupadd', 'groupdel',
    'passwd', 'chpasswd', 'chsh', 'visudo',

    # Modificación de sistema
    'modprobe', 'insmod', 'rmmod', 'sysctl',

    # Otros
    'docker', 'podman',
})


# ============================================================
# VALIDADORES
# ============================================================

def validar_ruta_archivo(ruta: str) -> bool:
    """Valida una ruta de archivo."""
    if not ruta:
        return False
    if len(ruta) > MAX_FILE_PATH_LENGTH:
        return False

    normalized = os.path.normpath(ruta)
    if normalized.startswith(('..', '/', '\\')):
        return False

    basename = os.path.basename(normalized)
    if basename.startswith('.') and basename not in ('.', '..'):
        return False

    return True


def validar_url(url: str) -> bool:
    """Valida una URL HTTP/HTTPS."""
    if not url:
        return False
    if len(url) > 2000:
        return False

    try:
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https'):
            return False
        if not parsed.netloc:
            return False
        if any(c in url for c in ('\n', '\r', '\t')):
            return False
        return True
    except Exception:
        return False


def es_lista_valida(items: Any) -> bool:
    """Verifica si un valor es una lista válida."""
    return isinstance(items, list)
