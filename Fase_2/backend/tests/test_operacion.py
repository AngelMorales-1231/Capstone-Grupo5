"""Operación diaria: alertas, dashboard, tendencia, calendario y actividad.

Son endpoints de lectura sobre datos que producen otros módulos, así que se
prueban dos cosas: la **forma** de la respuesta (la envoltura estándar y los
campos que el frontend consume) y los **casos límite** documentados, que son
donde se rompe el cliente: la tendencia sin histórico, el calendario con una
categoría reservada, la alerta de alcance empresa.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.database import worker_session
from app.models import Alerta, CumplimientoSnapshot

from tests.conftest import (API, crear_contract_admin, patente_valida,
                            rut_valido)

ENVOLTURA = {"items", "page", "page_size", "total", "total_pages"}


@pytest.fixture
def dotacion(app_cliente, empresa_a, contrato_a) -> dict:
    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Juan Soto",
        "rut": rut_valido(121), "cargo": "Conductor", "es_conductor": True})
    assert r.status_code == 201, r.text
    trabajador = r.json()["id"]
    r = app_cliente.post(f"{API}/equipos", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "patente": patente_valida(41),
        "tipo_equipo": "Camión", "marca": "Volvo"})
    assert r.status_code == 201, r.text
    return {"trabajador": trabajador, "equipo": r.json()["id"]}


def _alertas(company_id: str, contrato_id: str | None, *filas) -> list[str]:
    """Inserta alertas con el tenant fijado (no hay endpoint de alta)."""
    with worker_session(company_id=company_id) as db:
        objetos = [Alerta(company_id=uuid.UUID(company_id),
                          contrato_id=uuid.UUID(contrato_id) if contrato_id else None,
                          **f) for f in filas]
        db.add_all(objetos)
        db.commit()
        return [str(o.id) for o in objetos]


# ============================================================================
# Alertas
# ============================================================================
@pytest.fixture
def tres_alertas(app_cliente, empresa_a, contrato_a) -> list[str]:
    return _alertas(
        empresa_a["company_id"], contrato_a["id"],
        {"severidad": "critica", "origen": "vencimiento", "estado": "nueva",
         "titulo": "SOAP vencido", "descripcion": "El SOAP caducó ayer"},
        {"severidad": "advertencia", "origen": "ia", "estado": "nueva",
         "titulo": "Documento observado", "descripcion": "Firma no encontrada"},
        {"severidad": "informativa", "origen": "sistema", "estado": "informativa",
         "titulo": "Plataforma habilitada"},
    )


def test_listado_de_alertas(app_cliente, empresa_a, tres_alertas):
    r = app_cliente.get(f"{API}/alertas", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert set(cuerpo) == ENVOLTURA
    assert cuerpo["total"] == 3
    fila = cuerpo["items"][0]
    assert set(fila) >= {"id", "severidad", "estado", "origen", "titulo",
                         "descripcion", "leida", "resuelta", "leida_at",
                         "resuelta_at", "created_at"}
    assert fila["leida"] is False and fila["leida_at"] is None


@pytest.mark.parametrize("query, esperado", [
    ("severidad=critica", 1),
    ("origen=ia", 1),
    ("estado=informativa", 1),
    ("leida=false", 3),
    ("leida=true", 0),
    ("search=SOAP", 1),
])
def test_filtros_de_alertas(app_cliente, empresa_a, tres_alertas, query,
                            esperado):
    r = app_cliente.get(f"{API}/alertas?{query}", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["total"] == esperado


@pytest.mark.parametrize("query, codigo", [
    ("severidad=inventada", "SEVERIDAD_INVALIDA"),
    ("origen=inventado", "ORIGEN_INVALIDO"),
    ("estado=inventado", "ESTADO_INVALIDO"),
])
def test_filtros_invalidos_de_alertas(app_cliente, empresa_a, query, codigo):
    r = app_cliente.get(f"{API}/alertas?{query}", headers=empresa_a["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == codigo


def test_resumen_de_alertas(app_cliente, empresa_a, tres_alertas):
    r = app_cliente.get(f"{API}/alertas/resumen", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    resumen = r.json()
    assert set(resumen) == {"criticas", "advertencias", "informativas",
                            "resueltas_30d", "no_leidas", "activas"}
    assert resumen["criticas"] == 1
    assert resumen["advertencias"] == 1
    assert resumen["no_leidas"] == 3
    assert resumen["activas"] == 3
    assert resumen["resueltas_30d"] == 0


def test_patch_de_alerta_marca_y_resuelve(app_cliente, empresa_a, tres_alertas):
    aid = tres_alertas[0]
    r = app_cliente.patch(f"{API}/alertas/{aid}", headers=empresa_a["headers"],
                          json={"leida": True})
    assert r.status_code == 200, r.text
    assert r.json()["leida"] is True and r.json()["leida_at"] is not None

    r = app_cliente.patch(f"{API}/alertas/{aid}", headers=empresa_a["headers"],
                          json={"estado": "resuelta"})
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "resuelta"
    assert r.json()["resuelta_at"] is not None

    # Cualquier otro estado limpia la marca de resolución.
    r = app_cliente.patch(f"{API}/alertas/{aid}", headers=empresa_a["headers"],
                          json={"estado": "en_progreso"})
    assert r.status_code == 200, r.text
    assert r.json()["resuelta_at"] is None

    # Y por defecto el listado solo trae las activas.
    app_cliente.patch(f"{API}/alertas/{aid}", headers=empresa_a["headers"],
                      json={"resuelta": True})
    r = app_cliente.get(f"{API}/alertas", headers=empresa_a["headers"])
    assert r.json()["total"] == 2
    r = app_cliente.get(f"{API}/alertas?solo_activas=false",
                        headers=empresa_a["headers"])
    assert r.json()["total"] == 3


def test_patch_de_alerta_sin_cambios_y_estado_invalido(app_cliente, empresa_a,
                                                       tres_alertas):
    r = app_cliente.patch(f"{API}/alertas/{tres_alertas[0]}",
                          headers=empresa_a["headers"], json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SIN_CAMBIOS"

    r = app_cliente.patch(f"{API}/alertas/{tres_alertas[0]}",
                          headers=empresa_a["headers"],
                          json={"estado": "inventado"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ESTADO_INVALIDO"


def test_marcar_leidas_deja_rastro_en_actividad(app_cliente, empresa_a,
                                                tres_alertas):
    r = app_cliente.post(f"{API}/alertas/marcar-leidas",
                         headers=empresa_a["headers"], json={})
    assert r.status_code == 200, r.text
    assert r.json()["marcadas"] == 3

    r = app_cliente.get(f"{API}/alertas?leida=true", headers=empresa_a["headers"])
    assert r.json()["total"] == 3

    r = app_cliente.get(f"{API}/actividad?modulo=alertas&page_size=100",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert any("leída" in i["descripcion"] or "leídas" in i["descripcion"]
               for i in r.json()["items"]), r.json()["items"]


def test_marcar_leidas_solo_las_indicadas(app_cliente, empresa_a, tres_alertas):
    r = app_cliente.post(f"{API}/alertas/marcar-leidas",
                         headers=empresa_a["headers"],
                         json={"ids": [tres_alertas[0]]})
    assert r.status_code == 200, r.text
    assert r.json()["marcadas"] == 1
    r = app_cliente.get(f"{API}/alertas?leida=true", headers=empresa_a["headers"])
    assert [i["id"] for i in r.json()["items"]] == [tres_alertas[0]]


# ============================================================================
# Dashboard
# ============================================================================
def test_kpis(app_cliente, empresa_a, contrato_a, dotacion, tres_alertas):
    r = app_cliente.get(f"{API}/dashboard/kpis", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    k = r.json()
    assert set(k) == {"contratos_activos", "faenas_activas", "personal",
                      "equipos", "documentos", "cumplimiento_general_pct",
                      "alertas"}
    assert k["contratos_activos"] == 1
    assert k["faenas_activas"] == 1
    assert k["personal"] == {"total": 1, "acreditados": 0}
    assert k["equipos"] == {"total": 1, "acreditados": 0}
    assert k["documentos"]["total"] > 0 and k["documentos"]["ok"] == 0
    assert k["cumplimiento_general_pct"] == 0
    assert k["alertas"]["criticas"] == 1


def test_acreditaciones_estado(app_cliente, empresa_a, dotacion):
    r = app_cliente.get(f"{API}/dashboard/acreditaciones-estado",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) == {"acreditados", "pendientes", "vencidos", "total"}
    assert d["total"] == 2
    assert d["acreditados"] + d["pendientes"] + d["vencidos"] == d["total"]


@pytest.mark.parametrize("ruta", ["cumplimiento-contratos", "actividad",
                                  "proximos-vencimientos"])
def test_listados_del_dashboard_usan_la_envoltura(app_cliente, empresa_a,
                                                 contrato_a, dotacion, ruta):
    r = app_cliente.get(f"{API}/dashboard/{ruta}", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert set(r.json()) >= ENVOLTURA


def test_cumplimiento_contratos_lleva_faena_id(app_cliente, empresa_a,
                                               contrato_a, dotacion):
    r = app_cliente.get(f"{API}/dashboard/cumplimiento-contratos",
                        headers=empresa_a["headers"])
    fila = r.json()["items"][0]
    assert fila["id"] == contrato_a["id"]
    assert fila["faena_id"] == contrato_a["faena_id"]
    assert fila["faena"] == "Los Pelambres"
    assert fila["personal"] == {"total": 1, "acreditados": 0}


def test_actividad_del_dashboard_ignora_limit(app_cliente, empresa_a,
                                              contrato_a, dotacion):
    """§8 de RUPTURAS: `limit` desapareció; se pagina como todo lo demás."""
    r = app_cliente.get(f"{API}/dashboard/actividad?limit=1&page_size=2",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert len(r.json()["items"]) == 2
    fila = r.json()["items"][0]
    assert "entidad_tipo" in fila and "entidad_id" in fila


def test_proximos_vencimientos(app_cliente, empresa_a, dotacion):
    doc = app_cliente.get(f"{API}/documentos?sujeto_id={dotacion['equipo']}"
                          f"&page_size=1", headers=empresa_a["headers"]
                          ).json()["items"][0]
    vence = date.today() + timedelta(days=15)
    app_cliente.patch(f"{API}/documentos/{doc['id']}", headers=empresa_a["headers"],
                      json={"estado": "ok", "vence": vence.isoformat()})

    r = app_cliente.get(f"{API}/dashboard/proximos-vencimientos?dias=30",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["documento_id"] for i in items] == [doc["id"]]
    assert items[0]["sujeto_id"] == dotacion["equipo"]
    assert items[0]["dias"] == 15
    assert items[0]["estado_calc"] == "porvenc"

    r = app_cliente.get(f"{API}/dashboard/proximos-vencimientos?dias=7",
                        headers=empresa_a["headers"])
    assert r.json()["total"] == 0


# ============================================================================
# Tendencia
# ============================================================================
def _snapshots(company_id: str, contrato_id: str | None, *puntos) -> None:
    """Escribe la serie histórica. La tabla es de solo INSERT (trigger)."""
    with worker_session(company_id=company_id) as db:
        for fecha, pct, ok, total in puntos:
            db.add(CumplimientoSnapshot(
                company_id=uuid.UUID(company_id),
                contrato_id=uuid.UUID(contrato_id) if contrato_id else None,
                fecha=fecha, cumplimiento_pct=pct, docs_ok=ok, docs_total=total,
                personal_acreditados=1, personal_total=2,
                equipos_acreditados=1, equipos_total=1, alertas_criticas=0))
        db.commit()


def test_tendencia_sin_snapshots_omite_actual_y_anterior(app_cliente, empresa_a,
                                                        contrato_a):
    """Contrato explícito de §8: el frontend comprueba la presencia de claves."""
    r = app_cliente.get(f"{API}/dashboard/tendencia", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["serie"] == []
    assert t["snapshots_leidos"] == 0
    assert "actual" not in t
    assert "anterior" not in t
    assert "delta_pct" not in t
    assert "nota" in t


def test_tendencia_con_un_solo_periodo_omite_anterior(app_cliente, empresa_a,
                                                     contrato_a):
    hoy = date.today()
    _snapshots(empresa_a["company_id"], None,
               (hoy - timedelta(days=2), 60, 60, 100),
               (hoy, 70, 70, 100))

    r = app_cliente.get(f"{API}/dashboard/tendencia?periodo=trimestre",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    t = r.json()
    assert len(t["serie"]) == 1, "los dos días caen en el mismo trimestre"
    assert t["snapshots_leidos"] == 2
    assert t["actual"]["cumplimiento_pct"] == 70, "se toma el ÚLTIMO, no la media"
    assert "anterior" not in t
    assert "delta_pct" not in t
    assert "nota" in t


def test_tendencia_con_dos_periodos_calcula_el_delta(app_cliente, empresa_a,
                                                     contrato_a):
    hoy = date.today()
    mes_pasado = (hoy.replace(day=1) - timedelta(days=1))
    _snapshots(empresa_a["company_id"], None,
               (mes_pasado - timedelta(days=5), 50, 50, 100),
               (mes_pasado, 76, 76, 100),
               (hoy.replace(day=1), 80, 80, 100),
               (hoy, 82, 82, 100))

    r = app_cliente.get(f"{API}/dashboard/tendencia?periodo=mes",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    t = r.json()
    assert len(t["serie"]) == 2
    assert t["actual"]["cumplimiento_pct"] == 82
    assert t["anterior"]["cumplimiento_pct"] == 76
    assert t["delta_pct"] == 6
    assert "nota" not in t
    fila = t["serie"][0]
    assert set(fila) == {"periodo_inicio", "fecha", "cumplimiento_pct", "docs_ok",
                         "docs_total", "personal_acreditados", "personal_total",
                         "equipos_acreditados", "equipos_total",
                         "alertas_criticas"}


def test_tendencia_por_faena_agrega_por_documentos(app_cliente, empresa_a,
                                                   contrato_a, faena_pelambres):
    """Se recalcula `docs_ok/docs_total`, no se promedian porcentajes."""
    from tests.conftest import crear_contrato
    segundo = crear_contrato(app_cliente, empresa_a, faena_pelambres["id"],
                             nombre="Contrato Planta")
    hoy = date.today()
    _snapshots(empresa_a["company_id"], contrato_a["id"], (hoy, 100, 3, 3))
    _snapshots(empresa_a["company_id"], segundo["id"], (hoy, 0, 0, 297))

    r = app_cliente.get(f"{API}/dashboard/tendencia"
                        f"?faena_id={faena_pelambres['id']}",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    t = r.json()
    assert t["actual"]["docs_total"] == 300
    assert t["actual"]["docs_ok"] == 3
    assert t["actual"]["cumplimiento_pct"] == 1, (
        "la media de porcentajes daría 50; la ponderada da 1")


@pytest.mark.parametrize("query, codigo", [
    ("periodo=decada", "PERIODO_INVALIDO"),
    ("desde=2026-06-01&hasta=2026-01-01", "RANGO_INVALIDO"),
])
def test_tendencia_valida_los_parametros(app_cliente, empresa_a, query, codigo):
    r = app_cliente.get(f"{API}/dashboard/tendencia?{query}",
                        headers=empresa_a["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == codigo


def test_tendencia_rechaza_contrato_y_faena_a_la_vez(app_cliente, empresa_a,
                                                     contrato_a,
                                                     faena_pelambres):
    r = app_cliente.get(f"{API}/dashboard/tendencia"
                        f"?contrato_id={contrato_a['id']}"
                        f"&faena_id={faena_pelambres['id']}",
                        headers=empresa_a["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "FILTRO_AMBIGUO"


def test_los_snapshots_son_inmutables(app_cliente, empresa_a, contrato_a,
                                      motor_admin):
    import sqlalchemy as sa
    _snapshots(empresa_a["company_id"], None, (date.today(), 70, 70, 100))
    with motor_admin.connect() as conn:
        with pytest.raises(sa.exc.DBAPIError) as exc:
            conn.execute(sa.text(
                "UPDATE cumplimiento_snapshots SET cumplimiento_pct = 99"))
        assert exc.value.orig.sqlstate == "55000"
        conn.rollback()


def test_un_snapshot_por_empresa_contrato_y_fecha(app_cliente, empresa_a,
                                                  contrato_a):
    """`ux_snapshot` es UNIQUE NULLS NOT DISTINCT: la fila agregada también."""
    import sqlalchemy.exc
    hoy = date.today()
    _snapshots(empresa_a["company_id"], None, (hoy, 70, 70, 100))
    with pytest.raises(sqlalchemy.exc.IntegrityError):
        _snapshots(empresa_a["company_id"], None, (hoy, 80, 80, 100))


# ============================================================================
# Calendario
# ============================================================================
def test_calendario_proyecta_los_vencimientos(app_cliente, empresa_a, dotacion):
    doc = app_cliente.get(f"{API}/documentos?sujeto_id={dotacion['equipo']}"
                          f"&page_size=1", headers=empresa_a["headers"]
                          ).json()["items"][0]
    vence = date.today() + timedelta(days=12)
    app_cliente.patch(f"{API}/documentos/{doc['id']}", headers=empresa_a["headers"],
                      json={"estado": "ok", "vence": vence.isoformat()})

    desde = date.today().isoformat()
    hasta = (date.today() + timedelta(days=60)).isoformat()
    r = app_cliente.get(f"{API}/calendario/eventos?desde={desde}&hasta={hasta}",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    cal = r.json()
    assert set(cal) == {"items", "total", "por_categoria", "desde", "hasta",
                        "truncado"}
    assert cal["por_categoria"]["vencimiento"] == 1
    proyectado = next(i for i in cal["items"] if i["categoria"] == "vencimiento")
    assert proyectado["documento_id"] == doc["id"]
    assert proyectado["editable"] is False, (
        "un vencimiento se cambia en el documento, no en el calendario")


def test_crear_editar_y_borrar_un_evento_manual(app_cliente, empresa_a):
    fecha = (date.today() + timedelta(days=20)).isoformat()
    r = app_cliente.post(f"{API}/calendario/eventos", headers=empresa_a["headers"],
                         json={"titulo": "Reunión de arranque",
                               "categoria": "administrativo", "fecha": fecha,
                               "descripcion": "Con el mandante"})
    assert r.status_code == 201, r.text
    ev = r.json()
    assert ev["categoria"] == "administrativo"
    assert ev["editable"] is True
    assert ev["completado"] is False
    assert ev["dias"] == 20

    r = app_cliente.patch(f"{API}/calendario/eventos/{ev['id']}",
                          headers=empresa_a["headers"], json={"completado": True})
    assert r.status_code == 200, r.text
    assert r.json()["completado"] is True

    r = app_cliente.patch(f"{API}/calendario/eventos/{ev['id']}",
                          headers=empresa_a["headers"], json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SIN_CAMBIOS"

    r = app_cliente.delete(f"{API}/calendario/eventos/{ev['id']}",
                           headers=empresa_a["headers"])
    assert r.status_code == 200 and r.json()["ok"] is True


def test_la_categoria_vencimiento_esta_reservada(app_cliente, empresa_a):
    """Un evento manual con esa categoría duplicaría la proyección."""
    r = app_cliente.post(f"{API}/calendario/eventos", headers=empresa_a["headers"],
                         json={"titulo": "Vence algo", "categoria": "vencimiento",
                               "fecha": date.today().isoformat()})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "CATEGORIA_RESERVADA"


def test_categoria_inexistente(app_cliente, empresa_a):
    r = app_cliente.post(f"{API}/calendario/eventos", headers=empresa_a["headers"],
                         json={"titulo": "X", "categoria": "inventada",
                               "fecha": date.today().isoformat()})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CATEGORIA_INVALIDA"


def test_titulo_del_evento_obligatorio(app_cliente, empresa_a):
    r = app_cliente.post(f"{API}/calendario/eventos", headers=empresa_a["headers"],
                         json={"titulo": "   ", "fecha": date.today().isoformat()})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TITULO_REQUERIDO"


def test_el_calendario_exige_un_rango_razonable(app_cliente, empresa_a):
    hoy = date.today()
    r = app_cliente.get(f"{API}/calendario/eventos?desde={hoy}"
                        f"&hasta={hoy - timedelta(days=1)}",
                        headers=empresa_a["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "RANGO_INVALIDO"

    r = app_cliente.get(f"{API}/calendario/eventos?desde={hoy}"
                        f"&hasta={hoy + timedelta(days=3000)}",
                        headers=empresa_a["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "RANGO_DEMASIADO_AMPLIO"

    r = app_cliente.get(f"{API}/calendario/eventos", headers=empresa_a["headers"])
    assert r.status_code == 422, "desde y hasta son obligatorios"


# ============================================================================
# Actividad
# ============================================================================
def test_actividad_registra_las_mutaciones(app_cliente, empresa_a, contrato_a,
                                           dotacion):
    r = app_cliente.get(f"{API}/actividad?page_size=100",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert set(r.json()) >= ENVOLTURA
    items = r.json()["items"]
    assert items
    fila = items[0]
    assert set(fila) >= {"id", "tipo", "modulo", "descripcion", "entidad_tipo",
                         "entidad_id", "created_at", "usuario"}
    assert fila["usuario"]["email"] == empresa_a["email"]
    modulos = {i["modulo"] for i in items}
    assert {"contrato", "personal", "equipos"} <= modulos


def test_filtros_de_actividad(app_cliente, empresa_a, contrato_a, dotacion):
    h = empresa_a["headers"]
    r = app_cliente.get(f"{API}/actividad?modulo=personal&page_size=100",
                        headers=h)
    assert r.status_code == 200, r.text
    assert {i["modulo"] for i in r.json()["items"]} == {"personal"}

    r = app_cliente.get(f"{API}/actividad?tipo=creacion&page_size=100", headers=h)
    assert {i["tipo"] for i in r.json()["items"]} == {"creacion"}

    hoy = date.today().isoformat()
    r = app_cliente.get(f"{API}/actividad?desde={hoy}&hasta={hoy}&page_size=100",
                        headers=h)
    assert r.json()["total"] > 0

    ayer = (date.today() - timedelta(days=1)).isoformat()
    r = app_cliente.get(f"{API}/actividad?desde={ayer}&hasta={ayer}", headers=h)
    assert r.json()["total"] == 0


def test_actividad_por_contrato(app_cliente, empresa_a, contrato_a, dotacion):
    r = app_cliente.get(f"{API}/actividad?contrato_id={contrato_a['id']}"
                        f"&page_size=100", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["total"] > 0


def test_la_actividad_es_inmutable(motor_admin, app_cliente, empresa_a,
                                   contrato_a):
    import sqlalchemy as sa
    with motor_admin.connect() as conn:
        with pytest.raises(sa.exc.DBAPIError) as exc:
            conn.execute(sa.text("UPDATE actividad SET descripcion = 'falsificada'"))
        assert exc.value.orig.sqlstate == "55000"
        conn.rollback()
        with pytest.raises(sa.exc.DBAPIError) as exc:
            conn.execute(sa.text("DELETE FROM actividad"))
        assert exc.value.orig.sqlstate == "55000"
        conn.rollback()


# ============================================================================
# Requisitos (vista de cumplimiento por plantilla)
# ============================================================================
def test_vista_de_requisitos(app_cliente, empresa_a, contrato_a, dotacion):
    r = app_cliente.get(f"{API}/requisitos?page_size=100",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert set(cuerpo) >= ENVOLTURA | {"kpis"}
    fila = cuerpo["items"][0]
    assert set(fila) >= {"template_id", "titulo", "ambito", "docs", "ok",
                        "porvenc", "venc", "falta", "estado"}
    assert fila["docs"] == fila["ok"] + fila["porvenc"] + fila["venc"] + fila["falta"]

    r = app_cliente.get(f"{API}/requisitos?ambito=equipo&page_size=100",
                        headers=empresa_a["headers"])
    assert {i["ambito"] for i in r.json()["items"]} == {"equipo"}


def test_catalogo_de_plantillas(app_cliente, empresa_a):
    r = app_cliente.get(f"{API}/requisitos/templates?page_size=100",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items
    assert all(i["activo"] for i in items), (
        "GET /requisitos/templates no devuelve las inactivas")
    assert {i["ambito"] for i in items} == {"empresa", "personal", "equipo",
                                            "emsipor"}


# ============================================================================
# Notificaciones
# ============================================================================
def test_preferencias_devuelven_los_defectos(app_cliente, empresa_a):
    r = app_cliente.get(f"{API}/notificaciones/preferencias",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert len(items) == 7, "el catálogo de eventos notificables"
    assert all(i["origen"] == "defecto" for i in items)
    assert all(i["canal_email"] is True and i["canal_whatsapp"] is False
               for i in items)
    assert all(i["descripcion"] for i in items)


def test_editar_preferencias(app_cliente, empresa_a):
    r = app_cliente.patch(f"{API}/notificaciones/preferencias",
                          headers=empresa_a["headers"], json={"preferencias": [
                              {"evento": "vencimiento_7", "canal_email": False,
                               "canal_whatsapp": True}]})
    assert r.status_code == 200, r.text
    fila = next(i for i in r.json()["items"] if i["evento"] == "vencimiento_7")
    assert fila["canal_email"] is False
    assert fila["canal_whatsapp"] is True
    assert fila["origen"] != "defecto"

    r = app_cliente.patch(f"{API}/notificaciones/preferencias",
                          headers=empresa_a["headers"], json={"preferencias": [
                              {"evento": "inventado", "canal_email": True}]})
    assert r.status_code == 400
    assert r.json()["error"]["code"] in ("EVENTO_INVALIDO", "EVENTO_DESCONOCIDO")


# ============================================================================
# El contract_admin en la operación
# ============================================================================
def test_el_contract_admin_ve_solo_su_operacion(app_cliente, empresa_a,
                                                contrato_a, faena_pelambres,
                                                dotacion):
    from tests.conftest import crear_contrato
    segundo = crear_contrato(app_cliente, empresa_a, faena_pelambres["id"],
                             nombre="Contrato Planta")
    app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": segundo["id"], "nombre": "Ajena", "rut": rut_valido(122)})

    jefe = crear_contract_admin(app_cliente, empresa_a, contrato_a["id"])

    r = app_cliente.get(f"{API}/dashboard/kpis", headers=jefe["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["personal"]["total"] == 1
    assert r.json()["contratos_activos"] == 1

    r = app_cliente.get(f"{API}/dashboard/cumplimiento-contratos",
                        headers=jefe["headers"])
    assert [i["id"] for i in r.json()["items"]] == [contrato_a["id"]]

    r = app_cliente.get(f"{API}/dashboard/tendencia?contrato_id={segundo['id']}",
                        headers=jefe["headers"])
    assert r.status_code == 404, "pedir otro contrato no revela que existe"

    r = app_cliente.get(f"{API}/dashboard/tendencia", headers=jefe["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["contrato_id"] == contrato_a["id"], (
        "sin contrato explícito se le fuerza el suyo, no la serie de la empresa")
