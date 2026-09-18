# tests/test_agent_serialization.py
"""Pruebas de serialización y validación del modelo ``Agente``.

El round-trip ``to_dict`` → ``from_dict`` es crítico para el historial y el
aprendizaje. Se cubren los casos que antes rompían el round-trip
(``dependencias`` vs ``dependencias_ids``, ``prompt_usado`` vs ``prompt_llm``,
estado como string) y la clonación sin arrastrar estado de ejecución.
"""

import pytest

from core.agent import Agente, AgenteValidator, EstadoAgente, TipoAgente


class TestRoundTrip:
    def test_roundtrip_basico(self):
        original = Agente(
            nombre="Original",
            tipo=TipoAgente.HTTP,
            descripcion="desc",
            dependencias_ids=["dep1", "dep2"],
            estado=EstadoAgente.COMPLETADO,
            progreso=100,
            resultado={"ok": True},
        )
        reconstruido = Agente.from_dict(original.to_dict())

        assert reconstruido.id == original.id
        assert reconstruido.nombre == original.nombre
        assert reconstruido.tipo == original.tipo
        assert reconstruido.estado == original.estado
        assert reconstruido.dependencias_ids == ["dep1", "dep2"]
        assert reconstruido.resultado == {"ok": True}

    def test_roundtrip_llm_conserva_prompt(self):
        original = Agente(
            nombre="Llm",
            tipo=TipoAgente.LLM,
            prompt_llm="genera un cuento",
        )
        data = original.to_dict()
        assert data["prompt_usado"] == "genera un cuento"

        reconstruido = Agente.from_dict(data)
        assert reconstruido.prompt_llm == "genera un cuento"

    def test_to_dict_estado_es_string(self):
        agente = Agente(nombre="X", estado=EstadoAgente.ERROR)
        assert agente.to_dict()["estado"] == "Error"

    def test_to_dict_trunca_campos_largos(self):
        agente = Agente(
            nombre="Largo",
            tipo=TipoAgente.LLM,
            salida="s" * 600,
            error="e" * 600,
            prompt_llm="p" * 5000,
        )
        data = agente.to_dict()
        assert len(data["salida"]) == 500
        assert len(data["error"]) == 500
        assert len(data["prompt_usado"]) == 4000

    def test_to_json(self):
        import json

        agente = Agente(nombre="Json", resultado={"n": 1})
        assert json.loads(agente.to_json())["nombre"] == "Json"


class TestFromDictRobusto:
    def test_dependencias_string_se_convierte_en_lista(self):
        # Un string no debe iterarse carácter a carácter.
        agente = Agente.from_dict({"nombre": "X", "dependencias": "A1, A2"})
        assert agente.dependencias_ids == ["A1", "A2"]

    def test_dependencias_ids_string(self):
        agente = Agente.from_dict({"nombre": "X", "dependencias_ids": "A1"})
        assert agente.dependencias_ids == ["A1"]

    def test_dependencias_null_se_convierte_en_lista_vacia(self):
        agente = Agente.from_dict({"nombre": "X", "dependencias_ids": None})
        assert agente.dependencias_ids == []

    def test_campos_desconocidos_se_ignoran(self):
        agente = Agente.from_dict({"nombre": "X", "campo_inexistente": 42})
        assert agente.nombre == "X"
        assert not hasattr(agente, "campo_inexistente")

    def test_tipo_invalido_cae_en_python(self):
        agente = Agente.from_dict({"nombre": "X", "tipo": "NoExiste"})
        assert agente.tipo == TipoAgente.PYTHON

    def test_estado_invalido_cae_en_pendiente(self):
        agente = Agente.from_dict({"nombre": "X", "estado": "EstadoRaro"})
        assert agente.estado == EstadoAgente.PENDIENTE

    def test_no_muta_el_diccionario_original(self):
        data = {"nombre": "X", "dependencias": "A1"}
        Agente.from_dict(data)
        assert data == {"nombre": "X", "dependencias": "A1"}


class TestCloneYValidacion:
    def test_clonar_genera_nuevo_id_y_limpia_runtime(self):
        original = Agente(
            nombre="Original",
            estado=EstadoAgente.EJECUTANDO,
            progreso=50,
            salida="salida",
            error="error",
        )
        clon = original.clonar()

        assert clon is not original
        assert clon.id != original.id
        assert clon.nombre == original.nombre
        assert clon.estado == EstadoAgente.PENDIENTE
        assert clon.progreso == 0
        assert clon.salida == ""
        assert clon.error == ""

    def test_clonar_conservando_id(self):
        original = Agente(nombre="Original")
        assert original.clonar(nuevo_id=False).id == original.id

    def test_post_init_duracion_invalida(self):
        assert Agente(nombre="X", duracion=0).duracion == 0.1
        assert Agente(nombre="X", duracion=-5).duracion == 0.1

    def test_post_init_progreso_se_limita(self):
        assert Agente(nombre="X", progreso=150).progreso == 100
        assert Agente(nombre="X", progreso=-10).progreso == 0

    def test_post_init_nombre_por_defecto(self):
        agente = Agente()
        assert agente.nombre == f"Agente_{agente.id}"

    def test_eq_y_hash_por_id(self):
        a = Agente(nombre="A")
        b = Agente(nombre="B")
        assert a == a
        assert a != b
        assert len({a, b}) == 2
        assert a != "no es agente"


class TestAgenteValidator:
    def test_validar_nombre(self):
        assert AgenteValidator.validar_nombre("")[0] is False
        assert AgenteValidator.validar_nombre("   ")[0] is False
        assert AgenteValidator.validar_nombre("a")[0] is False
        assert AgenteValidator.validar_nombre("Agente OK")[0] is True
        assert AgenteValidator.validar_nombre("malo!")[0] is False
        assert AgenteValidator.validar_nombre("x" * 101)[0] is False

    def test_validar_codigo_vacio_es_valido(self):
        assert AgenteValidator.validar_codigo("") == (True, "")

    def test_validar_codigo_indentacion(self):
        assert AgenteValidator.validar_codigo("x = 1\ny = 2")[0] is True
        assert AgenteValidator.validar_codigo("    x = 1")[0] is True
        assert AgenteValidator.validar_codigo("  x = 1")[0] is False

    def test_validar_codigo_limite(self):
        ok, mensaje = AgenteValidator.validar_codigo("x" * 11, max_length=10)
        assert ok is False
        assert "limite" in mensaje.lower() or "límite" in mensaje.lower()


class TestEstados:
    @pytest.mark.parametrize("estado", [
        EstadoAgente.COMPLETADO,
        EstadoAgente.ERROR,
        EstadoAgente.TIMEOUT,
        EstadoAgente.CANCELADO,
        EstadoAgente.SALTADO,
        EstadoAgente.BLOQUEADO,
    ])
    def test_es_terminal(self, estado):
        assert EstadoAgente.es_terminal(estado) is True

    @pytest.mark.parametrize("estado", [
        EstadoAgente.PENDIENTE,
        EstadoAgente.EN_COLA,
        EstadoAgente.ESPERANDO,
        EstadoAgente.LISTO,
        EstadoAgente.EJECUTANDO,
        EstadoAgente.REINTENTANDO,
    ])
    def test_no_es_terminal(self, estado):
        assert EstadoAgente.es_terminal(estado) is False

    def test_color_y_emoji_siempre_presentes(self):
        for estado in EstadoAgente:
            assert isinstance(EstadoAgente.color(estado), str)
            assert isinstance(EstadoAgente.emoji(estado), str)
