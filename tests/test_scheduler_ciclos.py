# tests/test_scheduler_ciclos.py - VERSIÓN CORREGIDA COMPLETA

from core.agent import Agente
from core.scheduler import Scheduler


class TestSchedulerCiclos:
    """Pruebas específicas para detección de ciclos"""
    
    def test_detectar_ciclo_simple(self):
        """A → B → A (ciclo directo)"""
        scheduler = Scheduler()
        
        a = Agente(nombre="A", dependencias_nombres=["B"])
        b = Agente(nombre="B", dependencias_nombres=["A"])
        
        scheduler.agregar_agentes([a, b])
        scheduler.resolver_dependencias()
        
        tiene_ciclos, ciclos = scheduler.detectar_ciclos()
        
        assert tiene_ciclos
        assert len(ciclos) > 0
        
        # 🔥 AHORA VERIFICA NOMBRES (no IDs)
        ciclo = ciclos[0]
        assert "A" in ciclo
        assert "B" in ciclo
        assert len(ciclo) == 3  # A-B-A
    
    def test_detectar_ciclo_largo(self):
        """A → B → C → A (ciclo de 3 nodos)"""
        scheduler = Scheduler()
        
        a = Agente(nombre="A", dependencias_nombres=["B"])
        b = Agente(nombre="B", dependencias_nombres=["C"])
        c = Agente(nombre="C", dependencias_nombres=["A"])
        
        scheduler.agregar_agentes([a, b, c])
        scheduler.resolver_dependencias()
        
        tiene_ciclos, ciclos = scheduler.detectar_ciclos()
        
        assert tiene_ciclos
        assert len(ciclos) == 1
        assert len(ciclos[0]) == 4  # A-B-C-A
    
    def test_ciclo_con_dependencias_adicionales(self):
        """A → B → C → A, más D que depende de C (ciclo aislado)"""
        scheduler = Scheduler()
        
        a = Agente(nombre="A", dependencias_nombres=["B"])
        b = Agente(nombre="B", dependencias_nombres=["C"])
        c = Agente(nombre="C", dependencias_nombres=["A"])
        d = Agente(nombre="D", dependencias_nombres=["C"])
        
        scheduler.agregar_agentes([a, b, c, d])
        scheduler.resolver_dependencias()
        
        tiene_ciclos, ciclos = scheduler.detectar_ciclos()
        
        assert tiene_ciclos
        assert len(ciclos) >= 1
        # Verificar que el ciclo contiene A, B, C
        ciclo = ciclos[0]
        assert "A" in ciclo
        assert "B" in ciclo
        assert "C" in ciclo
    
    def test_sin_ciclos(self):
        """A → B → C (grafo acíclico)"""
        scheduler = Scheduler()
        
        a = Agente(nombre="A")
        b = Agente(nombre="B", dependencias_nombres=["A"])
        c = Agente(nombre="C", dependencias_nombres=["B"])
        
        scheduler.agregar_agentes([a, b, c])
        scheduler.resolver_dependencias()
        
        tiene_ciclos, ciclos = scheduler.detectar_ciclos()
        
        assert not tiene_ciclos
        assert len(ciclos) == 0
    
    def test_ciclo_auto_referencia(self):
        """A → A (auto-ciclo)"""
        scheduler = Scheduler()
        
        a = Agente(nombre="A", dependencias_nombres=["A"])
        
        scheduler.agregar_agente(a)
        scheduler.resolver_dependencias()
        
        tiene_ciclos, ciclos = scheduler.detectar_ciclos()
        
        assert tiene_ciclos
        assert len(ciclos) == 1
        assert ciclos[0] == ["A", "A"]
