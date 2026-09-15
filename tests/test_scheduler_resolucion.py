# tests/test_scheduler_resolucion.py
from core.agent import Agente
from core.scheduler import Scheduler  # <-- IMPORTANTE


class TestSchedulerResolucion:
    """Pruebas para la resolución de dependencias por nombre → ID"""
    
    def test_resolver_nombres_a_ids(self):
        scheduler = Scheduler()
        
        a1 = Agente(nombre="Productor")
        a2 = Agente(nombre="Consumidor", dependencias_nombres=["Productor"])
        
        scheduler.agregar_agentes([a1, a2])
        scheduler.resolver_dependencias()
        
        # El consumidor debe tener el ID del productor
        assert len(a2.dependencias_ids) == 1
        assert a2.dependencias_ids[0] == a1.id
        
        # Los nombres deben estar vacíos (se resuelven)
        assert len(a2.dependencias_nombres) == 0
    
    def test_resolver_dependencias_multiples(self):
        scheduler = Scheduler()
        
        a = Agente(nombre="A")
        b = Agente(nombre="B")
        c = Agente(nombre="C", dependencias_nombres=["A", "B"])
        
        scheduler.agregar_agentes([a, b, c])
        scheduler.resolver_dependencias()
        
        assert len(c.dependencias_ids) == 2
        assert a.id in c.dependencias_ids
        assert b.id in c.dependencias_ids
    
    def test_resolver_dependencia_inexistente(self):
        scheduler = Scheduler()
        
        a = Agente(nombre="A", dependencias_nombres=["Inexistente"])
        scheduler.agregar_agente(a)
        
        # No debería lanzar excepción, solo loguear advertencia
        scheduler.resolver_dependencias()
        
        # La dependencia inexistente se ignora
        assert len(a.dependencias_ids) == 0