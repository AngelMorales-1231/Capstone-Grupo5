"""Aislamiento multi-tenant: la empresa B no ve NADA de la empresa A.

La barrera es doble (§7 del modelo de datos): RLS en el motor y filtro explícito
`company_id` en cada consulta. La suite se conecta con `acredittia_app`
(`NOBYPASSRLS`), así que estas pruebas ejercitan las dos a la vez.

Para cada recurso se comprueban las dos mitades del contrato:

* **pedirlo por id devuelve 404** —y no 403— porque un 403 confirmaría que el
  recurso existe y eso ya es una fuga (§3.3 de la especificación);
* **no aparece en el listado** de la otra empresa.

Se parametriza sobre un catálogo de recursos para que añadir uno nuevo sea una
línea y no un test copiado.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable

import pytest

from app.database import worker_session
from app.models import Alerta

from tests.conftest import (API, crear_contract_admin, crear_contrato,
                            patente_valida, rut_valido)


# ============================================================================
# Catálogo de recursos
# ============================================================================
@dataclass
class Recurso:
    """Cómo crear un recurso, cómo pedirlo por id y dónde debería no aparecer."""
    nombre: str
    crear: Callable[..., str]
    # (método, ruta) para pedir el recurso; recibe el id del recurso y el id del
    # contrato que usa el solicitante (el suyo, no el ajeno).
    detalle: Callable[[str, str], tuple[str, str]]
    listado: Callable[[str], str]
    cuerpo: dict = field(default_factory=dict)


def _crear_personal(c, emp, contrato) -> str:
    r = c.post(f"{API}/personal", headers=emp["headers"], json={
        "contrato_id": contrato["id"], "nombre": "Juan Soto",
        "rut": rut_valido(101), "cargo": "Conductor", "es_conductor": True})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _crear_equipo(c, emp, contrato) -> str:
    r = c.post(f"{API}/equipos", headers=emp["headers"], json={
        "contrato_id": contrato["id"], "patente": patente_valida(7),
        "tipo_equipo": "Camión", "marca": "Volvo"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _crear_documento(c, emp, contrato) -> str:
    """Un documento del checklist de empresa, que cuelga del contrato."""
    r = c.get(f"{API}/contratos/{contrato['id']}/documentos",
              headers=emp["headers"])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "el alta del contrato debía instanciar el checklist de empresa"
    return items[0]["id"]


def _crear_alerta(c, emp, contrato) -> str:
    """Las alertas las escribe el motor de vencimientos y la revisión IA.

    Se insertan por la base con el tenant fijado porque no hay endpoint de alta
    (es correcto: una alerta es una consecuencia, no un recurso que se crea a
    mano); lo que se prueba aquí es que la lectura las aísla.
    """
    with worker_session(company_id=emp["company_id"]) as db:
        a = Alerta(company_id=uuid.UUID(emp["company_id"]), severidad="critica",
                   origen="vencimiento", titulo="Documento vencido",
                   descripcion="SOAP vencido",
                   contrato_id=uuid.UUID(contrato["id"]))
        db.add(a)
        db.commit()
        return str(a.id)


def _crear_plataforma(c, emp, contrato) -> str:
    r = c.post(f"{API}/contratos/{contrato['id']}/plataformas",
               headers=emp["headers"],
               json={"nombre": "PLATAFORMA PROPIA", "url": "https://x.cl"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _crear_credencial(c, emp, contrato) -> str:
    pid = _crear_plataforma(c, emp, contrato)
    r = c.post(f"{API}/contratos/{contrato['id']}/plataformas/{pid}/usuarios",
               headers=emp["headers"],
               json={"nombre": "Ana", "usuario": "ana", "password": "Sup3rSecreta!"})
    assert r.status_code == 201, r.text
    return f"{pid}/{r.json()['id']}"


def _crear_requisito(c, emp, contrato) -> str:
    r = c.post(f"{API}/contratos/{contrato['id']}/requisitos",
               headers=emp["headers"],
               json={"titulo": "Requisito propio", "ambito": "empresa",
                     "vinculo_tipo": "otro"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _crear_cargo(c, emp, contrato) -> str:
    r = c.post(f"{API}/cargos", headers=emp["headers"],
               json={"nombre": "Supervisor SSO", "categoria": "supervision"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _crear_evento(c, emp, contrato) -> str:
    r = c.post(f"{API}/calendario/eventos", headers=emp["headers"], json={
        "titulo": "Reunión de arranque", "categoria": "administrativo",
        "fecha": (date.today() + timedelta(days=20)).isoformat()})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _crear_integracion(c, emp, contrato) -> str:
    r = c.post(f"{API}/integraciones", headers=emp["headers"],
               json={"tipo": "siga", "config": {}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _crear_reporte(c, emp, contrato) -> str:
    r = c.post(f"{API}/reportes", headers=emp["headers"],
               json={"tipo": "estado_acreditacion", "formato": "excel"})
    assert r.status_code == 202, r.text
    return r.json()["id"]


RECURSOS = [
    Recurso("contratos", lambda c, e, k: k["id"],
            lambda rid, ctr: ("get", f"{API}/contratos/{rid}"),
            lambda ctr: f"{API}/contratos"),
    Recurso("personal", _crear_personal,
            lambda rid, ctr: ("get", f"{API}/personal/{rid}"),
            lambda ctr: f"{API}/personal"),
    Recurso("equipos", _crear_equipo,
            lambda rid, ctr: ("get", f"{API}/equipos/{rid}"),
            lambda ctr: f"{API}/equipos"),
    Recurso("documentos", _crear_documento,
            lambda rid, ctr: ("get", f"{API}/documentos/{rid}"),
            lambda ctr: f"{API}/documentos?page_size=100"),
    Recurso("alertas", _crear_alerta,
            lambda rid, ctr: ("patch", f"{API}/alertas/{rid}"),
            lambda ctr: f"{API}/alertas", cuerpo={"leida": True}),
    Recurso("plataformas_contrato", _crear_plataforma,
            lambda rid, ctr: ("patch", f"{API}/contratos/{ctr}/plataformas/{rid}"),
            lambda ctr: f"{API}/contratos/{ctr}/plataformas?page_size=100",
            cuerpo={"nota": "intento"}),
    Recurso("credenciales", _crear_credencial,
            lambda rid, ctr: ("get", f"{API}/contratos/{ctr}/plataformas/"
                                     f"{rid.split('/')[0]}/usuarios/"
                                     f"{rid.split('/')[1]}/usos"),
            lambda ctr: None),
    Recurso("requisitos_contrato", _crear_requisito,
            lambda rid, ctr: ("patch", f"{API}/contratos/{ctr}/requisitos/{rid}"),
            lambda ctr: f"{API}/contratos/{ctr}/requisitos?page_size=100",
            cuerpo={"titulo": "Robado"}),
    Recurso("cargos", _crear_cargo,
            lambda rid, ctr: ("get", f"{API}/cargos/{rid}/requisitos"),
            lambda ctr: f"{API}/cargos?page_size=100"),
    Recurso("calendario", _crear_evento,
            lambda rid, ctr: ("patch", f"{API}/calendario/eventos/{rid}"),
            lambda ctr: None, cuerpo={"completado": True}),
    Recurso("integraciones", _crear_integracion,
            lambda rid, ctr: ("get", f"{API}/integraciones/{rid}/logs"),
            lambda ctr: f"{API}/integraciones?page_size=100"),
    Recurso("reportes", _crear_reporte,
            lambda rid, ctr: ("get", f"{API}/reportes/{rid}"),
            lambda ctr: f"{API}/reportes?page_size=100"),
]

IDS = [r.nombre for r in RECURSOS]


def _ids_del_listado(cuerpo: dict) -> set[str]:
    return {str(i.get("id")) for i in cuerpo.get("items", [])}


# ============================================================================
# La prueba parametrizada
# ============================================================================
@pytest.mark.parametrize("recurso", RECURSOS, ids=IDS)
def test_empresa_b_no_alcanza_el_recurso_de_a(app_cliente, empresa_a, empresa_b,
                                              contrato_a, contrato_b, recurso):
    rid = recurso.crear(app_cliente, empresa_a, contrato_a)

    metodo, ruta = recurso.detalle(rid, contrato_b["id"])
    kw = {"json": recurso.cuerpo} if metodo in ("patch", "post", "put") else {}
    r = getattr(app_cliente, metodo)(ruta, headers=empresa_b["headers"], **kw)
    assert r.status_code == 404, (
        f"{recurso.nombre}: la empresa B obtuvo {r.status_code} en {ruta}; "
        f"debe ser 404 para no revelar que existe. Cuerpo: {r.text[:300]}")

    ruta_listado = recurso.listado(contrato_b["id"])
    if ruta_listado:
        r = app_cliente.get(ruta_listado, headers=empresa_b["headers"])
        assert r.status_code == 200, r.text
        assert rid.split("/")[-1] not in _ids_del_listado(r.json()), (
            f"{recurso.nombre}: el recurso de A aparece en el listado de B")


@pytest.mark.parametrize("recurso", RECURSOS, ids=IDS)
def test_la_empresa_a_si_alcanza_su_recurso(app_cliente, empresa_a, contrato_a,
                                            recurso):
    """Control negativo: sin esto un 404 universal haría pasar la prueba anterior."""
    rid = recurso.crear(app_cliente, empresa_a, contrato_a)
    metodo, ruta = recurso.detalle(rid, contrato_a["id"])
    kw = {"json": recurso.cuerpo} if metodo in ("patch", "post", "put") else {}
    r = getattr(app_cliente, metodo)(ruta, headers=empresa_a["headers"], **kw)
    assert r.status_code == 200, (recurso.nombre, r.status_code, r.text[:300])


# ============================================================================
# Aislamiento de los feeds agregados
# ============================================================================
def test_actividad_no_cruza_empresas(app_cliente, empresa_a, empresa_b,
                                     contrato_a, contrato_b):
    """La bitácora de negocio es por empresa: B no ve el alta del contrato de A."""
    _crear_personal(app_cliente, empresa_a, contrato_a)

    r = app_cliente.get(f"{API}/actividad?page_size=100",
                        headers=empresa_b["headers"])
    assert r.status_code == 200, r.text
    descripciones = " | ".join(i["descripcion"] for i in r.json()["items"])
    assert "Juan Soto" not in descripciones
    assert contrato_a["nombre"] not in descripciones

    r = app_cliente.get(f"{API}/actividad?page_size=100",
                        headers=empresa_a["headers"])
    assert any("Juan Soto" in i["descripcion"] for i in r.json()["items"])


def test_dashboard_no_agrega_datos_ajenos(app_cliente, empresa_a, empresa_b,
                                          contrato_a, contrato_b):
    _crear_personal(app_cliente, empresa_a, contrato_a)
    _crear_equipo(app_cliente, empresa_a, contrato_a)

    r = app_cliente.get(f"{API}/dashboard/kpis", headers=empresa_b["headers"])
    assert r.status_code == 200, r.text
    k = r.json()
    assert k["personal"]["total"] == 0
    assert k["equipos"]["total"] == 0
    assert k["contratos_activos"] == 1          # solo el suyo

    r = app_cliente.get(f"{API}/dashboard/kpis", headers=empresa_a["headers"])
    assert r.json()["personal"]["total"] == 1


def test_personas_y_flota_no_cruzan_empresas(app_cliente, empresa_a, empresa_b,
                                             contrato_a, contrato_b):
    """El agrupado por identidad es por empresa: el mismo RUT no se mezcla."""
    rut = rut_valido(300)
    patente = patente_valida(11)
    for emp, contrato in ((empresa_a, contrato_a), (empresa_b, contrato_b)):
        app_cliente.post(f"{API}/personal", headers=emp["headers"], json={
            "contrato_id": contrato["id"], "nombre": "Homónimo", "rut": rut})
        app_cliente.post(f"{API}/equipos", headers=emp["headers"], json={
            "contrato_id": contrato["id"], "patente": patente,
            "tipo_equipo": "Camión"})

    for emp in (empresa_a, empresa_b):
        r = app_cliente.get(f"{API}/personas/{rut}", headers=emp["headers"])
        assert r.status_code == 200, r.text
        assert r.json()["registros"] == 1, "se agregó el registro de la otra empresa"
        r = app_cliente.get(f"{API}/flota/{patente}", headers=emp["headers"])
        assert r.status_code == 200, r.text
        assert r.json()["registros"] == 1


def test_documento_de_a_no_se_descarga_desde_b(app_cliente, empresa_a, empresa_b,
                                               contrato_a, contrato_b):
    """Ni el archivo ni su URL firmada son alcanzables desde la otra empresa."""
    from tests.conftest import subir_archivo
    doc = _crear_documento(app_cliente, empresa_a, contrato_a)
    confirmacion = subir_archivo(app_cliente, empresa_a, doc)
    fid = confirmacion["archivo"]["id"]

    r = app_cliente.get(f"{API}/documentos/{doc}/archivos/{fid}/download-url",
                        headers=empresa_b["headers"])
    assert r.status_code == 404

    r = app_cliente.delete(f"{API}/documentos/{doc}/archivos/{fid}",
                           headers=empresa_b["headers"])
    assert r.status_code == 404


def test_no_se_puede_colgar_un_sujeto_de_un_contrato_ajeno(
        app_cliente, empresa_a, empresa_b, contrato_a):
    """La empresa B no puede crear personal dentro del contrato de A."""
    r = app_cliente.post(f"{API}/personal", headers=empresa_b["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Infiltrado",
        "rut": rut_valido(400)})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_ENCONTRADO"


def test_no_se_puede_invitar_un_contract_admin_a_un_contrato_ajeno(
        app_cliente, empresa_a, empresa_b, contrato_a):
    r = app_cliente.post(f"{API}/company/usuarios", headers=empresa_b["headers"],
                         json={"email": "cruzado@empresa.cl", "nombre": "X",
                               "role": "contract_admin",
                               "contrato_id": contrato_a["id"]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CONTRATO_INVALIDO"


def test_usuario_de_otra_empresa_da_404(app_cliente, empresa_a, empresa_b):
    r = app_cliente.patch(f"{API}/company/usuarios/{empresa_a['user_id']}",
                          headers=empresa_b["headers"], json={"activo": False})
    assert r.status_code == 404

    r = app_cliente.get(f"{API}/company/usuarios", headers=empresa_b["headers"])
    assert empresa_a["user_id"] not in _ids_del_listado(r.json())


# ============================================================================
# Alcance del contract_admin DENTRO de su propia empresa
# ============================================================================
def test_contract_admin_no_ve_otro_contrato_de_su_empresa(
        app_cliente, empresa_a, contrato_a, faena_pelambres):
    """El aislamiento no termina en la empresa: el contract_admin está acotado.

    Un 403 aquí sería una fuga menor pero real (confirmaría que el contrato 2
    existe), así que el contrato es 404, igual que entre empresas.
    """
    segundo = crear_contrato(app_cliente, empresa_a, faena_pelambres["id"],
                             nombre="Contrato Planta", codigo="C-2")
    jefe = crear_contract_admin(app_cliente, empresa_a, contrato_a["id"])

    assert app_cliente.get(f"{API}/contratos/{contrato_a['id']}",
                           headers=jefe["headers"]).status_code == 200

    r = app_cliente.get(f"{API}/contratos/{segundo['id']}",
                        headers=jefe["headers"])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NO_ENCONTRADO"

    # Y el listado se acota a su contrato, no a los de la empresa.
    r = app_cliente.get(f"{API}/contratos", headers=jefe["headers"])
    assert _ids_del_listado(r.json()) == {contrato_a["id"]}


def test_contract_admin_no_ve_sujetos_de_otro_contrato(
        app_cliente, empresa_a, contrato_a, faena_pelambres):
    segundo = crear_contrato(app_cliente, empresa_a, faena_pelambres["id"],
                             nombre="Contrato Planta")
    mio = _crear_personal(app_cliente, empresa_a, contrato_a)
    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": segundo["id"], "nombre": "Ajena", "rut": rut_valido(102)})
    ajeno = r.json()["id"]

    jefe = crear_contract_admin(app_cliente, empresa_a, contrato_a["id"])

    assert app_cliente.get(f"{API}/personal/{mio}",
                           headers=jefe["headers"]).status_code == 200
    assert app_cliente.get(f"{API}/personal/{ajeno}",
                           headers=jefe["headers"]).status_code == 404

    r = app_cliente.get(f"{API}/personal?page_size=100", headers=jefe["headers"])
    assert _ids_del_listado(r.json()) == {mio}

    r = app_cliente.get(f"{API}/documentos?page_size=100", headers=jefe["headers"])
    assert r.status_code == 200, r.text
    duenos = {i["dueno"]["id"] for i in r.json()["items"] if i["dueno"]}
    assert ajeno not in duenos


def test_contract_admin_no_ve_alertas_de_alcance_empresa(
        app_cliente, empresa_a, contrato_a):
    """Una alerta sin `contrato_id` es de la empresa: el contract_admin no la ve."""
    with worker_session(company_id=empresa_a["company_id"]) as db:
        de_empresa = Alerta(company_id=uuid.UUID(empresa_a["company_id"]),
                            severidad="alta", origen="sistema",
                            titulo="Suscripción por vencer")
        del_contrato = Alerta(company_id=uuid.UUID(empresa_a["company_id"]),
                              severidad="alta", origen="vencimiento",
                              titulo="SOAP vencido",
                              contrato_id=uuid.UUID(contrato_a["id"]))
        db.add_all([de_empresa, del_contrato])
        db.commit()
        id_empresa, id_contrato = str(de_empresa.id), str(del_contrato.id)

    jefe = crear_contract_admin(app_cliente, empresa_a, contrato_a["id"])
    r = app_cliente.get(f"{API}/alertas?page_size=100", headers=jefe["headers"])
    assert r.status_code == 200, r.text
    assert _ids_del_listado(r.json()) == {id_contrato}

    r = app_cliente.patch(f"{API}/alertas/{id_empresa}", headers=jefe["headers"],
                          json={"leida": True})
    assert r.status_code == 404


# ============================================================================
# La barrera del motor, sin pasar por los filtros de la aplicación
# ============================================================================
def test_rls_filtra_aunque_la_consulta_no_filtre(app_cliente, empresa_a,
                                                 empresa_b, contrato_a,
                                                 contrato_b):
    """Segunda barrera: una consulta SIN `where company_id` tampoco cruza.

    Se emula el bug que el modelo de datos anticipa —un endpoint nuevo que olvida
    el filtro explícito— consultando `contratos` sin condición con el contexto de
    tenant de cada empresa.
    """
    import sqlalchemy as sa

    from app.database import SessionLocal, set_ctx

    def contratos_visibles(company_id: str) -> set[str]:
        set_ctx(company_id=company_id, is_admin=False)
        with SessionLocal() as db:
            filas = db.execute(sa.text("SELECT id FROM contratos")).scalars().all()
        return {str(f) for f in filas}

    try:
        assert contratos_visibles(empresa_a["company_id"]) == {contrato_a["id"]}
        assert contratos_visibles(empresa_b["company_id"]) == {contrato_b["id"]}
    finally:
        from app.database import reset_ctx
        reset_ctx()


def test_rls_sin_contexto_no_devuelve_nada(app_cliente, empresa_a, contrato_a):
    """Sin tenant en la sesión, RLS devuelve vacío en vez de todo.

    Es la red de seguridad del middleware `limpiar_contexto`: si un endpoint
    consultara la base sin pasar por la autenticación, no vería datos de nadie.
    """
    import sqlalchemy as sa

    from app.database import SessionLocal, reset_ctx

    reset_ctx()
    with SessionLocal() as db:
        assert db.execute(sa.text("SELECT count(*) FROM contratos")).scalar() == 0
        assert db.execute(sa.text("SELECT count(*) FROM sujetos")).scalar() == 0
        assert db.execute(sa.text("SELECT count(*) FROM documentos")).scalar() == 0
