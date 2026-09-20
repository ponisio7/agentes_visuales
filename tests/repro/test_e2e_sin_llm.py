"""Repro end-to-end sin LLM real: plan sintético -> builder -> validator -> sandbox."""
import json, logging, os, sys, tempfile
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

from core.problem_solver.builder import PlanBuilder
from core.problem_solver.code_corrector import PythonCodeCorrector
from core.problem_solver.models import ExecutionPlan, StepPlan
from core.problem_solver.validator import PlanValidator

log = logging.getLogger("repro")
val = PlanValidator(log)
builder = PlanBuilder(log, PythonCodeCorrector(), val)

# Plan realista: el LLM produce un paso Python que escribe el documento.
# 'cuento' es la dependencia semántica que el paso declara.
plan = ExecutionPlan(problema_original="cuento con imagen a documento")
pasos = [
    StepPlan(orden=1, nombre="GenerarCuento", tipo_agente="Python",
             dependencia_ids=[],
             configuracion={"codigo": (
                 "historia = ('Habia una vez tres albondigas frias que sonaban con ser "
                 "servidas en un plato de porcelana. Una de ellas, la mas pequena, "
                 "sonaba con viajar por el mundo y conocer otras cocinas. ' * 6)\n"
                 "resultado = {'cuento': historia, 'descripciones_imagenes': ['plato']}"
             )}),
    StepPlan(orden=2, nombre="CrearDocumento", tipo_agente="Python",
             dependencia_ids=["GenerarCuento"],
             configuracion={"codigo": (
                 "texto = respuesta\n"
                 "if not texto or len(str(texto)) < 200:\n"
                 "    raise ValueError('El cuento esta vacio o es demasiado corto')\n"
                 "resultado = {'documento': str(texto)}"
             )}),
]
plan.pasos = pasos

print("=" * 70)
print("PASO A: que codigo entrega el corrector al sandbox")
print("=" * 70)
for p in pasos:
    corregido = PythonCodeCorrector.corregir(p.configuracion["codigo"], p.dependencia_ids)
    p.configuracion["codigo"] = corregido
    print(f"[{p.nombre}] deps={p.dependencia_ids}")
    print(corregido)
    print("-" * 40)

print("=" * 70)
print("PASO B: el validador, antes de ejecutar, dice:")
print("=" * 70)
plan.agentes_generados = builder.generar_agentes(plan)
ok, errores = val.validar_plan(plan)
print("valido:", ok)
for e in errores:
    print("  -", e)
if ok:
    print("  >>> El validador ACEPTA el plan (no detecta el binding roto)")

print("=" * 70)
print("PASO C: ejecutar los agentes como lo hace el scheduler")
print("=" * 70)
from core.agent import EstadoAgente
from core.executors.dispatcher import AgentExecutor

contexto = {}
for agente in plan.agentes_generados:
    agente.estado = EstadoAgente.EJECUTANDO
    exito, mensaje, resultado = AgentExecutor.ejecutar(agente, contexto)
    print(f"[{agente.nombre}] exito={exito} msg={mensaje}")
    print(f"    resultado={str(resultado)[:300]}")
    if exito:
        agente.resultado = resultado
        agente.estado = EstadoAgente.COMPLETADO
        contexto[agente.nombre] = resultado
        contexto[agente.id] = resultado
    else:
        agente.estado = EstadoAgente.ERROR
    print(f"    claves de contexto al salir: {sorted(k for k in contexto if not k.startswith('_'))[:6]}")
