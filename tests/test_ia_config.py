# tests/test_ia_config.py
"""Configuración de IA (H1): archivo de secretos, modelo y reset del cliente.

Ninguna prueba escribe en la configuración real del usuario: se redirige
``RUTA_ENV_PRINCIPAL`` a ``tmp_path``. No se llama a la red.
"""
import logging
import stat

import pytest

from core import ia_config
from core.ia_config import (
    MODELO_FLASH,
    MODELO_PRO,
    borrar_api_key,
    enmascarar_key,
    guardar_configuracion,
    leer_configuracion,
    modelo_por_defecto,
    normalizar_modelo,
)


@pytest.fixture
def config_temporal(tmp_path, monkeypatch):
    """Redirige el archivo de configuración a un tmp_path aislado."""
    destino = tmp_path / "config" / "agentes_visuales" / "env"
    monkeypatch.setattr(ia_config, "RUTA_ENV_PRINCIPAL", destino)
    monkeypatch.setattr(ia_config, "_FALLBACK_ENV_PATHS", [destino])
    for var in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "DEEPSEEK_BASE_URL",
                "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
    return destino


# ============================================================
# MODELO
# ============================================================

@pytest.mark.parametrize(
    "entrada, esperado",
    [
        ("pro", MODELO_PRO),
        ("Pro", MODELO_PRO),
        ("flash", MODELO_FLASH),
        ("FLASH", MODELO_FLASH),
        ("deepseek-v4-pro", MODELO_PRO),
        ("deepseek-v4-flash", MODELO_FLASH),
        (None, MODELO_PRO),
        ("", MODELO_PRO),
    ],
)
def test_normalizar_modelo(entrada, esperado):
    assert normalizar_modelo(entrada) == esperado


def test_modelo_por_defecto_lee_entorno(config_temporal, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "flash")
    assert modelo_por_defecto() == MODELO_FLASH


def test_modelo_por_defecto_lee_archivo(config_temporal):
    guardar_configuracion(api_key="sk-x", modelo="flash")
    import os
    os.environ.pop("DEEPSEEK_MODEL", None)
    assert modelo_por_defecto() == MODELO_FLASH


# ============================================================
# GUARDADO SEGURO
# ============================================================

def test_guardar_crea_archivo_con_permisos_600_y_dir_700(config_temporal):
    guardar_configuracion(api_key="sk-secreta-1234567890", modelo="pro")

    assert config_temporal.exists()
    modo = stat.S_IMODE(config_temporal.stat().st_mode)
    assert modo == 0o600, oct(modo)
    modo_dir = stat.S_IMODE(config_temporal.parent.stat().st_mode)
    assert modo_dir == 0o700, oct(modo_dir)


def test_leer_configuracion_enmascara_la_key(config_temporal):
    guardar_configuracion(api_key="sk-secreta-1234567890", modelo="flash")

    config = leer_configuracion()
    assert config["api_key_configurada"] is True
    assert config["api_key_enmascarada"] != "sk-secreta-1234567890"
    assert "1234567890" not in config["api_key_enmascarada"]
    assert config["modelo"] == MODELO_FLASH


def test_enmascarar_key():
    assert enmascarar_key(None) == ""
    assert enmascarar_key("sk-corta") == "sk-c…"
    assert enmascarar_key("sk-1234567890abcdef") == "sk-12345…cdef"


def test_borrar_api_key_conserva_el_resto(config_temporal):
    guardar_configuracion(
        api_key="sk-secreta-1234567890",
        modelo="flash",
        base_url="https://otro.example",
        http_proxy="http://proxy:8080",
    )
    config = borrar_api_key()

    assert config["api_key_configurada"] is False
    assert config["modelo"] == MODELO_FLASH
    assert config["base_url"] == "https://otro.example"
    assert config["http_proxy"] == "http://proxy:8080"

    texto = config_temporal.read_text(encoding="utf-8")
    assert "sk-secreta-1234567890" not in texto


def test_valor_none_conserva_y_vacio_borra(config_temporal, monkeypatch):
    guardar_configuracion(api_key="sk-secreta-1234567890", base_url="https://a.example")

    # None no toca la base_url; "" la borra.
    guardar_configuracion(modelo="pro")
    assert leer_configuracion()["base_url"] == "https://a.example"
    guardar_configuracion(base_url="")
    assert leer_configuracion()["base_url"] == "https://api.deepseek.com"


def test_la_key_nunca_se_registra_en_logs(config_temporal, caplog):
    with caplog.at_level(logging.DEBUG, logger="core.ia_config"):
        guardar_configuracion(api_key="sk-secreta-1234567890")
        leer_configuracion()

    assert "sk-secreta-1234567890" not in caplog.text


# ============================================================
# RESET DEL CLIENTE COMPARTIDO
# ============================================================

def test_reset_llm_client_compartido(monkeypatch):
    from core import llm_client

    cerrado = {"ok": False}

    class _ClienteFalso:
        def close(self):
            cerrado["ok"] = True

    monkeypatch.setattr(llm_client, "_shared_client", _ClienteFalso())
    llm_client.reset_llm_client_compartido()

    assert llm_client._shared_client is None
    assert cerrado["ok"] is True


# ============================================================
# BUILDER RESPETA EL MODELO CONFIGURADO
# ============================================================

def test_builder_usa_el_modelo_configurado(config_temporal, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_MODEL", "flash")
    # Evitar cargar el modelo de embeddings en el test.
    import learning.embedding_matcher as em

    def _no_disponible(*a, **k):
        raise RuntimeError("embeddings desactivados en test")

    monkeypatch.setattr(em, "obtener_matcher", _no_disponible)

    import logging as _logging

    from core.problem_solver.builder import PlanBuilder
    from core.problem_solver.code_corrector import PythonCodeCorrector
    from core.problem_solver.validator import PlanValidator

    log = _logging.getLogger("test.ia_config.builder")
    log.addHandler(_logging.NullHandler())
    validador = PlanValidator(log)
    builder = PlanBuilder(log, PythonCodeCorrector(), validador)

    plan = builder.construir_plan("x", {
        "titulo": "t",
        "pasos": [{
            "orden": 1,
            "nombre": "Analizar",
            "tipo": "LLM",
            "configuracion": {"prompt": "analiza {contexto}"},
        }],
    })
    agentes = builder.generar_agentes(plan)

    assert agentes[0].modelo_llm == MODELO_FLASH
