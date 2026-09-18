# core/text_parser.py
"""
Parser de texto para creación de agentes desde DSL estructurado.

Formato soportado:
    @agente Nombre
    tipo: Python
    descripcion: ...
    dependencias: A, B
    codigo: |
        línea 1
        línea 2

Soporta múltiples agentes en un solo texto.
"""
import logging
import re
from dataclasses import dataclass, field

from core.agent import Agente, TipoAgente

logger = logging.getLogger(__name__)


# ============================================================
# EXCEPCIONES
# ============================================================
class TextParseError(Exception):
    """Error de parseo del texto."""
    def __init__(self, mensaje: str, linea: int = 0, columna: int = 0):
        self.linea = linea
        self.columna = columna
        super().__init__(f"Línea {linea}: {mensaje}")


class TextValidationError(TextParseError):
    """Error de validación semántica."""
    pass


# ============================================================
# RESULTADO DEL PARSEO
# ============================================================
@dataclass
class ParseResult:
    """Resultado del parseo de texto."""
    agentes: list[dict] = field(default_factory=list)
    errores: list[str] = field(default_factory=list)
    advertencias: list[str] = field(default_factory=list)
    
    @property
    def exito(self) -> bool:
        return len(self.errores) == 0 and len(self.agentes) > 0
    
    @property
    def total_agentes(self) -> int:
        return len(self.agentes)


# ============================================================
# PARSER PRINCIPAL
# ============================================================
class AgentTextParser:
    """
    Parser de DSL para definición de agentes.
    
    Uso:
        parser = AgentTextParser()
        resultado = parser.parse(texto)
        if resultado.exito:
            agentes = [Agente.from_dict(d) for d in resultado.agentes]
    """
    
    # Patrones regex
    RE_AGENT_START = re.compile(r'^@agente\s+(.+)$', re.IGNORECASE)
    RE_KEY_VALUE = re.compile(r'^(\w[\w_]*)\s*:\s*(.*)$')
    RE_MULTILINE_START = re.compile(r'^(\w[\w_]*)\s*:\s*\|$')
    RE_COMMENT = re.compile(r'^\s*#.*$')
    
    # Mapeo de alias de tipo
    TYPE_ALIASES = {
        'python': 'Python', 'py': 'Python', 'script': 'Python',
        'shell': 'Shell', 'sh': 'Shell', 'bash': 'Shell', 'cmd': 'Shell',
        'http': 'HTTP', 'api': 'HTTP', 'rest': 'HTTP', 'web': 'HTTP',
        'llm': 'LLM', 'ia': 'LLM', 'ai': 'LLM', 'chat': 'LLM',
        'file': 'File', 'archivo': 'File', 'fichero': 'File',
        'loop': 'Loop', 'bucle': 'Loop', 'iterador': 'Loop',
    }
    
    # Mapeo de alias de campos
    FIELD_ALIASES = {
        'tipo': 'tipo', 'type': 'tipo',
        'descripcion': 'descripcion', 'desc': 'descripcion', 'description': 'descripcion',
        'dependencias': 'dependencias_nombres', 'deps': 'dependencias_nombres',
        'depende_de': 'dependencias_nombres', 'requires': 'dependencias_nombres',
        'codigo': 'codigo_python', 'code': 'codigo_python', 'script': 'codigo_python',
        'comando': 'comando_shell', 'command': 'comando_shell', 'cmd': 'comando_shell',
        'url': 'url_http', 'uri': 'url_http',
        'metodo': 'metodo_http', 'method': 'metodo_http',
        'prompt': 'prompt_llm', 'instruccion': 'prompt_llm',
        'modelo': 'modelo_llm', 'model': 'modelo_llm',
        'temperatura': 'temperatura_llm', 'temp': 'temperatura_llm',
        'fuente': 'fuente_items', 'fuente_items': 'fuente_items',
        'timeout': '_timeout_generico',
        'reintentos': 'max_reintentos', 'retries': 'max_reintentos',
    }
    
    def parse(self, texto: str) -> ParseResult:
        """
        Parsea el texto completo y retorna el resultado.
        
        Args:
            texto: Texto DSL a parsear
            
        Returns:
            ParseResult con agentes parseados y errores/advertencias
        """
        resultado = ParseResult()
        
        if not texto or not texto.strip():
            resultado.errores.append("El texto está vacío")
            return resultado
        
        lineas = texto.split('\n')
        agentes_raw = self._separar_bloques(lineas, resultado)
        
        for bloque in agentes_raw:
            agente_dict = self._parsear_bloque(bloque, resultado)
            if agente_dict:
                resultado.agentes.append(agente_dict)
        
        # Validación cruzada (dependencias existen, nombres únicos)
        self._validar_cruzado(resultado)
        
        return resultado
    
    # ========================================================
    # FASE 1: Separar bloques @agente
    # ========================================================
    def _separar_bloques(
        self, lineas: list[str], resultado: ParseResult
    ) -> list[dict]:
        """Separa el texto en bloques de agentes."""
        bloques = []
        bloque_actual = None

        for i, linea in enumerate(lineas, 1):
            # Ignorar comentarios
            if self.RE_COMMENT.match(linea):
                continue
            
            # ¿Inicio de nuevo agente?
            match = self.RE_AGENT_START.match(linea.strip())
            if match:
                if bloque_actual:
                    bloques.append(bloque_actual)
                nombre = match.group(1).strip()
                bloque_actual = {
                    '_nombre': nombre,
                    '_lineas': [],
                    '_linea_inicio': i
                }
                continue
            
            # Si estamos dentro de un bloque, acumular
            if bloque_actual is not None:
                bloque_actual['_lineas'].append((i, linea))
        
        # Último bloque
        if bloque_actual:
            bloques.append(bloque_actual)
        
        if not bloques:
            resultado.errores.append(
                "No se encontró ningún bloque '@agente Nombre'. "
                "Formato esperado:\n  @agente MiAgente\n  tipo: Python\n  codigo: |"
            )
        
        return bloques
    
    # ========================================================
    # FASE 2: Parsear un bloque individual
    # ========================================================
    def _parsear_bloque(
        self, bloque: dict, resultado: ParseResult
    ) -> dict | None:
        """Parsea un bloque de agente individual."""
        nombre = bloque['_nombre']
        lineas = bloque['_lineas']
        linea_inicio = bloque['_linea_inicio']
        
        agente_dict = {'nombre': nombre}
        i = 0
        
        while i < len(lineas):
            linea_num, linea = lineas[i]
            stripped = linea.strip()
            
            # Saltar líneas vacías
            if not stripped:
                i += 1
                continue
            
            # ¿Es un campo multilínea?
            match_multi = self.RE_MULTILINE_START.match(stripped)
            if match_multi:
                campo = match_multi.group(1)
                contenido, i = self._leer_multilinea(lineas, i + 1)
                self._asignar_campo(agente_dict, campo, contenido, resultado, linea_num)
                i += 1
                continue
            
            # ¿Es un campo clave: valor?
            match_kv = self.RE_KEY_VALUE.match(stripped)
            if match_kv:
                campo = match_kv.group(1)
                valor = match_kv.group(2).strip()
                self._asignar_campo(agente_dict, campo, valor, resultado, linea_num)
                i += 1
                continue
            
            # Línea no reconocida
            resultado.advertencias.append(
                f"Línea {linea_num}: '{stripped[:50]}' no reconocido (ignorado)"
            )
            i += 1
        
        # Verificar campos obligatorios
        if 'tipo' not in agente_dict:
            resultado.errores.append(
                f"Agente '{nombre}' (línea {linea_inicio}): falta campo 'tipo'"
            )
            return None
        
        return agente_dict
    
    # ========================================================
    # FASE 3: Leer bloque multilínea
    # ========================================================
    def _leer_multilinea(
        self, lineas: list[tuple[int, str]], start: int
    ) -> tuple[str, int]:
        """Lee un bloque multilínea (indentado)."""
        contenido = []
        i = start
        
        while i < len(lineas):
            linea_num, linea = lineas[i]
            
            # Si la línea está vacía o tiene indentación >= 4
            if not linea.strip():
                contenido.append('')
                i += 1
                continue
            
            # Verificar indentación (mínimo 4 espacios o 1 tab)
            indent = len(linea) - len(linea.lstrip())
            if indent >= 4 or linea.startswith('\t'):
                # Quitar 4 espacios de indentación base
                contenido.append(linea[4:] if linea.startswith('    ') else linea[1:])
                i += 1
            else:
                # Fin del bloque multilínea
                break
        
        return '\n'.join(contenido), i - 1
    
    # ========================================================
    # FASE 4: Asignar campo al diccionario
    # ========================================================
    def _asignar_campo(
        self,
        agente_dict: dict,
        campo_raw: str,
        valor: str,
        resultado: ParseResult,
        linea: int
    ):
        """Asigna un campo al diccionario del agente."""
        campo_lower = campo_raw.lower().strip()
        campo_real = self.FIELD_ALIASES.get(campo_lower)
        
        if not campo_real:
            resultado.advertencias.append(
                f"Línea {linea}: campo '{campo_raw}' desconocido"
            )
            return
        
        # Procesar valor según campo
        if campo_real == 'tipo':
            tipo_resuelto = self._resolver_tipo(valor, resultado, linea)
            if tipo_resuelto:
                agente_dict['tipo'] = tipo_resuelto
        
        elif campo_real == 'dependencias_nombres':
            deps = [d.strip() for d in valor.split(',') if d.strip()]
            agente_dict['dependencias_nombres'] = deps
        
        elif campo_real == '_timeout_generico':
            try:
                agente_dict['_timeout_generico'] = int(valor)
            except ValueError:
                resultado.advertencias.append(
                    f"Línea {linea}: timeout '{valor}' no es un número"
                )
        
        elif campo_real == 'temperatura_llm':
            try:
                agente_dict['temperatura_llm'] = float(valor)
            except ValueError:
                resultado.advertencias.append(
                    f"Línea {linea}: temperatura '{valor}' no es un número"
                )
        
        elif campo_real == 'max_reintentos':
            try:
                agente_dict['max_reintentos'] = int(valor)
            except ValueError:
                pass
        
        else:
            agente_dict[campo_real] = valor
    
    # ========================================================
    # UTILIDADES
    # ========================================================
    def _resolver_tipo(
        self, valor: str, resultado: ParseResult, linea: int
    ) -> str | None:
        """Resuelve el tipo de agente desde un string."""
        valor_lower = valor.lower().strip()
        tipo = self.TYPE_ALIASES.get(valor_lower)
        
        if not tipo:
            # Intentar coincidencia directa con TipoAgente
            try:
                TipoAgente(valor)
                tipo = valor
            except ValueError:
                resultado.errores.append(
                    f"Línea {linea}: tipo '{valor}' no válido. "
                    f"Opciones: Python, Shell, HTTP, LLM, File, Loop"
                )
                return None
        
        return tipo
    
    def _validar_cruzado(self, resultado: ParseResult):
        """Valida referencias cruzadas entre agentes."""
        nombres = {a.get('nombre') for a in resultado.agentes}
        
        # Nombres duplicados
        vistos = set()
        for agente in resultado.agentes:
            nombre = agente.get('nombre', '')
            if nombre in vistos:
                resultado.errores.append(f"Nombre duplicado: '{nombre}'")
            vistos.add(nombre)
        
        # Dependencias que no existen
        for agente in resultado.agentes:
            for dep in agente.get('dependencias_nombres', []):
                if dep not in nombres:
                    resultado.advertencias.append(
                        f"Agente '{agente.get('nombre')}': dependencia "
                        f"'{dep}' no definida en este texto"
                    )
    
    # ========================================================
    # MÉTODO DE CONVENIENCIA
    # ========================================================
    def parse_to_agents(self, texto: str) -> tuple[list[Agente], ParseResult]:
        """
        Parsea texto y retorna agentes listos para usar.
        
        Returns:
            Tuple[List[Agente], ParseResult]
        """
        resultado = self.parse(texto)
        agentes = []
        
        if resultado.exito:
            for agente_dict in resultado.agentes:
                # Aplicar timeout genérico
                timeout = agente_dict.pop('_timeout_generico', None)
                tipo = agente_dict.get('tipo', 'Python')
                
                if timeout:
                    timeout_field = {
                        'Python': 'timeout_python',
                        'Shell': 'timeout_shell',
                        'HTTP': 'timeout_http',
                        'Loop': 'timeout_loop',
                    }.get(tipo)
                    if timeout_field:
                        agente_dict[timeout_field] = timeout
                
                try:
                    agente = Agente.from_dict(agente_dict)
                    agentes.append(agente)
                except Exception as e:
                    resultado.errores.append(
                        f"Error creando agente '{agente_dict.get('nombre')}': {e}"
                    )
        
        return agentes, resultado
