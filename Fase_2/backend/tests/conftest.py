"""Infraestructura de la suite: PostgreSQL real, esquema real, cliente real.

Por qué PostgreSQL embebido y no SQLite
--------------------------------------
El modelo de datos usa ENUM nativos, dominios, RLS con `FORCE ROW LEVEL
SECURITY`, triggers PL/pgSQL, `UNIQUE ... NULLS NOT DISTINCT`, índices por
expresión y privilegios por columna. Nada de eso existe en SQLite: una suite
sobre SQLite probaría un esquema distinto del que corre en producción y dejaría
pasar precisamente los fallos que este modelo previene (fuga entre tenants,
credenciales legibles, tablas append-only mutables). Se usa `pgserver`, que trae
PostgreSQL 16 embebido.

La API se conecta con el rol **acredittia_app** (`NOBYPASSRLS`), no con
`postgres`. Es deliberado: conectarse como superusuario saltaría RLS y las
pruebas de aislamiento (`test_tenants.py`) medirían solo los filtros explícitos
de las consultas, no la barrera del motor. Las operaciones de fixture que
necesitan privilegios de dueño (TRUNCATE) y las aserciones que deben ver lo que
la API no puede ver (`credencial_jwe`) usan un segundo motor como `postgres`,
expuesto en el fixture `motor_admin`.

LIMITACIÓN DEL ENTORNO DE PRUEBAS
---------------------------------
El sandbox no dispone de las extensiones `citext` ni `pg_trgm`. Para poder
aplicar el esquema real sin tocarlo:

* se crea `CREATE DOMAIN citext AS text` antes de los scripts, de modo que las
  columnas `email citext` existan con la misma semántica salvo la insensibilidad
  a mayúsculas (la aplicación normaliza el email a minúsculas, así que no hay
  comportamiento que dependa de ella);
* se comentan las líneas `CREATE EXTENSION` de `01_esquema.sql` y los tres
  índices GIN `gin_trgm_ops` de `02_indices.sql`.

El parcheo se hace sobre **copias** en un directorio temporal; los `.sql` de
`modelo_datos/` no se modifican. Consecuencia asumida: los índices de búsqueda
por trigrama no se prueban (son de rendimiento, no de corrección) y el chequeo 2
de `05_verificacion.sql` se evalúa sobre el resto de los índices, que sí se crean.

Orden de arranque
-----------------
`app/config.py` construye `settings` al importarse y `app/database.py` crea el
motor con `settings.database_url`, así que el entorno tiene que estar fijado
ANTES de cualquier `import app.*`. Por eso el arranque del servidor y la
aplicación del esquema ocurren a nivel de módulo de este conftest —que pytest
importa antes que cualquier módulo de prueba— y no dentro de un fixture. Los
fixtures `pg_server` y `esquema` exponen el resultado para las pruebas que lo
necesiten.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import date, timedelta

import pytest

# ============================================================================
# 1. Arranque del servidor y aplicación del esquema (nivel de módulo)
# ============================================================================
import pgserver

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))     # app/backend
MODELO = os.path.normpath(os.path.join(RAIZ, "..", "..", "modelo_datos"))

SCRIPTS = ["01_esquema.sql", "02_indices.sql", "03_triggers.sql",
           "04_rls.sql", "06_migracion_v11.sql", 
           "07_consola_interna.sql", "08_funciones_consola.sql"]

_TMP = os.path.join(tempfile.gettempdir(), "acredittia_pruebas")
PGDATA = os.path.join(_TMP, "pgdata")
SQL_DIR = os.path.join(_TMP, "sql")
KEYS_DIR = os.path.join(_TMP, "keys")
STORAGE_DIR = os.path.join(_TMP, "uploads")

PSQL = os.path.join(os.path.dirname(pgserver.__file__), "pginstall", "bin", "psql")

# Catálogos globales sembrados al arrancar: NO se vacían entre pruebas.
CATALOGOS = {"faenas", "faena_plataformas", "doc_ejemplos", "requisito_templates",
             "requisitos_terreno", "proveedores_catalogo", "planes",
             "ops_componentes", "ops_jobs"}


def _parchear_scripts() -> str:
    """Copia los .sql a un temporal y neutraliza lo que el sandbox no soporta."""
    os.makedirs(SQL_DIR, exist_ok=True)
    for nombre in SCRIPTS:
        origen = os.path.join(MODELO, nombre)
        with open(origen, encoding="utf-8") as f:
            lineas = f.read().splitlines()
        salida = []
        for linea in lineas:
            desnudo = linea.strip()
            if desnudo.startswith("\\echo"):
                continue                                   # metacomando de psql
            if desnudo.upper().startswith("CREATE EXTENSION"):
                salida.append(f"-- [pruebas] extensión no disponible: {linea}")
                continue
            if "gin_trgm_ops" in linea:
                salida.append(f"-- [pruebas] pg_trgm no disponible: {linea}")
                continue
            salida.append(linea)
        with open(os.path.join(SQL_DIR, nombre), "w", encoding="utf-8") as f:
            f.write("\n".join(salida) + "\n")
    return SQL_DIR


def _psql(uri: str, *args: str) -> None:
    r = subprocess.run([PSQL, uri, "-v", "ON_ERROR_STOP=1", "-q", *args],
                       capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"psql falló ({args}):\n{r.stdout[-3000:]}\n{r.stderr[-6000:]}")


def _arrancar() -> tuple[object, str]:
    """Levanta PostgreSQL, recrea `public` y aplica 01→06. Devuelve (servidor, uri_app)."""
    os.makedirs(_TMP, exist_ok=True)
    servidor = pgserver.get_server(PGDATA)
    uri_super = servidor.get_uri()

    _psql(uri_super, "-c", "DROP SCHEMA IF EXISTS public CASCADE; CREATE SCHEMA public;")
    _psql(uri_super, "-c", "DROP DOMAIN IF EXISTS citext; CREATE DOMAIN citext AS text;")
    sql_dir = _parchear_scripts()
    for nombre in SCRIPTS:
        _psql(uri_super, "-f", os.path.join(sql_dir, nombre))

    # El rol de aplicación existe tras 04_rls.sql; el socket local admite
    # conexión sin contraseña, así que la API se conecta como acredittia_app y
    # RLS se aplica de verdad.
    socket = uri_super.split("host=", 1)[1]
    return servidor, f"postgresql+psycopg://acredittia_app@/postgres?host={socket}"


# Referencia global: pgserver apaga el servidor cuando se recolecta.
SERVIDOR, URI_APP = _arrancar()
URI_SUPER = SERVIDOR.get_uri()
URI_SUPER_SA = "postgresql+psycopg://postgres@/postgres?host=" + URI_SUPER.split("host=", 1)[1]

# ============================================================================
# 2. Entorno de la aplicación (antes de importar app.*)
# ============================================================================
shutil.rmtree(KEYS_DIR, ignore_errors=True)
shutil.rmtree(STORAGE_DIR, ignore_errors=True)
os.makedirs(KEYS_DIR, exist_ok=True)
os.makedirs(STORAGE_DIR, exist_ok=True)

os.environ.update({
    "DATABASE_URL": URI_APP,
    "QUEUE_BACKEND": "inproc",
    "JWE_BACKEND": "local",
    "KEYS_DIR": KEYS_DIR,
    "STORAGE_BACKEND": "local",
    "STORAGE_DIR": STORAGE_DIR,
    "JWT_SECRET": "dev-test",
    "DB_APPLY_SCHEMA_ON_START": "false",
    "DB_RLS_ENABLED": "true",
    "IA_BACKEND": "simulada",
    "PUBLIC_BASE_URL": "http://test",
    "SCHEMA_DIR": SQL_DIR,
    "CORS_ORIGINS": "http://test",
})

if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

import sqlalchemy as sa                                          # noqa: E402
from fastapi.testclient import TestClient                        # noqa: E402

from app import security                                         # noqa: E402
from app.config import settings                                  # noqa: E402
from app.database import worker_session                          # noqa: E402
from app.models import User                            # noqa: E402

assert settings.database_url == URI_APP, "el entorno no llegó a app.config"

# ----------------------------------------------------------------------------
# Hash de contraseñas rápido SOLO en pruebas.
# Argon2 cuesta ~0,2 s por operación y cada prueba registra dos empresas, un
# admin y hace tres logins: sin esto la suite tardaría más en derivar claves que
# en ejercitar la API. Se sustituye el objeto hasher, no la lógica de
# `app.security`, de modo que sigue probándose el mismo camino de código.
# ----------------------------------------------------------------------------
class _HasherPruebas:
    def hash(self, plain: str, **kw) -> str:
        return "pruebas$" + hashlib.sha256(plain.encode()).hexdigest()

    def verify(self, plain: str, hashed: str, **kw) -> bool:
        return hmac.compare_digest(self.hash(plain), hashed or "")


security.pwd = _HasherPruebas()

CLAVE = "Secreta123"
API = "/api/v1"


# ============================================================================
# 3. Utilidades
# ============================================================================
def dv_rut(cuerpo: str) -> str:
    suma, factor = 0, 2
    for c in reversed(cuerpo):
        suma += int(c) * factor
        factor = 2 if factor == 7 else factor + 1
    resto = 11 - (suma % 11)
    return "0" if resto == 11 else "k" if resto == 10 else str(resto)


def rut_valido(n: int) -> str:
    """RUT sintético con dígito verificador correcto, formato 99.999.999-K."""
    cuerpo = f"{70_000_000 + n:08d}"
    return f"{cuerpo[0:2]}.{cuerpo[2:5]}.{cuerpo[5:8]}-{dv_rut(cuerpo)}"


def patente_valida(i: int) -> str:
    letras = "BCDFGHJKLPRSTVWXYZ"
    return f"{letras[i % 18]}{letras[(i // 18) % 18]}{letras[(i // 324) % 18]}{letras[(i // 5832) % 18]}{i % 100:02d}"


def _tablas_de_negocio(conn) -> list[str]:
    filas = conn.execute(sa.text(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE'"
    )).scalars().all()
    return sorted(t for t in filas if t not in CATALOGOS)


# ============================================================================
# 4. Fixtures de infraestructura
# ============================================================================
@pytest.fixture(scope="session")
def pg_server():
    """Servidor PostgreSQL embebido (arrancado al importar el conftest)."""
    return SERVIDOR


@pytest.fixture(scope="session")
def esquema(pg_server) -> str:
    """URI de la base con los scripts 01→06 ya aplicados (rol acredittia_app)."""
    return URI_APP


@pytest.fixture(scope="session")
def motor_admin(esquema):
    """Motor como superusuario: TRUNCATE y lecturas que la API no puede hacer."""
    motor = sa.create_engine(URI_SUPER_SA, future=True)
    yield motor
    motor.dispose()


@pytest.fixture(scope="session")
def _app_arrancada(esquema):
    """TestClient con el lifespan ejecutado una sola vez (siembra catálogos)."""
    from app.main import app
    with TestClient(app) as cliente:
        yield cliente


@pytest.fixture
def app_cliente(_app_arrancada, motor_admin):
    """Cliente de pruebas con la base de negocio vacía.

    Los catálogos globales (faenas, plantillas, ejemplos) sobreviven porque los
    siembra el arranque y son de solo lectura para la empresa. Todo lo demás se
    vacía con TRUNCATE: `actividad`, `bitacora_cambios` y
    `plataforma_credencial_versiones` rechazan UPDATE y DELETE por trigger, pero
    TRUNCATE no dispara triggers de fila y sí funciona.
    """
    with motor_admin.begin() as conn:
        tablas = _tablas_de_negocio(conn)
        conn.execute(sa.text(
            "TRUNCATE " + ", ".join(f'"{t}"' for t in tablas)
            + " RESTART IDENTITY CASCADE"))
    shutil.rmtree(STORAGE_DIR, ignore_errors=True)
    os.makedirs(STORAGE_DIR, exist_ok=True)
    from app.database import reset_ctx
    reset_ctx()
    yield _app_arrancada
    reset_ctx()


@pytest.fixture
def sesion_db():
    """Sesión SQLAlchemy con contexto de admin, para aserciones sobre la base."""
    with worker_session(is_admin=True) as db:
        yield db


# ============================================================================
# 5. Fixtures de datos
# ============================================================================
@pytest.fixture
def admin(app_cliente):
    """Administrador de la plataforma.

    No hay endpoint de registro de admin (a propósito): se inserta en la base
    con `worker_session(is_admin=True)`, porque RLS bloquearía el INSERT sin
    contexto de tenant, y desde ahí se autentica por la API como cualquier otro.
    """
    with worker_session(is_admin=True) as db:
        u = User(email="admin@acredittia.cl", nombre="Admin Plataforma",
                 password_hash=security.hash_password(CLAVE),
                 role="admin", company_id=None, status="approved")
        db.add(u)
        db.commit()
        uid = str(u.id)

    r = app_cliente.post(f"{API}/auth/login",
                         json={"email": "admin@acredittia.cl", "password": CLAVE})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return {"user_id": uid, "email": "admin@acredittia.cl", "token": token,
            "headers": {"Authorization": f"Bearer {token}"}}


def _crear_empresa(cliente, admin, *, nombre: str, rut: str, email: str) -> dict:
    """Registro por la API + aprobación por el admin + login. El flujo real."""
    r = cliente.post(f"{API}/auth/register", json={
        "empresa": nombre, "rut": rut, "email": email, "password": CLAVE})
    assert r.status_code == 201, r.text
    company_id = r.json()["company_id"]

    r = cliente.post(f"{API}/admin/companies/{company_id}/approve",
                     headers=admin["headers"])
    assert r.status_code == 200, r.text

    r = cliente.post(f"{API}/auth/login", json={"email": email, "password": CLAVE})
    assert r.status_code == 200, r.text
    datos = r.json()
    token = datos["access_token"]
    return {
        "company_id": company_id,
        "user_id": datos["user"]["id"],
        "email": email,
        "nombre": nombre,
        "rut": rut,
        "token": token,
        "refresh_token": datos["refresh_token"],
        "headers": {"Authorization": f"Bearer {token}"},
    }


@pytest.fixture
def empresa_a(app_cliente, admin) -> dict:
    return _crear_empresa(app_cliente, admin, nombre="Transportes Alfa SpA",
                          rut=rut_valido(1), email="alfa@empresa.cl")


@pytest.fixture
def empresa_b(app_cliente, admin) -> dict:
    return _crear_empresa(app_cliente, admin, nombre="Servicios Beta Ltda",
                          rut=rut_valido(2), email="beta@empresa.cl")


@pytest.fixture
def faenas(app_cliente, empresa_a) -> list[dict]:
    r = app_cliente.get(f"{API}/faenas?page_size=100", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    return r.json()["items"]


@pytest.fixture
def faena_pelambres(faenas) -> dict:
    """Los Pelambres: la única faena sembrada con cinco plataformas."""
    return next(f for f in faenas if f["nombre"] == "Los Pelambres")


def crear_contrato(cliente, empresa, faena_id, *, nombre="Contrato Mina",
                   codigo=None) -> dict:
    r = cliente.post(f"{API}/contratos", headers=empresa["headers"], json={
        "nombre": nombre, "codigo": codigo, "faena_id": faena_id,
        "fecha_inicio": date.today().isoformat(),
        "fecha_termino": (date.today() + timedelta(days=365)).isoformat(),
    })
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def contrato_a(app_cliente, empresa_a, faena_pelambres) -> dict:
    """Contrato de la empresa A en Los Pelambres, con su faena resuelta."""
    c = crear_contrato(app_cliente, empresa_a, faena_pelambres["id"])
    c["faena_id"] = faena_pelambres["id"]
    return c


@pytest.fixture
def contrato_b(app_cliente, empresa_b, faenas) -> dict:
    faena = next(f for f in faenas if f["nombre"] == "Los Pelambres")
    c = crear_contrato(app_cliente, empresa_b, faena["id"], nombre="Contrato Beta")
    c["faena_id"] = faena["id"]
    return c


# ---------------------------------------------------------------- ayudantes
def subir_archivo(cliente, empresa, doc_id: str, *, filename="acta.pdf",
                  contenido: bytes = b"%PDF-1.4 documento de prueba") -> dict:
    """Ejecuta el flujo SAS completo de tres pasos y devuelve la confirmación."""
    r = cliente.post(f"{API}/documentos/{doc_id}/upload-url",
                     headers=empresa["headers"],
                     json={"filename": filename, "content_type": "application/pdf",
                           "size_bytes": len(contenido)})
    assert r.status_code == 200, r.text
    sas = r.json()

    ruta = sas["upload_url"].split("http://test", 1)[-1]
    r = cliente.put(ruta, content=contenido, headers=sas["headers"])
    assert r.status_code == 201, r.text

    r = cliente.post(f"{API}/documentos/{doc_id}/archivos",
                     headers=empresa["headers"],
                     json={"blob_path": sas["blob_path"], "filename": filename})
    assert r.status_code == 201, r.text
    return r.json()


def primer_documento(cliente, empresa, **filtros) -> dict:
    q = "&".join(f"{k}={v}" for k, v in filtros.items())
    r = cliente.get(f"{API}/documentos?{q}", headers=empresa["headers"])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "no hay documentos que cumplan el filtro"
    return items[0]


def crear_contract_admin(cliente, empresa, contrato_id: str, *,
                         email="jefe.contrato@empresa.cl") -> dict:
    """Usuario acotado a un contrato, creado por la API de la empresa."""
    r = cliente.post(f"{API}/company/usuarios", headers=empresa["headers"], json={
        "email": email, "nombre": "Jefe de Contrato",
        "role": "contract_admin", "contrato_id": contrato_id,
    })
    assert r.status_code == 201, r.text
    uid = r.json()["id"]

    # La invitación no fija contraseña; se fija directamente para poder loguear.
    with worker_session(is_admin=True) as db:
        u = db.get(User, uuid.UUID(uid))
        u.password_hash = security.hash_password(CLAVE)
        u.status = "approved"
        u.activo = True
        db.commit()

    r = cliente.post(f"{API}/auth/login", json={"email": email, "password": CLAVE})
    assert r.status_code == 200, r.text
    token = r.json()["access_token"]
    return {"user_id": uid, "email": email, "token": token,
            "contrato_id": contrato_id,
            "headers": {"Authorization": f"Bearer {token}"}}
