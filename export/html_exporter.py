# export/html_exporter.py
"""
Exportador HTML con estilos y gráficos embebidos.
"""

from datetime import datetime
from typing import List, Dict, Tuple

from .models import ExportConfig
from .utils import COLORES_ESTADO, MAX_HTML_ROWS


def exportar_html(
    agentes: List[Dict],
    ruta: str,
    config: ExportConfig
) -> Tuple[int, List[str], List[str]]:
    """Exporta a HTML con estilos y gráficos."""
    errores: List[str] = []
    advertencias: List[str] = []

    try:
        # Aplicar límite
        data = agentes
        if config.limit_rows:
            data = data[:min(config.limit_rows, MAX_HTML_ROWS)]
            if len(data) < len(agentes):
                advertencias.append(f"Limitado a {MAX_HTML_ROWS} filas para HTML")

        # Estadísticas
        total = len(agentes)
        completados = sum(1 for a in agentes if a.get('estado') == "Completado")
        errores_count = sum(1 for a in agentes if a.get('estado') == "Error")

        # Construir HTML
        html = _generar_html(data, total, completados, errores_count, config)

        with open(ruta, 'w', encoding=config.encoding) as f:
            f.write(html)

        return len(data), errores, advertencias

    except Exception as e:
        errores.append(f"Error exportando HTML: {str(e)}")
        return 0, errores, advertencias


def _generar_html(
    data: List[Dict],
    total: int,
    completados: int,
    errores: int,
    config: ExportConfig
) -> str:
    """Genera el contenido HTML."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    estilos = _obtener_estilos_html(config.estilo)

    # Tabla de datos
    if data:
        headers = sorted(set().union(*[d.keys() for d in data if isinstance(d, dict)]))
        headers = [h for h in headers if h not in ['resultado', 'salida', 'error']]

        rows_html = ""
        for row in data:
            estado = row.get('estado', 'Desconocido')
            color = COLORES_ESTADO.get(estado, '#6c757d')

            rows_html += '<tr style="border-bottom: 1px solid #dee2e6;">'
            for header in headers:
                value = row.get(header, '')
                if header == 'estado':
                    value = (
                        f'<span style="background-color: {color}; color: white; '
                        f'padding: 2px 8px; border-radius: 4px; font-size: 12px;">'
                        f'{value}</span>'
                    )
                elif header == 'duracion':
                    value = f"{value:.2f}s" if isinstance(value, (int, float)) else value
                rows_html += f'<td style="padding: 8px;">{value}</td>'
            rows_html += '</tr>'
    else:
        rows_html = (
            '<tr><td colspan="100%" style="text-align: center; '
            'padding: 40px; color: #999;">No hay datos para mostrar</td></tr>'
        )

    grafico_html = (
        _generar_grafico_html(completados, errores, total)
        if config.incluir_graficos else ""
    )

    html = f"""
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Reporte de Agentes - {timestamp}</title>
    <style>
        {estilos}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📊 Reporte de Ejecución de Agentes</h1>
            <div class="timestamp">Generado: {timestamp}</div>
        </div>

        <div class="stats">
            <div class="stat-box">
                <div class="stat-number">{total}</div>
                <div class="stat-label">Total Agentes</div>
            </div>
            <div class="stat-box" style="border-color: #28a745;">
                <div class="stat-number" style="color: #28a745;">{completados}</div>
                <div class="stat-label">Completados</div>
            </div>
            <div class="stat-box" style="border-color: #dc3545;">
                <div class="stat-number" style="color: #dc3545;">{errores}</div>
                <div class="stat-label">Errores</div>
            </div>
            <div class="stat-box" style="border-color: #6c757d;">
                <div class="stat-number" style="color: #6c757d;">{total - completados - errores}</div>
                <div class="stat-label">Pendientes/Cancelados</div>
            </div>
        </div>

        {grafico_html}

        <div class="table-container">
            <table>
                <thead>
                    <tr>
                        {"".join(f'<th>{h.replace("_", " ").title()}</th>' for h in headers)}
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>
        </div>

        <div class="footer">
            <p>Reporte generado por Agentes Visuales</p>
        </div>
    </div>
</body>
</html>
"""
    return html


def _obtener_estilos_html(estilo: str) -> str:
    """Obtiene los estilos CSS para HTML."""
    estilos_base = """
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; background-color: #f5f7fa; color: #333; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; background: white; border-radius: 12px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); padding: 30px; }
        .header { border-bottom: 2px solid #007bff; padding-bottom: 15px; margin-bottom: 25px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
        .header h1 { font-size: 28px; color: #2c3e50; }
        .timestamp { color: #6c757d; font-size: 14px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 15px; margin-bottom: 25px; }
        .stat-box { background: #f8f9fa; border-radius: 8px; padding: 15px; text-align: center; border-left: 4px solid #007bff; }
        .stat-number { font-size: 32px; font-weight: bold; color: #2c3e50; }
        .stat-label { font-size: 14px; color: #6c757d; margin-top: 4px; }
        .table-container { overflow-x: auto; margin-top: 20px; }
        table { width: 100%; border-collapse: collapse; font-size: 14px; }
        th { background: #f8f9fa; padding: 12px; text-align: left; font-weight: 600; color: #495057; border-bottom: 2px solid #dee2e6; position: sticky; top: 0; }
        td { padding: 10px 12px; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        tr:hover { background-color: #f8f9fa; }
        .footer { margin-top: 30px; padding-top: 15px; border-top: 1px solid #dee2e6; text-align: center; color: #6c757d; font-size: 12px; }
        .chart-container { background: #f8f9fa; border-radius: 8px; padding: 20px; margin-bottom: 20px; text-align: center; }
        .chart-bar { display: flex; justify-content: center; align-items: flex-end; gap: 30px; height: 100px; }
        .chart-item { display: flex; flex-direction: column; align-items: center; }
        .chart-bar-item { width: 60px; border-radius: 4px 4px 0 0; transition: height 0.5s; min-height: 10px; }
        .chart-label { margin-top: 8px; font-size: 12px; color: #6c757d; }
        .chart-value { font-weight: bold; font-size: 14px; }
        @media (max-width: 600px) { .container { padding: 15px; } .stats { grid-template-columns: 1fr 1fr; } }
    """

    estilos_clasico = """
        body { font-family: Arial, sans-serif; background: white; }
        .container { max-width: 100%; padding: 10px; box-shadow: none; }
        .stats { display: flex; gap: 10px; flex-wrap: wrap; }
        .stat-box { min-width: 100px; }
    """

    return estilos_base if estilo == "moderno" else estilos_clasico


def _generar_grafico_html(completados: int, errores: int, total: int) -> str:
    """Genera un gráfico de barras simple en HTML."""
    pendientes = total - completados - errores

    max_val = max(completados, errores, pendientes, 1)
    max_height = 80

    completados_height = (completados / max_val) * max_height if max_val > 0 else 10
    errores_height = (errores / max_val) * max_height if max_val > 0 else 10
    pendientes_height = (pendientes / max_val) * max_height if max_val > 0 else 10

    return f"""
    <div class="chart-container">
        <h3 style="margin-bottom: 15px; color: #2c3e50;">📊 Resumen de Estados</h3>
        <div class="chart-bar">
            <div class="chart-item">
                <div class="chart-bar-item" style="height: {completados_height}px; background: #28a745;"></div>
                <div class="chart-label">✅ Completados</div>
                <div class="chart-value" style="color: #28a745;">{completados}</div>
            </div>
            <div class="chart-item">
                <div class="chart-bar-item" style="height: {errores_height}px; background: #dc3545;"></div>
                <div class="chart-label">❌ Errores</div>
                <div class="chart-value" style="color: #dc3545;">{errores}</div>
            </div>
            <div class="chart-item">
                <div class="chart-bar-item" style="height: {pendientes_height}px; background: #6c757d;"></div>
                <div class="chart-label">⏳ Pendientes</div>
                <div class="chart-value" style="color: #6c757d;">{pendientes}</div>
            </div>
        </div>
    </div>
    """