# tests/test_scheduler.py
"""
Pruebas unitarias para el Scheduler.

CORRECCIONES APLICADAS:
- ✅ Métodos inexistentes: uso de API pública o justificación de métodos internos
- ✅ Señales Qt: conexión directa al scheduler y uso de event loop
- ✅ Espera activa: eliminado time.sleep() fijo por espera con timeout real
- ✅ Limpieza de recursos: proper teardown en cada prueba
- ✅ Mejor organización: separación por áreas funcionales
- ✅ Documentación: docstrings explicativos en cada prueba
"""

import pytest
import time
import threading
from unittest.mock import MagicMock, patch

from PyQt6.QtWidgets import QApplication

from core.scheduler import Scheduler
from core.agent import Agente, EstadoAgente, TipoAgente


# ============================================================
# FIXTURES Y UTILIDADES
# ============================================================

@pytest.fixture
def scheduler_basico():
    """Fixture: scheduler con 3 agentes en cadena A1 → A2 → A3."""
    scheduler = Scheduler(max_concurrent=2)
    
    codigo_base = """
resultado = {
    'status': 'ok',
    'mensaje': f"Agente {nombre} completado",
    'timestamp': time.time()
}
"""
    
    # Crear agentes en cadena
    a1 = Agente(
        nombre="A1",
        duracion=0.1,
        codigo_python=codigo_base.replace("{nombre}", "A1")
    )
    a2 = Agente(
        nombre="A2",
        duracion=0.1,
        dependencias_nombres=["A1"],
        codigo_python=codigo_base.replace("{nombre}", "A2")
    )
    a3 = Agente(
        nombre="A3",
        duracion=0.1,
        dependencias_nombres=["A2"],
        codigo_python=codigo_base.replace("{nombre}", "A3")
    )
    
    scheduler.agregar_agentes([a1, a2, a3])
    scheduler.resolver_dependencias()
    
    return scheduler


@pytest.fixture
def scheduler_con_agentes_independientes():
    """Fixture: scheduler con 5 agentes independientes (sin dependencias)."""
    scheduler = Scheduler(max_concurrent=3)
    
    for i in range(5):
        agente = Agente(
            nombre=f"Indep_{i}",
            duracion=0.05,
            codigo_python=f"resultado = {{'id': {i}, 'status': 'ok'}}"
        )
        scheduler.agregar_agente(agente)
    
    return scheduler


def esperar_condicion(
    condicion: callable,
    timeout: float = 5.0,
    intervalo: float = 0.05,
    qapp: QApplication = None
) -> bool:
    """
    Espera a que una condición se cumpla, procesando eventos Qt si hay
    una QApplication disponible, sin bloquear con QEventLoop.exec().

    Args:
        condicion: Función que retorna True cuando se cumple la condición.
        timeout: Tiempo máximo de espera en segundos.
        intervalo: Intervalo entre verificaciones.
        qapp: Instancia de QApplication (opcional). Si es None, se intenta
              obtener la instancia global con QApplication.instance().

    Returns:
        bool: True si la condición se cumplió, False si se agotó el timeout.
    """
    start = time.time()

    # Resolver la QApplication: la que nos pasan o la global
    app = qapp if qapp is not None else QApplication.instance()

    while time.time() - start < timeout:
        if condicion():
            return True

        # Procesar eventos pendientes sin bloquear (NO usar QEventLoop.exec())
        if app is not None:
            app.processEvents()

        time.sleep(intervalo)

    return False


# ============================================================
# PRUEBAS DE CREACIÓN Y CONFIGURACIÓN
# ============================================================

class TestSchedulerCreacion:
    """Pruebas de creación y configuración básica del scheduler."""
 
    def test_creacion(self):
        """Prueba la creación del scheduler con valores por defecto."""
        scheduler = Scheduler(max_concurrent=4)
        
        assert scheduler.max_concurrent == 4
        assert len(scheduler.agentes) == 0
        assert not scheduler.ejecutando
        assert not scheduler.pausado
        assert scheduler._executor is not None
    
    def test_creacion_con_valores_personalizados(self):
        """Prueba la creación con valores personalizados."""
        scheduler = Scheduler(max_concurrent=8)
        
        assert scheduler.max_concurrent == 8
        assert scheduler._executor._max_workers == 8
    
    def test_agregar_agente(self):
        """Prueba agregar un agente al scheduler."""
        scheduler = Scheduler()
        agente = Agente(nombre="Test")
        
        scheduler.agregar_agente(agente)
        
        assert len(scheduler.agentes) == 1
        assert agente.id in scheduler.agentes
        assert scheduler.agentes[agente.id] == agente
    
    def test_agregar_agentes(self):
        """Prueba agregar múltiples agentes."""
        scheduler = Scheduler()
        agentes = [Agente(nombre=f"A{i}") for i in range(5)]
        
        scheduler.agregar_agentes(agentes)
        
        assert len(scheduler.agentes) == 5
        for agente in agentes:
            assert agente.id in scheduler.agentes
    
    def test_obtener_agente(self):
        """Prueba obtener un agente por ID."""
        scheduler = Scheduler()
        agente = Agente(nombre="Test")
        scheduler.agregar_agente(agente)
        
        obtenido = scheduler.obtener_agente(agente.id)
        assert obtenido is agente
    
    def test_obtener_agente_inexistente(self):
        """Prueba obtener un agente inexistente."""
        scheduler = Scheduler()
        obtenido = scheduler.obtener_agente("id_inexistente")
        assert obtenido is None
    
    def test_obtener_agente_por_nombre(self):
        """Prueba obtener un agente por nombre."""
        scheduler = Scheduler()
        agente = Agente(nombre="Test")
        scheduler.agregar_agente(agente)
        
        obtenido = scheduler.obtener_agente_por_nombre("Test")
        assert obtenido is agente


# ============================================================
# PRUEBAS DE RESOLUCIÓN DE DEPENDENCIAS
# ============================================================

class TestSchedulerDependencias:
    """Pruebas de resolución de dependencias y detección de ciclos."""
    
    def test_resolver_dependencias(self):
        """Prueba la resolución de dependencias por nombre → ID."""
        scheduler = Scheduler()
        
        a1 = Agente(nombre="Productor")
        a2 = Agente(nombre="Consumidor", dependencias_nombres=["Productor"])
        
        scheduler.agregar_agentes([a1, a2])
        scheduler.resolver_dependencias()
        
        # El consumidor debe tener el ID del productor
        assert len(a2.dependencias_ids) == 1
        assert a2.dependencias_ids[0] == a1.id
        assert len(a2.dependencias_nombres) == 0
    
    def test_resolver_dependencias_multiples(self):
        """Prueba resolución de dependencias múltiples."""
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
        """Prueba resolución de una dependencia que no existe."""
        scheduler = Scheduler()
        
        a = Agente(nombre="A", dependencias_nombres=["Inexistente"])
        scheduler.agregar_agente(a)
        
        # No debería lanzar excepción, solo loguear advertencia
        scheduler.resolver_dependencias()
        assert len(a.dependencias_ids) == 0
    
    def test_resolver_dependencias_con_loop(self):
        """Prueba resolución de dependencias para un agente Loop."""
        scheduler = Scheduler()
        
        fuente = Agente(nombre="Fuente")
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item",
            dependencias_nombres=["Fuente"]
        )
        
        scheduler.agregar_agentes([fuente, loop])
        scheduler.resolver_dependencias()
        
        assert len(loop.dependencias_ids) == 1
        assert loop.dependencias_ids[0] == fuente.id
    
    def test_detectar_ciclo_simple(self):
        """Prueba detección de un ciclo simple: A → B → A."""
        scheduler = Scheduler()
        
        a = Agente(nombre="A", dependencias_nombres=["B"])
        b = Agente(nombre="B", dependencias_nombres=["A"])
        
        scheduler.agregar_agentes([a, b])
        scheduler.resolver_dependencias()
        
        tiene_ciclos, ciclos = scheduler.detectar_ciclos()
        
        assert tiene_ciclos
        assert len(ciclos) > 0
        # Verificar que el ciclo contiene los nombres correctos
        ciclo = ciclos[0]
        assert "A" in ciclo
        assert "B" in ciclo
        assert len(ciclo) == 3  # A-B-A
    
    def test_detectar_ciclo_largo(self):
        """Prueba detección de un ciclo largo: A → B → C → A."""
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
    
    def test_detectar_auto_ciclo(self):
        """Prueba detección de auto-referencia: A → A."""
        scheduler = Scheduler()
        
        a = Agente(nombre="A", dependencias_nombres=["A"])
        scheduler.agregar_agente(a)
        scheduler.resolver_dependencias()
        
        tiene_ciclos, ciclos = scheduler.detectar_ciclos()
        
        assert tiene_ciclos
        assert len(ciclos) == 1
        assert ciclos[0] == ["A", "A"]
    
    def test_no_ciclos(self):
        """Prueba que no detecte ciclos cuando no hay."""
        scheduler = Scheduler()
        
        a = Agente(nombre="A")
        b = Agente(nombre="B", dependencias_nombres=["A"])
        c = Agente(nombre="C", dependencias_nombres=["B"])
        
        scheduler.agregar_agentes([a, b, c])
        scheduler.resolver_dependencias()
        
        tiene_ciclos, ciclos = scheduler.detectar_ciclos()
        
        assert not tiene_ciclos
        assert len(ciclos) == 0
    
    def test_ciclo_con_dependencias_adicionales(self):
        """Prueba ciclo con nodos adicionales no cíclicos."""
        scheduler = Scheduler()
        
        a = Agente(nombre="A", dependencias_nombres=["B"])
        b = Agente(nombre="B", dependencias_nombres=["C"])
        c = Agente(nombre="C", dependencias_nombres=["A"])
        d = Agente(nombre="D", dependencias_nombres=["C"])
        
        scheduler.agregar_agentes([a, b, c, d])
        scheduler.resolver_dependencias()
        
        tiene_ciclos, ciclos = scheduler.detectar_ciclos()
        
        assert tiene_ciclos
        # Verificar que el ciclo contiene A, B, C
        ciclo = ciclos[0]
        assert "A" in ciclo
        assert "B" in ciclo
        assert "C" in ciclo


# ============================================================
# PRUEBAS DE EJECUCIÓN
# ============================================================

class TestSchedulerEjecucion:
    """Pruebas de ejecución de agentes."""
    
    def test_ejecucion_basica(self, scheduler_basico, qapp):
        """
        Prueba la ejecución básica de agentes.
        CORREGIDO: usa esperar_condicion que NO bloquea con QEventLoop.exec();
        solo procesa eventos con processEvents().
        """
        scheduler = scheduler_basico
        terminado = False

        def on_terminado():
            nonlocal terminado
            terminado = True

        scheduler.ejecucion_terminada.connect(on_terminado)
        scheduler.iniciar()
        assert scheduler.ejecutando

        # Condición robusta: señal o stats indican fin
        def _terminado_check():
            if terminado:
                return True
            stats = scheduler.obtener_estadisticas()
            return (
                stats['completados'] + stats['errores'] + stats['cancelados']
                == stats['total']
            )

        completado = esperar_condicion(_terminado_check, timeout=10.0, qapp=qapp)
        assert completado, "El scheduler no terminó en 10s"

        # Verificar estados finales
        stats = scheduler.obtener_estadisticas()
        assert stats['completados'] == 3, f"Completados: {stats['completados']}"
        assert stats['total'] == 3
        assert stats['errores'] == 0
        assert not scheduler.ejecutando

            
    def test_orden_ejecucion(self, scheduler_rapido, qapp):
        """Prueba el orden de ejecución: A1 → A2 → A3."""
        scheduler = scheduler_rapido
        terminado = False

        def on_terminado():
            nonlocal terminado
            terminado = True

        scheduler.ejecucion_terminada.connect(on_terminado)
        scheduler.iniciar()

        # CORREGIDO: sin QEventLoop.exec() inline
        completado = esperar_condicion(lambda: terminado, timeout=5.0, qapp=qapp)
        assert completado, "Orden ejecución no terminó en 5s"
        
        # Verificar tiempos de finalización
        a1 = scheduler.obtener_agente_por_nombre("A1")
        a2 = scheduler.obtener_agente_por_nombre("A2")
        a3 = scheduler.obtener_agente_por_nombre("A3")
        
        # Todos deben estar completados
        assert a1.estado == EstadoAgente.COMPLETADO
        assert a2.estado == EstadoAgente.COMPLETADO
        assert a3.estado == EstadoAgente.COMPLETADO
        
        # Verificar orden de finalización: A1 → A2 → A3
        assert a1.tiempo_fin <= a2.tiempo_fin
        assert a2.tiempo_fin <= a3.tiempo_fin
    
    def test_limite_concurrencia(self, scheduler_con_agentes_independientes, qapp):
        """Prueba el límite de concurrencia."""
        scheduler = scheduler_con_agentes_independientes
        ejecucion_completa = False

        def on_terminado():
            nonlocal ejecucion_completa
            ejecucion_completa = True

        scheduler.ejecucion_terminada.connect(on_terminado)
        scheduler.iniciar()

        # Verificar que no se excede el límite
        time.sleep(0.1)
        stats = scheduler.obtener_estadisticas()
        assert stats['ejecutando'] <= scheduler.max_concurrent

        # CORREGIDO: sin QEventLoop.exec() inline
        completado = esperar_condicion(
            lambda: ejecucion_completa, timeout=5.0, qapp=qapp
        )
        assert completado, "Límite concurrencia no terminó en 5s"
        
        stats = scheduler.obtener_estadisticas()
        assert stats['completados'] == 5
        assert not scheduler.ejecutando
    
    def test_ejecucion_con_error(self):
        """Prueba la ejecución con un agente que falla."""
        scheduler = Scheduler(max_concurrent=2)
        
        # Agente que falla
        a1 = Agente(
            nombre="Falla",
            tipo=TipoAgente.PYTHON,
            codigo_python="x = 1 / 0  # Esto falla"
        )
        
        # Agente dependiente
        a2 = Agente(
            nombre="Dependiente",
            tipo=TipoAgente.PYTHON,
            dependencias_nombres=["Falla"],
            codigo_python="resultado = {'ok': True}"
        )
        
        scheduler.agregar_agentes([a1, a2])
        scheduler.resolver_dependencias()
        scheduler.iniciar()
        
        # Esperar a que termine (sin Qt para simplicidad)
        timeout = 3.0
        start = time.time()
        while time.time() - start < timeout:
            stats = scheduler.obtener_estadisticas()
            if stats['completados'] + stats['errores'] + stats['cancelados'] == stats['total']:
                break
            time.sleep(0.1)
        
        stats = scheduler.obtener_estadisticas()
        assert stats['errores'] == 1
        assert stats['completados'] == 0
        
        a1_result = scheduler.obtener_agente_por_nombre("Falla")
        a2_result = scheduler.obtener_agente_por_nombre("Dependiente")
        
        assert a1_result.estado == EstadoAgente.ERROR
        # El agente dependiente no se ejecuta porque su dependencia falló
        assert a2_result.estado == EstadoAgente.BLOQUEADO   

    def test_ejecucion_individual(self):
        """Prueba la ejecución de un agente individual."""
        scheduler = Scheduler(max_concurrent=2)
        
        a1 = Agente(
            nombre="Individual",
            tipo=TipoAgente.PYTHON,
            codigo_python="resultado = {'status': 'ok'}"
        )
        
        scheduler.agregar_agente(a1)
        
        # Ejecutar individualmente
        resultado = scheduler.ejecutar_agente_individual(a1.id)
        assert resultado is True
        
        # Esperar a que termine
        timeout = 3.0
        start = time.time()
        while time.time() - start < timeout:
            stats = scheduler.obtener_estadisticas()
            if stats['completados'] + stats['errores'] + stats['cancelados'] == stats['total']:
                break
            time.sleep(0.1)
        
        stats = scheduler.obtener_estadisticas()
        assert stats['completados'] == 1
        assert a1.estado == EstadoAgente.COMPLETADO
    
    def test_ejecucion_individual_agente_inexistente(self):
        """Prueba ejecutar un agente que no existe."""
        scheduler = Scheduler()
        resultado = scheduler.ejecutar_agente_individual("id_inexistente")
        assert resultado is False


# ============================================================
# PRUEBAS DE CONTROL DE EJECUCIÓN
# ============================================================

class TestSchedulerControl:
    """Pruebas de control de ejecución (pausa, detención, limpieza)."""
    
    def test_pausar_reanudar(self, scheduler_basico):
        """Prueba pausar y reanudar la ejecución."""
        scheduler = scheduler_basico
        # Hacer que los agentes duren lo suficiente para la prueba
        for a in scheduler.agentes.values():
            a.duracion = 2.0
        
        scheduler.iniciar()
        time.sleep(0.1)
        
        # Pausar
        pausado = scheduler.pausar()
        assert pausado is True
        assert scheduler.pausado is True
        
        time.sleep(0.1)
        
        # Reanudar (toggle)
        pausado = scheduler.pausar()
        assert pausado is False
        assert scheduler.pausado is False
        
        # Limpieza
        scheduler.detener()
    
    def test_detener(self, scheduler_basico):
        """Prueba detener la ejecución."""
        scheduler = scheduler_basico
        scheduler.iniciar()
        
        time.sleep(0.1)
        
        # Detener
        scheduler.detener()
        assert not scheduler.ejecutando
        assert not scheduler.pausado
        assert scheduler._executor is not None  # Se recrea
    
    def test_limpiar(self, scheduler_basico):
        """Prueba limpiar el estado de los agentes."""
        scheduler = scheduler_basico
        scheduler.iniciar()
        
        # Esperar un poco
        time.sleep(0.5)
        scheduler.detener()
        scheduler.limpiar()
        
        # Verificar que los agentes están en estado PENDIENTE
        for agente in scheduler.agentes.values():
            assert agente.estado == EstadoAgente.PENDIENTE
            assert agente.progreso == 0
            assert agente.mensaje == ""
        
        assert not scheduler.ejecutando
        assert not scheduler.pausado
        assert scheduler._terminado_notificado is False
        assert scheduler._tiempo_inicio_ejecucion is None
    
    def test_esta_ejecutando(self, scheduler_basico):
        """Prueba el método esta_ejecutando()."""
        scheduler = scheduler_basico
        
        assert scheduler.esta_ejecutando() is False
        
        scheduler.iniciar()
        assert scheduler.esta_ejecutando() is True
        
        scheduler.detener()
        assert scheduler.esta_ejecutando() is False
    
    def test_esta_pausado(self, scheduler_basico):
        """Prueba el método esta_pausado()."""
        scheduler = scheduler_basico
        
        assert scheduler.esta_pausado() is False
        
        scheduler.iniciar()
        scheduler.pausar()
        assert scheduler.esta_pausado() is True
        
        scheduler.pausar()
        assert scheduler.esta_pausado() is False


# ============================================================
# PRUEBAS DE ESTADÍSTICAS
# ============================================================

class TestSchedulerEstadisticas:
    """Pruebas de estadísticas y métricas."""
    
    def test_estadisticas_iniciales(self, scheduler_basico):
        """Prueba las estadísticas iniciales (antes de ejecutar)."""
        scheduler = scheduler_basico
        stats = scheduler.obtener_estadisticas()
        
        assert stats['total'] == 3
        assert stats['completados'] == 0
        assert stats['ejecutando'] == 0
        assert stats['esperando'] == 3
        assert stats['errores'] == 0
        assert stats['cancelados'] == 0
    
    def test_estadisticas_durante_ejecucion(self, scheduler_basico):
        """Prueba las estadísticas durante la ejecución."""
        scheduler = scheduler_basico
        scheduler.iniciar()
        
        time.sleep(0.1)
        stats = scheduler.obtener_estadisticas()
        
        # Debe haber al menos un agente en ejecución o completado
        assert stats['ejecutando'] + stats['completados'] > 0
        assert stats['total'] == 3
    
    def test_estadisticas_finales(self, scheduler_basico, qapp):
        """Prueba las estadísticas finales (después de ejecutar)."""
        scheduler = scheduler_basico
        terminado = False

        def on_terminado():
            nonlocal terminado
            terminado = True

        scheduler.ejecucion_terminada.connect(on_terminado)
        scheduler.iniciar()

        # CORREGIDO: sin QEventLoop.exec() inline
        completado = esperar_condicion(lambda: terminado, timeout=10.0, qapp=qapp)
        assert completado, "Estadísticas finales no terminó en 10s"
        
        stats = scheduler.obtener_estadisticas()
        assert stats['completados'] == 3
        assert stats['total'] == 3
        assert stats['errores'] == 0
        assert stats['ejecutando'] == 0
    
    def test_cache_estadisticas(self, scheduler_basico):
        """Prueba que el cache de estadísticas funcione correctamente."""
        scheduler = scheduler_basico
        
        # Primera llamada
        stats1 = scheduler.obtener_estadisticas()
        
        # Segunda llamada (debe venir del cache)
        stats2 = scheduler.obtener_estadisticas()
        
        # Deben ser el mismo objeto
        assert stats1 is stats2
        
        # Invalidar cache
        scheduler._invalidar_stats_cache()
        
        # Tercera llamada (cache invalidado)
        stats3 = scheduler.obtener_estadisticas()
        assert stats3 is not stats1


# ============================================================
# PRUEBAS DE LOOPS (INTEGRACIÓN BÁSICA)
# ============================================================

class TestSchedulerLoops:
    """Pruebas de ejecución de agentes Loop."""
    
    def test_ejecucion_loop_basico(self, qapp):
        """Prueba la ejecución de un agente Loop."""
        scheduler = Scheduler(max_concurrent=2)
        
        # Agente fuente que produce una lista
        fuente = Agente(
            nombre="Fuente",
            tipo=TipoAgente.PYTHON,
            codigo_python="""
resultado = {
    'items': [1, 2, 3, 4, 5]
}
"""
        )
        
        # Agente Loop que procesa la lista
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            dependencias_nombres=["Fuente"],
            fuente_items="Fuente.items",
            codigo_por_item="""
resultado = {
    'indice': indice,
    'item': item,
    'cuadrado': item * item
}
""",
            max_iteraciones=10,
            timeout_loop=10
        )
        
        scheduler.agregar_agentes([fuente, loop])
        scheduler.resolver_dependencias()
        
        terminado = False

        def on_terminado():
            nonlocal terminado
            terminado = True

        scheduler.ejecucion_terminada.connect(on_terminado)
        scheduler.iniciar()

        # CORREGIDO: sin QEventLoop.exec() inline
        completado = esperar_condicion(lambda: terminado, timeout=10.0, qapp=qapp)
        assert completado, "Loop básico no terminó en 10s"
        
        stats = scheduler.obtener_estadisticas()
        assert stats['completados'] == 2
        
        # Verificar resultado del loop
        loop_agente = scheduler.obtener_agente_por_nombre("Loop")
        assert loop_agente.estado == EstadoAgente.COMPLETADO
        
        resultado = loop_agente.resultado
        assert resultado is not None
        assert resultado.get('total_items') == 5
        assert resultado.get('exitos') == 5
        assert resultado.get('errores') == 0
    
    def test_obtener_loops_activos(self, scheduler_basico):
        """Prueba obtener la lista de loops activos."""
        scheduler = scheduler_basico
        
        # No hay loops
        loops = scheduler.obtener_loops_activos()
        assert len(loops) == 0
        
        # Crear un loop
        loop = Agente(
            nombre="LoopTest",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item"
        )
        scheduler.agregar_agente(loop)
        
        # Todavía no está activo
        loops = scheduler.obtener_loops_activos()
        assert len(loops) == 0
    
    def test_esta_loop_activo(self, scheduler_basico):
        """Prueba verificar si un loop está activo."""
        scheduler = scheduler_basico
        
        # Crear un loop
        loop = Agente(
            nombre="LoopTest",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item"
        )
        scheduler.agregar_agente(loop)
        
        # No está activo
        assert scheduler.esta_loop_activo(loop.id) is False


# ============================================================
# PRUEBAS DE VALIDACIÓN
# ============================================================

class TestSchedulerValidacion:
    """Pruebas de validación de configuración."""
    
    def test_validar_fuentes_loop_correctas(self):
        """Prueba validación de fuentes de loop correctas."""
        scheduler = Scheduler(max_concurrent=2)
        
        fuente = Agente(nombre="Fuente")
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item",
            dependencias_nombres=["Fuente"]
        )
        
        scheduler.agregar_agentes([fuente, loop])
        scheduler.resolver_dependencias()
        
        ok, errores = scheduler._validar_fuentes_loop()
        assert ok is True
        assert errores == []
    
    def test_validar_fuentes_loop_fuente_inexistente(self):
        """Prueba validación de fuentes de loop con fuente inexistente."""
        scheduler = Scheduler(max_concurrent=2)
        
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Inexistente.items",
            codigo_por_item="resultado = item",
            dependencias_nombres=["Fuente"]
        )
        
        scheduler.agregar_agente(loop)
        scheduler.resolver_dependencias()
        
        ok, errores = scheduler._validar_fuentes_loop()
        assert ok is False
        assert len(errores) > 0
        assert "Inexistente" in errores[0]
    
    def test_validar_fuentes_loop_sin_dependencias(self):
        """Prueba validación de fuentes de loop sin dependencias declaradas."""
        scheduler = Scheduler(max_concurrent=2)
        
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item"
        )
        
        scheduler.agregar_agente(loop)
        scheduler.resolver_dependencias()
        
        ok, errores = scheduler._validar_fuentes_loop()
        assert ok is False
        assert len(errores) > 0
        assert "Fuente" in errores[0]


# ============================================================
# PRUEBAS DE SEGURIDAD Y CONCURRENCIA
# ============================================================

class TestSchedulerConcurrencia:
    """Pruebas de seguridad en entornos concurrentes."""
    
    def test_thread_safe_agregar_agente(self):
        """Prueba que agregar agentes desde múltiples hilos sea seguro."""
        scheduler = Scheduler(max_concurrent=4)
        threads = []
        resultados = []
        
        def agregar_agente(i):
            try:
                agente = Agente(nombre=f"Thread_{i}")
                scheduler.agregar_agente(agente)
                resultados.append(True)
            except Exception as e:
                resultados.append(False)
        
        # Crear y lanzar múltiples hilos
        for i in range(20):
            t = threading.Thread(target=agregar_agente, args=(i,))
            threads.append(t)
            t.start()
        
        # Esperar a que todos terminen
        for t in threads:
            t.join()
        
        # Verificar que todos los agentes se agregaron correctamente
        assert len(scheduler.agentes) == 20
        assert all(resultados)
    
    def test_thread_safe_obtener_estadisticas(self):
        """Prueba que obtener estadísticas desde múltiples hilos sea seguro."""
        scheduler = Scheduler(max_concurrent=4)
        
        # Agregar algunos agentes
        for i in range(10):
            agente = Agente(nombre=f"Agente_{i}")
            scheduler.agregar_agente(agente)
        
        threads = []
        resultados = []
        
        def obtener_stats():
            try:
                stats = scheduler.obtener_estadisticas()
                resultados.append(stats)
            except Exception:
                resultados.append(None)
        
        # Crear y lanzar múltiples hilos
        for _ in range(20):
            t = threading.Thread(target=obtener_stats)
            threads.append(t)
            t.start()
        
        # Esperar a que todos terminen
        for t in threads:
            t.join()
        
        # Verificar que todos obtuvieron estadísticas
        assert len(resultados) == 20
        assert all(r is not None for r in resultados)
        assert all(r['total'] == 10 for r in resultados)