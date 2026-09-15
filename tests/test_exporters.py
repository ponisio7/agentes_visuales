# tests/test_exporters.py
"""
Pruebas unitarias y de integración para el módulo de exportación de resultados.

CARACTERÍSTICAS CUBIERTAS:
- ✅ Exportación a múltiples formatos (JSON, CSV, HTML, Markdown, TXT)
- ✅ Exportación a Excel y PDF (con skip automático si faltan dependencias)
- ✅ Aplanamiento de estructuras anidadas (flattening)
- ✅ Compresión de archivos (ZIP)
- ✅ Exportación por streaming (para grandes volúmenes)
- ✅ Manejo de casos edge (datos vacíos, formatos inválidos)
- ✅ Pruebas de utilidades y configuración
"""

import csv
import json
import os
import tempfile
import zipfile

import pytest

from export.utils import aplanar_diccionario, aplanar_lista
from export.exporters import (
    ResultExporter,
    ExportConfig,
    ExportResult,  # noqa: F401  (reexportado para uso externo)
    exportar_resultados,
)


# ============================================================
# FIXTURES
# ============================================================

@pytest.fixture
def temp_dir():
    """Fixture: directorio temporal para archivos de exportación."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture
def sample_data():
    """Fixture: datos de ejemplo con estructuras anidadas y variados estados."""
    return [
        {
            "id": 1,
            "nombre": "Agente 1",
            "tipo": "Python",
            "estado": "Completado",
            "duracion": 1.5,
            "resultado": {"status": "ok", "data": {"value": 42}},
            "dependencias": ["Dep1", "Dep2"],
        },
        {
            "id": 2,
            "nombre": "Agente 2",
            "tipo": "HTTP",
            "estado": "Error",
            "duracion": 0.5,
            "resultado": None,
            "error": "Timeout",
            "dependencias": [],
        },
    ]


@pytest.fixture
def exporter():
    """Fixture: instancia de ResultExporter."""
    return ResultExporter()


# ============================================================
# PRUEBAS DE CONFIGURACIÓN Y UTILIDADES
# ============================================================

class TestConfiguracionYUtilidades:
    """Pruebas de inicialización, configuración y métodos estáticos."""

    def test_configuracion_por_defecto(self):
        """Verifica los valores por defecto de ExportConfig."""
        config = ExportConfig()
        assert config.formato == "json"
        assert config.flatten is True
        assert config.comprimir is False
        assert config.separator == "."
        assert config.encoding == "utf-8-sig"

    def test_obtener_formatos_soportados(self):
        """Verifica que todos los formatos estén registrados."""
        formatos = ResultExporter.obtener_formatos_soportados()
        esperados = ["csv", "json", "html", "excel", "markdown", "pdf", "txt"]
        for fmt in esperados:
            assert fmt in formatos

    def test_obtener_extension(self):
        """Verifica el mapeo de formatos a extensiones."""
        assert ResultExporter.obtener_extension("csv") == "csv"
        assert ResultExporter.obtener_extension("JSON") == "json"
        assert ResultExporter.obtener_extension("excel") == "xlsx"
        assert ResultExporter.obtener_extension("markdown") == "md"
        assert ResultExporter.obtener_extension("desconocido") == "json"  # Fallback


# ============================================================
# PRUEBAS DE APLANAMIENTO (FLATTENING)
# ============================================================

class TestFlattening:
    """
    Pruebas de aplanamiento de diccionarios y listas.

    Nota: las funciones `aplanar_diccionario` y `aplanar_lista` viven ahora
    en `export.utils` como funciones libres (antes eran métodos privados
    de ResultExporter). Por eso se importan arriba y se llaman directamente.
    """

    def test_aplanar_diccionario_simple(self):
        """Aplanar diccionario sin anidamiento."""
        d = {"a": 1, "b": "texto"}
        result = aplanar_diccionario(d)
        assert result == {"a": 1, "b": "texto"}

    def test_aplanar_diccionario_anidado(self):
        """Aplanar diccionario con múltiples niveles."""
        d = {"a": {"b": {"c": 1}}, "x": "y"}
        result = aplanar_diccionario(d)
        assert result == {"a.b.c": 1, "x": "y"}

    def test_aplanar_diccionario_con_listas(self):
        """Las listas se convierten a string con resumen."""
        d = {"items": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]}
        result = aplanar_diccionario(d)
        assert "items" in result
        assert "11 items" in result["items"]

    def test_aplanar_diccionario_valores_none(self):
        """Los valores None se convierten a string vacío."""
        d = {"a": None, "b": 1}
        result = aplanar_diccionario(d)
        assert result["a"] == ""
        assert result["b"] == 1

    def test_aplanar_lista_con_flatten(self, sample_data):
        """Aplanar lista de diccionarios."""
        result = aplanar_lista(sample_data, flatten=True)
        assert len(result) == 2
        assert "resultado.status" in result[0]
        assert "resultado.data.value" in result[0]
        assert result[0]["resultado.data.value"] == 42

    def test_aplanar_lista_sin_flatten(self, sample_data):
        """No aplanar lista de diccionarios."""
        result = aplanar_lista(sample_data, flatten=False)
        assert len(result) == 2
        assert "resultado" in result[0]
        assert isinstance(result[0]["resultado"], dict)


# ============================================================
# PRUEBAS DE EXPORTACIÓN JSON
# ============================================================

class TestJSONExport:
    """Pruebas de exportación a formato JSON."""

    def test_exportar_json_basico(self, exporter, sample_data, temp_dir):
        """Exportación JSON estándar."""
        ruta = os.path.join(temp_dir, "test.json")
        result = exporter.exportar(sample_data, ruta=ruta, formato="json")

        assert result.exito is True
        assert os.path.exists(ruta)
        assert result.filas_exportadas == 2

        with open(ruta, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "metadata" in data
        assert "agentes" in data
        assert len(data["agentes"]) == 2
        assert data["metadata"]["total_agentes"] == 2

    def test_exportar_json_con_limite(self, exporter, sample_data, temp_dir):
        """Exportación JSON con límite de filas."""
        ruta = os.path.join(temp_dir, "test_limit.json")
        result = exporter.exportar(
            sample_data, ruta=ruta, formato="json", limit_rows=1
        )

        assert result.exito is True
        assert result.filas_exportadas == 1
        assert len(result.advertencias) > 0

        with open(ruta, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["agentes"]) == 1


# ============================================================
# PRUEBAS DE EXPORTACIÓN CSV
# ============================================================

class TestCSVExport:
    """Pruebas de exportación a formato CSV."""

    def test_exportar_csv_basico(self, exporter, sample_data, temp_dir):
        """Exportación CSV estándar con aplanamiento."""
        ruta = os.path.join(temp_dir, "test.csv")
        result = exporter.exportar(sample_data, ruta=ruta, formato="csv")

        assert result.exito is True
        assert os.path.exists(ruta)
        assert result.filas_exportadas == 2

        with open(ruta, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        assert len(rows) == 2
        assert "resultado.status" in rows[0]
        assert rows[0]["resultado.status"] == "ok"


# ============================================================
# PRUEBAS DE EXPORTACIÓN HTML, MARKDOWN, TXT
# ============================================================

class TestTextBasedExports:
    """Pruebas de exportación a formatos basados en texto (HTML, MD, TXT)."""

    def test_exportar_html_basico(self, exporter, sample_data, temp_dir):
        """Exportación HTML con estilos y tabla."""
        ruta = os.path.join(temp_dir, "test.html")
        result = exporter.exportar(sample_data, ruta=ruta, formato="html")

        assert result.exito is True
        assert os.path.exists(ruta)

        with open(ruta, "r", encoding="utf-8") as f:
            content = f.read()
        assert "<!DOCTYPE html>" in content
        assert "Reporte de Ejecución de Agentes" in content
        assert "Agente 1" in content
        assert "#28a745" in content  # Color de completado

    def test_exportar_markdown_basico(self, exporter, sample_data, temp_dir):
        """Exportación Markdown con tabla."""
        ruta = os.path.join(temp_dir, "test.md")
        result = exporter.exportar(sample_data, ruta=ruta, formato="markdown")

        assert result.exito is True
        assert os.path.exists(ruta)

        with open(ruta, "r", encoding="utf-8") as f:
            content = f.read()
        assert "# 📊 Reporte de Agentes" in content
        assert "|" in content  # Tabla markdown
        assert "---" in content

    def test_exportar_txt_basico(self, exporter, sample_data, temp_dir):
        """Exportación TXT legible."""
        ruta = os.path.join(temp_dir, "test.txt")
        result = exporter.exportar(sample_data, ruta=ruta, formato="txt")

        assert result.exito is True
        assert os.path.exists(ruta)

        with open(ruta, "r", encoding="utf-8") as f:
            content = f.read()
        assert "📊 REPORTE DE AGENTES" in content
        assert "Agente 1" in content


# ============================================================
# PRUEBAS DE EXPORTACIÓN EXCEL Y PDF (DEPENDENCIAS OPCIONALES)
# ============================================================

class TestOptionalExports:
    """Pruebas de formatos que requieren librerías externas."""

    def test_exportar_excel(self, exporter, sample_data, temp_dir):
        """Exportación Excel (requiere pandas y openpyxl)."""
        pd = pytest.importorskip("pandas", reason="pandas no instalado")

        ruta = os.path.join(temp_dir, "test.xlsx")
        result = exporter.exportar(sample_data, ruta=ruta, formato="excel")

        assert result.exito is True
        assert os.path.exists(ruta)
        assert result.filas_exportadas == 2

        # Verificar hojas
        xl = pd.ExcelFile(ruta)
        assert "Agentes" in xl.sheet_names
        assert "Resumen" in xl.sheet_names

    def test_exportar_pdf(self, exporter, sample_data, temp_dir):
        """Exportación PDF (requiere reportlab)."""
        pytest.importorskip("reportlab", reason="reportlab no instalado")

        ruta = os.path.join(temp_dir, "test.pdf")
        result = exporter.exportar(sample_data, ruta=ruta, formato="pdf")

        assert result.exito is True
        assert os.path.exists(ruta)
        assert result.filas_exportadas == 2

        # Verificar que es un PDF válido
        with open(ruta, "rb") as f:
            header = f.read(5)
        assert header == b"%PDF-"


# ============================================================
# PRUEBAS DE COMPRESIÓN
# ============================================================

class TestCompression:
    """Pruebas de compresión de archivos exportados."""

    def test_exportar_con_compresion_zip(self, exporter, sample_data, temp_dir):
        """Exportar y comprimir en ZIP."""
        ruta_base = os.path.join(temp_dir, "test.json")
        result = exporter.exportar(
            sample_data, ruta=ruta_base, formato="json", comprimir=True
        )

        assert result.exito is True
        assert result.ruta.endswith(".zip")
        assert os.path.exists(result.ruta)

        with zipfile.ZipFile(result.ruta, "r") as zf:
            assert len(zf.namelist()) == 1
            assert zf.namelist()[0].endswith(".json")

        # El archivo original .json debe haber sido eliminado tras comprimir
        assert not os.path.exists(ruta_base)


# ============================================================
# PRUEBAS DE STREAMING
# ============================================================

class TestStreaming:
    """Pruebas de exportación por streaming para grandes volúmenes."""

    def test_exportar_csv_streaming(self, exporter, sample_data, temp_dir):
        """Exportar CSV desde un generador."""
        ruta = os.path.join(temp_dir, "stream.csv")

        def generador():
            for item in sample_data:
                yield item

        result = exporter.exportar_streaming(generador(), ruta=ruta, formato="csv")

        assert result.exito is True
        assert os.path.exists(ruta)
        assert result.filas_exportadas == 2

    def test_exportar_streaming_vacio(self, exporter, temp_dir):
        """Streaming con generador vacío."""
        ruta = os.path.join(temp_dir, "empty_stream.csv")

        def generador_vacio():
            return
            yield  # Nunca se ejecuta

        result = exporter.exportar_streaming(generador_vacio(), ruta=ruta, formato="csv")

        assert result.exito is False
        assert "No hay datos" in result.errores[0]


# ============================================================
# PRUEBAS DE CASOS EDGE Y ERRORES
# ============================================================

class TestEdgeCases:
    """Pruebas de casos límite y manejo de errores."""

    def test_exportar_datos_vacios(self, exporter, temp_dir):
        """Intentar exportar lista vacía."""
        ruta = os.path.join(temp_dir, "empty.json")
        result = exporter.exportar([], ruta=ruta, formato="json")

        assert result.exito is False
        assert "No hay datos" in result.errores[0]

    def test_formato_no_soportado(self, exporter, sample_data, temp_dir):
        """Intentar exportar a formato inválido."""
        ruta = os.path.join(temp_dir, "test.xyz")
        result = exporter.exportar(sample_data, ruta=ruta, formato="xyz")

        assert result.exito is False
        assert "Formato no soportado" in result.errores[0]

    def test_funcion_rapida_exportar_resultados(self, sample_data, temp_dir):
        """Prueba la función de acceso rápido."""
        ruta = os.path.join(temp_dir, "rapido.json")
        result = exportar_resultados(sample_data, ruta=ruta, formato="json")

        assert result.exito is True
        assert os.path.exists(ruta)