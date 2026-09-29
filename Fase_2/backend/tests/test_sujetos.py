"""Personal, equipos, cargos, expediente EMSIPOR e identidad agrupada (§9).

Dos decisiones de diseño gobiernan este módulo:

* **El cargo es un catálogo, no texto libre.** Se acepta texto porque el alta por
  extracción IA y las importaciones lo traen escrito a mano, pero se resuelve o
  se crea en `cargos`, y es el cargo el que decide si el trabajador necesita
  expediente EMSIPOR. `es_conductor` queda como override por trabajador.
* **La identidad vive en el RUT, no en la fila.** Un mismo trabajador puede estar
  en varios contratos: `sujetos` tiene una fila por contrato y `/personas` las
  agrupa. Confundir las dos cosas produce dobles conteos en toda la operación.
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.conftest import (API, crear_contrato, patente_valida, rut_valido,
                            subir_archivo)


# ============================================================================
# Alta de personal y resolución del cargo
# ============================================================================
def test_alta_con_cargo_texto_crea_el_cargo(app_cliente, empresa_a, contrato_a):
    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Juan Soto",
        "rut": rut_valido(41), "cargo": "Operador de Cargador Frontal"})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["cargo_creado"] is True
    assert s["cargo"] == "Operador de Cargador Frontal"
    assert s["cargo_id"] is not None
    assert s["documentos_creados"] > 0

    r = app_cliente.get(f"{API}/cargos?page_size=100", headers=empresa_a["headers"])
    cargo = next(c for c in r.json()["items"]
                 if c["nombre"] == "Operador de Cargador Frontal")
    assert cargo["categoria"] == "otro", "queda sin clasificar, para revisarlo"
    assert cargo["es_global"] is False
    assert cargo["trabajadores"] == 1


def test_alta_con_cargo_id_no_crea_nada(app_cliente, empresa_a, contrato_a):
    cargo = app_cliente.post(f"{API}/cargos", headers=empresa_a["headers"],
                             json={"nombre": "Prevencionista de Riesgos",
                                   "categoria": "supervision"}).json()

    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Ana Díaz",
        "rut": rut_valido(42), "cargo_id": cargo["id"]})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["cargo_creado"] is False
    assert s["cargo_id"] == cargo["id"]
    assert s["cargo"] == "Prevencionista de Riesgos"

    r = app_cliente.get(f"{API}/cargos?page_size=100", headers=empresa_a["headers"])
    assert r.json()["total"] == 1, "no debía aparecer un cargo nuevo"


def test_el_texto_del_cargo_se_deduplica_sin_tildes_ni_mayusculas(
        app_cliente, empresa_a, contrato_a):
    """`nombre_normalizado` es la clave única (company_id, normalizado)."""
    primero = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"],
                               json={"contrato_id": contrato_a["id"],
                                     "nombre": "A", "rut": rut_valido(43),
                                     "cargo": "Mecánico"}).json()
    segundo = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"],
                               json={"contrato_id": contrato_a["id"],
                                     "nombre": "B", "rut": rut_valido(44),
                                     "cargo": "  MECANICO "}).json()
    assert primero["cargo_creado"] is True
    assert segundo["cargo_creado"] is False
    assert segundo["cargo_id"] == primero["cargo_id"]


def test_cargo_id_de_otra_empresa(app_cliente, empresa_a, empresa_b, contrato_a,
                                  contrato_b):
    ajeno = app_cliente.post(f"{API}/cargos", headers=empresa_b["headers"],
                             json={"nombre": "Rigger"}).json()["id"]
    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Juan", "rut": rut_valido(45),
        "cargo_id": ajeno})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CARGO_INEXISTENTE"


def test_rut_invalido(app_cliente, empresa_a, contrato_a):
    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Juan",
        "rut": "12.345.678-0"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "RUT_INVALIDO"


def test_rut_duplicado_activo_en_el_mismo_contrato(app_cliente, empresa_a,
                                                   contrato_a):
    rut = rut_valido(46)
    cuerpo = {"contrato_id": contrato_a["id"], "nombre": "Juan Soto", "rut": rut}
    assert app_cliente.post(f"{API}/personal", headers=empresa_a["headers"],
                            json=cuerpo).status_code == 201

    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"],
                         json=cuerpo)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "RUT_DUPLICADO"


def test_tras_la_baja_el_rut_se_puede_reingresar(app_cliente, empresa_a,
                                                contrato_a):
    """El índice único es parcial (`estado <> 'baja'`): un reingreso es legítimo."""
    rut = rut_valido(47)
    cuerpo = {"contrato_id": contrato_a["id"], "nombre": "Juan Soto", "rut": rut}
    sid = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"],
                           json=cuerpo).json()["id"]

    r = app_cliente.post(f"{API}/personal/{sid}/baja", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "id": sid, "estado": "baja"}

    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"],
                         json=cuerpo)
    assert r.status_code == 201, r.text


def test_patente_duplicada_en_el_mismo_contrato(app_cliente, empresa_a,
                                                contrato_a):
    cuerpo = {"contrato_id": contrato_a["id"], "patente": patente_valida(21),
              "tipo_equipo": "Camión"}
    assert app_cliente.post(f"{API}/equipos", headers=empresa_a["headers"],
                            json=cuerpo).status_code == 201
    r = app_cliente.post(f"{API}/equipos", headers=empresa_a["headers"],
                         json=cuerpo)
    assert r.status_code == 409


def test_patch_separa_los_campos_por_tipo(app_cliente, empresa_a, contrato_a):
    """§5 de RUPTURAS: los campos del otro tipo se ignoran en silencio."""
    sid = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Juan Soto",
        "rut": rut_valido(48)}).json()["id"]

    r = app_cliente.patch(f"{API}/personal/{sid}", headers=empresa_a["headers"],
                          json={"nombre": "Juan Soto Pérez", "marca": "Volvo",
                                "anio": 2020})
    assert r.status_code == 200, r.text
    assert r.json()["nombre"] == "Juan Soto Pérez"
    assert r.json()["marca"] is None
    assert r.json()["anio"] is None

    r = app_cliente.patch(f"{API}/personal/{sid}", headers=empresa_a["headers"],
                          json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SIN_CAMBIOS"


# ============================================================================
# EMSIPOR: el cargo decide, `es_conductor` es un override
# ============================================================================
def test_un_cargo_con_emsipor_crea_el_expediente_sin_ser_conductor(
        app_cliente, empresa_a, contrato_a):
    """`cargos.requiere_emsipor` es la fuente de verdad (§9.2).

    El caso importante: `es_conductor=false` y aun así hay expediente, porque el
    cargo lo exige. Con la regla antigua (solo `es_conductor`) este trabajador se
    quedaba sin los nueve documentos EMSIPOR.
    """
    cargo = app_cliente.post(f"{API}/cargos", headers=empresa_a["headers"], json={
        "nombre": "Operador de Equipo Mina", "categoria": "operacion",
        "requiere_emsipor": True}).json()
    assert cargo["requiere_emsipor"] is True

    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Pedro Rojas",
        "rut": rut_valido(51), "cargo_id": cargo["id"], "es_conductor": False})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["es_conductor"] is False
    assert s["expediente_emsipor_creado"] is True

    r = app_cliente.get(f"{API}/documentos?sujeto_id={s['id']}&es_emsipor=true"
                        f"&page_size=100", headers=empresa_a["headers"])
    assert r.json()["total"] == 9, "el expediente EMSIPOR son nueve documentos"

    r = app_cliente.get(f"{API}/personal/{s['id']}/licencia-interna",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text


def test_sin_cargo_ni_conductor_no_hay_expediente(app_cliente, empresa_a,
                                                 contrato_a):
    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Ana Díaz",
        "rut": rut_valido(52), "cargo": "Administrativa"})
    assert r.json()["expediente_emsipor_creado"] is False
    sid = r.json()["id"]

    r = app_cliente.get(f"{API}/personal/{sid}/licencia-interna",
                        headers=empresa_a["headers"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "NO_REQUIERE_EMSIPOR"


def test_cambiar_el_cargo_crea_el_expediente_pero_no_reinstancia_el_checklist(
        app_cliente, empresa_a, contrato_a):
    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Ana Díaz",
        "rut": rut_valido(53), "cargo": "Administrativa"})
    sid = r.json()["id"]
    docs_antes = app_cliente.get(
        f"{API}/documentos?sujeto_id={sid}&es_emsipor=false&page_size=100",
        headers=empresa_a["headers"]).json()["total"]

    cargo = app_cliente.post(f"{API}/cargos", headers=empresa_a["headers"], json={
        "nombre": "Conductora Mina", "categoria": "conduccion",
        "requiere_emsipor": True}).json()
    r = app_cliente.patch(f"{API}/personal/{sid}", headers=empresa_a["headers"],
                          json={"cargo_id": cargo["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["expediente_emsipor_creado"] is True

    docs_despues = app_cliente.get(
        f"{API}/documentos?sujeto_id={sid}&es_emsipor=false&page_size=100",
        headers=empresa_a["headers"]).json()["total"]
    assert docs_despues == docs_antes, (
        "cambiar el cargo no reinstancia el checklist: se perderían archivos")


# ============================================================================
# Licencia interna de manejo
# ============================================================================
@pytest.fixture
def conductor(app_cliente, empresa_a, contrato_a) -> dict:
    r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Juan Soto",
        "rut": rut_valido(61), "cargo": "Conductor", "es_conductor": True})
    assert r.status_code == 201, r.text
    s = r.json()
    return {**s, "ruta": f"{API}/personal/{s['id']}/licencia-interna"}


def test_licencia_interna_arranca_pendiente(app_cliente, empresa_a, conductor):
    r = app_cliente.get(conductor["ruta"], headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    lim = r.json()
    assert lim["numero"] is None
    assert lim["estado"] == "pendiente"
    assert lim["vence"] is None
    assert lim["dias_para_vencer"] is None
    assert lim["emsipor_estado"] == "pendiente"
    assert len(lim["checklist"]) == 9
    assert lim["resumen"] == {"total": 9, "ok": 0, "faltan": 9}
    # Cada documento indica en qué plataforma se tramita: son cuatro sistemas.
    assert {d["plataforma"] for d in lim["checklist"]} == {
        "SIGA", "DIRECTIC", "Academia MLP", "EMSIPOR"}


@pytest.mark.parametrize("dias, estado", [(200, "vigente"), (10, "por_vencer")])
def test_patch_de_la_licencia_deriva_el_estado(app_cliente, empresa_a, conductor,
                                               dias, estado):
    """`estado` no se acepta del cliente: se deriva del número y la fecha."""
    vence = date.today() + timedelta(days=dias)
    r = app_cliente.patch(conductor["ruta"], headers=empresa_a["headers"],
                          json={"numero": "LIM-2026-014",
                                "vence": vence.isoformat()})
    assert r.status_code == 200, r.text
    lim = r.json()
    assert lim["numero"] == "LIM-2026-014"
    assert lim["vence"] == vence.isoformat()
    assert lim["dias_para_vencer"] == dias
    assert lim["estado"] == estado


def test_licencia_con_numero_y_sin_fecha_queda_vigente(app_cliente, empresa_a,
                                                       conductor):
    r = app_cliente.patch(conductor["ruta"], headers=empresa_a["headers"],
                          json={"numero": "LIM-1"})
    assert r.status_code == 200, r.text
    assert r.json()["estado"] == "vigente"
    assert r.json()["vence"] is None


def test_patch_de_la_licencia_sin_cambios(app_cliente, empresa_a, conductor):
    r = app_cliente.patch(conductor["ruta"], headers=empresa_a["headers"], json={})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "SIN_CAMBIOS"


def test_el_estado_emsipor_lo_deriva_el_trigger(app_cliente, empresa_a,
                                                conductor):
    """`fn_sync_emsipor` recalcula `emsipor_estado` al cambiar un documento."""
    docs = app_cliente.get(f"{API}/documentos?sujeto_id={conductor['id']}"
                           f"&es_emsipor=true&page_size=100",
                           headers=empresa_a["headers"]).json()["items"]
    app_cliente.patch(f"{API}/documentos/{docs[0]['id']}",
                      headers=empresa_a["headers"], json={"estado": "ok"})

    r = app_cliente.get(conductor["ruta"], headers=empresa_a["headers"])
    assert r.json()["emsipor_estado"] == "parcial"

    for d in docs[1:]:
        app_cliente.patch(f"{API}/documentos/{d['id']}",
                          headers=empresa_a["headers"], json={"estado": "ok"})
    r = app_cliente.get(conductor["ruta"], headers=empresa_a["headers"])
    assert r.json()["emsipor_estado"] == "aprobado"


def test_emsipor_estado_coincide_con_el_chequeo_8_de_verificacion(
        app_cliente, empresa_a, conductor, motor_admin):
    """El valor que escribe la API tiene que ser el que deriva la base.

    Chequeo 8 de `05_verificacion.sql`: cuenta por `estado = 'ok'`. Si la
    aplicación usara otra regla (por ejemplo `estado_calc`), el chequeo marcaría
    la fila como desincronizada en cada corrida aunque nadie hubiera tocado nada.
    """
    import sqlalchemy as sa

    docs = app_cliente.get(f"{API}/documentos?sujeto_id={conductor['id']}"
                           f"&es_emsipor=true&page_size=100",
                           headers=empresa_a["headers"]).json()["items"]
    for d in docs[:5]:
        app_cliente.patch(f"{API}/documentos/{d['id']}",
                          headers=empresa_a["headers"], json={"estado": "ok"})
    app_cliente.get(conductor["ruta"], headers=empresa_a["headers"])

    with motor_admin.connect() as conn:
        desincronizadas = conn.execute(sa.text("""
            WITH avance AS (
              SELECT sujeto_id,
                     count(*) FILTER (WHERE obligatorio) AS oblig,
                     count(*) FILTER (WHERE obligatorio AND estado = 'ok') AS ok
              FROM documentos WHERE es_emsipor GROUP BY sujeto_id
            )
            SELECT l.sujeto_id, l.emsipor_estado, a.ok, a.oblig
            FROM licencias_internas l JOIN avance a ON a.sujeto_id = l.sujeto_id
            WHERE l.emsipor_estado <> CASE
                    WHEN a.ok = 0 THEN 'pendiente'::emsipor_estado
                    WHEN a.ok < a.oblig THEN 'parcial'::emsipor_estado
                    ELSE 'aprobado'::emsipor_estado END
        """)).all()
    assert [tuple(f) for f in desincronizadas] == []


def test_reset_del_expediente_borra_documentos_archivos_y_marcas(
        app_cliente, empresa_a, conductor):
    """Irreversible y acotado: no toca el resto del checklist del trabajador."""
    docs = app_cliente.get(f"{API}/documentos?sujeto_id={conductor['id']}"
                           f"&es_emsipor=true&page_size=100",
                           headers=empresa_a["headers"]).json()["items"]
    subir_archivo(app_cliente, empresa_a, docs[0]["id"])
    app_cliente.patch(conductor["ruta"], headers=empresa_a["headers"],
                      json={"numero": "LIM-1",
                            "vence": (date.today() + timedelta(days=90)).isoformat()})

    no_emsipor_antes = app_cliente.get(
        f"{API}/documentos?sujeto_id={conductor['id']}&es_emsipor=false"
        f"&page_size=100", headers=empresa_a["headers"]).json()["total"]

    r = app_cliente.post(f"{conductor['ruta']}/reset", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    salida = r.json()
    assert salida["reset"]["documentos_eliminados"] == 9
    assert salida["reset"]["archivos_eliminados"] == 1
    assert salida["reset"]["documentos_creados"] == 9
    assert salida["numero"] is None
    assert salida["vence"] is None
    assert salida["estado"] == "pendiente"
    assert salida["emsipor_estado"] == "pendiente"
    assert all(d["estado"] == "falta" for d in salida["checklist"])

    no_emsipor_despues = app_cliente.get(
        f"{API}/documentos?sujeto_id={conductor['id']}&es_emsipor=false"
        f"&page_size=100", headers=empresa_a["headers"]).json()["total"]
    assert no_emsipor_despues == no_emsipor_antes


def test_la_licencia_de_un_no_conductor_no_se_edita(app_cliente, empresa_a,
                                                    contrato_a):
    sid = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Ana", "rut": rut_valido(62),
        "cargo": "Administrativa"}).json()["id"]
    ruta = f"{API}/personal/{sid}/licencia-interna"
    for metodo, kw in (("patch", {"json": {"numero": "X"}}),
                       ("post", {})):
        destino = ruta if metodo == "patch" else f"{ruta}/reset"
        r = getattr(app_cliente, metodo)(destino, headers=empresa_a["headers"], **kw)
        assert r.status_code == 409, (metodo, r.text)
        assert r.json()["error"]["code"] == "NO_REQUIERE_EMSIPOR"


# ============================================================================
# Identidad agrupada: /personas y /flota
# ============================================================================
def test_el_mismo_rut_en_dos_contratos_es_una_sola_persona(
        app_cliente, empresa_a, contrato_a, faena_pelambres):
    """§9.4: `sujetos` tiene una fila por contrato; la persona es una."""
    segundo = crear_contrato(app_cliente, empresa_a, faena_pelambres["id"],
                             nombre="Contrato Planta", codigo="C-2")
    rut = rut_valido(71)
    for contrato in (contrato_a, segundo):
        r = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
            "contrato_id": contrato["id"], "nombre": "Juan Soto", "rut": rut,
            "cargo": "Conductor", "es_conductor": True})
        assert r.status_code == 201, r.text

    r = app_cliente.get(f"{API}/personal?page_size=100",
                        headers=empresa_a["headers"])
    assert r.json()["total"] == 2, "dos registros, uno por contrato"

    r = app_cliente.get(f"{API}/personas?page_size=100",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1, "una sola persona"
    persona = r.json()["items"][0]
    assert persona["rut"] == rut
    assert persona["registros"] == 2
    assert persona["activos"] == 2
    assert len(persona["contratos"]) == 2
    assert persona["faenas"] == ["Los Pelambres"]

    r = app_cliente.get(f"{API}/personas/{rut}", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["registros"] == 2


def test_la_flota_agrupa_por_patente(app_cliente, empresa_a, contrato_a,
                                     faena_pelambres):
    segundo = crear_contrato(app_cliente, empresa_a, faena_pelambres["id"],
                             nombre="Contrato Planta")
    patente = patente_valida(31)
    for contrato in (contrato_a, segundo):
        r = app_cliente.post(f"{API}/equipos", headers=empresa_a["headers"], json={
            "contrato_id": contrato["id"], "patente": patente,
            "tipo_equipo": "Camión", "marca": "Volvo"})
        assert r.status_code == 201, r.text

    r = app_cliente.get(f"{API}/flota?page_size=100", headers=empresa_a["headers"])
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["registros"] == 2

    r = app_cliente.get(f"{API}/flota/{patente}", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["patente"] == patente


def test_persona_inexistente(app_cliente, empresa_a, contrato_a):
    r = app_cliente.get(f"{API}/personas/{rut_valido(99)}",
                        headers=empresa_a["headers"])
    assert r.status_code == 404


# ============================================================================
# Checklist y borrado
# ============================================================================
def test_el_checklist_del_sujeto_no_es_un_listado_paginado(app_cliente,
                                                           empresa_a, conductor):
    """Es un conjunto cerrado: se devuelve completo, con sus estadísticas."""
    r = app_cliente.get(f"{API}/personal/{conductor['id']}/documentos",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert set(cuerpo) == {"sujeto", "items", "documentos_emsipor", "total",
                           "stats"}
    assert "page" not in cuerpo
    assert len(cuerpo["documentos_emsipor"]) == 9
    assert cuerpo["stats"]["cumplimiento_pct"] == 0
    assert cuerpo["stats"]["docs_total"] == len(cuerpo["items"])


def test_borrar_un_sujeto_purga_sus_blobs(app_cliente, empresa_a, conductor):
    doc = app_cliente.get(f"{API}/documentos?sujeto_id={conductor['id']}"
                          f"&page_size=1", headers=empresa_a["headers"]
                          ).json()["items"][0]
    subir_archivo(app_cliente, empresa_a, doc["id"])

    r = app_cliente.delete(f"{API}/personal/{conductor['id']}",
                           headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["archivos_eliminados"] == 1
    assert app_cliente.get(f"{API}/personal/{conductor['id']}",
                           headers=empresa_a["headers"]).status_code == 404


def test_la_baja_recalcula_el_cumplimiento_del_contrato(app_cliente, empresa_a,
                                                        contrato_a, conductor):
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}",
                        headers=empresa_a["headers"])
    assert r.json()["stats"]["personal"]["total"] == 1

    app_cliente.post(f"{API}/personal/{conductor['id']}/baja",
                     headers=empresa_a["headers"])
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}",
                        headers=empresa_a["headers"])
    assert r.json()["stats"]["personal"]["total"] == 0, (
        "un trabajador de baja no cuenta en la dotación")


# ============================================================================
# Filtros del listado
# ============================================================================
def test_filtros_del_listado_de_personal(app_cliente, empresa_a, contrato_a,
                                         faena_pelambres):
    app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Juan Soto",
        "rut": rut_valido(81), "cargo": "Conductor", "es_conductor": True})
    app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Ana Díaz",
        "rut": rut_valido(82), "cargo": "Administrativa"})
    h = empresa_a["headers"]

    def nombres(query: str) -> set[str]:
        r = app_cliente.get(f"{API}/personal?{query}", headers=h)
        assert r.status_code == 200, r.text
        return {i["nombre"] for i in r.json()["items"]}

    assert nombres("page_size=100") == {"Juan Soto", "Ana Díaz"}
    assert nombres("es_conductor=true") == {"Juan Soto"}
    assert nombres("cargo=Administrativa") == {"Ana Díaz"}
    assert nombres(f"faena_id={faena_pelambres['id']}") == {"Juan Soto", "Ana Díaz"}
    assert nombres("search=Díaz") == {"Ana Díaz"}
    assert nombres("estado=ok") == set()

    r = app_cliente.get(f"{API}/personal?sort=nombre", headers=h)
    assert [i["nombre"] for i in r.json()["items"]] == ["Ana Díaz", "Juan Soto"]
    r = app_cliente.get(f"{API}/personal?sort=-nombre", headers=h)
    assert [i["nombre"] for i in r.json()["items"]] == ["Juan Soto", "Ana Díaz"]
