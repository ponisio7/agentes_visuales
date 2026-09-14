# examples/ejemplos_agentes.py

AGENTES_EJEMPLO = {
    "Web Scraper": {
        "nombre": "WebScraper",
        "tipo": "HTTP",
        "descripcion": "Obtiene datos de una API web",
        "url_http": "https://api.github.com/repos/python/cpython",
        "metodo_http": "GET",
        "timeout_http": 10
    },
    
    "Procesador Python": {
        "nombre": "ProcesadorDatos",
        "tipo": "Python",
        "descripcion": "Procesa datos con Python",
        "codigo_python": """
import json
import datetime

# Datos de entrada
data = contexto.get('WebScraper', {}).get('json', {})

if data:
    # Procesar datos
    resultado = {
        'nombre': data.get('name', 'Desconocido'),
        'estrellas': data.get('stargazers_count', 0),
        'forks': data.get('forks_count', 0),
        'procesado_en': str(datetime.datetime.now())
    }
else:
    resultado = {'error': 'No se recibieron datos'}

resultado = resultado  # Se guarda como resultado
print(f"Datos procesados: {resultado}")
"""
    },
    
    "Generador Reporte": {
        "nombre": "GeneradorReporte",
        "tipo": "File",
        "descripcion": "Genera un archivo de reporte",
        "operacion_file": "escribir",
        "archivo_destino": "reporte.json",
        "dependencias_nombres": ["ProcesadorDatos"]
    },
    
    "Analizador LLM": {
        "nombre": "AnalizadorLLM",
        "tipo": "LLM",
        "descripcion": "Analiza datos con IA",
        "prompt_llm": "Analiza los siguientes datos y genera un resumen ejecutivo:",
        "modelo_llm": "gpt-3.5-turbo",
        "temperatura_llm": 0.7,
        "max_tokens_llm": 500,
        "dependencias_nombres": ["ProcesadorDatos"]
    },
    
    "Comando Shell": {
        "nombre": "ListarArchivos",
        "tipo": "Shell",
        "descripcion": "Lista archivos del directorio actual",
        "comando_shell": "ls -la",
        "timeout_shell": 10
    }
}

def crear_agente_ejemplo(nombre: str):
    """Crea un agente a partir de un ejemplo"""
    from core.agent import Agente, TipoAgente
    
    if nombre not in AGENTES_EJEMPLO:
        return None
    
    data = AGENTES_EJEMPLO[nombre]
    tipo_str = data.get('tipo', 'Python')
    tipo = getattr(TipoAgente, tipo_str.upper()) if tipo_str.upper() in dir(TipoAgente) else TipoAgente.PYTHON
    
    agente = Agente(
        nombre=data.get('nombre', nombre),
        tipo=tipo,
        descripcion=data.get('descripcion', ''),
        dependencias_nombres=data.get('dependencias_nombres', [])
    )
    
    # Configurar según tipo
    if tipo == TipoAgente.PYTHON:
        agente.codigo_python = data.get('codigo_python', '')
    elif tipo == TipoAgente.SHELL:
        agente.comando_shell = data.get('comando_shell', '')
        agente.timeout_shell = data.get('timeout_shell', 30)
    elif tipo == TipoAgente.HTTP:
        agente.url_http = data.get('url_http', '')
        agente.metodo_http = data.get('metodo_http', 'GET')
        agente.timeout_http = data.get('timeout_http', 30)
    elif tipo == TipoAgente.LLM:
        agente.prompt_llm = data.get('prompt_llm', '')
        agente.modelo_llm = data.get('modelo_llm', 'gpt-3.5-turbo')
        agente.temperatura_llm = data.get('temperatura_llm', 0.7)
        agente.max_tokens_llm = data.get('max_tokens_llm', 500)
    elif tipo == TipoAgente.FILE:
        agente.operacion_file = data.get('operacion_file', 'leer')
        agente.archivo_origen = data.get('archivo_origen', '')
        agente.archivo_destino = data.get('archivo_destino', '')
    
    return agente
