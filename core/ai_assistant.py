# core/ai_assistant.py
"""
Asistente IA para ayudar en la creación y mejora de agentes.
Proporciona generación inteligente de código, prompts y configuraciones,
así como análisis reactivo del formulario en tiempo real.
"""

import json
import logging
import re
from typing import Dict, Optional, Any, List

from core.agent import TipoAgente
from core.utils import extraer_json_de_llm  # ✅ NUEVO: importar utilidad

logger = logging.getLogger(__name__)


class AIAssistant:
    """Asistente IA para creación de agentes."""

    def __init__(self, llm_client):
        """
        Inicializa el asistente.
        Args:
            llm_client: Instancia de LLMClient para hacer consultas
        """
        self.client = llm_client
        self.logger = logging.getLogger(f"{__name__}.AIAssistant")

    @property
    def disponible(self) -> bool:
        """Verifica si el asistente está disponible."""
        return self.client is not None and self.client.disponible

    # ============================================================
    # ANÁLISIS REACTIVO DEL FORMULARIO
    # ============================================================
    def analizar_formulario(self, contexto: dict) -> dict:
        """
        Analiza el estado actual del formulario y sugiere valores
        para campos vacíos o incompletos. Versión reactiva.
        
        Args:
            contexto: {
                'nombre': str,
                'tipo': str,
                'descripcion': str,
                'dependencias': list[str],
                'contenido': str,
                'campos_especificos': dict,
                'agentes_existentes': list[str],
                'modo': 'completar' | 'mejorar'
            }
        
        Returns:
            dict con solo los campos que se deben actualizar + 'explicacion'
        """
        if not self.disponible:
            raise ValueError("Asistente IA no disponible")

        tipo = contexto.get('tipo', 'Python')
        modo = contexto.get('modo', 'completar')
        agentes_existentes = contexto.get('agentes_existentes', [])

        deps_str = ', '.join(agentes_existentes) if agentes_existentes else 'ninguno'

        system_prompt = f"""Eres un experto en configuración de agentes del sistema Agentes Visuales.
Tu trabajo es analizar el formulario actual y devolver SOLO un objeto JSON (sin markdown)
con las sugerencias necesarias.

Tipos válidos: Python, Shell, HTTP, LLM, File, Loop

Campos que puedes devolver (solo los que deban cambiarse):
{{
  "nombre": "...",
  "tipo": "...",
  "descripcion": "...",
  "dependencias": ["..."],
  "codigo_python": "...",
  "comando_shell": "...",
  "url_http": "...",
  "metodo_http": "GET|POST|PUT|DELETE",
  "headers_http": {{}},
  "body_http": "",
  "prompt_llm": "...",
  "modelo_llm": "deepseek-v4-pro",
  "temperatura_llm": 0.7,
  "max_tokens_llm": 1000,
  "operacion_file": "leer|escribir|copiar|mover|eliminar",
  "archivo_origen": "...",
  "archivo_destino": "...",
  "fuente_items": "Dependencia.clave",
  "codigo_por_item": "...",
  "timeout_python": 30,
  "timeout_http": 15,
  "timeout_shell": 10,
  "timeout_loop": 300,
  "max_iteraciones": 100,
  "continuar_en_error": false,
  "max_reintentos": 2,
  "explicacion": "breve explicación de lo que has sugerido"
}}

Reglas estrictas:
- Responde ÚNICAMENTE con el JSON.
- Solo incluye campos que realmente necesiten ser rellenados o mejorados.
- Si el modo es "completar", prioriza campos vacíos.
- Si el modo es "mejorar", puedes reescribir contenido existente para hacerlo más robusto.
- El código Python siempre debe asignar la variable `resultado`.
- Para HTTP usa URLs reales y públicas cuando sea posible (ej: GitHub API, Open-Meteo, JSONPlaceholder).
- Para dependencias solo usa nombres de esta lista: {deps_str}.
- Sé práctico y conciso.
- Responde en español."""

        contenido_principal = contexto.get('contenido', '')
        if not contenido_principal:
            contenido_principal = '(vacío)'

        campos_especificos_str = json.dumps(
            contexto.get('campos_especificos', {}),
            ensure_ascii=False,
            indent=2
        )

        user_prompt = f"""Modo: {modo}
Estado actual del formulario:
- Nombre: {contexto.get('nombre') or '(vacío)'}
- Tipo: {tipo}
- Descripción: {contexto.get('descripcion') or '(vacía)'}
- Dependencias: {contexto.get('dependencias') or []}
- Contenido principal: {contenido_principal}
- Campos específicos: {campos_especificos_str}

Genera las sugerencias necesarias para completar o mejorar este agente."""

        try:
            respuesta = self.client.chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                temperature=0.25,
                max_tokens=1500
            )
            
            # ✅ USAR LA FUNCIÓN UNIFICADA
            data = extraer_json_de_llm(respuesta)
            
            if data is None:
                self.logger.warning("No se pudo extraer JSON de la respuesta")
                return {}

            # Validación mínima de tipo
            if 'tipo' in data and data['tipo'] not in {
                'Python', 'Shell', 'HTTP', 'LLM', 'File', 'Loop'
            }:
                data.pop('tipo', None)

            # Validar dependencias contra agentes existentes
            if 'dependencias' in data and agentes_existentes:
                data['dependencias'] = [
                    d for d in data['dependencias']
                    if d in agentes_existentes
                ]

            return data

        except Exception as e:
            self.logger.warning(f"Error en análisis reactivo: {e}")
            return {}

    # ============================================================
    # GENERACIÓN COMPLETA DE AGENTE
    # ============================================================
    def generar_agente_completo(self, descripcion: str) -> Dict[str, Any]:
        """
        Genera un agente completo desde una descripción en lenguaje natural.
        Args:
            descripcion: Descripción de lo que debe hacer el agente
        Returns:
            Dict con todos los campos del agente
        """
        if not self.disponible:
            raise ValueError("Asistente IA no disponible")

        system_prompt = """Eres un asistente experto en crear agentes automatizados.
El usuario te describe lo que quiere que haga un agente, y tú debes generar TODA la configuración necesaria.

Debes responder ÚNICAMENTE con un objeto JSON válido (sin markdown, sin explicaciones) con esta estructura:

{
  "nombre": "NombreDelAgente",
  "tipo": "Python|Shell|HTTP|LLM|File|Loop",
  "descripcion": "Descripción breve del agente",
  "contenido": "Código/prompt/comando/url según el tipo",
  "dependencias_sugeridas": ["nombre1", "nombre2"],
  "configuracion_avanzada": {
    // Campos específicos del tipo
  },
  "explicacion": "Explicación breve de por qué elegiste esta configuración"
}

Reglas según tipo:
- Python: 'contenido' es código Python. Debe asignar 'resultado' al final.
- Shell: 'contenido' es un comando de terminal.
- HTTP: 'contenido' es la URL. Incluye 'metodo', 'headers' (JSON string), 'body' (JSON string) en configuracion_avanzada.
- LLM: 'contenido' es el prompt. Incluye 'modelo', 'temperatura', 'max_tokens' en configuracion_avanzada.
- File: 'contenido' puede estar vacío. Incluye 'operacion', 'archivo_origen', 'archivo_destino' en configuracion_avanzada.
- Loop: 'contenido' es el código por item. Incluye 'fuente_items' en formato 'Dependencia.clave'.

Sé práctico y genera código/configuración que funcione de verdad.
Usa variables como {contexto} cuando sea apropiado."""

        user_prompt = f"Genera un agente para: {descripcion}"

        try:
            respuesta = self.client.chat(
                prompt=user_prompt,
                system_prompt=system_prompt,
                temperature=0.3,
                max_tokens=2000
            )

            # ✅ USAR LA FUNCIÓN UNIFICADA
            resultado = extraer_json_de_llm(respuesta)

            if resultado is None:
                raise ValueError("La IA no generó un JSON válido")

            if 'nombre' not in resultado or 'tipo' not in resultado:
                raise ValueError("La IA no generó los campos mínimos requeridos")

            self.logger.info(f"Agente generado: {resultado.get('nombre')} ({resultado.get('tipo')})")
            return resultado

        except Exception as e:
            self.logger.error(f"Error generando agente: {e}")
            raise

    # ============================================================
    # GENERACIÓN DE CONTENIDO ESPECÍFICO
    # ============================================================
    def generar_contenido(
        self,
        tipo: TipoAgente,
        descripcion: str,
        contexto_agente: Optional[Dict] = None
    ) -> str:
        """
        Genera contenido específico (código, prompt, comando) según el tipo.
        Args:
            tipo: Tipo de agente
            descripcion: Descripción de lo que debe hacer
            contexto_agente: Contexto del agente actual (nombre, dependencias, etc.)
        Returns:
            str: Contenido generado
        """
        if not self.disponible:
            raise ValueError("Asistente IA no disponible")

        prompts = {
            TipoAgente.PYTHON: self._prompt_python,
            TipoAgente.SHELL: self._prompt_shell,
            TipoAgente.HTTP: self._prompt_http,
            TipoAgente.LLM: self._prompt_llm,
            TipoAgente.FILE: self._prompt_file,
            TipoAgente.LOOP: self._prompt_loop,
        }

        system_prompt = prompts.get(tipo, self._prompt_generico)(contexto_agente)

        try:
            respuesta = self.client.chat(
                prompt=descripcion,
                system_prompt=system_prompt,
                temperature=0.3,
                max_tokens=1500
            )

            respuesta = self._limpiar_codigo(respuesta)
            return respuesta

        except Exception as e:
            self.logger.error(f"Error generando contenido: {e}")
            raise

    # ============================================================
    # MEJORA DE CONTENIDO EXISTENTE
    # ============================================================
    def mejorar_contenido(
        self,
        tipo: TipoAgente,
        contenido_actual: str,
        instruccion: str = ""
    ) -> str:
        """
        Mejora o modifica contenido existente según una instrucción.
        Args:
            tipo: Tipo de agente
            contenido_actual: Contenido actual
            instruccion: Qué mejorar (vacío = mejorar general)
        Returns:
            str: Contenido mejorado
        """
        if not self.disponible:
            raise ValueError("Asistente IA no disponible")

        if not instruccion:
            instruccion = "Mejora este código: optimiza, añade manejo de errores, mejora la legibilidad"

        system_prompt = f"""Eres un experto en {tipo.value}.
El usuario te pide que mejores/modifiques el siguiente código/configuración.

Instrucción: {instruccion}

Reglas:
- Devuelve SOLO el código/configuración mejorada, sin explicaciones
- Mantén la funcionalidad original
- Mejora la calidad, legibilidad y robustez
- No añadas markdown (```) ni comentarios explicativos fuera del código"""

        try:
            respuesta = self.client.chat(
                prompt=contenido_actual,
                system_prompt=system_prompt,
                temperature=0.2,
                max_tokens=1500
            )

            return self._limpiar_codigo(respuesta)

        except Exception as e:
            self.logger.error(f"Error mejorando contenido: {e}")
            raise

    # ============================================================
    # EXPLICACIÓN DE CONTENIDO
    # ============================================================
    def explicar_contenido(self, tipo: TipoAgente, contenido: str) -> str:
        """
        Explica qué hace un código/configuración.
        Args:
            tipo: Tipo de agente
            contenido: Contenido a explicar
        Returns:
            str: Explicación
        """
        if not self.disponible:
            raise ValueError("Asistente IA no disponible")

        system_prompt = f"""Eres un experto en {tipo.value}.
Explica de forma clara y concisa qué hace el siguiente código/configuración.

Reglas:
- Usa español
- Sé conciso (máximo 150 palabras)
- Destaca los puntos clave
- Usa emojis para hacer la explicación más visual
- No repitas el código"""

        try:
            return self.client.chat(
                prompt=contenido,
                system_prompt=system_prompt,
                temperature=0.3,
                max_tokens=500
            )
        except Exception as e:
            self.logger.error(f"Error explicando contenido: {e}")
            raise

    # ============================================================
    # SUGERENCIAS INTELIGENTES
    # ============================================================
    def sugerir_mejoras(self, agente_config: Dict) -> List[str]:
        """
        Sugiere mejoras para un agente basado en su configuración actual.
        Args:
            agente_config: Configuración actual del agente
        Returns:
            List[str]: Lista de sugerencias
        """
        if not self.disponible:
            return []

        system_prompt = """Eres un experto en diseño de agentes automatizados.
Analiza la configuración de un agente y sugiere 3-5 mejoras concretas y accionables.

Responde SOLO con una lista de sugerencias, una por línea, empezando con un emoji:
- 💡 Para ideas nuevas
- ⚠️ Para posibles problemas
- ✅ Para buenas prácticas
- 🔧 Para optimizaciones

Sé conciso y práctico."""

        config_str = json.dumps(agente_config, indent=2, ensure_ascii=False, default=str)

        try:
            respuesta = self.client.chat(
                prompt=f"Configuración del agente:\n{config_str}",
                system_prompt=system_prompt,
                temperature=0.4,
                max_tokens=500
            )

            sugerencias = [
                linea.strip() for linea in respuesta.split('\n')
                if linea.strip() and not linea.strip().startswith('#')
            ]

            return sugerencias[:5]

        except Exception as e:
            self.logger.error(f"Error generando sugerencias: {e}")
            return []

    # ============================================================
    # PROMPTS ESPECÍFICOS POR TIPO
    # ============================================================
    def _prompt_python(self, contexto: Optional[Dict] = None) -> str:
        deps_info = ""
        if contexto and contexto.get('dependencias'):
            deps_info = f"\nDependencias disponibles: {', '.join(contexto['dependencias'])}"

        return f"""Eres un experto en Python.
Genera código Python que:
- Use la variable 'contexto' (dict) para acceder a resultados de dependencias{deps_info}
- Asigne el resultado final a la variable 'resultado' (debe ser un dict o valor serializable)
- Incluya manejo de errores básico
- Sea legible y bien comentado
- Use solo módulos estándar o los más comunes (json, datetime, re, etc.)

Devuelve SOLO el código Python, sin markdown ni explicaciones.
Ejemplo de estructura:
```python
import json
from datetime import datetime

# Obtener datos del contexto
datos = contexto.get('NombreDependencia', {{}})

# Procesar...
resultado = {{
    'procesado': True,
    'timestamp': datetime.now().isoformat()
}}
```"""

    def _prompt_shell(self, contexto: Optional[Dict] = None) -> str:
        return """Eres un experto en comandos de terminal Linux/Unix.
Genera un comando shell que:
- Sea seguro (no uses rm -rf, sudo, etc. sin razón)
- Use variables como {nombre} si es apropiado
- Incluya pipes y redirecciones si mejora la funcionalidad
- Sea conciso pero completo

Devuelve SOLO el comando, sin markdown ni explicaciones.
Ejemplos:
- ls -la | grep ".py"
- find . -name "*.log" -mtime +7
- cat archivo.txt | sort | uniq -c | sort -rn"""

    def _prompt_http(self, contexto: Optional[Dict] = None) -> str:
        return """Eres un experto en APIs REST.
Genera una URL de API pública y funcional que:
- Sea de una API real y accesible (GitHub, JSONPlaceholder, Open-Meteo, etc.)
- Devuelva datos JSON útiles
- No requiera autenticación (a menos que sea estrictamente necesario)

Devuelve SOLO la URL, sin markdown ni explicaciones.
Ejemplos:
- https://api.github.com/repos/python/cpython
- https://jsonplaceholder.typicode.com/posts
- https://api.open-meteo.com/v1/forecast?latitude=40.4165&longitude=-3.7026&current_weather=true"""

    def _prompt_llm(self, contexto: Optional[Dict] = None) -> str:
        deps_info = ""
        if contexto and contexto.get('dependencias'):
            deps_info = f"\nEl agente recibe datos de: {', '.join(contexto['dependencias'])}"

        return f"""Eres un experto en prompt engineering.
Genera un prompt efectivo para un modelo LLM (DeepSeek) que:
- Sea claro y específico{deps_info}
- Use "{{contexto}}" para referirse a los datos de entrada
- Especifique el formato de salida deseado
- Incluya ejemplos si ayuda
- Sea conciso pero completo

Devuelve SOLO el prompt, sin markdown ni explicaciones.
Estructura recomendada:
1. Rol del asistente
2. Tarea específica
3. Datos de entrada ("{{contexto}}")
4. Formato de salida esperado"""

    def _prompt_file(self, contexto: Optional[Dict] = None) -> str:
        return """Eres un experto en operaciones con archivos.
Sugiere una operación con archivos útil y práctica.

Devuelve SOLO una breve descripción de la operación sugerida (1-2 líneas),
indicando:
- Qué operación (leer/escribir/copiar/mover)
- Qué tipo de archivo
- Para qué sirve"""

    def _prompt_loop(self, contexto: Optional[Dict] = None) -> str:
        return """Eres un experto en procesamiento de listas.
Genera código Python que procese un item individual de una lista.

Variables disponibles:
- item: El elemento actual
- indice: Índice (0, 1, 2, ...)
- total: Número total de items
- contexto: Dict con todas las dependencias

Reglas:
- Asigna el resultado a 'resultado' (debe ser dict serializable)
- Incluye el índice y el item procesado en el resultado
- Maneja errores con try/except
- Sé práctico y útil

Devuelve SOLO el código, sin markdown ni explicaciones."""

    def _prompt_generico(self, contexto: Optional[Dict] = None) -> str:
        return "Genera contenido útil y funcional según la descripción del usuario. Devuelve solo el contenido, sin explicaciones."

    # ============================================================
    # UTILIDADES (SIMPLIFICADAS)
    # ============================================================
    
    def _limpiar_codigo(self, codigo: str) -> str:
        """
        Limpia código de posibles marcas de markdown.
        """
        codigo = re.sub(r'^```\w*\s*\n', '', codigo, flags=re.MULTILINE)
        codigo = re.sub(r'\n```\s*$', '', codigo, flags=re.MULTILINE)
        return codigo.strip()