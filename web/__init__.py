"""Entorno web (Flask) de Agentes Visuales.

El paquete expone ``web.app.create_app`` (fábrica de la aplicación Flask con
el Blueprint ``api``) y ``web.app.ColaTrabajos`` (puente thread-safe entre el
hilo de Flask y el hilo principal de Qt).

Flask no se importa aquí: si no está instalado, ``import web`` debe seguir
funcionando y es ``main._ejecutar_web`` quien falla con un mensaje claro y
``EXIT_ENV_ERROR`` (3). Los símbolos se resuelven de forma perezosa.
"""

__all__ = ["create_app", "ColaTrabajos"]


def __getattr__(nombre: str):
    """Resuelve ``create_app``/``ColaTrabajos`` sin importar Flask al cargar."""
    if nombre in __all__:
        from web import app as _app

        return getattr(_app, nombre)
    raise AttributeError(f"module {__name__!r} has no attribute {nombre!r}")
