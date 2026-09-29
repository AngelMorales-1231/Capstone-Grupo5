"""Invariantes del esquema: versión, RLS y los chequeos de 05_verificacion.sql.

Estas pruebas no ejercitan la API: verifican que la base que la API asume es la
que realmente hay. Si una de ellas falla, todas las demás pierden valor porque
el aislamiento y la confidencialidad dejan de estar garantizados por el motor.
"""
from __future__ import annotations

import sqlalchemy as sa

from app.database import SCHEMA_VERSION, schema_version


def test_version_de_esquema(esquema):
    """01→04 + 06 aplicados equivalen a la versión 6 que la API exige."""
    assert SCHEMA_VERSION == 6
    assert schema_version() == 6


def test_health_esquema(app_cliente):
    r = app_cliente.get("/health/esquema")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "ok", "schema_version": 6}


def test_rls_habilitado_y_forzado(motor_admin):
    """Chequeo 1 de 05_verificacion: toda tabla con company_id tiene RLS.

    Se exige además FORCE: sin él, el dueño de la tabla (y por tanto cualquier
    migración o job que se conecte con ese rol) saltaría las políticas.
    """
    with motor_admin.connect() as conn:
        filas = conn.execute(sa.text("""
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN information_schema.columns col
              ON col.table_name = c.relname
             AND col.table_schema = 'public'
             AND col.column_name = 'company_id'
            WHERE n.nspname = 'public' AND c.relkind = 'r'
        """)).all()

    assert filas, "ninguna tabla con company_id: el esquema no se aplicó"
    sin_rls = [f.relname for f in filas if not f.relrowsecurity]
    assert sin_rls == []

    # bitacora_cambios queda sin FORCE por diseño (§4 de 04_rls.sql): los
    # triggers que la escriben corren como dueño de la tabla.
    sin_force = [f.relname for f in filas
                 if not f.relforcerowsecurity and f.relname != "bitacora_cambios"]
    assert sin_force == []


def test_foreign_keys_con_indice(motor_admin):
    """Chequeo 2 de 05_verificacion: ninguna FK sin índice de soporte."""
    with motor_admin.connect() as conn:
        filas = conn.execute(sa.text("""
            SELECT conrelid::regclass::text AS tabla, conname
            FROM pg_constraint pc
            WHERE contype = 'f'
              AND connamespace = 'public'::regnamespace
              AND NOT EXISTS (
                SELECT 1 FROM pg_index i
                WHERE i.indrelid = pc.conrelid
                  AND (i.indkey::int2[])[0:cardinality(pc.conkey)-1] @> pc.conkey::int2[]
              )
        """)).all()
    assert [(f.tabla, f.conname) for f in filas] == []


def test_constraints_validadas(motor_admin):
    """Chequeo 3: no quedan constraints NOT VALID pendientes."""
    with motor_admin.connect() as conn:
        filas = conn.execute(sa.text(
            "SELECT conrelid::regclass::text, conname FROM pg_constraint "
            "WHERE connamespace='public'::regnamespace AND NOT convalidated"
        )).all()
    assert [tuple(f) for f in filas] == []


def test_indices_validos(motor_admin):
    """Chequeo 13: ningún índice quedó inválido tras la migración."""
    with motor_admin.connect() as conn:
        filas = conn.execute(sa.text(
            "SELECT indexrelid::regclass::text FROM pg_index WHERE NOT indisvalid"
        )).all()
    assert [f[0] for f in filas] == []


def test_jwe_no_legible_fuera_del_worker(motor_admin):
    """Chequeo 17: solo acredittia_worker puede leer el JWE de la credencial.

    El resto de roles con LOGIN —en particular el de la API— tiene el SELECT
    revocado sobre `credencial_jwe`. Es la garantía de que un bug en un router
    no puede devolver el secreto ni siquiera por accidente.
    """
    with motor_admin.connect() as conn:
        roles = conn.execute(sa.text(
            "SELECT rolname FROM pg_roles "
            "WHERE rolcanlogin AND NOT rolsuper AND rolname NOT LIKE 'pg\\_%'"
        )).scalars().all()
        con_acceso = [
            r for r in roles
            if conn.execute(sa.text(
                "SELECT has_column_privilege(:r, 'plataforma_credenciales',"
                " 'credencial_jwe', 'SELECT')"), {"r": r}).scalar()
        ]
    assert "acredittia_worker" in roles
    assert con_acceso == ["acredittia_worker"], con_acceso

    # La API sí debe poder leer las columnas no secretas y escribir el JWE.
    with motor_admin.connect() as conn:
        assert conn.execute(sa.text(
            "SELECT has_column_privilege('acredittia_app',"
            " 'plataforma_credenciales', 'usuario', 'SELECT')")).scalar()
        assert conn.execute(sa.text(
            "SELECT has_column_privilege('acredittia_app',"
            " 'plataforma_credenciales', 'credencial_jwe', 'INSERT')")).scalar()


def test_api_conecta_sin_bypass_de_rls(motor_admin, esquema):
    """La suite prueba RLS de verdad: el rol de la API no puede saltarlo."""
    from app.database import engine
    with engine.connect() as conn:
        usuario = conn.execute(sa.text("SELECT current_user")).scalar()
        bypass = conn.execute(sa.text(
            "SELECT rolbypassrls FROM pg_roles WHERE rolname = current_user")).scalar()
    assert usuario == "acredittia_app"
    assert bypass is False


def test_tablas_append_only_rechazan_update(app_cliente, motor_admin, empresa_a):
    """Chequeo estructural: `actividad` y `bitacora_cambios` son inmutables."""
    with motor_admin.connect() as conn:
        for tabla in ("actividad", "bitacora_cambios",
                      "plataforma_credencial_versiones", "cumplimiento_snapshots"):
            disparadores = conn.execute(sa.text("""
                SELECT t.tgname FROM pg_trigger t
                JOIN pg_class c ON c.oid = t.tgrelid
                WHERE c.relname = :t AND NOT t.tgisinternal
            """), {"t": tabla}).scalars().all()
            assert any("inmutable" in d for d in disparadores), (tabla, disparadores)
