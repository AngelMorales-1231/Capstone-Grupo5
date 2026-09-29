"""Documentos: subida y descarga por SAS, revisión IA y listado transversal (§10).

La ruptura mayor de la v1.1 es que **el archivo no pasa por la API**: se emite
una URL firmada, el cliente sube el binario contra el storage y después confirma
la ruta del blob. Aquí se ejercita el flujo completo de tres pasos contra el
backend `local`, cuyas URLs firmadas apuntan a `/blobs` y tienen el mismo
contrato que una SAS de Azure.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import API, patente_valida, rut_valido, subir_archivo

CONTENIDO = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


# ============================================================================
# Utilidades
# ============================================================================
def _doc_de_contrato(cliente, empresa, contrato) -> dict:
    r = cliente.get(f"{API}/contratos/{contrato['id']}/documentos",
                    headers=empresa["headers"])
    assert r.status_code == 200, r.text
    return r.json()["items"][0]


def _ruta_local(url: str) -> str:
    """Convierte la URL firmada absoluta en la ruta que consume el TestClient."""
    assert url.startswith("http://test"), url
    return url[len("http://test"):]


@pytest.fixture
def documento(app_cliente, empresa_a, contrato_a) -> dict:
    return _doc_de_contrato(app_cliente, empresa_a, contrato_a)


# ============================================================================
# 1. Flujo SAS de tres pasos
# ============================================================================
def test_flujo_sas_completo(app_cliente, empresa_a, documento):
    doc_id = documento["id"]

    # (1) La API emite la URL de escritura; el blob_path lo decide el servidor.
    r = app_cliente.post(f"{API}/documentos/{doc_id}/upload-url",
                         headers=empresa_a["headers"],
                         json={"filename": "certificado.pdf",
                               "content_type": "application/pdf",
                               "size_bytes": len(CONTENIDO)})
    assert r.status_code == 200, r.text
    sas = r.json()
    assert sas["method"] == "PUT"
    assert sas["confirmar_en"] == f"/api/v1/documentos/{doc_id}/archivos"
    assert sas["blob_path"].startswith(f"{empresa_a['company_id']}/")
    assert doc_id in sas["blob_path"], "la ruta identifica el documento"
    assert sas["headers"]["Content-Type"] == "application/pdf"
    assert sas["max_bytes"] > 0

    # (2) El binario va al storage, no a la API.
    r = app_cliente.put(_ruta_local(sas["upload_url"]), content=CONTENIDO,
                        headers=sas["headers"])
    assert r.status_code == 201, r.text
    assert r.headers["x-blob-path"] == sas["blob_path"]
    assert r.headers["x-blob-size"] == str(len(CONTENIDO))

    # (3) Confirmación: la API relee el tamaño del storage, no lo cree al cliente.
    r = app_cliente.post(f"{API}/documentos/{doc_id}/archivos",
                         headers=empresa_a["headers"],
                         json={"blob_path": sas["blob_path"],
                               "filename": "certificado.pdf"})
    assert r.status_code == 201, r.text
    salida = r.json()
    assert salida["archivo"]["size_bytes"] == len(CONTENIDO)
    assert salida["archivo"]["content_type"] == "application/pdf"
    assert salida["ia_review"]["job_id"]
    assert salida["ia_review"]["context"] == "empresa"
    assert salida["documento"]["id"] == doc_id


def test_confirmar_dos_veces_el_mismo_blob(app_cliente, empresa_a, documento):
    salida = subir_archivo(app_cliente, empresa_a, documento["id"])
    r = app_cliente.post(f"{API}/documentos/{documento['id']}/archivos",
                         headers=empresa_a["headers"],
                         json={"blob_path": salida["archivo"]["blob_path"],
                               "filename": "acta.pdf"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "BLOB_YA_REGISTRADO"


def test_confirmar_un_blob_que_no_existe(app_cliente, empresa_a, documento):
    """El `PUT` puede fallar sin que el cliente se entere: la API lo comprueba."""
    r = app_cliente.post(f"{API}/documentos/{documento['id']}/upload-url",
                         headers=empresa_a["headers"],
                         json={"filename": "fantasma.pdf"})
    blob = r.json()["blob_path"]

    r = app_cliente.post(f"{API}/documentos/{documento['id']}/archivos",
                         headers=empresa_a["headers"],
                         json={"blob_path": blob, "filename": "fantasma.pdf"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BLOB_NO_ENCONTRADO"


def test_confirmar_un_blob_de_otra_empresa(app_cliente, empresa_a, empresa_b,
                                           contrato_a, contrato_b, documento):
    """Confusión de rutas: adjuntar el blob de otro tenant a un documento propio.

    El blob EXISTE en el storage, así que la comprobación de existencia no basta:
    lo que lo rechaza es el prefijo `company_id` de la ruta.
    """
    doc_b = _doc_de_contrato(app_cliente, empresa_b, contrato_b)
    ajeno = subir_archivo(app_cliente, empresa_b, doc_b["id"])
    blob_ajeno = ajeno["archivo"]["blob_path"]
    assert blob_ajeno.startswith(f"{empresa_b['company_id']}/")

    r = app_cliente.post(f"{API}/documentos/{documento['id']}/archivos",
                         headers=empresa_a["headers"],
                         json={"blob_path": blob_ajeno, "filename": "acta.pdf"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BLOB_NO_PERMITIDO"


def test_confirmar_sin_blob_path(app_cliente, empresa_a, documento):
    r = app_cliente.post(f"{API}/documentos/{documento['id']}/archivos",
                         headers=empresa_a["headers"],
                         json={"blob_path": "   ", "filename": "acta.pdf"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "BLOB_PATH_REQUERIDO"


@pytest.mark.parametrize("filename", ["acta.exe", "acta.zip", "acta"])
def test_extension_no_permitida(app_cliente, empresa_a, documento, filename):
    r = app_cliente.post(f"{API}/documentos/{documento['id']}/upload-url",
                         headers=empresa_a["headers"], json={"filename": filename})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "EXTENSION_NO_PERMITIDA"


def test_upload_url_valida_el_tamano_declarado(app_cliente, empresa_a, documento):
    r = app_cliente.post(f"{API}/documentos/{documento['id']}/upload-url",
                         headers=empresa_a["headers"],
                         json={"filename": "a.pdf", "size_bytes": 0})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ARCHIVO_VACIO"

    r = app_cliente.post(f"{API}/documentos/{documento['id']}/upload-url",
                         headers=empresa_a["headers"],
                         json={"filename": "a.pdf", "size_bytes": 99 * 1024 * 1024})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ARCHIVO_MUY_GRANDE"


def test_el_put_rechaza_un_cuerpo_vacio(app_cliente, empresa_a, documento):
    r = app_cliente.post(f"{API}/documentos/{documento['id']}/upload-url",
                         headers=empresa_a["headers"], json={"filename": "a.pdf"})
    sas = r.json()
    r = app_cliente.put(_ruta_local(sas["upload_url"]), content=b"",
                        headers=sas["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ARCHIVO_VACIO"


# ============================================================================
# 2. Revisión IA (síncrona con QUEUE_BACKEND=inproc)
# ============================================================================
def test_la_revisión_ia_se_aplica_en_el_acto(app_cliente, empresa_a, documento):
    """Con la cola en proceso el job ya terminó cuando responde la confirmación.

    El contrato de la API sigue siendo asíncrono (`status: queued` en la
    respuesta), pero el veredicto ya está disponible en el polling siguiente, y
    eso permite comprobar la decisión sin esperas ni sondeos.
    """
    salida = subir_archivo(app_cliente, empresa_a, documento["id"],
                           filename="certificado.pdf", contenido=CONTENIDO)
    job = salida["ia_review"]["job_id"]

    r = app_cliente.get(f"{API}/ia/revisiones/{job}", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    review = r.json()
    assert review["status"] == "done", review
    assert review["resultado"] in ("validado", "con_observaciones", "con_errores")
    assert 0.8 <= review["confianza"] <= 1.0
    assert review["hallazgos"], "la simulada emite al menos un hallazgo informativo"
    assert review["accion_aplicada"], review

    r = app_cliente.get(f"{API}/documentos/{documento['id']}",
                        headers=empresa_a["headers"])
    doc = r.json()
    hubo_error = any(h["tipo"] == "error" for h in review["hallazgos"])
    if hubo_error:
        assert doc["estado"] == "falta", (
            "con un hallazgo de tipo error el documento NO se aprueba")
        assert doc["estado_calc"] == "falta"
    else:
        assert doc["estado"] == "ok"
        assert doc["estado_calc"] in ("ok", "porvenc", "venc")
    assert doc["archivos_count"] == 1


def test_una_revision_con_errores_genera_alerta(app_cliente, empresa_a,
                                                contrato_a):
    """Se busca un archivo cuyo veredicto simulado tenga errores.

    La simulada es determinista por (contenido, nombre), así que probando
    variantes se llega a los tres veredictos sin depender del azar.
    """
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/documentos"
                        f"?page_size=100", headers=empresa_a["headers"])
    docs = r.json()["items"]

    for i, doc in enumerate(docs):
        salida = subir_archivo(app_cliente, empresa_a, doc["id"],
                               filename=f"doc-{i}.pdf",
                               contenido=CONTENIDO + bytes([i]))
        job = salida["ia_review"]["job_id"]
        review = app_cliente.get(f"{API}/ia/revisiones/{job}",
                                 headers=empresa_a["headers"]).json()
        if review["resultado"] == "con_errores":
            break
    else:
        pytest.skip("ninguno de los documentos del contrato dio 'con_errores'")

    r = app_cliente.get(f"{API}/alertas?origen=ia", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    alertas = r.json()["items"]
    assert alertas, "un veredicto con errores debe dejar alerta"
    assert alertas[0]["origen"] == "ia"
    assert alertas[0]["documento_id"] == doc["id"]


def test_revision_de_otra_empresa_no_es_consultable(app_cliente, empresa_a,
                                                    empresa_b, documento):
    salida = subir_archivo(app_cliente, empresa_a, documento["id"])
    job = salida["ia_review"]["job_id"]
    r = app_cliente.get(f"{API}/ia/revisiones/{job}", headers=empresa_b["headers"])
    assert r.status_code == 404


# ============================================================================
# 3. Descarga por URL firmada
# ============================================================================
def test_download_url_firmada_funciona(app_cliente, empresa_a, documento):
    salida = subir_archivo(app_cliente, empresa_a, documento["id"],
                           filename="certificado.pdf", contenido=CONTENIDO)
    fid = salida["archivo"]["id"]

    r = app_cliente.get(
        f"{API}/documentos/{documento['id']}/archivos/{fid}/download-url",
        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    dl = r.json()
    assert dl["filename"] == "certificado.pdf"
    assert dl["size_bytes"] == len(CONTENIDO)

    # La URL firmada no lleva token: la firma ES la autorización.
    ruta = _ruta_local(dl["download_url"])
    r = app_cliente.get(ruta)
    assert r.status_code == 200, r.text
    assert r.content == CONTENIDO
    assert 'filename="certificado.pdf"' in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"


def test_firma_manipulada_da_403(app_cliente, empresa_a, documento):
    salida = subir_archivo(app_cliente, empresa_a, documento["id"])
    fid = salida["archivo"]["id"]
    dl = app_cliente.get(
        f"{API}/documentos/{documento['id']}/archivos/{fid}/download-url",
        headers=empresa_a["headers"]).json()

    ruta = _ruta_local(dl["download_url"])
    manipulada = ruta.replace("sig=", "sig=0")[:-1] + "f"
    r = app_cliente.get(manipulada)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FIRMA_INVALIDA"


def test_la_firma_no_sirve_para_otro_blob(app_cliente, empresa_a, empresa_b,
                                          contrato_b, documento):
    """El HMAC cubre (modo, blob_path, exp): no es una llave maestra."""
    salida = subir_archivo(app_cliente, empresa_a, documento["id"])
    fid = salida["archivo"]["id"]
    dl = app_cliente.get(
        f"{API}/documentos/{documento['id']}/archivos/{fid}/download-url",
        headers=empresa_a["headers"]).json()

    doc_b = _doc_de_contrato(app_cliente, empresa_b, contrato_b)
    ajeno = subir_archivo(app_cliente, empresa_b, doc_b["id"])

    ruta = _ruta_local(dl["download_url"])
    sustituida = ruta.replace(salida["archivo"]["blob_path"],
                              ajeno["archivo"]["blob_path"])
    r = app_cliente.get(sustituida)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FIRMA_INVALIDA"


def test_la_firma_de_escritura_no_sirve_para_leer(app_cliente, empresa_a,
                                                  documento):
    """El modo va dentro del HMAC: una SAS de escritura no descarga."""
    r = app_cliente.post(f"{API}/documentos/{documento['id']}/upload-url",
                         headers=empresa_a["headers"], json={"filename": "a.pdf"})
    subida = _ruta_local(r.json()["upload_url"])
    r = app_cliente.get(subida.replace("/blobs/upload", "/blobs/download"))
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FIRMA_INVALIDA"


def test_borrar_el_archivo_devuelve_el_documento_a_falta(app_cliente, empresa_a,
                                                        documento):
    salida = subir_archivo(app_cliente, empresa_a, documento["id"])
    fid = salida["archivo"]["id"]
    r = app_cliente.delete(f"{API}/documentos/{documento['id']}/archivos/{fid}",
                           headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["documento"]["estado"] == "falta"
    assert r.json()["documento"]["archivos_count"] == 0

    # Y el blob se purgó: la URL firmada anterior ya no encuentra nada.
    r = app_cliente.get(
        f"{API}/documentos/{documento['id']}/archivos/{fid}/download-url",
        headers=empresa_a["headers"])
    assert r.status_code == 404


def test_el_endpoint_de_descarga_antiguo_ya_no_existe(app_cliente, empresa_a,
                                                     documento):
    """§6.2 de RUPTURAS: `/descarga` está eliminado, no deprecado."""
    salida = subir_archivo(app_cliente, empresa_a, documento["id"])
    fid = salida["archivo"]["id"]
    r = app_cliente.get(f"{API}/documentos/{documento['id']}/archivos/{fid}/descarga",
                        headers=empresa_a["headers"])
    assert r.status_code == 404


# ============================================================================
# 4. Listado transversal
# ============================================================================
@pytest.fixture
def poblado(app_cliente, empresa_a, contrato_a) -> dict:
    """Un trabajador conductor y un equipo, con sus checklists instanciados."""
    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Juan Soto",
        "rut": rut_valido(11), "cargo": "Conductor", "es_conductor": True})
    assert r.status_code == 201, r.text
    trabajador = r.json()["id"]
    r = app_cliente.post(f"{API}/equipos", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "patente": patente_valida(3),
        "tipo_equipo": "Camión", "marca": "Volvo"})
    assert r.status_code == 201, r.text
    return {"trabajador": trabajador, "equipo": r.json()["id"]}


def test_listado_transversal_tiene_la_envoltura_estandar(app_cliente, empresa_a,
                                                         poblado):
    r = app_cliente.get(f"{API}/documentos", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert set(cuerpo) == {"items", "page", "page_size", "total", "total_pages"}
    assert cuerpo["page"] == 1
    assert cuerpo["page_size"] == 25
    assert cuerpo["total"] > 25, "el checklist de un contrato ya pasa de una página"
    assert cuerpo["total_pages"] == (cuerpo["total"] + 24) // 25
    assert len(cuerpo["items"]) == 25

    fila = cuerpo["items"][0]
    assert fila["dueno"]["tipo"] in ("trabajador", "equipo", "contrato")
    assert "archivos_count" in fila and "archivos" not in fila
    assert "dias_para_vencer" in fila


def test_listado_respeta_los_filtros(app_cliente, empresa_a, poblado,
                                     contrato_a):
    h = empresa_a["headers"]

    def total(query: str) -> int:
        r = app_cliente.get(f"{API}/documentos?{query}", headers=h)
        assert r.status_code == 200, r.text
        return r.json()["total"]

    todos = total("page_size=1")
    del_sujeto = total(f"sujeto_id={poblado['trabajador']}&page_size=1")
    emsipor = total("es_emsipor=true&page_size=1")
    opcionales = total("obligatorio=false&page_size=1")

    assert 0 < del_sujeto < todos
    assert emsipor > 0, "el conductor recibe el expediente EMSIPOR"
    assert opcionales > 0

    # Los del sujeto son todos suyos.
    r = app_cliente.get(f"{API}/documentos?sujeto_id={poblado['trabajador']}"
                        f"&page_size=100", headers=h)
    assert {i["dueno"]["id"] for i in r.json()["items"]} == {poblado["trabajador"]}

    # El filtro por contrato cubre las dos rutas del dueño.
    assert total(f"contrato_id={contrato_a['id']}&page_size=1") == todos

    # Búsqueda por título.
    r = app_cliente.get(f"{API}/documentos?search=SOAP&page_size=100", headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["total"] >= 1
    assert all("soap" in i["titulo"].lower() for i in r.json()["items"])


def test_filtros_de_vencimiento(app_cliente, empresa_a, poblado):
    h = empresa_a["headers"]
    doc = app_cliente.get(f"{API}/documentos?sujeto_id={poblado['equipo']}"
                          f"&page_size=1", headers=h).json()["items"][0]
    vence = date.today() + timedelta(days=10)
    r = app_cliente.patch(f"{API}/documentos/{doc['id']}", headers=h,
                          json={"estado": "ok", "vence": vence.isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["estado_calc"] == "porvenc"

    hasta = (date.today() + timedelta(days=30)).isoformat()
    r = app_cliente.get(f"{API}/documentos?vence_antes={hasta}&page_size=100",
                        headers=h)
    assert [i["id"] for i in r.json()["items"]] == [doc["id"]], (
        "los documentos sin fecha se descartan del filtro por vencimiento")

    r = app_cliente.get(f"{API}/documentos?estado_calc=porvenc&page_size=100",
                        headers=h)
    assert [i["id"] for i in r.json()["items"]] == [doc["id"]]


@pytest.mark.parametrize("query, codigo", [
    ("estado=inventado", "ESTADO_INVALIDO"),
    ("estado_calc=inventado", "ESTADO_CALC_INVALIDO"),
    ("vence_antes=2026-01-01&vence_despues=2026-06-01", "RANGO_INVALIDO"),
])
def test_listado_valida_los_filtros(app_cliente, empresa_a, query, codigo):
    r = app_cliente.get(f"{API}/documentos?{query}", headers=empresa_a["headers"])
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == codigo


def test_page_size_por_encima_del_maximo_es_422(app_cliente, empresa_a):
    r = app_cliente.get(f"{API}/documentos?page_size=500",
                        headers=empresa_a["headers"])
    assert r.status_code == 422


# ============================================================================
# 5. PATCH y derivación de vencimiento
# ============================================================================
def test_patch_deriva_el_vencimiento_de_la_plantilla(app_cliente, empresa_a,
                                                    contrato_a):
    """Sin fecha, un documento `ok` lo estaría para siempre: se deriva."""
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/documentos"
                        f"?page_size=100", headers=empresa_a["headers"])
    doc = next(d for d in r.json()["items"]
               if d["titulo"] == "Certificado Ley 16.744")     # vigencia 1 mes

    r = app_cliente.patch(f"{API}/documentos/{doc['id']}",
                          headers=empresa_a["headers"], json={"estado": "ok"})
    assert r.status_code == 200, r.text
    salida = r.json()
    assert salida["vence_derivado"] is True
    assert salida["vence"] is not None
    assert "nota" in salida
    assert salida["estado_calc"] == "porvenc", "un mes cae dentro del umbral de 30 días"


def test_patch_sin_cambios(app_cliente, empresa_a, documento):
    r = app_cliente.patch(f"{API}/documentos/{documento['id']}",
                          headers=empresa_a["headers"], json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SIN_CAMBIOS"


def test_patch_rechaza_un_estado_derivado(app_cliente, empresa_a, documento):
    """`estado` es la marca manual (ok/falta); `estado_calc` lo calcula el sistema."""
    r = app_cliente.patch(f"{API}/documentos/{documento['id']}",
                          headers=empresa_a["headers"], json={"estado": "venc"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "TRANSICION_INVALIDA"
