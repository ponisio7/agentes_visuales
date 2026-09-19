# tests/test_postprocesar_plan.py
from core.problem_solver.solver import ProblemSolver

def test_postprocesar_reemplaza_ensamblar_html():
    plan = {
        'pasos': [
            {'nombre': 'GenerarEstructuraHTML', 'tipo': 'LLM', 'configuracion': {}},
            {'nombre': 'GenerarCSS', 'tipo': 'LLM', 'configuracion': {}},
            {'nombre': 'GenerarJS', 'tipo': 'LLM', 'configuracion': {}},
            {
                'nombre': 'EnsamblarHTML',
                'tipo': 'Python',
                'configuracion': {'codigo': '# código viejo del LLM'},
            },
        ]
    }
    plan2 = ProblemSolver._postprocesar_plan(plan)
    paso = next(p for p in plan2['pasos'] if p['nombre'] == 'EnsamblarHTML')
    assert 'ensamblar_calculadora_html' in paso['configuracion']['codigo']
    assert 'código viejo del LLM' not in paso['configuracion']['codigo']
    print('✅ test_postprocesar_reemplaza_ensamblar_html OK')

def test_postprocesar_no_toca_pasos_no_html():
    plan = {
        'pasos': [
            {'nombre': 'EscribirArchivo', 'tipo': 'Python', 'configuracion': {'codigo': 'x = 1'}},
        ]
    }
    plan2 = ProblemSolver._postprocesar_plan(plan)
    assert plan2['pasos'][0]['configuracion']['codigo'] == 'x = 1'
    print('✅ test_postprocesar_no_toca_pasos_no_html OK')

if __name__ == '__main__':
    test_postprocesar_reemplaza_ensamblar_html()
    test_postprocesar_no_toca_pasos_no_html()
    print('✅ Todos los tests pasaron')