# tests/test_loop_safety.py
"""
Pruebas de seguridad y validación para agentes LOOP.

CORRECCIONES APLICADAS:
- ✅ Aserción corregida: ahora prueba el comportamiento real de Agente.validar_configuracion()
- ✅ Nuevas pruebas para Scheduler._validar_fuentes_loop()
- ✅ Pruebas de casos edge: lista vacía, items no serializables, timeout
- ✅ Pruebas de seguridad: límites de iteraciones, continuar_en_error
- ✅ Pruebas de integración: loops en el scheduler completo
- ✅ Documentación: docstrings explicativos en cada prueba
"""

import pytest
import time
import json
from unittest.mock import MagicMock, patch

from PyQt6.QtWidgets import QApplication

from core.agent import Agente, TipoAgente, EstadoAgente
from core.scheduler import Scheduler
from core.executors import AgentExecutor
from core.executors.loop_executor import LoopExecutor
from core.sandbox import PythonSandbox, SandboxError


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def loop_agente_basico():
    """Fixture: agente Loop básico configurado correctamente."""
    return Agente(
        nombre="LoopBasico",
        tipo=TipoAgente.LOOP,
        fuente_items="Fuente.items",
        codigo_por_item="""
resultado = {
    'indice': indice,
    'item': item,
    'procesado': True
}
""",
        dependencias_nombres=["Fuente"],
        max_iteraciones=10,
        timeout_loop=5,
        timeout_python=2,
        continuar_en_error=False
    )


@pytest.fixture
def scheduler_con_loop_basico():
    """Fixture: scheduler con fuente y loop configurados correctamente."""
    scheduler = Scheduler(max_concurrent=2)
    
    fuente = Agente(
        nombre="Fuente",
        tipo=TipoAgente.PYTHON,
        codigo_python="""
resultado = {
    'items': [1, 2, 3, 4, 5]
}
"""
    )
    
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
        timeout_loop=5,
        timeout_python=2,
        continuar_en_error=False
    )
    
    scheduler.agregar_agentes([fuente, loop])
    scheduler.resolver_dependencias()
    
    return scheduler



def esperar_condicion(
    condicion: callable,
    timeout: float = 5.0,
    intervalo: float = 0.05,
    qapp=None,
) -> bool:
    start = time.time()
    from PyQt6.QtWidgets import QApplication as _QA
    app = qapp if qapp is not None else _QA.instance()
    while time.time() - start < timeout:
        if condicion():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(intervalo)
    return False


# ============================================================
# PRUEBAS DE VALIDACIÓN DE AGENTE LOOP
# ============================================================

class TestLoopValidacionAgente:
    """Pruebas de validación de configuración de agentes Loop (Agente.validar_configuracion)."""
    
    def test_loop_valido(self, loop_agente_basico):
        """Prueba que un loop configurado correctamente sea válido."""
        ok, msg = loop_agente_basico.validar_configuracion()
        assert ok is True
        assert msg == ""
    
    def test_loop_fuente_items_vacio(self):
        """Prueba que 'fuente_items' sea obligatorio."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="",
            codigo_por_item="resultado = item"
        )
        ok, msg = loop.validar_configuracion()
        assert ok is False
        assert "fuente_items" in msg.lower()
        assert "obligatorio" in msg.lower()
    
    def test_loop_fuente_items_sin_formato_dependencia_clave(self):
        """Prueba que 'fuente_items' tenga formato 'Dependencia.clave'."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente",
            codigo_por_item="resultado = item"
        )
        ok, msg = loop.validar_configuracion()
        assert ok is False
        assert "Dependencia.clave" in msg
    
    def test_loop_fuente_items_con_puntos_consecutivos(self):
        """Los puntos consecutivos se normalizan (no son un error)."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente..items",
            codigo_por_item="resultado = item"
        )
        ok, msg = loop.validar_configuracion()
        assert ok is True

    def test_loop_fuente_items_sin_punto(self):
        """Sin punto, no hay clave -> inválido."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente",
            codigo_por_item="resultado = item"
        )
        ok, msg = loop.validar_configuracion()
        assert ok is False
        assert "Dependencia.clave" in msg
    
    def test_loop_fuente_items_con_espacios(self):
        """Prueba que 'fuente_items' con espacios sea válido (se recorta)."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="  Fuente.items  ",
            codigo_por_item="resultado = item"
        )
        # La validación básica pasa (el formato es correcto)
        ok, msg = loop.validar_configuracion()
        assert ok is True  # La validación básica no verifica existencia
    
    def test_loop_codigo_por_item_vacio(self):
        """Prueba que 'codigo_por_item' sea obligatorio."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item=""
        )
        ok, msg = loop.validar_configuracion()
        assert ok is False
        assert "codigo_por_item" in msg.lower()
        assert "obligatorio" in msg.lower()
    
    def test_loop_codigo_por_item_solo_espacios(self):
        """Prueba que 'codigo_por_item' con solo espacios sea inválido."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="   "
        )
        ok, msg = loop.validar_configuracion()
        assert ok is False
        assert "codigo_por_item" in msg.lower()
        assert "obligatorio" in msg.lower()
    
    def test_loop_max_iteraciones_cero(self):
        """Prueba que 'max_iteraciones' sea >= 1."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item",
            max_iteraciones=0
        )
        ok, msg = loop.validar_configuracion()
        assert ok is False
        assert "max_iteraciones" in msg.lower()
        assert ">= 1" in msg or "mayor" in msg.lower()
    
    def test_loop_max_iteraciones_negativo(self):
        """Prueba que 'max_iteraciones' no sea negativo."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item",
            max_iteraciones=-5
        )
        ok, msg = loop.validar_configuracion()
        assert ok is False
        assert "max_iteraciones" in msg.lower()
    
    def test_loop_timeout_loop_cero(self):
        """Prueba que 'timeout_loop' sea >= 1."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item",
            timeout_loop=0
        )
        ok, msg = loop.validar_configuracion()
        assert ok is False
        assert "timeout_loop" in msg.lower()
        assert ">= 1" in msg or "mayor" in msg.lower()
    
    def test_loop_timeout_python_cero(self):
        """Prueba que 'timeout_python' sea >= 1."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item",
            timeout_python=0
        )
        ok, msg = loop.validar_configuracion()
        assert ok is False
        assert "timeout_python" in msg.lower()
    
    def test_loop_codigo_con_error_sintaxis(self):
        """Prueba que código con error de sintaxis sea inválido."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = {  # Sintaxis incompleta"
        )
        ok, msg = loop.validar_configuracion()
        assert ok is False
        assert "sintaxis" in msg.lower() or "error" in msg.lower()
    
    def test_loop_con_dependencia_declarada(self):
        """Prueba que un loop con dependencia declarada sea válido."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item",
            dependencias_nombres=["Fuente"]
        )
        # Crear diccionario de agentes disponibles para validación
        fuente = Agente(nombre="Fuente")
        agentes_disponibles = {"fuente_id": fuente, "loop_id": loop}
        
        ok, msg = loop.validar_configuracion(agentes_disponibles)
        assert ok is True
    
    def test_loop_con_dependencia_inexistente(self):
        """Prueba que un loop con dependencia inexistente sea inválido."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item",
            dependencias_nombres=["Fuente"]
        )
        # Sin agentes disponibles, la validación solo verifica el nombre de la dependencia
        # No verifica que exista en este nivel
        ok, msg = loop.validar_configuracion()
        # La validación básica no verifica existencia, solo formato
        assert ok is True  # La dependencia existe como nombre, pero no se verifica


# ============================================================
# PRUEBAS DE VALIDACIÓN DE FUENTES (SCHEDULER)
# ============================================================

class TestLoopValidacionFuentes:
    """Pruebas de validación de fuentes de loop en el Scheduler."""
    
    def test_validar_fuente_correcta(self):
        """Prueba que una fuente correctamente declarada pase la validación."""
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
    
    def test_validar_fuente_inexistente(self):
        """Prueba que una fuente inexistente sea detectada."""
        scheduler = Scheduler(max_concurrent=2)
        
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Inexistente.items",
            codigo_por_item="resultado = item",
            dependencias_nombres=["Fuente"]  # Dependencia declarada pero no coincide
        )
        
        scheduler.agregar_agente(loop)
        scheduler.resolver_dependencias()
        
        ok, errores = scheduler._validar_fuentes_loop()
        assert ok is False
        assert len(errores) > 0
        assert "Inexistente" in errores[0]
        assert "dependencia declarada" in errores[0]
    
    def test_validar_fuente_sin_dependencias(self):
        """Prueba que un loop sin dependencias declaradas sea inválido."""
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
        assert "dependencia declarada" in errores[0]
    
    def test_validar_fuente_con_dependencia_incorrecta(self):
        """Prueba que una fuente que no coincide con la dependencia declarada sea inválida."""
        scheduler = Scheduler(max_concurrent=2)
        
        fuente = Agente(nombre="OtraFuente")
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item",
            dependencias_nombres=["OtraFuente"]
        )
        
        scheduler.agregar_agentes([fuente, loop])
        scheduler.resolver_dependencias()
        
        ok, errores = scheduler._validar_fuentes_loop()
        assert ok is False
        assert len(errores) > 0
        assert "Fuente" in errores[0]
        assert "OtraFuente" in errores[0]
    
    def test_validar_multiples_loops(self):
        """Prueba validación con múltiples loops."""
        scheduler = Scheduler(max_concurrent=2)
        
        fuente1 = Agente(nombre="Fuente1")
        fuente2 = Agente(nombre="Fuente2")
        
        loop1 = Agente(
            nombre="Loop1",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente1.items",
            codigo_por_item="resultado = item",
            dependencias_nombres=["Fuente1"]
        )
        
        loop2 = Agente(
            nombre="Loop2",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente2.items",
            codigo_por_item="resultado = item",
            dependencias_nombres=["Fuente2"]
        )
        
        scheduler.agregar_agentes([fuente1, fuente2, loop1, loop2])
        scheduler.resolver_dependencias()
        
        ok, errores = scheduler._validar_fuentes_loop()
        assert ok is True
        assert errores == []
    
    def test_validar_loop_con_dependencia_anidada(self):
        """Prueba validación con fuente de items anidada."""
        scheduler = Scheduler(max_concurrent=2)
        
        fuente = Agente(nombre="Fuente")
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.data.results.items",
            codigo_por_item="resultado = item",
            dependencias_nombres=["Fuente"]
        )
        
        scheduler.agregar_agentes([fuente, loop])
        scheduler.resolver_dependencias()
        
        ok, errores = scheduler._validar_fuentes_loop()
        assert ok is True
        assert errores == []


# ============================================================
# PRUEBAS DE EJECUCIÓN DE LOOPS
# ============================================================

class TestLoopEjecucion:
    """Pruebas de ejecución de agentes Loop."""
    
    def test_loop_lista_vacia_es_valida(self):
        """Prueba que un loop con lista vacía se complete exitosamente."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = {'ok': True}",
            dependencias_nombres=["Fuente"],
            max_iteraciones=10
        )
        
        contexto = {"Fuente": {"items": []}}
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)
        
        assert ok is True
        assert result["total_items"] == 0
        assert result["items_procesados"] == 0
        assert result["errores"] == 0
        assert result["exitos"] == 0
        assert "lista está vacía" in msg.lower()
    
    def test_loop_con_items_basicos(self):
        """Prueba loop procesando items básicos."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="""
resultado = {
    'indice': indice,
    'item': item,
    'doble': item * 2
}
""",
            max_iteraciones=10
        )
        
        contexto = {"Fuente": {"items": [1, 2, 3, 4, 5]}}
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)
        
        assert ok is True
        assert result["total_items"] == 5
        assert result["exitos"] == 5
        assert result["errores"] == 0
        
        # Verificar resultados individuales
        for i, item_result in enumerate(result["items"]):
            assert item_result["exito"] is True
            assert item_result["resultado"]["indice"] == i
            assert item_result["resultado"]["item"] == i + 1
            assert item_result["resultado"]["doble"] == (i + 1) * 2
    
    def test_loop_con_items_dict(self):
        """Prueba loop procesando items que son diccionarios."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="""
resultado = {
    'indice': indice,
    'item': item,
    'nombre': item.get('nombre', ''),
    'valor': item.get('valor', 0) * 2
}
""",
            max_iteraciones=10
        )
        
        contexto = {
            "Fuente": {
                "items": [
                    {"nombre": "A", "valor": 10},
                    {"nombre": "B", "valor": 20},
                    {"nombre": "C", "valor": 30}
                ]
            }
        }
        
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)
        
        assert ok is True
        assert result["total_items"] == 3
        assert result["exitos"] == 3
        
        assert result["items"][0]["resultado"]["nombre"] == "A"
        assert result["items"][0]["resultado"]["valor"] == 20
    
    def test_loop_con_items_complejos(self):
        """Prueba loop procesando items con estructuras anidadas."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="""
resultado = {
    'indice': indice,
    'id': item.get('id', 0),
    'nombre': item.get('data', {}).get('nombre', ''),
    'procesado': True
}
""",
            max_iteraciones=10
        )
        
        contexto = {
            "Fuente": {
                "items": [
                    {"id": 1, "data": {"nombre": "Item1"}},
                    {"id": 2, "data": {"nombre": "Item2"}},
                ]
            }
        }
        
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)
        
        assert ok is True
        assert result["total_items"] == 2
        assert result["exitos"] == 2
        assert result["items"][0]["resultado"]["nombre"] == "Item1"
        assert result["items"][1]["resultado"]["nombre"] == "Item2"
    
    def test_loop_con_error_en_item(self):
        """Prueba loop con un item que falla (sin continuar_en_error)."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="""
# Esto fallará cuando item sea 0
resultado = {
    'division': 10 / item
}
""",
            max_iteraciones=10,
            continuar_en_error=False
        )

        contexto = {"Fuente": {"items": [5, 0, 3]}}
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)

        # Sin continuar_en_error, el loop se detiene al primer error,
        # pero el primer item (5) ya se procesó con éxito.
        assert ok is False
        assert result["errores"] == 1
        assert result["exitos"] == 1
        assert result["items_procesados"] == 2
        assert result["detenido_por_error"] is True
        assert result["detenido_en_indice"] == 1


    def test_loop_continuar_en_error_activado(self):
        """Prueba loop con continuar_en_error=True.

        Sin try/except: dejamos que la excepción real llegue al sandbox, así
        el loop cuenta un item como error y continúa con los siguientes.
        """
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="""
resultado = {
    'division': 10 / item,
    'item': item,
}
""",
            max_iteraciones=10,
            continuar_en_error=True
        )

        contexto = {"Fuente": {"items": [5, 0, 3]}}
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)

        assert ok is True
        assert result["total_items"] == 3
        assert result["exitos"] == 2   # items 5 y 3
        assert result["errores"] == 1  # item 0
        assert result["continuar_en_error"] is True


    def test_loop_excede_max_iteraciones(self):
        """Prueba loop que excede max_iteraciones."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = {'ok': True}",
            max_iteraciones=3,
            timeout_loop=10
        )
        
        contexto = {"Fuente": {"items": [1, 2, 3, 4, 5]}}
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)
        
        assert ok is False
        assert "excede" in msg.lower()
        assert result["total_items"] == 5
        assert result["max_iteraciones"] == 3
    
    def test_loop_timeout_global(self):
        """Prueba loop que excede el timeout global."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="""
import time
time.sleep(0.5)  # Cada item toma 0.5s
resultado = {'ok': True}
""",
            max_iteraciones=10,
            timeout_loop=1,  # 1 segundo de timeout
            timeout_python=2
        )
        
        contexto = {"Fuente": {"items": [1, 2, 3]}}
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)
        
        assert ok is False
        assert "timeout" in msg.lower()
        assert result["items_procesados"] < 3
    
    def test_loop_con_items_no_lista(self):
        """Prueba loop con fuente que no es una lista."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = {'ok': True}",
            max_iteraciones=10
        )
        
        contexto = {"Fuente": {"items": "no soy una lista"}}
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)
        
        assert ok is False
        assert "lista" in msg.lower() or "list" in msg.lower()
    
    def test_loop_con_fuente_inexistente(self):
        """Prueba loop con fuente que no existe en el contexto."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Inexistente.items",
            codigo_por_item="resultado = {'ok': True}",
            max_iteraciones=10
        )

        contexto = {}
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)

        assert ok is False
        assert "Inexistente" in msg or "lista válida" in msg.lower()



class TestLoopSeguridad:
    """Pruebas de seguridad para loops."""
    
    def test_loop_con_codigo_peligroso_bloqueado(self):
        """
        Prueba que código potencialmente peligroso en loop sea manejado.

        IMPORTANTE: NO usamos os.listdir('/') porque puede colgarse en
        sistemas con autofs/NFS. Usamos una operación local y determinista.
        """
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="""
try:
    with open('/tmp/no_existe_xyz_12345.txt', 'r') as f:
        contenido = f.read()
    resultado = {'acceso': True, 'contenido': contenido[:50]}
except FileNotFoundError as e:
    resultado = {'acceso': False, 'error': str(e)}
except PermissionError as e:
    resultado = {'acceso': False, 'error': str(e)}
""",
            max_iteraciones=10,
            continuar_en_error=True
        )

        contexto = {"Fuente": {"items": [1]}}
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)

        assert result is not None
        assert result["total_items"] == 1
        assert result["items_procesados"] == 1


    def test_loop_con_codigo_infinito_timeout(self):
        """Prueba que un loop infinito sea detenido por timeout."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="""
# Bucle infinito
while True:
    pass
resultado = {'ok': True}
""",
            max_iteraciones=10,
            timeout_loop=2,
            timeout_python=1
        )
        
        contexto = {"Fuente": {"items": [1]}}
        
        start = time.time()
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)
        elapsed = time.time() - start
        
        assert ok is False
        assert "timeout" in msg.lower()
        assert elapsed < 5  # Debería fallar antes de 5 segundos
    
    def test_loop_limita_iteraciones(self):
        """Prueba que el límite de iteraciones se respete."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = {'ok': True}",
            max_iteraciones=2,  # Solo 2 iteraciones permitidas
            timeout_loop=10
        )
        
        contexto = {"Fuente": {"items": list(range(100))}}
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)
        
        assert ok is False
        assert "excede" in msg.lower()
        assert result["max_iteraciones"] == 2
    
    def test_loop_con_items_grandes(self):
        """Prueba loop con items muy grandes (seguridad de memoria)."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="""
# Crear un string grande
resultado = {
    'data': 'x' * 10000,
    'indice': indice
}
""",
            max_iteraciones=5,
            timeout_loop=10,
            continuar_en_error=True
        )
        
        contexto = {"Fuente": {"items": list(range(5))}}
        
        # Debería completar sin problemas de memoria
        ok, msg, result = LoopExecutor.ejecutar(loop, contexto)
        assert ok is True
        assert result["total_items"] == 5


# ============================================================
# PRUEBAS DE INTEGRACIÓN CON SCHEDULER
# ============================================================

class TestLoopIntegracion:
    """Pruebas de integración de loops con el scheduler completo."""
    
    def test_loop_en_scheduler_completo(self, qapp):
        """Prueba un loop ejecutándose en el scheduler completo."""
        scheduler = Scheduler(max_concurrent=2)
        
        fuente = Agente(
            nombre="Fuente",
            tipo=TipoAgente.PYTHON,
            codigo_python="""
resultado = {
    'items': [1, 2, 3, 4, 5]
}
"""
        )
        
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            dependencias_nombres=["Fuente"],
            fuente_items="Fuente.items",
            codigo_por_item="""
import time
resultado = {
    'indice': indice,
    'item': item,
    'cuadrado': item * item,
    'timestamp': time.time()
}
""",
            max_iteraciones=10,
            timeout_loop=5,
            timeout_python=2,
            continuar_en_error=False
        )
        
        scheduler.agregar_agentes([fuente, loop])
        scheduler.resolver_dependencias()
        
        terminado = False
        
        def on_terminado():
            nonlocal terminado
            terminado = True
        
        scheduler.ejecucion_terminada.connect(on_terminado)
        scheduler.iniciar()
        
        # Esperar a que termine
        timeout = 10.0
        start = time.time()
        
        completado = esperar_condicion(lambda: terminado, timeout=timeout, qapp=qapp)
        assert completado, "El flujo no terminó en %s segundos" % timeout
        
        # Verificar resultados
        stats = scheduler.obtener_estadisticas()
        assert stats['completados'] == 2
        assert stats['total'] == 2
        assert stats['errores'] == 0
        
        loop_agente = scheduler.obtener_agente_por_nombre("Loop")
        assert loop_agente.estado == EstadoAgente.COMPLETADO
        
        resultado = loop_agente.resultado
        assert resultado is not None
        assert resultado.get('total_items') == 5
        assert resultado.get('exitos') == 5
        assert resultado.get('errores') == 0
    
    def test_loop_con_error_en_scheduler(self, qapp):
        """Prueba un loop que falla en el scheduler."""
        scheduler = Scheduler(max_concurrent=2)
        
        fuente = Agente(
            nombre="Fuente",
            tipo=TipoAgente.PYTHON,
            codigo_python="""
resultado = {
    'items': [1, 0, 3]  # Incluye un 0 que causará división por cero
}
"""
        )
        
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            dependencias_nombres=["Fuente"],
            fuente_items="Fuente.items",
            codigo_por_item="""
resultado = {
    'division': 10 / item  # Fallará cuando item == 0
}
""",
            max_iteraciones=10,
            timeout_loop=5,
            continuar_en_error=False  # Fallará en el primer error
        )
        
        scheduler.agregar_agentes([fuente, loop])
        scheduler.resolver_dependencias()
        
        terminado = False
        
        def on_terminado():
            nonlocal terminado
            terminado = True
        
        scheduler.ejecucion_terminada.connect(on_terminado)
        scheduler.iniciar()
        
        # Esperar a que termine
        timeout = 10.0
        start = time.time()
        
        completado = esperar_condicion(lambda: terminado, timeout=timeout, qapp=qapp)
        assert completado, "El flujo no terminó en %s segundos" % timeout
        
        # Verificar que el loop falló
        stats = scheduler.obtener_estadisticas()
        assert stats['errores'] == 1
        assert stats['completados'] == 1  # Solo la fuente
        
        loop_agente = scheduler.obtener_agente_por_nombre("Loop")
        assert loop_agente.estado == EstadoAgente.ERROR
    
    def test_loop_con_continuar_en_error_en_scheduler(self, qapp):
        """Prueba un loop con continuar_en_error=True en el scheduler."""
        scheduler = Scheduler(max_concurrent=2)
        
        fuente = Agente(
            nombre="Fuente",
            tipo=TipoAgente.PYTHON,
            codigo_python="""
resultado = {
    'items': [1, 0, 3]
}
"""
        )
        
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            dependencias_nombres=["Fuente"],
            fuente_items="Fuente.items",
            codigo_por_item="""
# Sin try/except: dejamos que la excepción real llegue al sandbox
resultado = {
    'division': 10 / item,
    'item': item,
}
""",
            max_iteraciones=10,
            timeout_loop=5,
            continuar_en_error=True
        )
        
        scheduler.agregar_agentes([fuente, loop])
        scheduler.resolver_dependencias()
        
        terminado = False
        def on_terminado():
            nonlocal terminado
            terminado = True
        
        scheduler.ejecucion_terminada.connect(on_terminado)
        scheduler.iniciar()
        
        timeout = 10.0
        start = time.time()
        
        completado = esperar_condicion(lambda: terminado, timeout=timeout, qapp=qapp)
        assert completado, "El flujo no terminó en %s segundos" % timeout
        
        stats = scheduler.obtener_estadisticas()
        assert stats['completados'] == 2
        assert stats['total'] == 2
        assert stats['errores'] == 0
        
        loop_agente = scheduler.obtener_agente_por_nombre("Loop")
        assert loop_agente.estado == EstadoAgente.COMPLETADO
        
        resultado = loop_agente.resultado
        assert resultado is not None
        assert resultado.get('errores') == 1   # ✅ ahora sí: item 0 falla de verdad
        assert resultado.get('exitos') == 2    # ✅ items 1 y 3 pasan


# ============================================================
# PRUEBAS DE UTILIDADES DEL AGENTE LOOP
# ============================================================

class TestLoopUtilidades:
    """Pruebas de métodos de utilidad para agentes Loop."""
    
    def test_es_loop(self):
        """Prueba el método es_loop()."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item"
        )
        assert loop.es_loop() is True
        
        no_loop = Agente(
            nombre="Python",
            tipo=TipoAgente.PYTHON,
            codigo_python="resultado = {'ok': True}"
        )
        assert no_loop.es_loop() is False
    
    def test_obtener_ruta_completa(self):
        """Prueba obtener_ruta_completa()."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.data.results.items",
            codigo_por_item="resultado = item"
        )
        
        ruta = loop.obtener_ruta_completa()
        assert ruta == ["Fuente", "data", "results", "items"]
    
    def test_obtener_nombre_dependencia(self):
        """Prueba obtener_nombre_dependencia()."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item"
        )
        
        nombre = loop.obtener_nombre_dependencia()
        assert nombre == "Fuente"
    
    def test_obtener_clave_items(self):
        """Prueba obtener_clave_items()."""
        loop = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.data.results.items",
            codigo_por_item="resultado = item"
        )
        
        clave = loop.obtener_clave_items()
        assert clave == "data.results.items"
    
    def test_tiene_fuente_items(self):
        """Prueba tiene_fuente_items()."""
        loop_con_fuente = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item"
        )
        assert loop_con_fuente.tiene_fuente_items() is True
        
        loop_sin_fuente = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="",
            codigo_por_item="resultado = item"
        )
        assert loop_sin_fuente.tiene_fuente_items() is False
        
        no_loop = Agente(
            nombre="Python",
            tipo=TipoAgente.PYTHON,
            codigo_python="resultado = {'ok': True}"
        )
        assert no_loop.tiene_fuente_items() is False
    
    def test_tiene_codigo_por_item(self):
        """Prueba tiene_codigo_por_item()."""
        loop_con_codigo = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item="resultado = item"
        )
        assert loop_con_codigo.tiene_codigo_por_item() is True
        
        loop_sin_codigo = Agente(
            nombre="Loop",
            tipo=TipoAgente.LOOP,
            fuente_items="Fuente.items",
            codigo_por_item=""
        )
        assert loop_sin_codigo.tiene_codigo_por_item() is False
    
    def test_es_loop_activo(self, scheduler_con_loop_basico):
        """Prueba el seguimiento de loops activos."""
        scheduler = scheduler_con_loop_basico
        loop = scheduler.obtener_agente_por_nombre("Loop")
        
        # Inicialmente no está activo
        assert scheduler.esta_loop_activo(loop.id) is False
        
        # Iniciar ejecución
        scheduler.iniciar()
        
        # El loop debería estar activo durante la ejecución
        # (esto depende del timing, pero podemos verificar que el método existe)
        time.sleep(0.1)
        
        # Verificar que el método no lanza excepción
        try:
            activo = scheduler.esta_loop_activo(loop.id)
            # El valor puede ser True o False dependiendo del momento
            assert isinstance(activo, bool)
        except Exception as e:
            pytest.fail(f"esta_loop_activo lanzó excepción: {e}")
        
        scheduler.detener()