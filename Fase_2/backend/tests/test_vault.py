"""Vault de credenciales de plataforma: cifrado JWE, rotación y no fuga (§8.2).

Lo que se protege aquí no es una regla de negocio, es un secreto: la contraseña
con la que la empresa entra al portal del mandante. El diseño lo garantiza en
tres capas y cada una tiene su prueba:

1. **La API nunca devuelve el secreto.** Ni el password, ni el JWE, ni el `kid`.
2. **La base nunca lo guarda en claro.** La columna contiene un JWE compact de
   cinco partes y el password no aparece como subcadena.
3. **El historial no se puede reescribir.** `plataforma_credencial_versiones` es
   append-only por trigger (SQLSTATE 55000), así que no se puede falsificar qué
   secreto estuvo vigente.

Las lecturas de `credencial_jwe` se hacen con `motor_admin` (superusuario)
porque el rol de la API tiene el SELECT revocado sobre esa columna: si estas
pruebas pudieran leerla con el motor de la aplicación, el modelo estaría roto.
"""
from __future__ import annotations

import json
import uuid

import pytest
import sqlalchemy as sa

from app.services.crypto import CredencialError, descifrar_credencial

from tests.conftest import API

PASSWORD = "Cl4v3-Del-Portal-2026!"


# ============================================================================
# Utilidades
# ============================================================================
def _plataforma(cliente, empresa, contrato, nombre="PORTAL PROPIO") -> str:
    r = cliente.post(f"{API}/contratos/{contrato['id']}/plataformas",
                     headers=empresa["headers"],
                     json={"nombre": nombre, "url": "https://siga.cl"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _credencial(cliente, empresa, contrato, pid, *, usuario="jperez",
                password=PASSWORD):
    return cliente.post(
        f"{API}/contratos/{contrato['id']}/plataformas/{pid}/usuarios",
        headers=empresa["headers"],
        json={"nombre": "Juan Pérez", "usuario": usuario, "password": password})


def _jwe_almacenado(motor_admin, credencial_id: str) -> tuple[str, str, int]:
    with motor_admin.connect() as conn:
        return conn.execute(sa.text(
            "SELECT credencial_jwe, kid, version FROM plataforma_credenciales "
            "WHERE id = :i"), {"i": credencial_id}).one()


@pytest.fixture
def credencial(app_cliente, empresa_a, contrato_a) -> dict:
    pid = _plataforma(app_cliente, empresa_a, contrato_a)
    r = _credencial(app_cliente, empresa_a, contrato_a, pid)
    assert r.status_code == 201, r.text
    return {"pid": pid, "id": r.json()["id"], "cuerpo": r.json(),
            "base": f"{API}/contratos/{contrato_a['id']}/plataformas/{pid}"}


# ============================================================================
# 1. La API no devuelve el secreto
# ============================================================================
def test_alta_no_filtra_el_secreto(app_cliente, empresa_a, contrato_a):
    pid = _plataforma(app_cliente, empresa_a, contrato_a)
    r = _credencial(app_cliente, empresa_a, contrato_a, pid)
    assert r.status_code == 201, r.text

    cuerpo = r.json()
    assert cuerpo["usuario"] == "jperez"
    assert cuerpo["password_set"] is True
    assert cuerpo["version"] == 1

    # Se asevera sobre el JSON serializado completo, no clave a clave: así una
    # fuga anidada (dentro de `meta`, de una lista…) también falla.
    crudo = json.dumps(cuerpo, ensure_ascii=False)
    assert PASSWORD not in crudo
    assert "credencial_jwe" not in crudo
    assert "kid" not in crudo
    assert "password" not in cuerpo


def test_ningun_endpoint_de_la_cuenta_expone_el_jwe(app_cliente, empresa_a,
                                                   credencial):
    """Listado y detalle de usos: ninguno menciona el secreto."""
    rutas = [
        f"{credencial['base']}/usuarios",
        f"{credencial['base']}/usuarios/{credencial['id']}/usos",
    ]
    for ruta in rutas:
        r = app_cliente.get(ruta, headers=empresa_a["headers"])
        assert r.status_code == 200, (ruta, r.text)
        assert PASSWORD not in r.text
        assert "credencial_jwe" not in r.text


def test_el_kid_no_viaja_en_el_historial_de_la_cuenta(app_cliente, credencial,
                                                      empresa_a):
    """`kid` sí está concedido en la tabla de versiones: identifica la clave de
    Key Vault, no el secreto, y el auditor lo necesita. Lo que no puede viajar
    es el JWE."""
    app_cliente.post(f"{credencial['base']}/usuarios/{credencial['id']}/rotar",
                     headers=empresa_a["headers"], json={"password": "Otra-Cl4ve!"})
    r = app_cliente.get(f"{credencial['base']}/usuarios/{credencial['id']}/usos",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    versiones = r.json()["versiones"]
    assert versiones and versiones[0]["kid"]
    assert "credencial_jwe" not in r.text
    assert PASSWORD not in r.text


def test_la_api_no_puede_leer_la_columna_del_jwe(app_cliente, credencial):
    """El GRANT por columnas es real: el rol de la API recibe 42501 al pedirla."""
    import sqlalchemy.exc

    from app.database import SessionLocal, set_ctx

    set_ctx(is_admin=True)
    with SessionLocal() as db:
        with pytest.raises(sqlalchemy.exc.ProgrammingError) as exc:
            db.execute(sa.text("SELECT credencial_jwe FROM plataforma_credenciales"))
        assert "permission denied" in str(exc.value)
        db.rollback()
        # Las columnas concedidas sí se leen.
        assert db.execute(sa.text(
            "SELECT count(*) FROM plataforma_credenciales")).scalar() == 1


# ============================================================================
# 2. La base guarda un JWE, no el secreto
# ============================================================================
def test_la_columna_contiene_un_jwe_de_cinco_partes(motor_admin, credencial):
    jwe, kid, version = _jwe_almacenado(motor_admin, credencial["id"])
    assert jwe.count(".") == 4, "la serialización compacta tiene 5 partes"
    assert all(parte for parte in jwe.split(".")[:1]), "cabecera protegida vacía"
    assert kid.startswith("local://"), kid
    assert version == 1

    # El secreto no aparece en claro ni en base64url «ingenuo».
    import base64
    assert PASSWORD not in jwe
    b64 = base64.urlsafe_b64encode(PASSWORD.encode()).decode().rstrip("=")
    assert b64 not in jwe

    cabecera = json.loads(_b64u_dec(jwe.split(".")[0]))
    assert cabecera["alg"] == "RSA-OAEP-256"
    assert cabecera["enc"] == "A256GCM"


def _b64u_dec(s: str) -> bytes:
    import base64
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def test_el_secreto_no_aparece_en_ninguna_tabla(motor_admin, credencial,
                                                app_cliente, empresa_a):
    """Barrido de confidencialidad: ni la bitácora ni la actividad lo copian.

    `fn_bitacora` serializa la fila completa en INSERT y DELETE, y por eso
    `plataforma_credenciales` queda deliberadamente fuera de la bitácora técnica
    (§9.7 de la migración). Esta prueba es la que detectaría que alguien la
    añadiera.
    """
    app_cliente.post(f"{credencial['base']}/usuarios/{credencial['id']}/rotar",
                     headers=empresa_a["headers"], json={"password": "Otra-Cl4ve!"})
    with motor_admin.connect() as conn:
        for tabla, columna in (("bitacora_cambios", "campos::text"),
                               ("actividad", "metadata::text"),
                               ("actividad", "descripcion")):
            filas = conn.execute(sa.text(
                f"SELECT count(*) FROM {tabla} WHERE {columna} LIKE :p"),
                {"p": f"%{PASSWORD}%"}).scalar()
            assert filas == 0, f"{tabla}.{columna} contiene el secreto"
        # Tampoco el JWE vigente se copió a la bitácora.
        jwe, _, _ = _jwe_almacenado(motor_admin, credencial["id"])
        assert conn.execute(sa.text(
            "SELECT count(*) FROM bitacora_cambios WHERE campos::text LIKE :p"),
            {"p": f"%{jwe[:40]}%"}).scalar() == 0


# ============================================================================
# 3. Rotación y archivado
# ============================================================================
def test_dos_rotaciones_dejan_version_3_y_dos_archivadas(
        app_cliente, empresa_a, credencial, motor_admin):
    ruta = f"{credencial['base']}/usuarios/{credencial['id']}/rotar"
    r = app_cliente.post(ruta, headers=empresa_a["headers"],
                         json={"password": "Segunda-Cl4ve!"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 2

    r = app_cliente.post(ruta, headers=empresa_a["headers"],
                         json={"password": "Tercera-Cl4ve!"})
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 3

    jwe, kid, version = _jwe_almacenado(motor_admin, credencial["id"])
    assert version == 3

    with motor_admin.connect() as conn:
        filas = conn.execute(sa.text(
            "SELECT version, kid, rotada_por, credencial_jwe, "
            "       vigente_desde, vigente_hasta "
            "FROM plataforma_credencial_versiones WHERE credencial_id = :i "
            "ORDER BY version"), {"i": credencial["id"]}).all()

    assert [f.version for f in filas] == [1, 2], (
        "el trigger archiva la versión ANTERIOR, así que quedan la 1 y la 2")
    for f in filas:
        assert f.kid, "cada versión archivada conserva la clave con que se cifró"
        assert str(f.rotada_por) == empresa_a["user_id"], (
            "rotada_por viene de app.user_id, que fija el contexto de tenant")
        assert f.credencial_jwe.count(".") == 4
        assert f.vigente_hasta >= f.vigente_desde
    # El JWE vigente no está duplicado en el historial.
    assert jwe not in [f.credencial_jwe for f in filas]


def test_la_rotacion_no_es_idempotente_en_la_version(app_cliente, empresa_a,
                                                     credencial, motor_admin):
    """Rotar con la MISMA contraseña también cuenta: el JWE cambia (IV y CEK)."""
    ruta = f"{credencial['base']}/usuarios/{credencial['id']}/rotar"
    primero, _, _ = _jwe_almacenado(motor_admin, credencial["id"])
    r = app_cliente.post(ruta, headers=empresa_a["headers"],
                         json={"password": PASSWORD})
    assert r.status_code == 200, r.text
    segundo, _, version = _jwe_almacenado(motor_admin, credencial["id"])
    assert primero != segundo
    assert version == 2


def test_el_historial_rechaza_update_con_55000(motor_admin, app_cliente,
                                               empresa_a, credencial):
    """Append-only: una versión archivada no se puede REESCRIBIR."""
    app_cliente.post(f"{credencial['base']}/usuarios/{credencial['id']}/rotar",
                     headers=empresa_a["headers"], json={"password": "Otra-Cl4ve!"})

    with motor_admin.connect() as conn:
        with pytest.raises(sa.exc.DBAPIError) as exc:
            conn.execute(sa.text(
                "UPDATE plataforma_credencial_versiones SET kid = 'falsificado' "
                "WHERE credencial_id = :i"), {"i": credencial["id"]})
        assert exc.value.orig.sqlstate == "55000", exc.value.orig.sqlstate
        conn.rollback()

    with motor_admin.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM plataforma_credencial_versiones "
            "WHERE kid = 'falsificado'"), ).scalar() == 0


def test_borrar_la_credencial_arrastra_su_historial(app_cliente, empresa_a,
                                                    credencial, motor_admin):
    """El DELETE del historial está permitido a propósito.

    Con el DELETE bloqueado por el trigger append-only, la cascada de
    `ON DELETE CASCADE` abortaba con SQLSTATE 55000 y la credencial quedaba
    imborrable en cuanto tenía una sola rotación. Esta prueba fija ese contrato.
    """
    app_cliente.post(f"{credencial['base']}/usuarios/{credencial['id']}/rotar",
                     headers=empresa_a["headers"], json={"password": "Otra-Cl4ve!"})
    with motor_admin.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM plataforma_credencial_versiones "
            "WHERE credencial_id = :i"), {"i": credencial["id"]}).scalar() == 1

    r = app_cliente.delete(f"{credencial['base']}/usuarios/{credencial['id']}",
                           headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["versiones_eliminadas"] == 1

    with motor_admin.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM plataforma_credenciales WHERE id = :i"),
            {"i": credencial["id"]}).scalar() == 0
        assert conn.execute(sa.text(
            "SELECT count(*) FROM plataforma_credencial_versiones "
            "WHERE credencial_id = :i"), {"i": credencial["id"]}).scalar() == 0


def test_borrar_la_plataforma_arrastra_credenciales_con_historial(
        app_cliente, empresa_a, contrato_a, credencial, motor_admin):
    app_cliente.post(f"{credencial['base']}/usuarios/{credencial['id']}/rotar",
                     headers=empresa_a["headers"], json={"password": "Otra-Cl4ve!"})
    r = app_cliente.delete(f"{credencial['base']}?confirm=true",
                           headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["credenciales_eliminadas"] == 1
    with motor_admin.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT count(*) FROM plataforma_credencial_versiones")).scalar() == 0


# ============================================================================
# 4. Descifrado (solo el worker)
# ============================================================================
def test_descifrar_devuelve_el_secreto_y_valida_los_claims(
        motor_admin, credencial, empresa_a, contrato_a):
    jwe, kid, _ = _jwe_almacenado(motor_admin, credencial["id"])

    payload = descifrar_credencial(
        jwe, esperado_sub=credencial["id"],
        esperado_cid=empresa_a["company_id"], esperado_ctr=contrato_a["id"])

    assert payload["pwd"] == PASSWORD
    assert payload["usr"] == "jperez"
    assert payload["plt"] == "PORTAL PROPIO"
    assert payload["iss"] == "acredittia-api"
    assert payload["aud"] == "acredittia-platform-worker"
    assert payload["sub"] == credencial["id"]
    assert payload["cid"] == empresa_a["company_id"]
    assert payload["ctr"] == contrato_a["id"]


def test_descifrar_rechaza_un_cid_que_no_corresponde(motor_admin, credencial,
                                                     contrato_a):
    """Una copia del JWE entre filas se detecta: los claims atan la credencial."""
    jwe, _, _ = _jwe_almacenado(motor_admin, credencial["id"])
    with pytest.raises(CredencialError) as exc:
        descifrar_credencial(jwe, esperado_cid=str(uuid.uuid4()))
    assert "cid" in str(exc.value)
    assert PASSWORD not in str(exc.value)

    with pytest.raises(CredencialError):
        descifrar_credencial(jwe, esperado_sub=str(uuid.uuid4()))
    with pytest.raises(CredencialError):
        descifrar_credencial(jwe, esperado_ctr=str(uuid.uuid4()))


def test_descifrar_rechaza_un_jwe_manipulado(motor_admin, credencial):
    jwe, _, _ = _jwe_almacenado(motor_admin, credencial["id"])
    partes = jwe.split(".")
    partes[3] = partes[3][:-4] + "AAAA"          # ciphertext alterado
    with pytest.raises(CredencialError) as exc:
        descifrar_credencial(".".join(partes))
    assert "No se pudo descifrar" in str(exc.value)

    with pytest.raises(CredencialError) as exc:
        descifrar_credencial("solo.tres.partes")
    assert "5 partes" in str(exc.value)


# ============================================================================
# 5. El trigger valida el formato sin volcar el valor
# ============================================================================
def test_el_trigger_rechaza_un_jwe_mal_formado_sin_revelarlo(motor_admin,
                                                            credencial):
    """Sin CHECK constraints por diseño: el mensaje no puede contener la fila.

    Un CHECK violado emite «DETAIL: Failing row contains (…)» con el JWE dentro,
    y ese texto va al log del servidor. La validación vive en
    `fn_check_credencial`, que solo nombra el id de la credencial.
    """
    basura = "esto-no-es-un-jwe-" + PASSWORD
    with motor_admin.connect() as conn:
        with pytest.raises(sa.exc.DBAPIError) as exc:
            conn.execute(sa.text(
                "UPDATE plataforma_credenciales SET credencial_jwe = :v "
                "WHERE id = :i"), {"v": basura, "i": credencial["id"]})
        conn.rollback()

    # Se inspecciona el mensaje que emitió EL SERVIDOR (lo que acaba en
    # postgresql.log), no el `str()` de SQLAlchemy, que añade los parámetros
    # enlazados del lado del cliente y por tanto siempre contendría el valor.
    original = exc.value.orig
    del_servidor = str(original) + " " + (original.diag.message_detail or "")
    assert original.sqlstate == "23514"
    assert "formato JWE compact de 5 partes" in del_servidor
    assert basura not in del_servidor, "el error del servidor filtró el valor"
    assert PASSWORD not in del_servidor
    assert "Failing row contains" not in del_servidor, (
        "un CHECK constraint volcaría la fila completa —con el JWE— al log")

    # Y la fila siguió intacta.
    jwe, _, version = _jwe_almacenado(motor_admin, credencial["id"])
    assert jwe.count(".") == 4 and version == 1


def test_el_trigger_exige_coherencia_de_tenant(motor_admin, credencial,
                                               empresa_b):
    with motor_admin.connect() as conn:
        with pytest.raises(sa.exc.DBAPIError) as exc:
            conn.execute(sa.text(
                "UPDATE plataforma_credenciales SET company_id = :c "
                "WHERE id = :i"),
                {"c": empresa_b["company_id"], "i": credencial["id"]})
        conn.rollback()
    assert exc.value.orig.sqlstate == "23514"
    assert "no coincide con la plataforma" in str(exc.value)


# ============================================================================
# 6. Reglas de negocio del vault
# ============================================================================
def test_usuario_duplicado_en_la_misma_plataforma(app_cliente, empresa_a,
                                                  contrato_a, credencial):
    r = _credencial(app_cliente, empresa_a, contrato_a, credencial["pid"],
                    usuario="JPerez")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "USUARIO_DUPLICADO"


def test_password_debil_no_se_guarda(app_cliente, empresa_a, contrato_a):
    pid = _plataforma(app_cliente, empresa_a, contrato_a)
    r = _credencial(app_cliente, empresa_a, contrato_a, pid, password="corta")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "PASSWORD_DEBIL"


def test_no_se_cambia_el_password_por_patch(app_cliente, empresa_a, credencial):
    """La rotación tiene endpoint propio para que quede archivada."""
    r = app_cliente.patch(f"{credencial['base']}/usuarios/{credencial['id']}",
                          headers=empresa_a["headers"],
                          json={"password": "Nueva-Cl4ve!"})
    assert r.status_code == 400, r.text


def test_una_credencial_revocada_no_se_rota(app_cliente, empresa_a, credencial):
    r = app_cliente.patch(f"{credencial['base']}/usuarios/{credencial['id']}",
                          headers=empresa_a["headers"],
                          json={"estado": "revocada"})
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "revocada"

    r = app_cliente.post(f"{credencial['base']}/usuarios/{credencial['id']}/rotar",
                         headers=empresa_a["headers"],
                         json={"password": "Otra-Cl4ve!"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "CREDENCIAL_REVOCADA"
