# core/problem_solver/file_normalizer.py
"""
FileNameNormalizer: normalización de nombres de archivos en agentes File.
Extraído literalmente de core/problem_solver.py (monolito) — Paso 4.
"""
import logging

logger = logging.getLogger(__name__)


class FileNameNormalizer:
    """Normaliza nombres de archivos en agentes File."""

    GENERIC_NAMES = frozenset([
        "salida.txt", "output.txt", "resultado.txt", "out.txt",
        "file.txt", "archivo.txt", "data.txt", "datos.txt"
    ])

    CONTEXT_KEYWORDS = {
        "tiempo": "tiempo.txt",
        "clima": "clima.txt",
        "weather": "weather.txt",
        "reporte": "reporte.txt",
        "report": "report.txt",
        "datos": "datos.txt",
        "data": "data.txt",
        "resultado": "resultado.txt",
        "result": "result.txt",
        "log": "log.txt",
        "github": "github.txt",
        "api": "api_response.txt",
        "json": "data.json",
        "config": "config.txt",
        "resumen": "resumen.txt",
        "summary": "summary.txt",
    }

    @classmethod
    def normalizar(cls, plan_dict: dict) -> dict:
        """Normaliza nombres de archivos en el plan."""
        pasos = plan_dict.get('pasos', [])
        titulo = plan_dict.get('titulo', '')

        for paso in pasos:
            if paso.get('tipo') != 'File':
                continue

            config = paso.get('configuracion', {})
            if config.get('operacion') != 'escribir':
                continue

            destino = config.get('archivo_destino', '')
            if destino in cls.GENERIC_NAMES:
                nuevo_nombre = cls._inferir_nombre(titulo, paso)
                if nuevo_nombre:
                    config['archivo_destino'] = nuevo_nombre
                    logger.info(f"📁 Nombre de archivo normalizado: {destino} → {nuevo_nombre}")

        return plan_dict

    @classmethod
    def _inferir_nombre(cls, titulo: str, paso: dict) -> str | None:
        """Infiere un nombre de archivo a partir del título y el paso."""
        titulo_lower = titulo.lower()

        for keyword, filename in cls.CONTEXT_KEYWORDS.items():
            if keyword in titulo_lower:
                return filename

        descripcion = paso.get('descripcion', '').lower()
        for keyword, filename in cls.CONTEXT_KEYWORDS.items():
            if keyword in descripcion:
                return filename

        nombre_paso = paso.get('nombre', 'archivo')
        return f"{nombre_paso.lower()}.txt"
