# core/executors/loop_executor.py
"""Ejecutor de agentes Loop."""

import logging
import time
from typing import Any

from core.agent import Agente
from core.cancellation import CancellationToken
from core.sandbox import PythonSandbox

from .content_extractor import resolver_ruta_en_contexto
from .security import es_lista_valida

logger = logging.getLogger(__name__)


class LoopExecutor:
    """Itera sobre una lista de items ejecutando código por cada uno."""

    @staticmethod
    def actualizar_progreso(agente, progreso, mensaje=""):
        agente.progreso = min(100, max(0, progreso))
        if mensaje:
            agente.mensaje = mensaje
        if hasattr(agente, '_bridge') and agente._bridge is not None:
            try:
                agente._bridge.agente_actualizado.emit(agente.id)
            except Exception:
                pass

    @classmethod
    def ejecutar(
        cls,
        agente: Agente,
        contexto: dict,
        cancellation_token: CancellationToken | None = None
    ) -> tuple[bool, str, dict]:
        if cancellation_token and cancellation_token.esta_cancelado():
            return False, "Cancelado antes de ejecutar", {'error': 'cancelled'}

        cls.actualizar_progreso(agente, 10, "Preparando loop...")

        if not agente.fuente_items:
            cls.actualizar_progreso(agente, 100, "Fuente de items vacía")
            return False, "Loop: 'fuente_items' no está configurado", {}

        if not agente.codigo_por_item or not agente.codigo_por_item.strip():
            cls.actualizar_progreso(agente, 100, "Código por item vacío")
            return False, "Loop: 'codigo_por_item' está vacío", {}

        items = resolver_ruta_en_contexto(contexto, agente.fuente_items)
        if not es_lista_valida(items):
            # Enriquecer el mensaje con las claves disponibles en la dependencia raíz
            nombre_dep = agente.obtener_nombre_dependencia() or "(desconocida)"
            claves_disponibles = []
            if nombre_dep in contexto:
                dep_resultado = contexto[nombre_dep]
                if isinstance(dep_resultado, dict):
                    claves_disponibles = list(dep_resultado.keys())[:10]

            msg = (
                f"Loop: '{agente.fuente_items}' no resolvió a una lista válida "
                f"(obtuve: {type(items).__name__})."
            )
            if claves_disponibles:
                msg += (
                    f" El agente '{nombre_dep}' produjo las claves: "
                    f"{claves_disponibles}."
                )
            return False, msg, {
                "fuente": agente.fuente_items,
                "valor": items,
                "claves_disponibles": claves_disponibles,
            }

        total_items = len(items)
        if total_items == 0:
            cls.actualizar_progreso(agente, 100, "Lista vacía")
            return True, (
                "Loop: La lista está vacía, no hay items que procesar"
            ), {
                'total_items': 0, 'items_procesados': 0,
                'exitos': 0, 'errores': 0, 'no_ejecutados': 0
            }

        if total_items > agente.max_iteraciones:
            cls.actualizar_progreso(agente, 100, "Excede máx. iteraciones")
            return False, (
                f"Loop: La lista tiene {total_items} items, "
                f"excede el máximo de {agente.max_iteraciones}"
            ), {"total_items": total_items, "max_iteraciones": agente.max_iteraciones}

        resultados = []
        errores = 0
        exitos = 0
        tiempo_inicio = time.time()
        timeout_total = agente.timeout_loop or 300
        timeout_item = agente.timeout_python or 30

        cls.actualizar_progreso(agente, 15, f"Procesando {total_items} items...")

        for idx, item in enumerate(items):
            if cancellation_token and cancellation_token.esta_cancelado():
                cls.actualizar_progreso(agente, 100, "⛔ Cancelado por usuario")
                return False, f"Loop cancelado en item {idx+1}/{total_items}", {
                    'error': 'cancelled',
                    'items_procesados': idx,
                    'total_items': total_items,
                    'errores': errores, 'exitos': exitos,
                    'no_ejecutados': total_items - idx,
                    'resultados_parciales': resultados
                }

            elapsed = time.time() - tiempo_inicio
            if elapsed > timeout_total:
                cls.actualizar_progreso(agente, 100, "Timeout global")
                return False, (
                    f"Loop: Timeout global excedido ({timeout_total}s) "
                    f"después de {idx} items"
                ), {
                    "items_procesados": idx,
                    "total_items": total_items,
                    "errores": errores, "exitos": exitos,
                    "no_ejecutados": total_items - idx,
                    "resultados_parciales": resultados
                }

            progreso_actual = 15 + int((idx / total_items) * 75)
            cls.actualizar_progreso(
                agente, progreso_actual,
                f"Item {idx+1}/{total_items}: {str(item)[:30]}..."
            )

            ctx_item = dict(contexto)
            ctx_item['item'] = item
            ctx_item['indice'] = idx
            ctx_item['total'] = total_items

            try:
                exito, msg, res = PythonSandbox.ejecutar(
                    agente.codigo_por_item,
                    ctx_item,
                    timeout=timeout_item,
                    cancellation_token=cancellation_token
                )
                resultados.append({
                    'indice': idx, 'item': item,
                    'exito': exito, 'mensaje': msg, 'resultado': res
                })
                if exito:
                    exitos += 1
                else:
                    errores += 1
                    if not agente.continuar_en_error:
                        duracion_total = time.time() - tiempo_inicio
                        no_ejecutados = total_items - len(resultados)
                        cls.actualizar_progreso(
                            agente, 100, f"⛔ Detenido en item {idx+1} por error"
                        )
                        resultado_final = {
                            'total_items': total_items,
                            'items_procesados': len(resultados),
                            'exitos': exitos, 'errores': errores,
                            'no_ejecutados': no_ejecutados,
                            'duracion_total': duracion_total,
                            'detenido_en_indice': idx,
                            'detenido_por_error': True,
                            'continuar_en_error': False,
                            'mensaje_error': msg[:500],
                            'items': resultados
                        }
                        return False, (
                            f"Loop detenido en item {idx+1} por error: {msg[:200]}"
                        ), resultado_final
            except Exception as e:
                errores += 1
                resultados.append({
                    'indice': idx, 'item': item, 'exito': False,
                    'mensaje': f"Error crítico: {str(e)}",
                    'resultado': {'error': str(e)}
                })
                if not agente.continuar_en_error:
                    duracion_total = time.time() - tiempo_inicio
                    no_ejecutados = total_items - len(resultados)
                    cls.actualizar_progreso(
                        agente, 100, f"⛔ Detenido en item {idx+1} por error crítico"
                    )
                    resultado_final = {
                        'total_items': total_items,
                        'items_procesados': len(resultados),
                        'exitos': exitos, 'errores': errores,
                        'no_ejecutados': no_ejecutados,
                        'duracion_total': duracion_total,
                        'detenido_en_indice': idx,
                        'detenido_por_error': True,
                        'continuar_en_error': False,
                        'mensaje_error': str(e)[:500],
                        'items': resultados
                    }
                    return False, (
                        f"Loop detenido en item {idx+1} por error crítico: {e}"
                    ), resultado_final

        duracion_total = time.time() - tiempo_inicio
        resultado_final = {
            'total_items': total_items,
            'items_procesados': len(resultados),
            'exitos': exitos, 'errores': errores,
            'no_ejecutados': 0,
            'duracion_total': duracion_total,
            'detenido_por_error': False,
            'continuar_en_error': agente.continuar_en_error,
            'items': resultados
        }

        mensaje = (
            f"Loop completado: {total_items} items, "
            f"{exitos} éxitos, {errores} errores, "
            f"duración: {duracion_total:.2f}s"
        )
        if errores > 0 and agente.continuar_en_error:
            mensaje += " (continuar_en_error activo: se ignoraron los fallos individuales)"

        cls.actualizar_progreso(agente, 100, "Loop completado")
        exito_general = errores == 0 or agente.continuar_en_error
        return exito_general, mensaje, resultado_final

    @classmethod
    def probar_loop(cls, agente: Agente, items: list[Any]) -> tuple[bool, str, dict]:
        """Método de prueba para verificar la configuración de un agente LOOP."""
        from core.agent import TipoAgente
        if agente.tipo != TipoAgente.LOOP:
            return False, "El agente no es de tipo LOOP", {}

        es_valido, mensaje = agente.validar_configuracion()
        if not es_valido:
            return False, f"Configuración inválida: {mensaje}", {}

        partes = agente.fuente_items.split('.')
        nombre_dep = partes[0]
        clave_items = '.'.join(partes[1:]) if len(partes) > 1 else 'items'

        contexto = {}
        dep_data = {clave_items: items}
        contexto[nombre_dep] = dep_data
        contexto['items_prueba'] = items

        return cls.ejecutar(agente, contexto)
