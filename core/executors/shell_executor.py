# core/executors/shell_executor.py
"""Ejecutor de agentes Shell con detección de privilegios."""

import logging
import os
import platform
import shlex
import shutil
import subprocess
import time

from core.agent import Agente
from core.cancellation import CancellationToken

from .content_extractor import (
    comando_requiere_root,
    sustituir_variables,
    sustituir_variables_shell,
    variables_disponibles,
)
from .security import DANGEROUS_SHELL_COMMANDS, MAX_SHELL_COMMAND_LENGTH, validar_ruta_archivo

logger = logging.getLogger(__name__)


class ShellExecutor:
    """Ejecuta comandos shell con sustitución de variables y pkexec."""

    @staticmethod
    def actualizar_progreso(agente, progreso, mensaje=""):
        agente.progreso = min(100, max(0, progreso))
        if mensaje:
            agente.mensaje = mensaje
        if hasattr(agente, '_bridge') and agente._bridge is not None:
            try:
                agente._bridge.agente_actualizado.emit(agente.id)
            except Exception:
                pass

    @classmethod
    def ejecutar(
        cls,
        agente: Agente,
        contexto: dict,
        cancellation_token: CancellationToken | None = None
    ) -> tuple[bool, str, dict]:
        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {'error': 'cancelled'}

        cls.actualizar_progreso(agente, 20, "Preparando comando shell...")

        variables = variables_disponibles(agente, contexto)
        # Citar los valores sustituidos para evitar inyección de comandos.
        comando = sustituir_variables_shell(agente.comando_shell, variables)
        working_dir = sustituir_variables(agente.working_dir, variables)

        if not comando:
            cls.actualizar_progreso(agente, 100, "Comando vacío")
            return False, "No hay comando shell definido", {}

        if len(comando) > MAX_SHELL_COMMAND_LENGTH:
            cls.actualizar_progreso(agente, 100, "Comando demasiado largo")
            return False, (
                f"Comando demasiado largo (máx {MAX_SHELL_COMMAND_LENGTH} caracteres)"
            ), {}

        for dangerous in DANGEROUS_SHELL_COMMANDS:
            if dangerous in comando:
                logger.warning(
                    f"Comando shell contiene operación potencialmente peligrosa: "
                    f"{dangerous}"
                )

        # ══════════════════════════════════════════════════════════
        # Detección de privilegios + pkexec
        # ══════════════════════════════════════════════════════════
        comando_original = comando
        comando_ejecutable = comando
        usar_pkexec = False

        if comando_requiere_root(comando):
            pkexec_path = shutil.which('pkexec')
            if pkexec_path:
                comando_ejecutable = f"{pkexec_path} /bin/sh -c {shlex.quote(comando)}"
                usar_pkexec = True
                logger.info(
                    f"[{agente.nombre}] Comando requiere root → aplicando pkexec: "
                    f"{comando[:60]}..."
                )
                cls.actualizar_progreso(agente, 35, "🔒 Solicitando privilegios (pkexec)...")
            else:
                mensaje_error = (
                    f"🔒 El comando requiere privilegios de root, pero 'pkexec' "
                    f"no está instalado.\n\n"
                    f"Comando: {comando[:120]}\n\n"
                    f"Opciones:\n"
                    f"  1. Instala policykit:\n"
                    f"       sudo apt install policykit-1   (Debian/Ubuntu)\n"
                    f"       sudo dnf install polkit        (Fedora)\n"
                    f"  2. Edita el agente y antepón 'sudo' manualmente al comando.\n"
                    f"  3. Configura NOPASSWD en /etc/sudoers.d/ para este comando."
                )
                logger.warning(f"[{agente.nombre}] {mensaje_error}")
                cls.actualizar_progreso(agente, 100, "❌ Falta pkexec")
                return False, mensaje_error, {
                    'error': 'requires_root_no_pkexec',
                    'comando': comando[:200],
                    'requiere_root': True,
                    'pkexec_disponible': False,
                }

        cls.actualizar_progreso(agente, 50, f"Ejecutando: {comando[:50]}...")

        try:
            cwd = None
            if working_dir and validar_ruta_archivo(working_dir):
                if os.path.exists(working_dir) and os.path.isdir(working_dir):
                    cwd = working_dir

            env = os.environ.copy()
            safe_env_vars = ['PATH', 'HOME', 'USER', 'LANG', 'LC_ALL', 'TMPDIR']
            if platform.system() == "Windows":
                safe_env_vars.extend(['SYSTEMROOT', 'TEMP', 'APPDATA'])

            clean_env = {k: env.get(k, '') for k in safe_env_vars if k in env}
            clean_env['PYTHONUNBUFFERED'] = '1'

            timeout = getattr(agente, 'timeout_shell', 30)
            timeout_efectivo = timeout + 30 if usar_pkexec else timeout

            args = comando_ejecutable
            usar_shell = True

            proceso = subprocess.Popen(
                args,
                shell=usar_shell,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                env=clean_env,
                encoding='utf-8',
                errors='replace'
            )

            def cancelar_proceso(token):
                nonlocal proceso
                try:
                    if proceso and proceso.poll() is None:
                        proceso.terminate()
                        time.sleep(0.3)
                        if proceso.poll() is None:
                            proceso.kill()
                        logger.info(f"Proceso shell cancelado: {comando[:50]}...")
                except Exception as e:
                    logger.warning(f"Error cancelando proceso shell: {e}")

            if cancellation_token:
                cancellation_token.agregar_callback(cancelar_proceso)

            try:
                # communicate() drena stdout/stderr y espera hasta el timeout.
                # Se llama UNA vez (repetirlo en bucle desde varios hilos
                # provoca SIGSEGV en CPython 3.13); la cancelación la fuerza
                # el callback terminando el proceso.
                try:
                    stdout, stderr = proceso.communicate(timeout=timeout_efectivo)
                except subprocess.TimeoutExpired:
                    cancelar_proceso(cancellation_token)
                    try:
                        stdout, stderr = proceso.communicate(timeout=2)
                    except subprocess.TimeoutExpired:
                        try:
                            proceso.kill()
                        except Exception:
                            pass
                        stdout, stderr = "", ""
                    return False, f"Timeout ({timeout_efectivo}s)", {
                        'error': 'timeout', 'comando': comando[:100]
                    }

                if cancellation_token and cancellation_token.esta_cancelado():
                    return False, "Cancelado por usuario", {
                        'error': 'cancelled', 'comando': comando[:100]
                    }

                codigo = proceso.returncode
            finally:
                if cancellation_token:
                    cancellation_token.eliminar_callback(cancelar_proceso)

            cls.actualizar_progreso(agente, 90, "Procesando resultado...")

            stdout = stdout.strip()
            stderr = stderr.strip()
            if len(stdout) > 10000:
                stdout = stdout[:10000] + "\n... (truncado)"
            if len(stderr) > 10000:
                stderr = stderr[:10000] + "\n... (truncado)"

            cls.actualizar_progreso(agente, 100, "Comando completado")

            resultado_dict = {
                "codigo": codigo,
                "returncode": codigo,
                "exit_code": codigo,
                "codigo_salida": codigo,
                "stdout": stdout,
                "stderr": stderr,
                "comando_original": comando_original,
                "usado_pkexec": usar_pkexec,
            }

            if codigo != 0:
                error_lower = (stdout + " " + stderr).lower()
                patrones_permisos = (
                    "permission denied", "permiso denegado",
                    "operation not permitted", "are you root",
                    "must be root", "access denied",
                    "no se pudo abrir el fichero de bloqueo",
                )
                if any(p in error_lower for p in patrones_permisos):
                    resultado_dict['error_permisos'] = True
                    resultado_dict['sugerencia'] = (
                        "El comando falló por permisos. Verifica que:\n"
                        "  1. 'pkexec' está correctamente instalado y configurado\n"
                        "  2. Tu usuario está en el grupo adecuado\n"
                        "  3. El comando no está bloqueado por Polkit"
                    )

            if codigo == 0:
                mensaje = f"Comando ejecutado correctamente (código: {codigo})"
                if usar_pkexec:
                    mensaje += " [con pkexec]"
                if stdout:
                    mensaje += f"\nSalida: {stdout[:200]}..."
                return True, mensaje, resultado_dict
            else:
                mensaje = f"Comando falló (código: {codigo})"
                if usar_pkexec:
                    mensaje += " [con pkexec]"
                if stderr:
                    mensaje += f"\nError: {stderr[:200]}..."
                elif stdout:
                    mensaje += f"\nSalida: {stdout[:200]}..."
                return False, mensaje, resultado_dict

        except subprocess.TimeoutExpired:
            cls.actualizar_progreso(agente, 100, "Timeout")
            return False, f"Comando excedió el tiempo límite ({timeout}s)", {}
        except subprocess.SubprocessError as e:
            cls.actualizar_progreso(agente, 100, f"Error: {str(e)[:50]}")
            return False, f"Error en subproceso: {str(e)}", {}
        except Exception as e:
            cls.actualizar_progreso(agente, 100, f"Error: {str(e)[:50]}")
            return False, f"Error en comando shell: {str(e)}", {}
