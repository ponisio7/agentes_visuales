# tests/test_env_checker.py
"""Pruebas del diagnóstico de entorno (``core/env_checker.py``).

Se mockean las peticiones HTTP y el sistema de archivos para no depender de
la red ni de la máquina. Se cubren los códigos de salida y, sobre todo, que
un ``ValueError`` de ``requests`` (p. ej. ``timeout <= 0``) no escape como
excepción no controlada.
"""

import types

import pytest

import core.env_checker as ec


class TestEnmascararKey:
    def test_key_larga(self):
        assert ec._enmascarar_key("sk-1234567890abcdef") == "sk-12345…cdef"

    def test_key_corta(self):
        assert ec._enmascarar_key("abc") == "abc…"

    def test_longitud_limite(self):
        # 12 caracteres o menos: no se recorta por el final.
        assert ec._enmascarar_key("123456789012") == "1234…"


class TestDetectarColores:
    def test_no_color_desactiva(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        assert ec._detectar_colores().habilitado is False

    def test_no_tty_desactiva(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(ec.sys, "stdout", types.SimpleNamespace(isatty=lambda: False))
        assert ec._detectar_colores().habilitado is False

    def test_tty_activa(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setattr(ec.sys, "stdout", types.SimpleNamespace(isatty=lambda: True))
        monkeypatch.setattr(ec.platform, "system", lambda: "Linux")
        assert ec._detectar_colores().habilitado is True


class TestVerificarProxies:
    def test_sin_proxies(self, monkeypatch):
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                    "http_proxy", "https_proxy", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        assert ec._verificar_proxies(None) == []

    def test_con_proxies(self, monkeypatch):
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
                    "http_proxy", "https_proxy", "no_proxy"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HTTP_PROXY", "http://proxy:8080")
        monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8443")
        proxies = ec._verificar_proxies(None)
        assert "HTTP_PROXY=http://proxy:8080" in proxies
        assert "HTTPS_PROXY=http://proxy:8443" in proxies


class TestVerificarApiKey:
    def test_desde_variable_entorno(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")
        key, origen, mensajes = ec._verificar_api_key(None)
        assert key == "sk-env"
        assert origen == "env"
        assert mensajes == []

    def test_desde_archivo(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        archivo = tmp_path / "env"
        archivo.write_text('export DEEPSEEK_API_KEY="sk-file"\n', encoding="utf-8")
        archivo.chmod(0o600)
        monkeypatch.setattr("core.llm_client._FALLBACK_ENV_PATHS", [archivo])

        key, origen, mensajes = ec._verificar_api_key(None)
        assert key == "sk-file"
        assert origen == "archivo"
        assert mensajes == []

    def test_archivo_con_key_vacia(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        archivo = tmp_path / "env"
        archivo.write_text("DEEPSEEK_API_KEY=\n", encoding="utf-8")
        archivo.chmod(0o600)
        monkeypatch.setattr("core.llm_client._FALLBACK_ENV_PATHS", [archivo])

        key, origen, mensajes = ec._verificar_api_key(None)
        assert key is None
        assert origen is None
        assert any("vacía" in m for m in mensajes)

    def test_advertencia_por_permisos(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        archivo = tmp_path / "env"
        archivo.write_text('DEEPSEEK_API_KEY="sk-file"\n', encoding="utf-8")
        archivo.chmod(0o644)
        monkeypatch.setattr("core.llm_client._FALLBACK_ENV_PATHS", [archivo])

        _, origen, mensajes = ec._verificar_api_key(None)
        assert origen == "archivo"
        assert any("permisos" in m for m in mensajes)

    def test_sin_fuentes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setattr("core.llm_client._FALLBACK_ENV_PATHS", [tmp_path / "no-existe"])
        key, origen, _ = ec._verificar_api_key(None)
        assert key is None
        assert origen is None


class _RespuestaFalsa:
    def __init__(self, status_code):
        self.status_code = status_code


class TestPingHttpDeepseek:
    @pytest.fixture(autouse=True)
    def _sin_red(self, monkeypatch):
        import requests

        self.requests = requests
        monkeypatch.setattr(requests, "get", self._no_llamado)

    @staticmethod
    def _no_llamado(url, **kwargs):  # pragma: no cover - solo si falta el mock
        raise AssertionError("requests.get no fue mockeado en este test")

    def _mock(self, monkeypatch, respuestas):
        """Devuelve las respuestas en orden (una por endpoint)."""
        iterator = iter(respuestas)

        def fake_get(url, **kwargs):
            try:
                siguiente = next(iterator)
            except StopIteration:
                return _RespuestaFalsa(200)
            if isinstance(siguiente, Exception):
                raise siguiente
            return siguiente

        monkeypatch.setattr(self.requests, "get", fake_get)

    def test_ok(self, monkeypatch):
        self._mock(monkeypatch, [_RespuestaFalsa(200)])
        exito, _, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=1)
        assert exito is True
        assert detalles["status_code"] == 200

    def test_auth(self, monkeypatch):
        self._mock(monkeypatch, [_RespuestaFalsa(401)])
        exito, mensaje, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=1)
        assert exito is False
        assert detalles["error_tipo"] == "auth"
        assert "rechazada" in mensaje

    def test_rate_limit(self, monkeypatch):
        self._mock(monkeypatch, [_RespuestaFalsa(429)])
        exito, _, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=1)
        assert exito is False
        assert detalles["error_tipo"] == "rate_limit"

    def test_server_error(self, monkeypatch):
        self._mock(monkeypatch, [_RespuestaFalsa(503)])
        exito, _, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=1)
        assert exito is False
        assert detalles["error_tipo"] == "server"

    def test_404_prueba_el_siguiente_endpoint(self, monkeypatch):
        self._mock(monkeypatch, [_RespuestaFalsa(404), _RespuestaFalsa(200)])
        exito, _, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=1)
        assert exito is True
        assert detalles["endpoint"] == "https://api.deepseek.com/"

    def test_timeout(self, monkeypatch):
        self._mock(monkeypatch, [self.requests.exceptions.Timeout()])
        exito, _, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=1)
        assert exito is False
        assert detalles["error_tipo"] == "timeout"

    def test_error_de_conexion(self, monkeypatch):
        self._mock(monkeypatch, [self.requests.exceptions.ConnectionError()])
        exito, _, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=1)
        assert exito is False
        assert detalles["error_tipo"] == "connection"

    def test_valor_invalido(self, monkeypatch):
        # Regresión: ValueError (timeout <= 0) no hereda de RequestException.
        self._mock(monkeypatch, [ValueError("timeout must be positive")])
        exito, _, detalles = ec._ping_http_deepseek(None, "sk-x", timeout=0)
        assert exito is False
        assert detalles["error_tipo"] == "valor_invalido"


class TestEjecutarCheckEnv:
    def test_sin_key(self, monkeypatch, capsys):
        monkeypatch.setattr(ec, "_verificar_api_key", lambda c: (None, None, ["falta"]))
        assert ec.ejecutar_check_env() == ec.EXIT_CONFIG_ERROR

    def test_todo_ok(self, monkeypatch, capsys):
        monkeypatch.setattr(ec, "_verificar_api_key", lambda c: ("sk-x", "env", []))
        monkeypatch.setattr(
            ec, "_ping_http_deepseek",
            lambda c, key, timeout=5.0: (True, "ok", {"status_code": 200}),
        )
        assert ec.ejecutar_check_env() == ec.EXIT_OK

    def test_key_invalida(self, monkeypatch, capsys):
        monkeypatch.setattr(ec, "_verificar_api_key", lambda c: ("sk-x", "env", []))
        monkeypatch.setattr(
            ec, "_ping_http_deepseek",
            lambda c, key, timeout=5.0: (False, "rechazada", {"error_tipo": "auth"}),
        )
        assert ec.ejecutar_check_env() == ec.EXIT_CONFIG_ERROR

    def test_error_de_red(self, monkeypatch, capsys):
        monkeypatch.setattr(ec, "_verificar_api_key", lambda c: ("sk-x", "env", []))
        monkeypatch.setattr(
            ec, "_ping_http_deepseek",
            lambda c, key, timeout=5.0: (False, "timeout", {"error_tipo": "timeout"}),
        )
        assert ec.ejecutar_check_env() == ec.EXIT_NETWORK_ERROR

    def test_error_desconocido(self, monkeypatch, capsys):
        monkeypatch.setattr(ec, "_verificar_api_key", lambda c: ("sk-x", "env", []))
        monkeypatch.setattr(
            ec, "_ping_http_deepseek",
            lambda c, key, timeout=5.0: (False, "raro", {"error_tipo": "otro"}),
        )
        assert ec.ejecutar_check_env() == ec.EXIT_UNEXPECTED
