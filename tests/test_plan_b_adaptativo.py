# tests/test_plan_b_adaptativo.py
"""Plan B adaptativo (H7): límites configurables, anti-repetición, escalera
de estrategias, reutilización de agentes válidos y traza en reparaciones_plan.

Sin LLM real ni red: el recovery se sustituye por un doble controlado.
"""
from core.agent import Agente, EstadoAgente, TipoAgente
from core.plan_recovery import (
    ESTRATEGIAS,
    configuracion_plan_b,
    estrategia_para_intento,
    firma_agente,
    firma_plan,
    formatear_error_estructurado,
    registrar_reparacion,
)
from core.problem_solver.models import ExecutionPlan, StepPlan
from core.scheduler import Scheduler

# ============================================================
# ESCALERA DE ESTRATEGIAS Y FIRMAS
# ============================================================

def test_escalera_de_estrategias_diversifica():
    vistas = [estrategia_para_intento(i) for i in range(1, len(ESTRATEGIAS) + 1)]
    assert len(set(vistas)) == len(ESTRATEGIAS)
    # Pasado el último escalón, se mantiene el fallback (no hay infinitos).
    assert estrategia_para_intento(99) == ESTRATEGIAS[-1]


def test_configuracion_plan_b_por_entorno(monkeypatch):
    monkeypatch.setenv("AGENTES_PLAN_B_MAX_INTENTOS", "7")
    monkeypatch.setenv("AGENTES_PLAN_B_MAX_SEGUNDOS", "42")
    cfg = configuracion_plan_b()
    assert cfg["max_intentos"] == 7
    assert cfg["presupuesto_seg"] == 42.0


def test_firma_de_agente_ignora_estado_y_resultado():
    a = Agente(nombre="A", tipo=TipoAgente.PYTHON, codigo_python="resultado = {}")
    b = Agente(nombre="A", tipo=TipoAgente.PYTHON, codigo_python="resultado = {}")
    b.estado = EstadoAgente.COMPLETADO
    b.resultado = {"x": 1}
    assert firma_agente(a) == firma_agente(b)

    c = Agente(nombre="A", tipo=TipoAgente.PYTHON, codigo_python="resultado = {'y': 2}")
    assert firma_agente(a) != firma_agente(c)


def test_firma_de_plan_igual_para_planes_equivalentes():
    def _plan():
        return ExecutionPlan(problema_original="p", pasos=[
            StepPlan(nombre="A", tipo_agente="Python", configuracion={"codigo": "x"}),
            StepPlan(nombre="B", tipo_agente="File", dependencia_ids=["A"],
                     configuracion={"operacion": "escribir", "archivo_destino": "o.txt"}),
        ])

    assert firma_plan(_plan()) == firma_plan(_plan())

    otro = _plan()
    otro.pasos[1] = StepPlan(nombre="B", tipo_agente="File", dependencia_ids=["A"],
                             configuracion={"operacion": "escribir", "archivo_destino": "otro.txt"})
    assert firma_plan(_plan()) != firma_plan(otro)


def test_error_estructurado_incluye_paso_tipo_estrategia_y_previos():
    agente = Agente(nombre="GenerarDocumento", tipo=TipoAgente.PYTHON)
    texto = formatear_error_estructurado(
        agente,
        "DOCX creado pero no contiene imagen",
        "cambiar_tipo_agente",
        errores_previos=[{"intento": 1, "estrategia": "correccion_puntual",
                          "agente": "GenerarDocumento", "error": "sin imagen"}],
    )
    assert "GenerarDocumento" in texto
    assert "Python" in texto
    assert "cambiar_tipo_agente" in texto
    assert "intento 1" in texto
    assert "NO REPETIR" in texto


# ============================================================
# LÍMITES CONFIGURABLES
# ============================================================

def test_limites_plan_b_configurables_por_argumento():
    s0 = Scheduler(max_concurrent=1, max_intentos_plan_b=0)
    s5 = Scheduler(max_concurrent=1, max_intentos_plan_b=5)
    try:
        assert s0._max_intentos_plan_b == 0
        assert s5._max_intentos_plan_b == 5
        # Con 0 intentos, el turno de Plan B nunca se concede.
        s0.recovery = object()
        assert s0._reclamar_plan_b() is False
    finally:
        for s in (s0, s5):
            s._executor.shutdown(wait=True, cancel_futures=True)


def test_presupuesto_de_tiempo_bloquea_plan_b():
    import time

    s = Scheduler(max_concurrent=1, max_intentos_plan_b=10,
                  presupuesto_plan_b_seg=0.01)
    try:
        s.recovery = object()
        assert s._reclamar_plan_b() is True
        s._plan_b_en_progreso = False
        s._plan_b_inicio = time.time() - 10
        assert s._reclamar_plan_b() is False
    finally:
        s._executor.shutdown(wait=True, cancel_futures=True)


# ============================================================
# TRAZA EN reparaciones_plan
# ============================================================

def test_registrar_reparacion_guarda_la_traza(tmp_path):
    import sqlite3

    db = tmp_path / "hist.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""
            CREATE TABLE reparaciones_plan (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                problema TEXT NOT NULL, tipo TEXT NOT NULL, fecha TEXT NOT NULL,
                ejecucion_id INTEGER, intento INTEGER DEFAULT 0,
                agente TEXT DEFAULT '', error TEXT DEFAULT '',
                estrategia TEXT DEFAULT '', plan_firma TEXT DEFAULT '',
                resultado TEXT DEFAULT '', exito INTEGER DEFAULT 0
            )
        """)
        conn.commit()

    assert registrar_reparacion(
        str(db),
        problema="crea un docx con imagen",
        intento=2,
        agente="GenerarDocumento",
        error="sin imagen",
        estrategia="cambiar_tipo_agente",
        plan_firma="abc123",
        resultado="plan_generado",
        exito=True,
    ) is True

    with sqlite3.connect(db) as conn:
        fila = conn.execute(
            "SELECT problema, intento, agente, estrategia, plan_firma, exito "
            "FROM reparaciones_plan"
        ).fetchone()
    assert fila == ("crea un docx con imagen", 2, "GenerarDocumento",
                    "cambiar_tipo_agente", "abc123", 1)


def test_registrar_reparacion_no_rompe_sin_tabla(tmp_path):
    assert registrar_reparacion(
        str(tmp_path / "vacia.db"), problema="p", intento=1, estrategia="x"
    ) is False


# ============================================================
# REUTILIZACIÓN DE AGENTES VÁLIDOS
# ============================================================

class _PlanFalso:
    def __init__(self, agentes):
        self.agentes_generados = agentes
        self.pasos = []


class _RecoveryFalso:
    db_path = ""

    def __init__(self, plan):
        self._plan = plan
        self.llamadas = []

    def generar_plan_b(self, **kwargs):
        self.llamadas.append(kwargs)
        return self._plan


def test_plan_b_reutiliza_agentes_completados_validos(monkeypatch, tmp_path):
    scheduler = Scheduler(max_concurrent=1)
    try:
        # Agente A ya completado y verificado.
        a = Agente(nombre="A", tipo=TipoAgente.PYTHON, codigo_python="resultado = {'x': 1}")
        a.estado = EstadoAgente.COMPLETADO
        a.resultado = {"x": 1}
        scheduler.agregar_agente(a)
        scheduler._verificaciones[a.id] = {"aceptado": True, "motivos": []}

        b = Agente(nombre="B", tipo=TipoAgente.PYTHON, codigo_python="resultado = {'y': 2}")
        fallido = Agente(nombre="D", tipo=TipoAgente.PYTHON)

        plan_viejo = ExecutionPlan(problema_original="p", pasos=[
            StepPlan(nombre="A", tipo_agente="Python"),
            StepPlan(nombre="D", tipo_agente="Python"),
        ])
        scheduler._problema_original = "p"
        scheduler._plan_original = plan_viejo

        # El plan B repite A (idéntico) y añade B (nuevo).
        plan_b = _PlanFalso([a, b])
        scheduler.recovery = _RecoveryFalso(plan_b)
        monkeypatch.setattr(scheduler, "iniciar", lambda *args, **kwargs: None)

        assert scheduler._intentar_plan_b(fallido, "falló la aceptación") is True

        reutilizado = scheduler.obtener_agente_por_nombre("A")
        assert reutilizado.estado == EstadoAgente.COMPLETADO
        assert reutilizado.resultado == {"x": 1}
        assert "reutilizado" in reutilizado.mensaje

        nuevo = scheduler.obtener_agente_por_nombre("B")
        assert nuevo.estado != EstadoAgente.COMPLETADO

        # El intento se registró con estrategia y anti-repetición.
        kwargs = scheduler.recovery.llamadas[0]
        assert kwargs["estrategia"] == ESTRATEGIAS[0]
        assert kwargs["intento"] == 1
        assert kwargs["firmas_fallidas"]
    finally:
        try:
            scheduler.detener()
        except Exception:
            pass
        scheduler._executor.shutdown(wait=True, cancel_futures=True)


def test_plan_b_anti_repeticion_descarta_plan_repetido(monkeypatch):
    """Un plan con la misma firma que uno fallido no se acepta."""
    from core.plan_recovery import PlanRecovery

    agente = Agente(nombre="A", tipo=TipoAgente.PYTHON, codigo_python="resultado = {}")

    class _Builder:
        def construir_plan(self, problema, plan_dict):
            return ExecutionPlan(problema_original=problema, pasos=[])

        def generar_agentes(self, plan):
            return [agente]

    class _Solver:
        builder = _Builder()
        validator = None

    class _LLM:
        def chat(self, **kwargs):
            return '{"pasos": [{"nombre": "A", "tipo": "Python", "configuracion": {"codigo": "resultado = {}"}}]}'

    recovery = PlanRecovery(_LLM(), _Solver(), ":memory:")
    # La anti-repetición compara firmas de PLAN (no de agente suelto).
    plan_repetido = ExecutionPlan(problema_original="p")
    plan_repetido.agentes_generados = [agente]
    firma = firma_plan(plan_repetido)

    plan = recovery.generar_plan_b(
        problema_original="p",
        plan_fallido=ExecutionPlan(problema_original="p"),
        agente_fallido=agente,
        error="boom",
        firmas_fallidas={firma},
    )

    assert plan is None

    # Sin la firma en el conjunto, el mismo plan sí se acepta.
    plan_ok = recovery.generar_plan_b(
        problema_original="p",
        plan_fallido=ExecutionPlan(problema_original="p"),
        agente_fallido=agente,
        error="boom",
        firmas_fallidas=set(),
    )
    assert plan_ok is not None
