# tests/test_security.py
"""Pruebas de seguridad de los ejecutores.

Verifica los validadores estáticos (rutas, URLs, listas) y la sustitución
de variables con citado ``shlex.quote`` que cierra la inyección de comandos
en el ``ShellExecutor``. Son guardas de seguridad: un fallo aquí permite
borrar el proyecto o ejecutar comandos arbitrarios.
"""

import shlex

import pytest

from core.executors.content_extractor import (
    comando_requiere_root,
    extraer_contenido_relevante,
    extraer_primer_comando,
    sustituir_variables,
    sustituir_variables_shell,
)
from core.executors.security import (
    MAX_FILE_PATH_LENGTH,
    es_lista_valida,
    validar_ruta_archivo,
    validar_url,
)


class TestValidarRutaArchivo:
    """Rechaza rutas que escapan del directorio de trabajo o borran todo."""

    @pytest.mark.parametrize("ruta", [
        "",
        "   ",
        ".",
        "..",
        "~",
        "/",
        "\\",
        "~/archivo",
        "/etc/passwd",
        "/tmp/x",
        "../archivo.txt",
        "dir/../../archivo.txt",
        ".oculto",
        "dir/.oculto",
        "x" * (MAX_FILE_PATH_LENGTH + 1),
    ])
    def test_rechaza_rutas_peligrosas(self, ruta):
        assert validar_ruta_archivo(ruta) is False

    @pytest.mark.parametrize("ruta", [
        "archivo.txt",
        "dir/archivo.txt",
        "dir/sub/archivo.txt",
        "dir/../archivo.txt",   # resuelto dentro del cwd
        "con espacios.txt",
        "acentuación.txt",
    ])
    def test_acepta_rutas_seguras(self, ruta):
        assert validar_ruta_archivo(ruta) is True


class TestValidarUrl:
    def test_urls_validas(self):
        assert validar_url("https://api.example.com") is True
        assert validar_url("http://localhost:8080/path?q=1") is True

    @pytest.mark.parametrize("url", [
        "",
        "no-es-una-url",
        "ftp://example.com",
        "https://",
        "http://",
        "file:///etc/passwd",
        "https://example.com/path\ninyectado",
        "https://example.com/path\tinyectado",
        "https://" + "a" * 2000,
    ])
    def test_urls_invalidas(self, url):
        assert validar_url(url) is False


class TestEsListaValida:
    def test_lista(self):
        assert es_lista_valida([]) is True
        assert es_lista_valida([1, 2]) is True

    def test_no_lista(self):
        assert es_lista_valida(()) is False
        assert es_lista_valida("x") is False
        assert es_lista_valida({}) is False
        assert es_lista_valida(None) is False


class TestSustitucionVariables:
    def test_sustitucion_basica(self):
        assert sustituir_variables("hola {nombre}", {"nombre": "mundo"}) == "hola mundo"

    def test_variable_desconocida_se_conserva(self):
        assert sustituir_variables("hola {otra}", {"nombre": "x"}) == "hola {otra}"

    def test_texto_vacio(self):
        assert sustituir_variables("", {"a": "b"}) == ""

    def test_sin_variables(self):
        assert sustituir_variables("hola {a}", {}) == "hola {a}"

    def test_espacios_dentro_de_llaves(self):
        assert sustituir_variables("hola { nombre }", {"nombre": "x"}) == "hola x"


class TestSustitucionVariablesShell:
    def test_cita_valores(self):
        resultado = sustituir_variables_shell("echo {nombre}", {"nombre": "a b"})
        assert resultado == "echo " + shlex.quote("a b")

    def test_previene_inyeccion(self):
        payload = "a; rm -rf / && echo hackeado"
        resultado = sustituir_variables_shell("echo {valor}", {"valor": payload})
        assert resultado == "echo " + shlex.quote(payload)
        # El comando malicioso queda dentro de una única cadena citada:
        # no se separa en un comando adicional.
        assert shlex.split(resultado) == ["echo", payload]

    def test_variable_desconocida(self):
        assert sustituir_variables_shell("echo {otra}", {"x": "y"}) == "echo {otra}"

    def test_vacio_o_sin_variables(self):
        assert sustituir_variables_shell("", {"a": "b"}) == ""
        assert sustituir_variables_shell("echo x", {}) == "echo x"


class TestExtraerPrimerComando:
    def test_comando_simple(self):
        assert extraer_primer_comando("ls -la") == "ls"

    def test_salta_asignaciones_y_flags(self):
        assert extraer_primer_comando("VAR=1 echo -n hola") == "echo"

    def test_ruta_absoluta_se_queda_con_basename(self):
        assert extraer_primer_comando("/usr/bin/python3 script.py") == "python3"

    def test_estructura_de_control(self):
        assert extraer_primer_comando("if true then echo hola fi") == "true"

    def test_vacio(self):
        assert extraer_primer_comando("") is None
        assert extraer_primer_comando("   ") is None


class TestComandoRequiereRoot:
    @pytest.mark.parametrize("comando", [
        "apt install algo",
        "apt-get update",
        "systemctl restart servicio",
        "mount /dev/sda1 /mnt",
        "useradd nuevo",
        "if true; then apt update; fi",
    ])
    def test_detecta_privilegiados(self, comando):
        assert comando_requiere_root(comando) is True

    @pytest.mark.parametrize("comando", [
        "ls -la",
        "python3 script.py",
        "echo hola",
        "",
    ])
    def test_no_privilegiados(self, comando):
        assert comando_requiere_root(comando) is False

    def test_ya_elevado_no_requiere(self):
        assert comando_requiere_root("sudo apt install algo") is False
        assert comando_requiere_root("pkexec apt install algo") is False


class TestExtraerContenidoRelevante:
    def test_extrae_clave_prioritaria(self):
        assert extraer_contenido_relevante({"respuesta": "hola"}) == "hola"

    def test_estructura_documento_se_devuelve_entera(self):
        data = {"titulo": "T", "cuento": "había una vez"}
        assert extraer_contenido_relevante(data) == data

    def test_multiples_claves_utiles_devuelve_el_dict(self):
        data = {"html": "<p>x</p>", "imagenes": ["a.png"]}
        assert extraer_contenido_relevante(data) == data

    def test_escalar(self):
        assert extraer_contenido_relevante("texto") == "texto"
        assert extraer_contenido_relevante(None) is None

    def test_dict_sin_claves_conocidas_se_devuelve(self):
        data = {"foo": "bar"}
        assert extraer_contenido_relevante(data) == data
