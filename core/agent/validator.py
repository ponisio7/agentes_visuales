# core/agent/validator.py
"""
Validadores para la configuración de agentes.
"""

import logging
import re
import os
from typing import List, Set, Tuple

logger = logging.getLogger(__name__)


# ============================================================
# VALIDADORES
# ============================================================

class AgenteValidator:
    """Valida la configuración de agentes."""
    
    @staticmethod
    def validar_nombre(nombre: str) -> Tuple[bool, str]:
        """Valida el nombre del agente."""
        if not nombre or not nombre.strip():
            return False, "El nombre es obligatorio"
        
        nombre = nombre.strip()
        if len(nombre) < 2:
            return False, "El nombre debe tener al menos 2 caracteres"
        if len(nombre) > 100:
            return False, "El nombre no puede tener más de 100 caracteres"
        
        # Caracteres permitidos: letras, números, guiones, guiones bajos, espacios
        if not re.match(r'^[A-Za-z0-9_\-\s]+$', nombre):
            return False, "El nombre solo puede contener letras, números, guiones y espacios"
        
        return True, ""
    
    @staticmethod
    def validar_codigo(codigo: str, max_length: int = 100000) -> Tuple[bool, str]:
        """Valida código Python."""
        if not codigo:
            return True, ""  # Opcional
        
        if len(codigo) > max_length:
            return False, f"El código excede el límite de {max_length} caracteres"
        
        # Verificar indentación básica
        lines = codigo.split('\n')
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            
            # Verificar que la indentación sea consistente
            if line.startswith(' '):
                spaces = len(line) - len(line.lstrip(' '))
                if spaces % 4 != 0:
                    return False, "La indentación debe ser múltiplo de 4 espacios"
        
        return True, ""
    
    @staticmethod
    def validar_url(url: str) -> Tuple[bool, str]:
        """Valida una URL."""
        if not url or not url.strip():
            return False, "La URL es obligatoria"
        
        url = url.strip()
        if not url.startswith(('http://', 'https://')):
            return False, "La URL debe comenzar con http:// o https://"
        
        if ' ' in url:
            return False, "La URL no puede contener espacios"
        
        # Verificar caracteres no permitidos
        for c in url:
            if ord(c) < 32:
                return False, "La URL contiene caracteres de control"
        
        return True, ""
    
    @staticmethod
    def validar_ruta(ruta: str) -> Tuple[bool, str]:
        """Valida una ruta de archivo."""
        if not ruta or not ruta.strip():
            return False, "La ruta es obligatoria"
        
        ruta = ruta.strip()
        if len(ruta) > 1000:
            return False, "La ruta es demasiado larga"
        
        # Verificar path traversal
        normalized = os.path.normpath(ruta)
        if normalized.startswith('..') or normalized.startswith('/') or normalized.startswith('\\'):
            return False, "La ruta contiene intento de path traversal"
        
        # Verificar caracteres no permitidos
        for c in ruta:
            if ord(c) < 32:
                return False, "La ruta contiene caracteres de control"
        
        return True, ""
    
    @staticmethod
    def validar_dependencias(dependencias: List[str], agentes_existentes: Set[str] = None) -> Tuple[bool, str]:
        """Valida dependencias."""
        if not dependencias:
            return True, ""
        
        for dep in dependencias:
            if not dep or not dep.strip():
                return False, "Dependencia vacía"
            if len(dep) > 100:
                return False, f"Dependencia '{dep}' es demasiado larga"
        
        if agentes_existentes is not None:
            for dep in dependencias:
                if dep not in agentes_existentes:
                    return False, f"Dependencia '{dep}' no existe"
        
        return True, ""