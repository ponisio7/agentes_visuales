# core/problem_solver/cli.py
"""
Helpers de línea de comandos y ejecución de planes de prueba.
Extraído literalmente de core/problem_solver.py (monolito) — Paso 6.

Contiene:
    - ejecutar_plan_prueba(): ejecuta un ExecutionPlan completo para pruebas.
    - main(): bloque __main__ que pide un problema por stdin, genera un plan
      y opcionalmente lo ejecuta. Se expone como `main()` para poder
      invocarse desde un script externo si hiciera falta.

NOTA: `ejecutar_plan_prueba` importa Scheduler de forma diferida
(dentro de la función) para evitar ciclos: Scheduler no depende de
ProblemSolver, pero mantener el import local preserva el comportamiento
del monolito y evita cargar el scheduler si solo se importa el paquete
para otra cosa.
"""
import logging
import os
import sys
import time
from typing import Optional

from .models import ExecutionPlan


def ejecutar_plan_prueba(plan: ExecutionPlan, mostrar_detalle: bool = True):
    """Ejecuta un plan generado para pruebas."""
    from core.scheduler import Scheduler

    print("\n" + "=" * 70)
    print("🚀 EJECUTANDO PLAN DE PRUEBA")
    print("=" * 70)

    if not plan.agentes_generados:
        print("❌ No hay agentes para ejecutar")
        return None

    scheduler = Scheduler(max_concurrent=2)

    print(f"\n📦 Registrando {len(plan.agentes_generados)} agente(s):")
    for agente in plan.agentes_generados:
        scheduler.agregar_agente(agente)
        print(f"   ✅ {agente.nombre} ({agente.tipo.value})")
        if agente.dependencias_nombres:
            print(f"      ↳ Depende de: {', '.join(agente.dependencias_nombres)}")

    print("\n🔗 Resolviendo dependencias...")
    scheduler.resolver_dependencias()

    tiene_ciclos, ciclos = scheduler.detectar_ciclos()
    if tiene_ciclos:
        print(f"❌ Se detectaron ciclos: {ciclos}")
        return None

    print("\n▶️ Iniciando ejecución...\n")
    scheduler.iniciar()

    inicio = time.time()
    ultimo_progreso = -1

    while scheduler.ejecutando:
        time.sleep(0.3)
        stats = scheduler.obtener_estadisticas()
        total = stats.get('total', 0)
        completados = stats.get('completados', 0)
        errores = stats.get('errores', 0)
        cancelados = stats.get('cancelados', 0)
        ejecutando = stats.get('ejecutando', 0)

        if total > 0:
            progreso = completados + errores + cancelados
            if progreso != ultimo_progreso:
                ultimo_progreso = progreso
                elapsed = time.time() - inicio
                barra = "█" * int((progreso / total) * 20) + "░" * (20 - int((progreso / total) * 20))
                print(
                    f"\r   [{barra}] {progreso}/{total} | "
                    f"✅{completados} ❌{errores} ⚡{ejecutando} ⏱{elapsed:.1f}s",
                    end=''
                )

    print("\n")

    elapsed = time.time() - inicio
    stats = scheduler.obtener_estadisticas()

    print("=" * 70)
    print("📊 RESUMEN DE EJECUCIÓN")
    print("=" * 70)
    print(f"⏱ Tiempo total: {elapsed:.2f}s")
    print(f"📊 Total agentes: {stats['total']}")
    print(f"   ✅ Completados: {stats['completados']}")
    print(f"   ❌ Errores: {stats['errores']}")
    print(f"   ⛔ Cancelados: {stats['cancelados']}")

    if mostrar_detalle:
        print("\n📋 DETALLE POR AGENTE:")
        for agente in scheduler.agentes.values():
            if agente.estado.value == "Completado":
                print(f"   ✅ {agente.nombre}: {agente.mensaje}")
                if agente.resultado:
                    resumen = str(agente.resultado)
                    if len(resumen) > 200:
                        resumen = resumen[:200] + "..."
                    print(f"      📊 Resultado: {resumen}")
            elif agente.estado.value == "Error":
                print(f"   ❌ {agente.nombre}: {agente.error}")
            elif agente.estado.value == "Cancelado":
                print(f"   ⛔ {agente.nombre}: Cancelado")

    print("\n" + "=" * 70)
    print("✅ EJECUCIÓN COMPLETADA")
    print("=" * 70)

    return scheduler


def main():
    """Entrypoint de prueba: pide un problema, genera un plan, ofrece ejecutarlo."""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("=" * 70)
    print("🧠 PROBLEM SOLVER - PRUEBA DE CONCEPTO")
    print("=" * 70)

    if not os.environ.get('DEEPSEEK_API_KEY'):
        print("\n⚠️  No se encontró DEEPSEEK_API_KEY en variables de entorno.")
        print("   Configúrala con: export DEEPSEEK_API_KEY='tu-api-key'")
        sys.exit(0)

    problema = input("\n📝 Describe el problema que quieres resolver: ")
    if not problema.strip():
        problema = "Conseguir los programas más forkeados de GitHub y guardar como github10.txt"
        print(f"   Usando problema de ejemplo: {problema}")

    print("\n🚀 Generando plan... (esto puede tomar unos segundos)\n")

    try:
        # Import diferido para evitar cargar el solver (y por ende el LLM
        # client) si este módulo solo se importa para `ejecutar_plan_prueba`.
        from .solver import ProblemSolver

        solver = ProblemSolver()
        plan = solver.resolver_problema(problema, max_pasos=6)

        print("✅ PLAN GENERADO EXITOSAMENTE")
        print(f"📌 Título: {plan.titulo}")
        print(f"📊 Pasos: {len(plan.pasos)}")
        print(f"🤖 Agentes: {len(plan.agentes_generados)}")
        print(f"📈 Complejidad: {solver.estimar_complejidad(plan)}")
        print(f"⏱ Tiempo estimado: {solver.estimar_tiempo(plan)}s")

        if plan.advertencias:
            print("\n⚠️ ADVERTENCIAS:")
            for adv in plan.advertencias:
                print(f"   • {adv}")

        print("\n📋 DETALLE DE PASOS:")
        for paso in plan.pasos:
            print(f"   {paso.orden}. {paso.nombre} ({paso.tipo_agente})")
            if paso.dependencia_ids:
                print(f"      ↳ Depende de: {', '.join(paso.dependencia_ids)}")
            print(f"      📝 {paso.descripcion[:60]}...")
            config = paso.configuracion
            if 'operacion' in config:
                print(f"      ⚙️ Operación: {config.get('operacion')}")
            if 'url' in config:
                print(f"      🔗 URL: {config.get('url')[:60]}...")
            if 'archivo_destino' in config:
                print(f"      📁 Destino: {config.get('archivo_destino')}")

        print("\n📄 DSL GENERADO:")
        print("-" * 50)
        print(solver.generar_dsl(plan))
        print("-" * 50)

        print("\n" + "-" * 50)
        respuesta = input("🔧 ¿Ejecutar el plan ahora? (s/N): ").strip().lower()
        if respuesta in ('s', 'si', 'sí', 'y', 'yes'):
            ejecutar_plan_prueba(plan, mostrar_detalle=True)
        else:
            print("\nℹ️ Puedes ejecutar el plan más tarde desde la interfaz principal.")

        print("\n✅ Prueba completada con éxito.")

    except KeyboardInterrupt:
        print("\n\n⏹ Ejecución interrumpida por el usuario")
        sys.exit(0)
    except Exception as e:
        print(f"❌ Error en la prueba: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()