"""Catálogo de faenas, plataformas del mandante y catálogos de apoyo (§6 y §7).

`faenas`, `faena_plataformas`, `requisitos_terreno`, `proveedores_catalogo`,
`doc_ejemplos` y `requisito_templates` son catálogo GLOBAL: no tienen
`company_id` y los mantiene el admin desde `/admin`. Aquí son de solo lectura.

Lo que sí es de la empresa es su **acceso** a cada plataforma del mandante, que
vive en `company_faena_plataformas` y se administra con
`PATCH /faenas/{id}/plataformas/{pid}/acceso`. Es la razón por la que el listado
de plataformas de una faena no es un catálogo plano: cada fila se devuelve con el
estado de acceso de la empresa que pregunta, y sin fila propia el estado es
`sin_acceso`.

Todas las estadísticas por faena están acotadas a los contratos de la empresa en
esa faena, y un contract_admin ve solo los de su contrato.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import TIPOS_EQUIPO
from ..database import get_db
from ..deps import (Page, aplicar_orden, contrato_scope, err, get_company_id,
                    get_current_user, paginacion, require_company, sobre)
from ..models import (
    CompanyFaenaPlataforma, Contrato, DocEjemplo, Documento, Faena,
    FaenaPlataforma, ProveedorCatalogo, RequisitoTemplate, RequisitoTerreno,
    Sujeto, User,
)
from ..services import actividad

logger = logging.getLogger("acredittia.faenas")

router = APIRouter(tags=["faenas"], dependencies=[Depends(get_current_user)])

AMBITOS_TERRENO: tuple[str, ...] = ("conductor", "equipo")
ESTADOS_ACCESO: tuple[str, ...] = ("activa", "solicitada", "sin_acceso")

ORDEN_FAENAS = {"nombre", "mandante", "grupo", "region", "sector", "created_at"}
ORDEN_EJEMPLOS = {"clave", "nombre", "referencia"}
ORDEN_PROVEEDORES = {"nombre", "localidad", "certificacion", "created_at"}
ORDEN_TEMPLATES = {"titulo", "codigo", "ambito", "tipo", "vigencia_meses"}

# Categorías de `proveedores_catalogo` expuestas cada una en su propia ruta.
CATEGORIA_LABORATORIO = "laboratorio"
CATEGORIA_TALLER = "taller"
CATEGORIA_GPS = "gps"


class AccesoIn(BaseModel):
    estado: str
    nota: str | None = None


# ============================================================================
# Helpers
# ============================================================================
def _num(v) -> float | None:
    return float(v) if v is not None else None


def _iso(v) -> str | None:
    return v.isoformat() if v else None


def _get_faena(db: Session, faena_id: uuid.UUID) -> Faena:
    f = db.get(Faena, faena_id)
    if not f:
        raise err(404, "NO_ENCONTRADO", "Faena no existe")
    return f


def _contratos_en_faena(db: Session, cid: uuid.UUID, faena_id: uuid.UUID,
                        scope: uuid.UUID | None) -> list[uuid.UUID]:
    """Contratos de la empresa en la faena; acotados al scope del contract_admin."""
    q = select(Contrato.id).where(Contrato.company_id == cid,
                                  Contrato.faena_id == faena_id)
    if scope:
        q = q.where(Contrato.id == scope)
    return list(db.scalars(q))


def _stats_faena(db: Session, cid: uuid.UUID, contratos: list[uuid.UUID]) -> dict:
    """Presencia de la empresa en la faena: dotación y cumplimiento documental."""
    base = {"contratos": len(contratos), "personal": 0, "equipos": 0,
            "personal_acreditado": 0, "equipos_acreditados": 0,
            "docs_total": 0, "docs_ok": 0, "cumplimiento_pct": 0}
    if not contratos:
        return base

    for tipo, estado, n in db.execute(
            select(Sujeto.tipo, Sujeto.estado, func.count())
            .where(Sujeto.company_id == cid,
                   Sujeto.contrato_id.in_(contratos),
                   Sujeto.estado != "baja")
            .group_by(Sujeto.tipo, Sujeto.estado)).all():
        clave = "personal" if tipo == "trabajador" else "equipos"
        base[clave] += int(n)
        if estado == "ok":
            base["personal_acreditado" if tipo == "trabajador"
                 else "equipos_acreditados"] += int(n)

    # Los documentos cuelgan del contrato (ámbito empresa) o del sujeto: hay que
    # cubrir las dos rutas. Los EMSIPOR quedan fuera, igual que en
    # `checklist.stats_sujeto`, porque son expediente aparte.
    fila = db.execute(
        select(func.count().label("total"),
               func.count().filter(Documento.estado_calc == "ok").label("ok"))
        .select_from(Documento)
        .outerjoin(Sujeto, Sujeto.id == Documento.sujeto_id)
        .where(Documento.company_id == cid,
               Documento.obligatorio.is_(True),
               Documento.es_emsipor.is_(False),
               or_(Documento.contrato_id.in_(contratos),
                   Sujeto.contrato_id.in_(contratos)))).one()
    base["docs_total"] = int(fila.total or 0)
    base["docs_ok"] = int(fila.ok or 0)
    base["cumplimiento_pct"] = (round(100 * base["docs_ok"] / base["docs_total"])
                                if base["docs_total"] else 0)
    return base


def _faena_out(f: Faena, stats: dict | None = None) -> dict:
    d = {
        "id": str(f.id), "nombre": f.nombre, "mandante": f.mandante,
        "grupo": f.grupo, "region": f.region, "sector": f.sector,
        "activa": f.activa, "logo_url": f.logo_url, "color": f.color,
        "lat": _num(f.lat), "lng": _num(f.lng),
    }
    if stats is not None:
        d["stats"] = stats
    return d


def _accesos_de(db: Session, cid: uuid.UUID, fp_ids: list[uuid.UUID]
                ) -> dict[uuid.UUID, CompanyFaenaPlataforma]:
    if not fp_ids:
        return {}
    return {a.faena_plataforma_id: a for a in db.scalars(
        select(CompanyFaenaPlataforma).where(
            CompanyFaenaPlataforma.company_id == cid,
            CompanyFaenaPlataforma.faena_plataforma_id.in_(fp_ids)))}


def _plataforma_out(p: FaenaPlataforma,
                    acceso: CompanyFaenaPlataforma | None) -> dict:
    """Plataforma del mandante con el estado de acceso de la empresa.

    Sin fila en `company_faena_plataformas` el estado es `sin_acceso`: la empresa
    todavía no lo ha pedido. Se devuelve así y no como `null` para que la UI no
    tenga que distinguir «no solicitado» de «sin datos».
    """
    return {
        "id": str(p.id), "faena_id": str(p.faena_id), "nombre": p.nombre,
        "descripcion": p.descripcion, "url": p.url, "nota": p.nota,
        "orden": p.orden,
        "acceso": {
            "estado": acceso.estado if acceso else "sin_acceso",
            "nota": acceso.nota if acceso else None,
            "solicitado_at": _iso(acceso.solicitado_at) if acceso else None,
            "habilitado_at": _iso(acceso.habilitado_at) if acceso else None,
        },
    }


def _ejemplo_out(e: DocEjemplo, completo: bool = False) -> dict:
    d = {
        "clave": e.clave, "nombre": e.nombre, "referencia": e.referencia,
        "tip": e.tip, "pdf_url": e.pdf_url,
        # En el listado no se embeben los campos ni las notas: son arrays que
        # multiplicarían el payload de la biblioteca completa. Se informa si los
        # hay para que la UI sepa si vale la pena abrir el detalle.
        "tiene_campos_clave": bool(e.campos_clave),
        "tiene_notas": bool(e.notas),
    }
    if completo:
        d["campos_clave"] = e.campos_clave
        d["notas"] = e.notas
    return d


def _proveedor_out(p: ProveedorCatalogo) -> dict:
    return {
        "id": str(p.id), "categoria": p.categoria, "nombre": p.nombre,
        "localidad": p.localidad, "certificacion": p.certificacion,
        "faena_id": str(p.faena_id) if p.faena_id else None,
    }


def _template_out(t: RequisitoTemplate) -> dict:
    return {
        "id": str(t.id), "ambito": t.ambito, "titulo": t.titulo,
        "codigo": t.codigo, "tipo": t.tipo, "obligatorio": t.obligatorio,
        "vigencia_meses": t.vigencia_meses, "plataforma": t.plataforma,
        "ejemplo_clave": t.ejemplo_clave,
        "faena_id": str(t.faena_id) if t.faena_id else None,
    }


def _listar_proveedores(db: Session, categoria: str, faena_id: uuid.UUID | None,
                        p: Page) -> dict:
    """Listado de una categoría de `proveedores_catalogo`.

    `faena_id` incluye los proveedores generales (faena_id NULL) además de los
    específicos de esa faena: un laboratorio homologado por el mandante no
    excluye a los de cobertura nacional.
    """
    q = select(ProveedorCatalogo).where(ProveedorCatalogo.categoria == categoria)
    if faena_id:
        q = q.where(or_(ProveedorCatalogo.faena_id == faena_id,
                        ProveedorCatalogo.faena_id.is_(None)))
    if p.search:
        like = f"%{p.search}%"
        q = q.where(or_(ProveedorCatalogo.nombre.ilike(like),
                        ProveedorCatalogo.localidad.ilike(like),
                        ProveedorCatalogo.certificacion.ilike(like)))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, ProveedorCatalogo, p.sort, ORDEN_PROVEEDORES, "nombre")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))
    return sobre([_proveedor_out(x) for x in filas], total, p)


# ============================================================================
# §6 — Faenas
# ============================================================================
@router.get("/faenas")
def list_faenas(grupo: str | None = Query(None),
                region: str | None = Query(None),
                sector: str | None = Query(None),
                activa: bool | None = Query(None),
                mandante: str | None = Query(None),
                p: Page = Depends(paginacion),
                db: Session = Depends(get_db),
                cid: uuid.UUID = Depends(get_company_id),
                user: User = Depends(get_current_user)):
    """Faenas del catálogo con la presencia de la empresa en cada una."""
    scope = contrato_scope(user)
    q = select(Faena)
    if grupo:
        q = q.where(Faena.grupo == grupo)
    if region:
        q = q.where(Faena.region == region)
    if sector:
        q = q.where(Faena.sector == sector)
    if mandante:
        q = q.where(Faena.mandante.ilike(f"%{mandante}%"))
    if activa is not None:
        q = q.where(Faena.activa.is_(activa))
    if p.search:
        like = f"%{p.search}%"
        q = q.where(or_(Faena.nombre.ilike(like), Faena.mandante.ilike(like),
                        Faena.region.ilike(like)))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, Faena, p.sort, ORDEN_FAENAS, "nombre")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))
    items = [_faena_out(f, _stats_faena(
        db, cid, _contratos_en_faena(db, cid, f.id, scope))) for f in filas]
    return sobre(items, total, p)


@router.get("/faenas/{faena_id}")
def detalle_faena(faena_id: uuid.UUID, db: Session = Depends(get_db),
                  cid: uuid.UUID = Depends(get_company_id),
                  user: User = Depends(get_current_user)):
    """Ficha de la faena: mandante, branding, coordenadas y plataformas.

    Las plataformas llegan con el estado de acceso de la empresa y las stats
    están acotadas a sus contratos en esta faena, de modo que la misma faena se
    vea distinta para dos contratistas.
    """
    f = _get_faena(db, faena_id)
    scope = contrato_scope(user)
    plataformas = list(db.scalars(
        select(FaenaPlataforma).where(FaenaPlataforma.faena_id == f.id)
        .order_by(FaenaPlataforma.orden, FaenaPlataforma.nombre)))
    accesos = _accesos_de(db, cid, [p.id for p in plataformas])

    out = _faena_out(f, _stats_faena(db, cid,
                                     _contratos_en_faena(db, cid, f.id, scope)))
    out["plataformas"] = [_plataforma_out(p, accesos.get(p.id))
                          for p in plataformas]
    out["requisitos_faena"] = db.scalar(
        select(func.count()).select_from(RequisitoTemplate)
        .where(RequisitoTemplate.faena_id == f.id,
               RequisitoTemplate.activo.is_(True))) or 0
    return out


@router.get("/faenas/{faena_id}/plataformas")
def plataformas(faena_id: uuid.UUID, p: Page = Depends(paginacion),
                db: Session = Depends(get_db),
                cid: uuid.UUID = Depends(get_company_id)):
    """Plataformas del mandante en la faena, con el acceso de la empresa."""
    f = _get_faena(db, faena_id)
    q = select(FaenaPlataforma).where(FaenaPlataforma.faena_id == f.id)
    if p.search:
        q = q.where(FaenaPlataforma.nombre.ilike(f"%{p.search}%"))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    filas = list(db.scalars(q.order_by(FaenaPlataforma.orden,
                                       FaenaPlataforma.nombre)
                            .offset(p.offset).limit(p.page_size)))
    accesos = _accesos_de(db, cid, [x.id for x in filas])
    return sobre([_plataforma_out(x, accesos.get(x.id)) for x in filas], total, p)


@router.patch("/faenas/{faena_id}/plataformas/{pid}/acceso")
def editar_acceso(faena_id: uuid.UUID, pid: uuid.UUID, body: AccesoIn,
                  db: Session = Depends(get_db),
                  cid: uuid.UUID = Depends(get_company_id),
                  user: User = Depends(require_company)):
    """Registra el estado de acceso de la empresa a una plataforma del mandante.

    Es un upsert sobre `company_faena_plataformas`: la primera vez crea la fila.
    Las marcas de tiempo se fijan al entrar en cada estado y NO se borran al
    salir —`solicitado_at` al pasar a `solicitada`, `habilitado_at` al pasar a
    `activa`—, porque el CHECK de `contrato_plataformas` las exige cuando el
    contrato materializa la plataforma heredada, y porque saber cuándo se pidió
    el acceso es lo que permite reclamar la demora al mandante.
    """
    if body.estado not in ESTADOS_ACCESO:
        raise err(400, "ESTADO_INVALIDO",
                  f"Estado debe ser uno de: {', '.join(ESTADOS_ACCESO)}")
    _get_faena(db, faena_id)
    fp = db.get(FaenaPlataforma, pid)
    if not fp or fp.faena_id != faena_id:
        raise err(404, "NO_ENCONTRADO", "La plataforma no existe en esta faena")

    ahora = datetime.now(timezone.utc)
    acceso = db.scalar(select(CompanyFaenaPlataforma).where(
        CompanyFaenaPlataforma.company_id == cid,
        CompanyFaenaPlataforma.faena_plataforma_id == fp.id))
    creado = acceso is None
    if creado:
        acceso = CompanyFaenaPlataforma(company_id=cid, faena_plataforma_id=fp.id)
        db.add(acceso)

    anterior = acceso.estado
    acceso.estado = body.estado
    if body.nota is not None:
        acceso.nota = body.nota
    if body.estado == "solicitada" and acceso.solicitado_at is None:
        acceso.solicitado_at = ahora
    if body.estado == "activa" and acceso.habilitado_at is None:
        acceso.habilitado_at = ahora
    acceso.updated_at = ahora

    actividad.log(db, cid, "actualizacion", "plataformas",
                  f"Acceso a '{fp.nombre}' {anterior} → {body.estado}",
                  user_id=user.id, entidad_tipo="faena_plataforma",
                  entidad_id=fp.id, plataforma=fp.nombre)
    db.commit()
    logger.info("acceso empresa=%s plataforma=%s %s→%s", cid, fp.id, anterior,
                body.estado)
    out = _plataforma_out(fp, acceso)
    out["creado"] = creado
    out["estado_anterior"] = anterior
    return out


# ============================================================================
# §7 — Catálogos de apoyo
# ============================================================================
@router.get("/catalogo/tipos-equipo")
def tipos_equipo():
    """Vocabulario cerrado de `sujetos.tipo_equipo`. No es una tabla."""
    return {"items": TIPOS_EQUIPO, "total": len(TIPOS_EQUIPO)}


@router.get("/catalogo/requisitos-templates")
def templates(ambito: str | None = Query(None),
              faena_id: uuid.UUID | None = Query(None),
              p: Page = Depends(paginacion),
              db: Session = Depends(get_db)):
    """Plantillas activas del catálogo global (lectura)."""
    q = select(RequisitoTemplate).where(RequisitoTemplate.activo.is_(True))
    if ambito:
        q = q.where(RequisitoTemplate.ambito == ambito)
    if faena_id:
        q = q.where(or_(RequisitoTemplate.faena_id == faena_id,
                        RequisitoTemplate.faena_id.is_(None)))
    if p.search:
        q = q.where(RequisitoTemplate.titulo.ilike(f"%{p.search}%"))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, RequisitoTemplate, p.sort, ORDEN_TEMPLATES, "titulo")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))
    return sobre([_template_out(t) for t in filas], total, p)


@router.get("/catalogo/ejemplos")
def ejemplos(p: Page = Depends(paginacion), db: Session = Depends(get_db)):
    """Biblioteca de ejemplos documentales (§7.2).

    Es el material de apoyo que la UI enseña junto a cada requisito: qué es el
    documento, en qué norma se basa y qué campos revisa el mandante.
    """
    q = select(DocEjemplo)
    if p.search:
        like = f"%{p.search}%"
        q = q.where(or_(DocEjemplo.nombre.ilike(like),
                        DocEjemplo.clave.ilike(like),
                        DocEjemplo.referencia.ilike(like)))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, DocEjemplo, p.sort, ORDEN_EJEMPLOS, "nombre")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))
    return sobre([_ejemplo_out(e) for e in filas], total, p)


@router.get("/catalogo/ejemplos/{clave}")
def ejemplo(clave: str, db: Session = Depends(get_db)):
    e = db.get(DocEjemplo, clave)
    if not e:
        raise err(404, "NO_ENCONTRADO", "Ejemplo no existe")
    return _ejemplo_out(e, completo=True)


@router.get("/catalogo/requisitos-terreno")
def requisitos_terreno(ambito: str | None = Query(None),
                       nivel: str | None = Query(None),
                       db: Session = Depends(get_db)):
    """Requisitos NO documentales que se verifican en terreno (Reglamento MLP).

    No se pagina: es un conjunto cerrado de pocas decenas de filas que la UI
    muestra completo como checklist previo al ingreso a la faena. Devolverlo
    troceado obligaría al frontend a recomponerlo para nada.
    """
    if ambito and ambito not in AMBITOS_TERRENO:
        raise err(400, "AMBITO_INVALIDO", "Ámbito debe ser 'conductor' o 'equipo'")
    q = select(RequisitoTerreno)
    if ambito:
        q = q.where(RequisitoTerreno.ambito == ambito)
    if nivel:
        q = q.where(RequisitoTerreno.nivel == nivel)
    filas = list(db.scalars(q.order_by(RequisitoTerreno.ambito,
                                       RequisitoTerreno.nivel,
                                       RequisitoTerreno.titulo)))
    items = [{
        "id": str(r.id), "ambito": r.ambito, "titulo": r.titulo,
        "descripcion": r.descripcion, "nivel": r.nivel, "icono": r.icono,
        "referencia": r.referencia,
    } for r in filas]
    return {"items": items, "total": len(items)}


@router.get("/catalogo/laboratorios")
def laboratorios(faena_id: uuid.UUID | None = Query(None),
                 p: Page = Depends(paginacion),
                 db: Session = Depends(get_db)):
    """Laboratorios y centros médicos homologados para exámenes de altura."""
    return _listar_proveedores(db, CATEGORIA_LABORATORIO, faena_id, p)


@router.get("/catalogo/talleres")
def talleres(faena_id: uuid.UUID | None = Query(None),
             p: Page = Depends(paginacion),
             db: Session = Depends(get_db)):
    """Talleres homologados para mantenciones y certificados de operatividad."""
    return _listar_proveedores(db, CATEGORIA_TALLER, faena_id, p)


@router.get("/catalogo/proveedores-gps")
def proveedores_gps(faena_id: uuid.UUID | None = Query(None),
                    p: Page = Depends(paginacion),
                    db: Session = Depends(get_db)):
    """Proveedores GPS aceptados por el mandante (multiflota)."""
    return _listar_proveedores(db, CATEGORIA_GPS, faena_id, p)
