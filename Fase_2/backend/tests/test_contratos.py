"""Contratos: alta, herencia de plataformas, requisitos y matriz (§8).

Tres reglas de negocio concentran casi todo el riesgo de este módulo:

* **La herencia de plataformas es destructiva si se implementa mal.** Insertar la
  primera fila propia en `contrato_plataformas` convierte esa lista en la
  efectiva; si no se materializan antes las de la faena, añadir una plataforma
  manual borraría silenciosamente las del mandante.
* **Los requisitos base son de solo lectura.** Se proyectan desde
  `requisito_templates` y no tienen fila propia; editarlos desde el contrato
  rompería el catálogo maestro.
* **La matriz es dispersa.** Una celda ausente significa «no aplica», que no es
  lo mismo que `falta`; emitir celdas nulas convertiría huecos en incumplimientos.
"""
from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.database import worker_session
from app.models import ContratoRequisito, RequisitoTemplate
from app.services.checklist import normalizar

from tests.conftest import API, patente_valida, rut_valido

PLATAFORMAS_PELAMBRES = {"SIGA", "DIRECTIC", "SGES", "Academia MLP", "EMSIPOR"}


# ============================================================================
# CRUD
# ============================================================================
def test_alta_instancia_el_checklist_de_empresa(app_cliente, empresa_a,
                                                faena_pelambres):
    r = app_cliente.post(f"{API}/contratos", headers=empresa_a["headers"], json={
        "nombre": "Servicio de Transporte", "codigo": "MLP-2026-01",
        "faena_id": faena_pelambres["id"],
        "fecha_inicio": date.today().isoformat()})
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["documentos_creados"] > 0
    assert c["faena"]["nombre"] == "Los Pelambres"
    assert c["faena"]["region"] == "Coquimbo"
    assert c["origen_ia_review_id"] is None

    r = app_cliente.get(f"{API}/contratos/{c['id']}/documentos?page_size=100",
                        headers=empresa_a["headers"])
    assert r.json()["total"] == c["documentos_creados"]


def test_listado_valida_el_estado(app_cliente, empresa_a, contrato_a):
    r = app_cliente.get(f"{API}/contratos?estado=inventado",
                        headers=empresa_a["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "ESTADO_INVALIDO"


def test_ia_review_de_otra_empresa_no_se_puede_referenciar(
        app_cliente, empresa_a, faena_pelambres):
    r = app_cliente.post(f"{API}/contratos", headers=empresa_a["headers"], json={
        "nombre": "Con IA", "faena_id": faena_pelambres["id"],
        "ia_review_id": str(uuid.uuid4())})
    assert r.status_code == 404


def test_borrar_el_contrato_purga_sujetos_y_archivos(app_cliente, empresa_a,
                                                    contrato_a):
    from tests.conftest import subir_archivo
    app_cliente.post(f"{API}/personal", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "nombre": "Juan Soto",
        "rut": rut_valido(21)})
    doc = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/documentos",
                          headers=empresa_a["headers"]).json()["items"][0]
    subir_archivo(app_cliente, empresa_a, doc["id"])

    r = app_cliente.delete(f"{API}/contratos/{contrato_a['id']}",
                           headers=empresa_a["headers"])
    assert r.status_code == 400, "borrar exige confirmación"
    r = app_cliente.delete(f"{API}/contratos/{contrato_a['id']}?confirm=true",
                           headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["sujetos_eliminados"] == 1
    assert r.json()["archivos_eliminados"] >= 1
    assert app_cliente.get(f"{API}/contratos/{contrato_a['id']}",
                           headers=empresa_a["headers"]).status_code == 404


# ============================================================================
# Herencia de plataformas (§8.1)
# ============================================================================
def test_contrato_nuevo_hereda_las_plataformas_de_la_faena(app_cliente,
                                                           empresa_a,
                                                           contrato_a):
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/plataformas",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert {i["nombre"] for i in items} == PLATAFORMAS_PELAMBRES
    for i in items:
        assert i["id"] is None, "una heredada sin materializar no tiene fila propia"
        assert i["heredada"] is True and i["materializada"] is False
        assert i["es_custom"] is False
        assert i["faena_plataforma_id"]
        assert i["estado"] == "sin_acceso"


def test_la_primera_manual_materializa_las_heredadas(app_cliente, empresa_a,
                                                     contrato_a):
    """El riesgo real: insertar la primera fila propia no debe borrar la herencia."""
    r = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/plataformas",
                         headers=empresa_a["headers"],
                         json={"nombre": "PORTAL INTERNO", "url": "https://x.cl"})
    assert r.status_code == 201, r.text
    assert r.json()["heredadas_materializadas"] == len(PLATAFORMAS_PELAMBRES)
    assert r.json()["es_custom"] is True

    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/plataformas",
                        headers=empresa_a["headers"])
    items = r.json()["items"]
    assert {i["nombre"] for i in items} == PLATAFORMAS_PELAMBRES | {"PORTAL INTERNO"}
    for i in items:
        assert i["id"] is not None
        assert i["materializada"] is True
    # La herencia se conserva en el origen de cada fila.
    heredadas = [i for i in items if i["nombre"] in PLATAFORMAS_PELAMBRES]
    assert all(i["faena_plataforma_id"] and not i["es_custom"] for i in heredadas)

    # Y una segunda manual ya no materializa nada.
    r = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/plataformas",
                         headers=empresa_a["headers"], json={"nombre": "OTRO PORTAL"})
    assert r.status_code == 201, r.text
    assert r.json()["heredadas_materializadas"] == 0


def test_nombre_duplicado_de_plataforma(app_cliente, empresa_a, contrato_a):
    """El choque se detecta contra la lista EFECTIVA, incluidas las heredadas."""
    r = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/plataformas",
                         headers=empresa_a["headers"], json={"nombre": "siga"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLATAFORMA_DUPLICADA"


def test_no_se_borra_una_plataforma_heredada(app_cliente, empresa_a, contrato_a):
    """Una plataforma del mandante no desaparece: se pone en `sin_acceso`."""
    heredada = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/plataformas",
                               headers=empresa_a["headers"]).json()["items"][0]
    fp_id = heredada["faena_plataforma_id"]

    # Sin materializar todavía: se responde 409 y no 404, porque existe.
    r = app_cliente.delete(
        f"{API}/contratos/{contrato_a['id']}/plataformas/{fp_id}?confirm=true",
        headers=empresa_a["headers"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLATAFORMA_HEREDADA"

    # Y tampoco después de materializarla.
    app_cliente.post(f"{API}/contratos/{contrato_a['id']}/plataformas",
                     headers=empresa_a["headers"], json={"nombre": "PORTAL INTERNO"})
    materializada = next(
        i for i in app_cliente.get(f"{API}/contratos/{contrato_a['id']}/plataformas",
                                   headers=empresa_a["headers"]).json()["items"]
        if i["faena_plataforma_id"] == fp_id)
    r = app_cliente.delete(
        f"{API}/contratos/{contrato_a['id']}/plataformas/{materializada['id']}"
        f"?confirm=true", headers=empresa_a["headers"])
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLATAFORMA_HEREDADA"


def test_se_borra_una_plataforma_manual_con_confirmacion(app_cliente, empresa_a,
                                                        contrato_a):
    pid = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/plataformas",
                           headers=empresa_a["headers"],
                           json={"nombre": "PORTAL INTERNO"}).json()["id"]

    r = app_cliente.delete(f"{API}/contratos/{contrato_a['id']}/plataformas/{pid}",
                           headers=empresa_a["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "REQUIERE_CONFIRMACION"

    r = app_cliente.delete(
        f"{API}/contratos/{contrato_a['id']}/plataformas/{pid}?confirm=true",
        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["nombre"] == "PORTAL INTERNO"

    nombres = {i["nombre"] for i in app_cliente.get(
        f"{API}/contratos/{contrato_a['id']}/plataformas",
        headers=empresa_a["headers"]).json()["items"]}
    assert "PORTAL INTERNO" not in nombres
    assert nombres == PLATAFORMAS_PELAMBRES, "las heredadas siguen ahí"


def test_solicitar_acceso_fija_las_marcas_que_exige_el_check(app_cliente,
                                                             empresa_a,
                                                             contrato_a):
    """`estado='solicitada'` exige `solicitado_at` (CHECK ck_cplat_solicitud)."""
    heredada = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/plataformas",
                               headers=empresa_a["headers"]).json()["items"][0]
    r = app_cliente.post(
        f"{API}/contratos/{contrato_a['id']}/plataformas/"
        f"{heredada['faena_plataforma_id']}/solicitar-acceso",
        headers=empresa_a["headers"], json={"nota": "pedido por correo"})
    assert r.status_code == 200, r.text
    salida = r.json()
    assert salida["estado"] == "solicitada"
    assert salida["solicitado_at"] is not None
    assert salida["heredadas_materializadas"] == len(PLATAFORMAS_PELAMBRES)

    r = app_cliente.patch(
        f"{API}/contratos/{contrato_a['id']}/plataformas/{salida['id']}",
        headers=empresa_a["headers"], json={"estado": "activa"})
    assert r.status_code == 200, r.text
    assert r.json()["habilitado_at"] is not None
    assert r.json()["solicitado_at"] is not None, "las marcas no se borran"

    r = app_cliente.post(
        f"{API}/contratos/{contrato_a['id']}/plataformas/{salida['id']}"
        f"/solicitar-acceso", headers=empresa_a["headers"], json={})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "PLATAFORMA_ACTIVA"


# ============================================================================
# Requisitos del contrato (§8.3)
# ============================================================================
def test_los_vinculos_incluyen_plataformas_y_los_fijos(app_cliente, empresa_a,
                                                      contrato_a):
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/vinculos",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    tipos = {i["tipo"] for i in items}
    assert tipos == {"plataforma", "arranque", "otro"}
    plataformas = {i["nombre"] for i in items if i["tipo"] == "plataforma"}
    assert plataformas == PLATAFORMAS_PELAMBRES
    # Los conteos incluyen los base proyectados: EMSIPOR tiene requisitos.
    emsipor = next(i for i in items if i["nombre"] == "EMSIPOR")
    assert emsipor["requisitos"]["emsipor"] > 0


def test_el_listado_mezcla_base_proyectados_y_personalizados(app_cliente,
                                                             empresa_a,
                                                             contrato_a):
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/requisitos"
                        f"?page_size=100", headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    cuerpo = r.json()
    assert cuerpo["kpis"]["base"] > 0
    assert cuerpo["kpis"]["personalizados"] == 0
    base = [i for i in cuerpo["items"] if i["origen"] == "base"]
    assert base and all(i["id"] is None and i["editable"] is False for i in base)

    app_cliente.post(f"{API}/contratos/{contrato_a['id']}/requisitos",
                     headers=empresa_a["headers"],
                     json={"titulo": "Plan de Izaje", "ambito": "equipo"})
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/requisitos"
                        f"?page_size=100", headers=empresa_a["headers"])
    propio = next(i for i in r.json()["items"] if i["titulo"] == "Plan de Izaje")
    assert propio["origen"] == "custom"
    assert propio["editable"] is True
    assert propio["id"] is not None


def test_requisito_duplicado_por_titulo_sin_distinguir_mayusculas(
        app_cliente, empresa_a, contrato_a):
    """El índice único usa `lower(titulo)` con NULLS NOT DISTINCT."""
    cuerpo = {"titulo": "Plan de Izaje", "ambito": "equipo",
              "vinculo_tipo": "otro"}
    r = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/requisitos",
                         headers=empresa_a["headers"], json=cuerpo)
    assert r.status_code == 201, r.text

    r = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/requisitos",
                         headers=empresa_a["headers"],
                         json={**cuerpo, "titulo": "  plan DE izaje  "})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "REQUISITO_DUPLICADO"

    # Mismo título en OTRO ámbito sí se admite: el índice incluye el ámbito.
    r = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/requisitos",
                         headers=empresa_a["headers"],
                         json={**cuerpo, "ambito": "personal"})
    assert r.status_code == 201, r.text


def test_bulk_omite_los_duplicados_en_vez_de_fallar(app_cliente, empresa_a,
                                                    contrato_a):
    """La confirmación de la Carpeta de Arranque repropone lo que ya existe."""
    app_cliente.post(f"{API}/contratos/{contrato_a['id']}/requisitos",
                     headers=empresa_a["headers"],
                     json={"titulo": "Plan de Izaje", "ambito": "equipo"})
    r = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/requisitos",
                         headers=empresa_a["headers"], json=[
                             {"titulo": "Plan de Izaje", "ambito": "equipo"},
                             {"titulo": "Plan de Rescate", "ambito": "equipo"}])
    assert r.status_code == 201, r.text
    assert r.json()["creados"] == 1
    assert r.json()["omitidos"] == ["Plan de Izaje"]


def test_cargo_id_solo_aplica_a_ambito_personal(app_cliente, empresa_a,
                                                contrato_a):
    cargo = app_cliente.post(f"{API}/cargos", headers=empresa_a["headers"],
                             json={"nombre": "Rigger"}).json()["id"]
    r = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/requisitos",
                         headers=empresa_a["headers"],
                         json={"titulo": "Curso de Izaje", "ambito": "equipo",
                               "cargo_id": cargo})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CARGO_FUERA_DE_AMBITO"

    r = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/requisitos",
                         headers=empresa_a["headers"],
                         json={"titulo": "Curso de Izaje", "ambito": "personal",
                               "cargo_id": cargo})
    assert r.status_code == 201, r.text
    assert r.json()["cargo_id"] == cargo


def test_vinculo_de_plataforma_exige_la_referencia(app_cliente, empresa_a,
                                                   contrato_a):
    r = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/requisitos",
                         headers=empresa_a["headers"],
                         json={"titulo": "X", "ambito": "empresa",
                               "vinculo_tipo": "plataforma"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VINCULO_INVALIDO"

    heredada = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/plataformas",
                               headers=empresa_a["headers"]).json()["items"][0]
    r = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/requisitos",
                         headers=empresa_a["headers"],
                         json={"titulo": "X", "ambito": "empresa",
                               "vinculo_tipo": "plataforma",
                               "vinculo_ref": heredada["faena_plataforma_id"]})
    assert r.status_code == 201, r.text
    assert r.json()["heredadas_materializadas"] == len(PLATAFORMAS_PELAMBRES)
    assert r.json()["vinculo_ref"] is not None


@pytest.fixture
def requisito_base(app_cliente, empresa_a, contrato_a) -> str:
    """Fila persistida con `origen='base'`.

    La API no crea filas base (las proyecta desde las plantillas), pero el modelo
    las admite —`origen='base'` exige `requisito_template_id`— y los endpoints
    tienen la guarda de solo lectura. Se inserta por la base con el tenant fijado
    para poder ejercitarla.
    """
    with worker_session(company_id=empresa_a["company_id"]) as db:
        template_id = db.scalars(
            db.query(RequisitoTemplate.id).filter(
                RequisitoTemplate.ambito == "empresa").limit(1).statement).first()
        r = ContratoRequisito(
            company_id=uuid.UUID(empresa_a["company_id"]),
            contrato_id=uuid.UUID(contrato_a["id"]),
            vinculo_tipo="otro", ambito="empresa", titulo="Requisito maestro",
            origen="base", requisito_template_id=template_id, activo=True)
        db.add(r)
        db.commit()
        return str(r.id)


def test_un_requisito_base_no_se_edita(app_cliente, empresa_a, contrato_a,
                                      requisito_base):
    r = app_cliente.patch(
        f"{API}/contratos/{contrato_a['id']}/requisitos/{requisito_base}",
        headers=empresa_a["headers"], json={"titulo": "Otro"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "REQUISITO_BASE"


def test_un_requisito_base_no_se_borra(app_cliente, empresa_a, contrato_a,
                                      requisito_base):
    r = app_cliente.delete(
        f"{API}/contratos/{contrato_a['id']}/requisitos/{requisito_base}",
        headers=empresa_a["headers"])
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "REQUISITO_BASE"


def test_un_requisito_custom_se_edita_y_se_borra(app_cliente, empresa_a,
                                                 contrato_a):
    rid = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/requisitos",
                           headers=empresa_a["headers"],
                           json={"titulo": "Plan de Izaje",
                                 "ambito": "equipo"}).json()["id"]
    r = app_cliente.patch(
        f"{API}/contratos/{contrato_a['id']}/requisitos/{rid}",
        headers=empresa_a["headers"], json={"titulo": "Plan de Izaje v2",
                                            "vigencia_meses": 12})
    assert r.status_code == 200, r.text
    assert r.json()["titulo"] == "Plan de Izaje v2"
    assert r.json()["vigencia_meses"] == 12

    r = app_cliente.delete(
        f"{API}/contratos/{contrato_a['id']}/requisitos/{rid}",
        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text


def test_requisito_retroactivo_instancia_documentos(app_cliente, empresa_a,
                                                    contrato_a):
    """Por defecto un requisito nuevo no toca a los sujetos ya creados."""
    app_cliente.post(f"{API}/equipos", headers=empresa_a["headers"], json={
        "contrato_id": contrato_a["id"], "patente": patente_valida(5),
        "tipo_equipo": "Camión"})

    r = app_cliente.post(f"{API}/contratos/{contrato_a['id']}/requisitos",
                         headers=empresa_a["headers"],
                         json={"titulo": "Plan de Izaje", "ambito": "equipo"})
    assert r.json()["documentos_creados"] == 0

    r = app_cliente.post(
        f"{API}/contratos/{contrato_a['id']}/requisitos?aplicar_retroactivo=true",
        headers=empresa_a["headers"],
        json={"titulo": "Plan de Rescate", "ambito": "equipo"})
    assert r.status_code == 201, r.text
    assert r.json()["documentos_creados"] == 1

    r = app_cliente.get(f"{API}/documentos?search=Plan de Rescate",
                        headers=empresa_a["headers"])
    assert r.json()["total"] == 1


# ============================================================================
# Matriz de cumplimiento (§8.4)
# ============================================================================
@pytest.fixture
def contrato_con_dotacion(app_cliente, empresa_a, contrato_a) -> dict:
    """Dos trabajadores con cargos distintos y un equipo."""
    conductor = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"],
                                 json={"contrato_id": contrato_a["id"],
                                       "nombre": "Juan Soto", "rut": rut_valido(31),
                                       "cargo": "Conductor", "es_conductor": True})
    assert conductor.status_code == 201, conductor.text
    admin = app_cliente.post(f"{API}/personal", headers=empresa_a["headers"],
                             json={"contrato_id": contrato_a["id"],
                                   "nombre": "Ana Díaz", "rut": rut_valido(32),
                                   "cargo": "Prevencionista"})
    assert admin.status_code == 201, admin.text
    equipo = app_cliente.post(f"{API}/equipos", headers=empresa_a["headers"],
                              json={"contrato_id": contrato_a["id"],
                                    "patente": patente_valida(9),
                                    "tipo_equipo": "Camión", "marca": "Volvo"})
    assert equipo.status_code == 201, equipo.text
    return {"conductor": conductor.json(), "admin": admin.json(),
            "equipo": equipo.json()}


def test_la_matriz_ordena_columnas_y_es_dispersa(app_cliente, empresa_a,
                                                contrato_a,
                                                contrato_con_dotacion):
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/matriz",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["tipo"] == "personal"
    assert m["incluir_opcionales"] is False
    assert m["total_filas"] == 2
    assert m["page"] == 1 and m["total_pages"] == 1

    # Orden determinista: ámbito, obligatorios primero, título alfabético. Se
    # compara con `normalizar` (minúsculas y sin tildes) porque «alfabético» en
    # español no es el orden de code points: con el título crudo, «IRL Mina»
    # quedaría antes de «Inducción» y «Cédula» después de «Certificado».
    orden_ambito = {"empresa": 0, "personal": 1, "equipo": 2, "emsipor": 3}
    claves = [(orden_ambito[c["ambito"]], 0 if c["obligatorio"] else 1,
               normalizar(c["titulo"])) for c in m["columnas"]]
    assert claves == sorted(claves), m["columnas"]

    n = len(m["columnas"])
    for fila in m["filas"]:
        assert 0 <= fila["cumplimiento_pct"] <= 100
        cols = [c["col"] for c in fila["celdas"]]
        assert cols == sorted(cols), "las celdas vienen ordenadas por col"
        assert all(0 <= c < n for c in cols)
        assert len(cols) == len(set(cols))
        for celda in fila["celdas"]:
            assert celda["estado_calc"] is not None, (
                "una celda emitida siempre tiene estado; el hueco se omite")
            assert celda["estado_calc"] in ("ok", "porvenc", "venc", "falta")

    # Dispersa de verdad: el conductor tiene columnas EMSIPOR que la otra no.
    por_sujeto = {f["sujeto_id"]: {c["col"] for c in f["celdas"]}
                  for f in m["filas"]}
    conductor = contrato_con_dotacion["conductor"]["id"]
    otra = contrato_con_dotacion["admin"]["id"]
    assert por_sujeto[conductor] != por_sujeto[otra]
    assert por_sujeto[conductor] > por_sujeto[otra]


def test_la_columna_n_de_la_matriz_es_la_columna_n_del_excel(
        app_cliente, empresa_a, contrato_a, contrato_con_dotacion):
    """Contrato explícito de §8.4: pantalla y exportación comparten el orden."""
    m = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/matriz"
                        f"?page_size=100", headers=empresa_a["headers"]).json()

    r = app_cliente.post(f"{API}/exportaciones", headers=empresa_a["headers"],
                         json={"recurso": "matriz", "formato": "csv",
                               "filtros": {"contrato_id": contrato_a["id"],
                                           "tipo": "personal"}})
    assert r.status_code == 200, r.text
    csv_texto = app_cliente.get(
        r.json()["download_url"][len("http://test"):]).content.decode("utf-8-sig")
    cabecera = csv_texto.splitlines()[0].split(";")

    # Las cuatro primeras columnas del fichero son la identidad del sujeto.
    assert cabecera[:4] == ["Sujeto", "RUT / Patente", "Cargo", "Cumplimiento %"]
    assert cabecera[4:] == [c["titulo"] for c in m["columnas"]]


def test_la_matriz_de_equipos_solo_lleva_equipos(app_cliente, empresa_a,
                                                 contrato_a,
                                                 contrato_con_dotacion):
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/matriz?tipo=equipo",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["total_filas"] == 1
    assert m["filas"][0]["sujeto_id"] == contrato_con_dotacion["equipo"]["id"]
    assert {c["ambito"] for c in m["columnas"]} <= {"empresa", "equipo"}


def test_la_matriz_rechaza_cargo_id_con_equipos(app_cliente, empresa_a,
                                                contrato_a):
    cargo = app_cliente.post(f"{API}/cargos", headers=empresa_a["headers"],
                             json={"nombre": "Rigger"}).json()["id"]
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/matriz"
                        f"?tipo=equipo&cargo_id={cargo}",
                        headers=empresa_a["headers"])
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "CARGO_SOLO_PERSONAL"


def test_la_matriz_pagina_por_filas(app_cliente, empresa_a, contrato_a,
                                    contrato_con_dotacion):
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/matriz?page_size=1",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    m = r.json()
    assert len(m["filas"]) == 1
    assert m["total_filas"] == 2
    assert m["total_pages"] == 2


def test_la_matriz_refleja_un_documento_aprobado(app_cliente, empresa_a,
                                                 contrato_a,
                                                 contrato_con_dotacion):
    sid = contrato_con_dotacion["admin"]["id"]
    doc = app_cliente.get(f"{API}/documentos?sujeto_id={sid}&es_emsipor=false"
                          f"&page_size=100", headers=empresa_a["headers"]
                          ).json()["items"][0]
    vence = (date.today() + timedelta(days=200)).isoformat()
    app_cliente.patch(f"{API}/documentos/{doc['id']}", headers=empresa_a["headers"],
                      json={"estado": "ok", "vence": vence})

    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/matriz",
                        headers=empresa_a["headers"])
    fila = next(f for f in r.json()["filas"] if f["sujeto_id"] == sid)
    columnas = r.json()["columnas"]
    celda = next(c for c in fila["celdas"]
                 if columnas[c["col"]]["titulo"] == doc["titulo"])
    assert celda["estado_calc"] == "ok"
    assert celda["vence"] == vence
    assert fila["cumplimiento_pct"] > 0


# ============================================================================
# Vistas agregadas del contrato
# ============================================================================
@pytest.mark.parametrize("sufijo", ["documentos", "personal", "equipos",
                                    "alertas", "historial"])
def test_las_vistas_del_contrato_usan_la_envoltura_estandar(
        app_cliente, empresa_a, contrato_a, contrato_con_dotacion, sufijo):
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}/{sufijo}",
                        headers=empresa_a["headers"])
    assert r.status_code == 200, r.text
    assert set(r.json()) >= {"items", "page", "page_size", "total", "total_pages"}


def test_stats_del_contrato(app_cliente, empresa_a, contrato_a,
                            contrato_con_dotacion):
    r = app_cliente.get(f"{API}/contratos/{contrato_a['id']}",
                        headers=empresa_a["headers"])
    stats = r.json()["stats"]
    assert stats["personal"] == {"total": 2, "acreditados": 0}
    assert stats["equipos"] == {"total": 1, "acreditados": 0}
    assert stats["docs_empresa"]["total"] > 0
    assert stats["cumplimiento_pct"] == 0
    assert stats["alertas_activas"] == 0
