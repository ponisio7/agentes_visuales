"""
core/dependency_manager.py
Grafo de dependencias entre agentes (V3.8-6).

Funciones puras sobre el diccionario de agentes del ``Scheduler``:
resolución de nombres a IDs, detección de ciclos, validación de fuentes de
los LOOP y consultas de estado de las dependencias.

Vivían dentro de ``Scheduler``; se extraen para que el orquestador delegue y
para poder probarlas sin Qt ni hilos. El contrato es el mismo.
"""
from __future__ import annotations

from typing import Any

from .agent import EstadoAgente, TipoAgente


def resolver_dependencias(agentes: dict[str, Any]) -> list[tuple[str, str]]:
    """Resuelve ``dependencias_nombres`` a IDs.

    Muta cada agente (rellena ``dependencias_ids`` y limpia los nombres) y
    devuelve las dependencias NO encontradas como ``(agente, nombre)`` para
    que el llamante las registre.
    """
    nombre_a_id = {a.nombre: a.id for a in agentes.values()}
    no_encontradas: list[tuple[str, str]] = []

    for agente in agentes.values():
        if not agente.dependencias_nombres:
            continue
        ids_resueltos = []
        for nombre in agente.dependencias_nombres:
            if nombre in nombre_a_id:
                ids_resueltos.append(nombre_a_id[nombre])
            else:
                no_encontradas.append((agente.nombre, nombre))
        agente.dependencias_ids = ids_resueltos
        agente.dependencias_nombres = []

    return no_encontradas


def detectar_ciclos(agentes: dict[str, Any]) -> tuple[bool, list[list[str]]]:
    """Detecta ciclos en las dependencias usando DFS."""
    visitados: set[str] = set()
    pila: set[str] = set()
    ciclos: list[list[str]] = []
    id_a_nombre = {aid: a.nombre for aid, a in agentes.items()}

    def dfs(agente_id: str, path: list[str]):
        if agente_id in pila:
            try:
                idx = path.index(agente_id)
            except ValueError:
                return
            ciclo_ids = path[idx:] + [agente_id]
            ciclo_nombres = [id_a_nombre.get(aid, aid) for aid in ciclo_ids]
            if ciclo_nombres not in ciclos:
                ciclos.append(ciclo_nombres)
            return

        if agente_id in visitados:
            return

        visitados.add(agente_id)
        pila.add(agente_id)
        path.append(agente_id)

        agente = agentes.get(agente_id)
        if agente:
            for dep_id in list(agente.dependencias_ids):
                if dep_id in agentes:
                    dfs(dep_id, path)

        path.pop()
        pila.remove(agente_id)

    for agente_id in list(agentes.keys()):
        if agente_id not in visitados:
            dfs(agente_id, [])

    return bool(ciclos), ciclos


def validar_fuentes_loop(agentes: dict[str, Any]) -> tuple[bool, list[str]]:
    """Valida que las fuentes de items de los loops sean válidas."""
    errores: list[str] = []
    for agente in agentes.values():
        if agente.tipo != TipoAgente.LOOP:
            continue

        nombre_fuente = agente.obtener_nombre_dependencia()
        if not nombre_fuente:
            errores.append(
                f"{agente.nombre}: 'fuente_items' debe tener formato 'Dependencia.clave'"
            )
            continue

        nombres_deps = {
            agentes[dep_id].nombre
            for dep_id in agente.dependencias_ids
            if dep_id in agentes
        }

        if nombre_fuente not in nombres_deps:
            errores.append(
                f"{agente.nombre}: la fuente '{nombre_fuente}' no es una "
                f"dependencia declarada (dependencias: {sorted(nombres_deps) or 'ninguna'})"
            )

    return not errores, errores


def obtener_dependencias_pendientes(agente: Any, agentes: dict[str, Any]) -> list[str]:
    """Dependencias que aún NO están COMPLETADAS.

    Solo COMPLETADO satisface una dependencia. ERROR, TIMEOUT, CANCELADO,
    SALTADO y BLOQUEADO NO la satisfacen.
    """
    pendientes = []
    for dep_id in agente.dependencias_ids:
        dep = agentes.get(dep_id)
        if dep and dep.estado != EstadoAgente.COMPLETADO:
            pendientes.append(dep_id)
    return pendientes


def tiene_dependencias_fallidas(agente: Any, agentes: dict[str, Any]) -> bool:
    """True si alguna dependencia terminó en estado terminal no exitoso."""
    for dep_id in agente.dependencias_ids:
        dep = agentes.get(dep_id)
        if dep and dep.estado in (
            EstadoAgente.ERROR,
            EstadoAgente.TIMEOUT,
            EstadoAgente.CANCELADO,
            EstadoAgente.SALTADO,
            EstadoAgente.BLOQUEADO,
        ):
            return True
    return False


__all__ = [
    "detectar_ciclos",
    "obtener_dependencias_pendientes",
    "resolver_dependencias",
    "tiene_dependencias_fallidas",
    "validar_fuentes_loop",
]
