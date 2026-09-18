# tests/test_database_unit.py
"""Pruebas unitarias de la capa de persistencia (``storage/database.py``).

Cubre las partes que antes fallaban en silencio: transacciones (commit y
rollback), serialización comprimida, normalización de estados y backup /
restore. Se usa una base de datos temporal por test para no tocar el
historial real del proyecto.
"""

import sqlite3

import pytest

from storage.database import Database


def _agente(nombre="A1", estado="Completado"):
    return {
        "id": "id-1",
        "nombre": nombre,
        "estado": estado,
        "tipo": "Python",
        "resultado": {"ok": True},
        "dependencias": [],
    }


@pytest.fixture
def db(tmp_path):
    base = Database(str(tmp_path / "test.db"))
    yield base
    base.close()


class TestIntegridad:
    def test_db_nueva_es_integra(self, db):
        integro, _ = db.verificar_integridad()
        assert integro is True


class TestGuardarYConsultar:
    def test_guardar_y_recuperar(self, db):
        ejecucion_id = db.guardar_ejecucion([_agente()], 1.5)
        assert ejecucion_id > 0

        historial = db.obtener_historial(limit=10)
        assert len(historial) == 1

        detalle = db.obtener_detalle_ejecucion(ejecucion_id)
        assert len(detalle) == 1
        assert detalle[0]["nombre"] == "A1"
        assert detalle[0]["resultado"] == {"ok": True}

    def test_normaliza_estados(self, db):
        ejecucion_id = db.guardar_ejecucion(
            [
                _agente("A1", "completado"),
                _agente("A2", "FAILED"),
                _agente("A3", "canceled"),
            ],
            2.0,
        )
        ejecucion = db.obtener_ejecucion(ejecucion_id)
        assert ejecucion["completados"] == 1
        assert ejecucion["errores"] == 1
        assert ejecucion["cancelados"] == 1
        assert ejecucion["agentes_total"] == 3

    def test_sin_agentes_lanza_value_error(self, db):
        with pytest.raises(ValueError):
            db.guardar_ejecucion([], 0.0)

    def test_agente_sin_estado_lanza_value_error(self, db):
        with pytest.raises(ValueError):
            db.guardar_ejecucion([{"nombre": "X"}], 0.0)

    def test_agente_no_dict_lanza_value_error(self, db):
        with pytest.raises(ValueError):
            db.guardar_ejecucion(["no soy dict"], 0.0)

    def test_eliminar_ejecucion(self, db):
        ejecucion_id = db.guardar_ejecucion([_agente()], 1.0)
        assert db.eliminar_ejecucion(ejecucion_id) is True
        assert db.obtener_ejecucion(ejecucion_id) is None
        assert db.eliminar_ejecucion(ejecucion_id) is False


class TestTransacciones:
    def test_commit_persiste(self, db):
        with db._transaction() as conn:
            conn.execute("CREATE TABLE _tmp_commit (x INTEGER)")
            conn.execute("INSERT INTO _tmp_commit (x) VALUES (1)")

        with db._transaction() as conn:
            total = conn.execute("SELECT COUNT(*) FROM _tmp_commit").fetchone()[0]
        assert total == 1

    def test_rollback_descarta_cambios(self, db):
        # La tabla se crea fuera de la transacción (autocommit).
        conn = db._get_connection()
        conn.execute("CREATE TABLE IF NOT EXISTS _tmp_rollback (x INTEGER)")

        with pytest.raises(RuntimeError):
            with db._transaction() as c:
                c.execute("INSERT INTO _tmp_rollback (x) VALUES (1)")
                raise RuntimeError("boom")

        with db._transaction() as c:
            total = c.execute("SELECT COUNT(*) FROM _tmp_rollback").fetchone()[0]
        assert total == 0

    def test_rollback_no_deja_transaccion_abierta(self, db):
        conn = db._get_connection()
        with pytest.raises(ValueError):
            with db._transaction():
                raise ValueError("fallo")
        # Una transacción posterior debe poder abrirse sin problemas.
        assert conn.in_transaction is False
        with db._transaction() as c:
            assert c.execute("SELECT 1").fetchone()[0] == 1


class TestSerializacionComprimida:
    def test_json_pequeno_no_se_comprime(self, db):
        serializado = db._comprimir_json({"a": 1})
        assert not serializado.startswith("GZIP:")
        assert db._descomprimir_json(serializado) == {"a": 1}

    def test_json_grande_se_comprime(self, db):
        data = {"lista": list(range(5000))}
        serializado = db._comprimir_json(data)
        assert serializado.startswith("GZIP:")
        assert db._descomprimir_json(serializado) == data

    def test_descomprimir_vacio(self, db):
        assert db._descomprimir_json("") == {}
        assert db._descomprimir_json("null") == {}
        assert db._descomprimir_json("{}") == {}

    def test_descomprimir_json_plano(self, db):
        assert db._descomprimir_json('{"a": 1}') == {"a": 1}


class TestNormalizarEstado:
    def test_variantes(self, db):
        assert db._normalizar_estado("completado") == "Completado"
        assert db._normalizar_estado("COMPLETADO") == "Completado"
        assert db._normalizar_estado("error") == "Error"
        assert db._normalizar_estado("failed") == "Error"
        assert db._normalizar_estado("cancelado") == "Cancelado"
        assert db._normalizar_estado("pendiente") == "Pendiente"
        assert db._normalizar_estado("running") == "Ejecutando"

    def test_vacio(self, db):
        assert db._normalizar_estado("") == "pendiente"

    def test_desconocido_se_conserva(self, db):
        assert db._normalizar_estado("estado-raro") == "estado-raro"


class TestBackupRestore:
    def test_backup_y_restore(self, db, tmp_path):
        ejecucion_id = db.guardar_ejecucion([_agente()], 1.0)
        destino = tmp_path / "copia.db"

        ruta = db.backup(str(destino))
        assert ruta == str(destino)
        assert destino.exists()

        # Mutar la base y restaurar.
        assert db.eliminar_ejecucion(ejecucion_id) is True
        assert db.obtener_ejecucion(ejecucion_id) is None

        assert db.restore_backup(str(destino)) is True
        assert db.obtener_ejecucion(ejecucion_id) is not None

    def test_restore_de_archivo_inexistente(self, db, tmp_path):
        assert db.restore_backup(str(tmp_path / "no-existe.db")) is False

    def test_restore_de_backup_corrupto(self, db, tmp_path):
        corrupto = tmp_path / "corrupto.db"
        corrupto.write_bytes(b"esto no es una base sqlite")
        assert db.restore_backup(str(corrupto)) is False
        # La base original debe seguir funcionando.
        integro, _ = db.verificar_integridad()
        assert integro is True


class TestCompatibilidadSqlite:
    def test_es_sqlite_valido(self, db):
        conn = db._get_connection()
        assert isinstance(conn, sqlite3.Connection)
