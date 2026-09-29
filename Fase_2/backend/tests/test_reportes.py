"""Reportes, programaciones y exportaciones (§15.3).

El punto de unión del módulo es `filas_de_recurso()`: una sola función construye
(cabeceras, filas) para cada vista exportable y la usan tanto la exportación
síncrona como la tarea del worker. Se prueba por eso que el mismo recurso salga
igual por las dos vías, además de la forma de cada endpoint y los errores
documentados.

Con `QUEUE_BACKEND=inproc` el job de `POST /reportes` corre en el acto, así que
el reporte ya está en `done` y descargable en la aserción siguiente.
"""
from __future__ import annotations

import uuid

import pytest

from tests.conftest import API, crear_contract_admin, patente_valida, rut_valido

ENVOLTURA = {"items", "page", "page_size", "total", "total_pages"}

TIPOS_PERSISTIBLES = ("estado_acreditacion", "cumplimiento_requisitos",
                      "personal_acreditado", "equipos_vehiculos", "vencimientos")
RECURSOS = ("personal", "equipos", "personas", "flota", "documentos",
            "requisitos", "alertas", "contratos")


@pytest.fixture
def dotacion(app_cliente, empresa_a, contrato_a) -> dict:
    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Juan Soto",
        "rut": rut_valido(131), "cargo": "Conductor", "es_conductor": True})
    assert r.status_code == 201, r.text
    trabajador = r.json()["id"]
    r = app_cliente.post(f"{API}/equipos", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "patente": patente_valida(51),
        "tipo_equipo": "Camión", "marca": "Volvo"})
    assert r.status_code == 201, r.text
    return {"trabajador": trabajador, "equipo": r.json()["id"]}


def _descargar(app_cliente, url: str) -> bytes:
    assert url.startswith("http://test"), url
    r = app_cliente.get(url[len("http://test"):])
    assert r.status_code == 200, r.text
    return r.content


# ============================================================================
# Generación de reportes
# ============================================================================
@pytest.mark.parametrize("tipo", TIPOS_PERSISTIBLES)
def test_generar_cada_tipo_de_reporte(app_cliente, empresa_a, contrato_a,
                                      dotacion, tipo):
    r = app_cliente.post(f"{API}/reportes", headers=empresa_a["headers"],
                         json={"tipo": tipo, "formato": "excel"})
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    assert r.json()["status"] == "queued", (
        "el contrato de la API es asíncrono, aunque inproc lo ejecute ya")

    r = app_cliente.get(f"{API}/reportes/{rid}", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    rep = r.json()
    assert rep["tipo"] == tipo
    assert rep["status"] == "done", rep
    assert rep["descargable"] is True
    assert rep["generado_por"]["email"] == empresa_a["email"]
    assert rep["nombre"]

    r = app_cliente.get(f"{API}/reportes/{rid}/download-url",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["filename"].endswith((".xlsx", ".csv"))
    assert _descargar(app_cliente, r.json()["download_url"])


def test_listado_de_reportes(app_cliente, empresa_a, contrato_a, dotacion):
    for tipo in ("vencimientos", "personal_acreditado"):
        app_cliente.post(f"{API}/reportes", headers=empresa_a["headers"],
                         json={"tipo": tipo, "formato": "excel"})

    r = app_cliente.get(f"{API}/reportes", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert set(r.json()) >= ENVOLTURA
    assert r.json()["total"] == 2

    r = app_cliente.get(f"{API}/reportes?tipo=vencimientos",
                        headers=empresa_a["headers"])
    assert r.json()["total"] == 1
    r = app_cliente.get(f"{API}/reportes?formato=pdf", headers=empresa_a["headers"])
    assert r.json()["total"] == 0


def test_la_matriz_no_se_persiste_como_reporte(app_cliente, empresa_a,
                                               contrato_a):
    """El enum `reporte_tipo` de la BD no tiene 'matriz_cumplimiento'.

    Se rechaza con un 400 explicativo que indica la alternativa, en lugar de
    dejar que el driver devuelva un 22P02 opaco.
    """
    r = app_cliente.post(f"{API}/reportes", headers=empresa_a["headers"],
                         json={"tipo": "matriz_cumplimiento", "formato": "excel"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TIPO_NO_SOPORTADO"
    assert "exportaciones" in r.json()["error"]["message"]


@pytest.mark.parametrize("cuerpo, codigo", [
    ({"tipo": "inventado", "formato": "excel"}, "TIPO_INVALIDO"),
    ({"tipo": "vencimientos", "formato": "docx"}, "FORMATO_INVALIDO"),
])
def test_generar_valida_tipo_y_formato(app_cliente, empresa_a, cuerpo, codigo):
    r = app_cliente.post(f"{API}/reportes", headers=empresa_a["headers"],
                         json=cuerpo)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == codigo


def test_download_url_de_un_reporte_inexistente(app_cliente, empresa_a):
    r = app_cliente.get(f"{API}/reportes/{uuid.uuid4()}/download-url",
                        headers=empresa_a["headers"])
    assert r.status_code == 404


def test_el_contract_admin_no_accede_a_reportes(app_cliente, empresa_a,
                                                contrato_a):
    """Un informe agregado de la empresa le mostraría datos de otros contratos."""
    jefe = crear_contract_admin(app_cliente, empresa_a, contrato_a["id"])
    for metodo, ruta, kw in (
        ("get", f"{API}/reportes", {}),
        ("post", f"{API}/reportes", {"json": {"tipo": "vencimientos",
                                              "formato": "excel"}}),
        ("get", f"{API}/reportes/programados", {}),
        ("post", f"{API}/exportaciones", {"json": {"recurso": "personal",
                                                   "formato": "csv"}}),
    ):
        r = getattr(app_cliente, metodo)(ruta, headers=jefe["headers"], **kw)
        assert r.status_code == 403, (metodo, ruta, r.status_code, r.text[:200])
        assert r.json()["error"]["code"] == "ROL_INSUFICIENTE"


# ============================================================================
# Programaciones
# ============================================================================
def test_ciclo_de_vida_de_un_programado(app_cliente, empresa_a):
    r = app_cliente.post(f"{API}/reportes/programados",
                         headers=empresa_a["headers"], json={
                             "nombre": "Vencimientos semanales",
                             "tipo": "vencimientos", "formato": "pdf",
                             "cron_expr": "0 7 * * 1"})
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["cron_expr"] == "0 7 * * 1"
    assert p["activo"] is True
    assert p["ultimo_run_at"] is None

    r = app_cliente.get(f"{API}/reportes/programados",
                        headers=empresa_a["headers"])
    assert set(r.json()) >= ENVOLTURA
    assert r.json()["total"] == 1

    r = app_cliente.patch(f"{API}/reportes/programados/{p['id']}",
                          headers=empresa_a["headers"],
                          json={"activo": False, "cron_expr": "30 6 1 * *"})
    assert r.status_code == 200, r.text
    assert r.json()["activo"] is False
    assert r.json()["cron_expr"] == "30 6 1 * *"

    r = app_cliente.get(f"{API}/reportes/programados?activo=false",
                        headers=empresa_a["headers"])
    assert r.json()["total"] == 1
    r = app_cliente.get(f"{API}/reportes/programados?activo=true",
                        headers=empresa_a["headers"])
    assert r.json()["total"] == 0

    r = app_cliente.patch(f"{API}/reportes/programados/{p['id']}",
                          headers=empresa_a["headers"], json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SIN_CAMBIOS"

    r = app_cliente.delete(f"{API}/reportes/programados/{p['id']}",
                           headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert app_cliente.get(f"{API}/reportes/programados",
                           headers=empresa_a["headers"]).json()["total"] == 0


@pytest.mark.parametrize("cron", ["0 7 * * 1", "*/15 * * * *", "0 6,18 * * *",
                                  "0 7 1-5 * *", "30 0 * * 0"])
def test_cron_valido(app_cliente, empresa_a, cron):
    r = app_cliente.post(f"{API}/reportes/programados",
                         headers=empresa_a["headers"], json={
                             "nombre": f"P {cron}", "tipo": "vencimientos",
                             "formato": "pdf", "cron_expr": cron})
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("cron", ["0 7 * *", "", "99 7 * * 1", "0 25 * * 1",
                                  "0 7 * * MON", "a b c d e", "0 7 32 * *"])
def test_cron_invalido(app_cliente, empresa_a, cron):
    r = app_cliente.post(f"{API}/reportes/programados",
                         headers=empresa_a["headers"], json={
                             "nombre": "P", "tipo": "vencimientos",
                             "formato": "pdf", "cron_expr": cron})
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "CRON_INVALIDO"


def test_programado_inexistente(app_cliente, empresa_a):
    r = app_cliente.patch(f"{API}/reportes/programados/{uuid.uuid4()}",
                          headers=empresa_a["headers"], json={"activo": False})
    assert r.status_code == 404


# ============================================================================
# Exportaciones
# ============================================================================
@pytest.mark.parametrize("recurso", RECURSOS)
def test_exportar_cada_recurso_en_csv(app_cliente, empresa_a, contrato_a,
                                      dotacion, recurso):
    r = app_cliente.post(f"{API}/exportaciones", headers=empresa_a["headers"],
                         json={"recurso": recurso, "formato": "csv"})
    assert r.status_code == 200, r.text
    salida = r.json()
    assert salida["formato"] == "csv"
    assert salida["filename"].endswith(".csv")
    assert salida["filas"] >= 0

    contenido = _descargar(app_cliente, salida["download_url"]).decode("utf-8-sig")
    cabecera = contenido.splitlines()[0]
    assert ";" in cabecera, "separador ';' para Excel en configuración chilena"
    assert len(contenido.splitlines()) == salida["filas"] + 1


def test_exportar_en_excel(app_cliente, empresa_a, contrato_a, dotacion):
    r = app_cliente.post(f"{API}/exportaciones", headers=empresa_a["headers"],
                         json={"recurso": "personal", "formato": "excel"})
    assert r.status_code == 200, r.text
    salida = r.json()
    if salida["degradado_a_csv"]:
        pytest.skip("openpyxl no instalado: la exportación degrada a CSV")
    assert salida["formato"] == "xlsx"
    datos = _descargar(app_cliente, salida["download_url"])
    assert datos[:2] == b"PK", "un XLSX es un ZIP"


def test_la_exportacion_de_matriz_exige_contrato(app_cliente, empresa_a,
                                                 contrato_a):
    r = app_cliente.post(f"{API}/exportaciones", headers=empresa_a["headers"],
                         json={"recurso": "matriz", "formato": "csv"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CONTRATO_REQUERIDO"


def test_la_exportacion_respeta_los_filtros(app_cliente, empresa_a, contrato_a,
                                            dotacion):
    r = app_cliente.post(f"{API}/exportaciones", headers=empresa_a["headers"],
                         json={"recurso": "documentos", "formato": "csv",
                               "filtros": {"sujeto_id": dotacion["equipo"]}})
    assert r.status_code == 200, r.text
    con_filtro = r.json()["filas"]

    r = app_cliente.post(f"{API}/exportaciones", headers=empresa_a["headers"],
                         json={"recurso": "documentos", "formato": "csv"})
    sin_filtro = r.json()["filas"]
    assert 0 < con_filtro < sin_filtro


@pytest.mark.parametrize("cuerpo, codigo", [
    ({"recurso": "inventado", "formato": "csv"}, "RECURSO_INVALIDO"),
    ({"recurso": "personal", "formato": "pdf"}, "FORMATO_INVALIDO"),
])
def test_exportar_valida_recurso_y_formato(app_cliente, empresa_a, cuerpo,
                                           codigo):
    r = app_cliente.post(f"{API}/exportaciones", headers=empresa_a["headers"],
                         json=cuerpo)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == codigo


def test_la_exportacion_y_el_reporte_producen_la_misma_tabla(
        app_cliente, empresa_a, contrato_a, dotacion):
    """`filas_de_recurso()` es la única fuente: si divergiera, el mismo listado
    saldría distinto según su tamaño."""
    r = app_cliente.post(f"{API}/exportaciones", headers=empresa_a["headers"],
                         json={"recurso": "personal", "formato": "csv"})
    assert r.status_code == 200, r.text
    csv_export = _descargar(app_cliente,
                            r.json()["download_url"]).decode("utf-8-sig")

    r = app_cliente.post(f"{API}/reportes", headers=empresa_a["headers"],
                         json={"tipo": "personal_acreditado", "formato": "excel",
                               "params": {"recurso": "personal",
                                          "formato_export": "csv"}})
    assert r.status_code == 202, r.text
    rid = r.json()["id"]
    r = app_cliente.get(f"{API}/reportes/{rid}/download-url",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    csv_job = _descargar(app_cliente,
                         r.json()["download_url"]).decode("utf-8-sig")

    assert csv_export.splitlines()[0] == csv_job.splitlines()[0]
    assert len(csv_export.splitlines()) == len(csv_job.splitlines())


def test_la_exportacion_deja_rastro_en_actividad(app_cliente, empresa_a,
                                                 contrato_a, dotacion):
    app_cliente.post(f"{API}/exportaciones", headers=empresa_a["headers"],
                     json={"recurso": "personal", "formato": "csv"})
    r = app_cliente.get(f"{API}/actividad?modulo=reportes&page_size=100",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert any("Exportación" in i["descripcion"] for i in r.json()["items"])


def test_la_url_de_exportacion_esta_firmada(app_cliente, empresa_a, contrato_a,
                                            dotacion):
    r = app_cliente.post(f"{API}/exportaciones", headers=empresa_a["headers"],
                         json={"recurso": "personal", "formato": "csv"})
    url = r.json()["download_url"][len("http://test"):]
    manipulada = url.replace("sig=", "sig=0")[:-1] + "f"
    r = app_cliente.get(manipulada)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FIRMA_INVALIDA"
