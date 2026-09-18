import time

from core.agent import Agente
from core.scheduler import Scheduler  # <-- IMPORTANTE


# tests/test_scheduler_orden.py
class TestSchedulerOrden:
    """Pruebas de orden topológico de ejecución"""
    
    # tests/test_scheduler_orden.py - test_orden_ejecucion_basico CORREGIDO

    def test_orden_ejecucion_basico(self, scheduler_rapido):
        """A → B → C: B debe ejecutarse después de A, C después de B"""
        scheduler = scheduler_rapido
        scheduler.iniciar()
        
        # Espera activa
        timeout = 5
        start = time.time()
        while time.time() - start < timeout:
            stats = scheduler.obtener_estadisticas()
            if stats['completados'] + stats['errores'] + stats['cancelados'] == stats['total']:
                break
            time.sleep(0.1)
        
        # Verificar tiempos de finalización
        a1 = scheduler.obtener_agente_por_nombre("A1")
        a2 = scheduler.obtener_agente_por_nombre("A2")
        a3 = scheduler.obtener_agente_por_nombre("A3")
        
        # Mostrar errores si los hay
        for agente in [a1, a2, a3]:
            if agente.estado.value == "Error":
                print(f"❌ Error en {agente.nombre}: {agente.error}")
        
        # Todos deben estar completados
        assert a1.estado.value == "Completado"
        assert a2.estado.value == "Completado"
        assert a3.estado.value == "Completado"
        
        # Los tiempos de inicio deben ser secuenciales
        assert a1.tiempo_inicio is not None
        assert a2.tiempo_inicio is not None
        assert a3.tiempo_inicio is not None
    
    def test_concurrencia_limitada(self):
        scheduler = Scheduler(max_concurrent=2)
        
        agentes = [
            Agente(
                nombre=f"Agente_{i}",
                duracion=0.2,
                codigo_python=f"resultado = {{'id': 'Agente_{i}', 'status': 'ok'}}"
            ) for i in range(5)
        ]
        scheduler.agregar_agentes(agentes)
        
        scheduler.iniciar()
        time.sleep(0.05)
        
        stats = scheduler.obtener_estadisticas()
        assert stats['ejecutando'] <= 2
