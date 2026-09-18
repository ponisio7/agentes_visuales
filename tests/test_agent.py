# tests/test_agent.py
from core.agent import Agente, EstadoAgente, TipoAgente


class TestAgente:
    """Pruebas para la clase Agente"""
    
    def test_creacion_basica(self):
        """Prueba la creación básica de un agente"""
        agente = Agente(nombre="Test", duracion=3.0)
        
        assert agente.nombre == "Test"
        assert agente.duracion == 3.0
        assert agente.estado == EstadoAgente.PENDIENTE
        assert agente.progreso == 0
        assert agente.id is not None
        assert len(agente.id) == 8
    
    def test_creacion_con_tipo(self):
        """Prueba la creación con tipo específico"""
        agente = Agente(
            nombre="HTTPTest",
            tipo=TipoAgente.HTTP,
            url_http="https://api.example.com"
        )
        
        assert agente.tipo == TipoAgente.HTTP
        assert agente.url_http == "https://api.example.com"
    
    def test_dependencias_nombres(self):
        """Prueba la resolución de dependencias por nombre"""
        agente = Agente(
            nombre="Dependiente",
            dependencias_nombres=["A1", "A2"]
        )
        
        assert len(agente.dependencias_nombres) == 2
        assert "A1" in agente.dependencias_nombres
        assert "A2" in agente.dependencias_nombres
    
    def test_to_dict(self):
        """Prueba la conversión a diccionario"""
        agente = Agente(
            nombre="TestDict",
            duracion=2.5,
            descripcion="Agente de prueba para dict"
        )
        
        data = agente.to_dict()
        
        assert data['nombre'] == "TestDict"
        assert data['duracion'] == 2.5
        assert data['descripcion'] == "Agente de prueba para dict"
        assert data['estado'] == "Pendiente"
        assert data['id'] == agente.id
    
    def test_estado_inicial(self):
        """Prueba el estado inicial del agente"""
        agente = Agente(nombre="EstadoTest")
        
        assert agente.estado == EstadoAgente.PENDIENTE
        assert agente.progreso == 0
        assert agente.mensaje == ""
        assert agente.tiempo_inicio is None
        assert agente.tiempo_fin is None
    
    def test_actualizacion_estado(self):
        """Prueba la actualización de estado"""
        agente = Agente(nombre="ActualizacionTest")
        
        agente.estado = EstadoAgente.EJECUTANDO
        assert agente.estado == EstadoAgente.EJECUTANDO
        
        agente.progreso = 50
        assert agente.progreso == 50
        
        agente.mensaje = "Procesando..."
        assert agente.mensaje == "Procesando..."
    
    def test_tipos_agente(self):
        """Prueba todos los tipos de agente"""
        tipos = [
            TipoAgente.PYTHON,
            TipoAgente.SHELL,
            TipoAgente.LLM,
            TipoAgente.HTTP,
            TipoAgente.FILE
        ]
        
        for tipo in tipos:
            agente = Agente(nombre=f"Test_{tipo.value}", tipo=tipo)
            assert agente.tipo == tipo
            assert agente.nombre == f"Test_{tipo.value}"
    
    def test_agente_con_campos_especificos(self):
        """Prueba agentes con campos específicos por tipo"""
        # Python
        agente_py = Agente(
            nombre="PythonTest",
            tipo=TipoAgente.PYTHON,
            codigo_python="print('Hola')"
        )
        assert agente_py.codigo_python == "print('Hola')"
        
        # HTTP
        agente_http = Agente(
            nombre="HTTPTest",
            tipo=TipoAgente.HTTP,
            url_http="https://api.test",
            metodo_http="POST",
            body_http='{"key": "value"}'
        )
        assert agente_http.url_http == "https://api.test"
        assert agente_http.metodo_http == "POST"
        assert agente_http.body_http == '{"key": "value"}'
        
        # Shell
        agente_shell = Agente(
            nombre="ShellTest",
            tipo=TipoAgente.SHELL,
            comando_shell="ls -la",
            timeout_shell=60
        )
        assert agente_shell.comando_shell == "ls -la"
        assert agente_shell.timeout_shell == 60
