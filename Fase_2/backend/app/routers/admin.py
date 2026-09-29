"""Back-office de Acredittia: empresas, catálogo global y datos demo (§5).

Todo el módulo exige rol `admin` (dependencia del router). El admin NO opera con
`X-Company-Id` aquí: estos endpoints administran el catálogo compartido —faenas,
plataformas del mandante, plantillas de requisitos— y el ciclo de vida de las
cuentas, no los datos de una empresa concreta.

Dos cosas que conviene tener presentes:

* **Las plantillas se borran en blando.** `DELETE /admin/requisitos/templates/{id}`
  pone `activo=false`: los `documentos` ya instanciados apuntan a la plantilla y
  siguen existiendo, con su historial y sus archivos. Desactivar significa «no
  volver a instanciarla», nunca «borrar lo instanciado».
* **`reset-demo` es irreversible y solo aplica a `companies.es_demo`.** Borra los
  datos de operación de la empresa demo y vuelve a sembrarla. La tabla
  `actividad` es append-only por trigger (SQLSTATE 55000) y por eso NO se borra:
  el rastro de auditoría sobrevive al reset a propósito.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import (Page, aplicar_orden, err, paginacion, require_admin, sobre)
from ..models import (
    Alerta, Company, CompanyFaenaPlataforma, Contrato, ContratoPlantillaOverride,
    ContratoPlataforma, ContratoRequisito, CumplimientoSnapshot, DocEjemplo,
    Documento, DocumentoArchivo, EventoCalendario, Faena, FaenaPlataforma,
    IaHallazgo, IaReview, LicenciaInterna, PlataformaCredencial,
    RequisitoTemplate, Sujeto, User,
)
from ..services import actividad
from ..services.checklist import (calc_estado_doc, calc_estado_sujeto,
                                  crear_expediente_conductor, instanciar_docs,
                                  vencimiento_por_plantilla)
from ..services.storage import get_storage

logger = logging.getLogger("acredittia.admin")

router = APIRouter(prefix="/admin", tags=["admin"],
                   dependencies=[Depends(require_admin)])

AMBITOS: tuple[str, ...] = ("empresa", "personal", "equipo", "emsipor")
TIPOS_REQ: tuple[str, ...] = ("legal", "medico", "capacitacion", "certificacion",
                              "tecnico", "medioambiental")
ESTADOS_CALC: tuple[str, ...] = ("ok", "porvenc", "venc", "falta")

ORDEN_COMPANIES = {"nombre", "rut", "status", "created_at", "updated_at"}
ORDEN_TEMPLATES = {"titulo", "codigo", "ambito", "tipo", "vigencia_meses",
                   "created_at", "updated_at"}

NOTA_SOFT_DELETE = (
    "La plantilla queda inactiva: no se instanciará en altas futuras. Los "
    "documentos ya creados a partir de ella NO se modifican ni se borran.")


# ============================================================================
# Entradas
# ============================================================================
class RejectIn(BaseModel):
    reason: str


class FaenaIn(BaseModel):
    nombre: str
    mandante: str
    grupo: str | None = None
    region: str | None = None
    sector: str = "mineria"
    activa: bool = True
    logo_url: str | None = None
    color: str | None = None
    lat: float | None = None
    lng: float | None = None


class FaenaPatch(BaseModel):
    nombre: str | None = None
    mandante: str | None = None
    grupo: str | None = None
    region: str | None = None
    sector: str | None = None
    activa: bool | None = None
    logo_url: str | None = None
    color: str | None = None
    lat: float | None = None
    lng: float | None = None


class PlataformaIn(BaseModel):
    nombre: str
    descripcion: str | None = None
    url: str | None = None
    nota: str | None = None
    orden: int = 0


class TemplateIn(BaseModel):
    ambito: str
    titulo: str
    codigo: str | None = None
    tipo: str | None = None
    obligatorio: bool = True
    ejemplo_clave: str | None = None
    faena_id: uuid.UUID | None = None
    vigencia_meses: int | None = None
    plataforma: str | None = None
    aplica_a: str | None = None
    activo: bool = True


class TemplatePatch(BaseModel):
    ambito: str | None = None
    titulo: str | None = None
    codigo: str | None = None
    tipo: str | None = None
    obligatorio: bool | None = None
    ejemplo_clave: str | None = None
    faena_id: uuid.UUID | None = None
    vigencia_meses: int | None = None
    plataforma: str | None = None
    aplica_a: str | None = None
    activo: bool | None = None


# ============================================================================
# Helpers
# ============================================================================
def _iso(v) -> str | None:
    return v.isoformat() if v else None


def _num(v) -> float | None:
    return float(v) if v is not None else None


def _get_company(db: Session, company_id: uuid.UUID) -> Company:
    c = db.get(Company, company_id)
    if not c:
        raise err(404, "NO_ENCONTRADO", "Empresa no existe")
    return c


def _get_faena(db: Session, faena_id: uuid.UUID) -> Faena:
    f = db.get(Faena, faena_id)
    if not f:
        raise err(404, "NO_ENCONTRADO", "Faena no existe")
    return f


def _get_template(db: Session, tid: uuid.UUID) -> RequisitoTemplate:
    t = db.get(RequisitoTemplate, tid)
    if not t:
        raise err(404, "NO_ENCONTRADO", "Plantilla no existe")
    return t


def _cuenta(db: Session, modelo, *filtros) -> int:
    return db.scalar(select(func.count()).select_from(modelo).where(*filtros)) or 0


def _docs_por_estado(db: Session, company_id: uuid.UUID) -> dict:
    """Documentos de la empresa agrupados por `estado_calc`."""
    filas = db.execute(
        select(Documento.estado_calc, func.count())
        .where(Documento.company_id == company_id)
        .group_by(Documento.estado_calc)).all()
    conteo = {e: 0 for e in ESTADOS_CALC}
    for estado, n in filas:
        conteo[estado] = int(n)
    conteo["total"] = sum(conteo[e] for e in ESTADOS_CALC)
    return conteo


def _sujetos_por_tipo(db: Session, company_id: uuid.UUID | None = None) -> dict:
    """Personal y equipos activos (sin bajas). Sin `company_id` es global."""
    q = (select(Sujeto.tipo, Sujeto.estado, func.count())
         .where(Sujeto.estado != "baja").group_by(Sujeto.tipo, Sujeto.estado))
    if company_id:
        q = q.where(Sujeto.company_id == company_id)
    salida = {"trabajador": {"total": 0, "acreditados": 0},
              "equipo": {"total": 0, "acreditados": 0}}
    for tipo, estado, n in db.execute(q).all():
        if tipo not in salida:
            continue
        salida[tipo]["total"] += int(n)
        if estado == "ok":
            salida[tipo]["acreditados"] += int(n)
    return salida


def _company_out(db: Session, c: Company, contratos: int | None = None) -> dict:
    # `_cumplimiento_empresa` vive en routers/company.py y es el único cálculo
    # de cumplimiento de la empresa: se importa para que el back-office y el
    # panel de la empresa no puedan divergir.
    from .company import _cumplimiento_empresa

    m = _cumplimiento_empresa(db, c.id, None)
    return {
        "id": str(c.id), "nombre": c.nombre, "rut": c.rut, "email": c.email,
        "status": c.status, "es_demo": c.es_demo,
        "rejection_reason": c.rejection_reason,
        "approved_at": _iso(c.approved_at),
        "created_at": _iso(c.created_at),
        "contratos": (contratos if contratos is not None
                      else _cuenta(db, Contrato, Contrato.company_id == c.id)),
        "cumplimiento_pct": m["cumplimiento_pct"],
        "docs_ok": m["docs_ok"], "docs_total": m["docs_total"],
    }


def _faena_out(f: Faena, plataformas: int | None = None) -> dict:
    d = {
        "id": str(f.id), "nombre": f.nombre, "mandante": f.mandante,
        "grupo": f.grupo, "region": f.region, "sector": f.sector,
        "activa": f.activa, "logo_url": f.logo_url, "color": f.color,
        "lat": _num(f.lat), "lng": _num(f.lng),
        "created_at": _iso(f.created_at),
    }
    if plataformas is not None:
        d["plataformas"] = plataformas
    return d


def _plataforma_out(p: FaenaPlataforma) -> dict:
    return {
        "id": str(p.id), "faena_id": str(p.faena_id), "nombre": p.nombre,
        "descripcion": p.descripcion, "url": p.url, "nota": p.nota,
        "orden": p.orden,
    }


def _template_out(t: RequisitoTemplate) -> dict:
    return {
        "id": str(t.id), "ambito": t.ambito, "titulo": t.titulo,
        "codigo": t.codigo, "tipo": t.tipo, "obligatorio": t.obligatorio,
        "ejemplo_clave": t.ejemplo_clave,
        "faena_id": str(t.faena_id) if t.faena_id else None,
        "es_estandar": t.faena_id is None,
        "vigencia_meses": t.vigencia_meses, "plataforma": t.plataforma,
        "aplica_a": t.aplica_a, "activo": t.activo,
        "created_at": _iso(t.created_at), "updated_at": _iso(t.updated_at),
    }


def _valida_template(db: Session, ambito: str | None, tipo: str | None,
                     ejemplo_clave: str | None, faena_id: uuid.UUID | None) -> None:
    """Valida antes de tocar la BD: los ENUM y las FK del catálogo dan 500 si no."""
    if ambito is not None and ambito not in AMBITOS:
        raise err(400, "AMBITO_INVALIDO",
                  f"Ámbito debe ser uno de: {', '.join(AMBITOS)}")
    if tipo is not None and tipo not in TIPOS_REQ:
        raise err(400, "TIPO_INVALIDO",
                  f"Tipo debe ser uno de: {', '.join(TIPOS_REQ)}")
    if ejemplo_clave is not None and not db.get(DocEjemplo, ejemplo_clave):
        raise err(400, "EJEMPLO_INEXISTENTE",
                  f"No existe el ejemplo documental '{ejemplo_clave}'")
    if faena_id is not None:
        _get_faena(db, faena_id)


# ============================================================================
# §5.1 — Empresas
# ============================================================================
@router.get("/companies")
def list_companies(status: str | None = Query(None),
                   es_demo: bool | None = Query(None),
                   p: Page = Depends(paginacion),
                   db: Session = Depends(get_db)):
    """Solicitudes y cuentas de empresa, la más reciente primero."""
    q = select(Company)
    if status:
        q = q.where(Company.status == status)
    if es_demo is not None:
        q = q.where(Company.es_demo.is_(es_demo))
    if p.search:
        like = f"%{p.search}%"
        q = q.where(or_(Company.nombre.ilike(like), Company.rut.ilike(like),
                        Company.email.ilike(like)))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, Company, p.sort, ORDEN_COMPANIES, "-created_at")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))
    return sobre([_company_out(db, c) for c in filas], total, p)


@router.get("/companies/{company_id}")
def detalle_company(company_id: uuid.UUID, db: Session = Depends(get_db)):
    """Ficha de una empresa con sus estadísticas de operación.

    Es la vista que usa el back-office para decidir sobre una cuenta: cuántos
    usuarios y contratos tiene, cuánta gente y cuántos equipos ha cargado y en
    qué estado está su documentación.
    """
    c = _get_company(db, company_id)
    sujetos = _sujetos_por_tipo(db, c.id)
    out = _company_out(db, c)
    out["stats"] = {
        "usuarios": _cuenta(db, User, User.company_id == c.id),
        "usuarios_activos": _cuenta(db, User, User.company_id == c.id,
                                    User.activo.is_(True)),
        "contratos": _cuenta(db, Contrato, Contrato.company_id == c.id),
        "contratos_vigentes": _cuenta(db, Contrato, Contrato.company_id == c.id,
                                      Contrato.estado == "vigente"),
        "personal": sujetos["trabajador"],
        "equipos": sujetos["equipo"],
        "documentos": _docs_por_estado(db, c.id),
        "alertas_activas": _cuenta(db, Alerta, Alerta.company_id == c.id,
                                   Alerta.resuelta_at.is_(None)),
        "cumplimiento_pct": out["cumplimiento_pct"],
    }
    return out


def _set_status(db: Session, company_id: uuid.UUID, admin: User,
                status: str, reason: str | None = None) -> Company:
    c = _get_company(db, company_id)
    c.status = status
    c.approved_by = admin.id
    c.approved_at = datetime.now(timezone.utc)
    c.rejection_reason = reason
    for u in db.scalars(select(User).where(User.company_id == c.id)):
        u.status = status
    actividad.log(db, c.id, "actualizacion", "empresa",
                  f"Cuenta {status} por administrador", user_id=admin.id,
                  entidad_tipo="empresa", entidad_id=c.id)
    db.commit()
    return c


@router.post("/companies/{company_id}/approve")
def approve(company_id: uuid.UUID, db: Session = Depends(get_db),
            admin: User = Depends(require_admin)):
    c = _set_status(db, company_id, admin, "approved")
    return {"id": str(c.id), "status": c.status}


@router.post("/companies/{company_id}/reject")
def reject(company_id: uuid.UUID, body: RejectIn, db: Session = Depends(get_db),
           admin: User = Depends(require_admin)):
    c = _set_status(db, company_id, admin, "rejected", body.reason)
    return {"id": str(c.id), "status": c.status}


# ============================================================================
# §5.2 — Catálogo de faenas y plataformas del mandante
# ============================================================================
@router.post("/faenas", status_code=201)
def crear_faena(body: FaenaIn, db: Session = Depends(get_db),
                admin: User = Depends(require_admin)):
    """Da de alta una faena en el catálogo global.

    `faenas.nombre` es único en toda la plataforma: la faena identifica al
    mandante y sus requisitos, y dos filas con el mismo nombre partirían en dos
    la plantilla efectiva de los contratos.
    """
    nombre = body.nombre.strip()
    if db.scalar(select(Faena.id).where(func.lower(Faena.nombre) == nombre.lower())):
        raise err(409, "FAENA_DUPLICADA", "Ya existe una faena con ese nombre")
    f = Faena(**{**body.model_dump(), "nombre": nombre})
    db.add(f)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "FAENA_DUPLICADA", "Ya existe una faena con ese nombre")
    # La faena es catálogo global y no tiene company_id: la auditoría se registra
    # bajo la empresa del admin si la tiene, y si no (admin de plataforma) queda
    # solo en el log de aplicación, porque `actividad.company_id` es NOT NULL.
    logger.info("faena creada %s (%s) por admin=%s", f.id, f.nombre, admin.id)
    db.commit()
    return _faena_out(f, plataformas=0)


@router.patch("/faenas/{faena_id}")
def editar_faena(faena_id: uuid.UUID, body: FaenaPatch,
                 db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    """Edita los datos y el branding de una faena del catálogo."""
    f = _get_faena(db, faena_id)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a modificar")
    nuevo = (data.get("nombre") or "").strip()
    if nuevo and nuevo.lower() != f.nombre.lower():
        if db.scalar(select(Faena.id).where(
                func.lower(Faena.nombre) == nuevo.lower(), Faena.id != f.id)):
            raise err(409, "FAENA_DUPLICADA", "Ya existe una faena con ese nombre")
        data["nombre"] = nuevo
    for k, v in data.items():
        setattr(f, k, v)
    f.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise err(409, "FAENA_DUPLICADA", "Ya existe una faena con ese nombre")
    logger.info("faena %s actualizada por admin=%s (%s)", f.id, admin.id,
                ", ".join(sorted(data)))
    return _faena_out(f, plataformas=_cuenta(db, FaenaPlataforma,
                                             FaenaPlataforma.faena_id == f.id))


@router.post("/faenas/{faena_id}/plataformas", status_code=201)
def crear_plataforma_faena(faena_id: uuid.UUID, body: PlataformaIn,
                           db: Session = Depends(get_db),
                           admin: User = Depends(require_admin)):
    """Agrega una plataforma del mandante a la faena (SIGA, DIRECTIC, …).

    De aquí las heredan los contratos: `contrato_plataformas` copia estas filas
    la primera vez que una empresa personaliza su lista (§8.1). El nombre es
    único dentro de la faena porque es la clave con la que el contrato empareja
    la plataforma heredada.
    """
    f = _get_faena(db, faena_id)
    nombre = body.nombre.strip()
    if not nombre:
        raise err(400, "NOMBRE_REQUERIDO", "El nombre de la plataforma es obligatorio")
    if db.scalar(select(FaenaPlataforma.id).where(
            FaenaPlataforma.faena_id == f.id,
            func.lower(FaenaPlataforma.nombre) == nombre.lower())):
        raise err(409, "PLATAFORMA_DUPLICADA",
                  f"'{nombre}' ya está registrada en {f.nombre}")
    p = FaenaPlataforma(faena_id=f.id, nombre=nombre,
                        descripcion=body.descripcion, url=body.url,
                        nota=body.nota, orden=body.orden)
    db.add(p)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise err(409, "PLATAFORMA_DUPLICADA",
                  f"'{nombre}' ya está registrada en {f.nombre}")
    logger.info("plataforma '%s' añadida a faena %s por admin=%s", nombre, f.id,
                admin.id)
    return _plataforma_out(p)


# ============================================================================
# §5.3 — Plantillas de requisitos (catálogo global)
# ============================================================================
@router.get("/requisitos/templates")
def listar_templates(ambito: str | None = Query(None),
                     faena_id: uuid.UUID | None = Query(None),
                     activo: bool | None = Query(None),
                     p: Page = Depends(paginacion),
                     db: Session = Depends(get_db)):
    """Catálogo completo de plantillas, incluidas las inactivas.

    Es la vista de administración: a diferencia de `GET /requisitos/templates`
    (que devuelve solo lo aplicable a una empresa y solo lo activo), aquí se ven
    también las plantillas desactivadas para poder reactivarlas.
    """
    if ambito and ambito not in AMBITOS:
        raise err(400, "AMBITO_INVALIDO",
                  f"Ámbito debe ser uno de: {', '.join(AMBITOS)}")
    q = select(RequisitoTemplate)
    if ambito:
        q = q.where(RequisitoTemplate.ambito == ambito)
    if faena_id:
        q = q.where(RequisitoTemplate.faena_id == faena_id)
    if activo is not None:
        q = q.where(RequisitoTemplate.activo.is_(activo))
    if p.search:
        like = f"%{p.search}%"
        q = q.where(or_(RequisitoTemplate.titulo.ilike(like),
                        RequisitoTemplate.codigo.ilike(like)))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, RequisitoTemplate, p.sort, ORDEN_TEMPLATES, "titulo")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))
    return sobre([_template_out(t) for t in filas], total, p)


@router.post("/requisitos/templates", status_code=201)
def crear_template(body: TemplateIn, db: Session = Depends(get_db),
                   admin: User = Depends(require_admin)):
    """Crea una plantilla de requisito.

    Con `faena_id` la plantilla es un override de esa faena y prevalece por
    título sobre la estándar homónima al instanciar checklists (§8.3); sin
    `faena_id` es estándar y aplica a todos los contratos.
    """
    _valida_template(db, body.ambito, body.tipo, body.ejemplo_clave, body.faena_id)
    titulo = body.titulo.strip()
    if not titulo:
        raise err(400, "TITULO_REQUERIDO", "El título es obligatorio")
    codigo = (body.codigo or "").strip() or None
    if codigo and db.scalar(select(RequisitoTemplate.id).where(
            RequisitoTemplate.codigo == codigo)):
        raise err(409, "CODIGO_DUPLICADO", f"El código '{codigo}' ya existe")
    t = RequisitoTemplate(**{**body.model_dump(), "titulo": titulo,
                             "codigo": codigo})
    db.add(t)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise err(409, "CODIGO_DUPLICADO", f"El código '{codigo}' ya existe")
    logger.info("plantilla %s '%s' creada por admin=%s", t.id, t.titulo, admin.id)
    return _template_out(t)


@router.patch("/requisitos/templates/{template_id}")
def editar_template(template_id: uuid.UUID, body: TemplatePatch,
                    db: Session = Depends(get_db),
                    admin: User = Depends(require_admin)):
    """Edita una plantilla. Los cambios NO se propagan a lo ya instanciado.

    Cambiar el título o la vigencia afecta a los documentos que se creen desde
    ahora; los existentes conservan el título con el que se instanciaron, que es
    el que el mandante ya vio en la plataforma.
    """
    t = _get_template(db, template_id)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a modificar")
    _valida_template(db, data.get("ambito"), data.get("tipo"),
                     data.get("ejemplo_clave"), data.get("faena_id"))
    if "titulo" in data:
        data["titulo"] = (data["titulo"] or "").strip()
        if not data["titulo"]:
            raise err(400, "TITULO_REQUERIDO", "El título es obligatorio")
    if "codigo" in data:
        codigo = (data["codigo"] or "").strip() or None
        if codigo and db.scalar(select(RequisitoTemplate.id).where(
                RequisitoTemplate.codigo == codigo,
                RequisitoTemplate.id != t.id)):
            raise err(409, "CODIGO_DUPLICADO", f"El código '{codigo}' ya existe")
        data["codigo"] = codigo
    for k, v in data.items():
        setattr(t, k, v)
    t.updated_at = datetime.now(timezone.utc)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise err(409, "CODIGO_DUPLICADO", "El código ya existe")
    logger.info("plantilla %s actualizada por admin=%s (%s)", t.id, admin.id,
                ", ".join(sorted(data)))
    out = _template_out(t)
    out["documentos_instanciados"] = _cuenta(db, Documento,
                                             Documento.template_id == t.id)
    out["nota"] = ("Los documentos ya instanciados conservan el título y la "
                   "obligatoriedad con los que se crearon.")
    return out


@router.delete("/requisitos/templates/{template_id}")
def eliminar_template(template_id: uuid.UUID, db: Session = Depends(get_db),
                      admin: User = Depends(require_admin)):
    """Desactiva la plantilla (borrado en blando). Nunca borra documentos."""
    t = _get_template(db, template_id)
    instanciados = _cuenta(db, Documento, Documento.template_id == t.id)
    if not t.activo:
        return {"ok": True, "id": str(t.id), "activo": False,
                "documentos_instanciados": instanciados,
                "ya_estaba_inactiva": True, "nota": NOTA_SOFT_DELETE}
    t.activo = False
    t.updated_at = datetime.now(timezone.utc)
    db.commit()
    logger.info("plantilla %s desactivada por admin=%s (%s documentos vivos)",
                t.id, admin.id, instanciados)
    return {"ok": True, "id": str(t.id), "titulo": t.titulo, "activo": False,
            "documentos_instanciados": instanciados,
            "ya_estaba_inactiva": False, "nota": NOTA_SOFT_DELETE}


# ============================================================================
# §5.4 — Estadísticas globales
# ============================================================================
@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    """KPI de la plataforma.

    `cumplimiento_promedio` es la media SIMPLE del cumplimiento de las empresas
    aprobadas que tienen al menos un documento obligatorio: mide cómo le va a
    una empresa típica. `cumplimiento_global_pct` es la media ponderada por
    documentos y la domina la empresa más grande; se devuelven las dos porque
    responden preguntas distintas.
    """
    total = _cuenta(db, Company)
    pend = _cuenta(db, Company, Company.status == "pending")
    aprob = _cuenta(db, Company, Company.status == "approved")
    rech = _cuenta(db, Company, Company.status == "rejected")

    # Cumplimiento por empresa en una sola pasada: documentos obligatorios no
    # EMSIPOR, que es la misma base que usa `_cumplimiento_empresa`.
    filas = db.execute(
        select(Documento.company_id,
               func.count().label("total"),
               func.count().filter(Documento.estado_calc == "ok").label("ok"))
        .select_from(Documento)
        .join(Company, Company.id == Documento.company_id)
        .where(Documento.obligatorio.is_(True),
               Documento.es_emsipor.is_(False),
               Company.status == "approved")
        .group_by(Documento.company_id)).all()
    pcts = [round(100 * int(f.ok) / int(f.total)) for f in filas if int(f.total)]
    docs_total = sum(int(f.total) for f in filas)
    docs_ok = sum(int(f.ok) for f in filas)

    sujetos = _sujetos_por_tipo(db)
    return {
        "empresas_total": total, "pendientes": pend, "aprobadas": aprob,
        "rechazadas": rech,
        "contratos_total": _cuenta(db, Contrato),
        "faenas_total": _cuenta(db, Faena),
        "faenas_activas": _cuenta(db, Faena, Faena.activa.is_(True)),
        "plantillas_activas": _cuenta(db, RequisitoTemplate,
                                      RequisitoTemplate.activo.is_(True)),
        "personal": sujetos["trabajador"],
        "equipos": sujetos["equipo"],
        # Se conserva por compatibilidad con la versión anterior del endpoint.
        "sujetos_total": sujetos["trabajador"]["total"] + sujetos["equipo"]["total"],
        "documentos_total": docs_total, "documentos_ok": docs_ok,
        "cumplimiento_promedio": round(sum(pcts) / len(pcts)) if pcts else 0,
        "cumplimiento_global_pct": (round(100 * docs_ok / docs_total)
                                    if docs_total else 0),
        "empresas_medidas": len(pcts),
    }


# ============================================================================
# §5.5 — Reinicio de la empresa demo
# ============================================================================
# Dotación de la empresa demo. RUT y patente se generan al sembrar para que sean
# válidos y estables; el cargo va como texto porque `checklist.resolver_cargo`
# es quien decide si crearlo en el catálogo de la empresa.
DEMO_CONTRATOS = [
    ("Transporte de Personal Mina", "CTR-DEMO-001", "Los Pelambres"),
    ("Servicios de Mantención Planta", "CTR-DEMO-002", "Minera Centinela"),
]
DEMO_PERSONAL = [
    ("Juan Pérez Soto", "Conductor Nacional", True),
    ("María González Rojas", "Supervisora de Terreno", False),
    ("Carlos Muñoz Araya", "Conductor Nacional", True),
    ("Patricia Silva Contreras", "Prevencionista de Riesgos", False),
    ("Luis Fuentes Cárdenas", "Operador de Equipo Pesado", False),
    ("Rodrigo Tapia Núñez", "Mecánico Mantenedor", False),
]
DEMO_EQUIPOS = [
    ("Tracto-Camión", "Mercedes-Benz", "Actros 2646", 2021),
    ("Camioneta", "Toyota", "Hilux 4x4", 2022),
    ("Camión Aljibe", "Volvo", "FM 440", 2019),
    ("Retroexcavadora", "Caterpillar", "420F", 2020),
]

# 1 de cada 3 documentos se deja en `falta` para que la demo tenga pendientes:
# una empresa al 100 % no permite probar alertas ni la matriz de cumplimiento.
DEMO_UNO_DE_CADA = 3


def _dv_rut(cuerpo: str) -> str:
    """Dígito verificador módulo 11 (mismo algoritmo que `security.validar_rut`)."""
    suma, factor = 0, 2
    for c in reversed(cuerpo):
        suma += int(c) * factor
        factor = 2 if factor == 7 else factor + 1
    resto = 11 - (suma % 11)
    return "0" if resto == 11 else "K" if resto == 10 else str(resto)


def _rut_demo(n: int) -> str:
    return f"{n:,}".replace(",", ".") + "-" + _dv_rut(str(n))


def _patente_demo(i: int) -> str:
    letras = "BCDFGHJKLPRSTVWXYZ"
    base = "".join(letras[(i * 5 + k) % len(letras)] for k in range(4))
    return f"{base}{10 + i:02d}"


def _borrar_datos_empresa(db: Session, cid: uuid.UUID) -> dict:
    """Borra los datos de operación de la empresa. Devuelve los conteos.

    No toca `companies`, `users` de empresa ni `suscripciones`: el reset devuelve
    la demo a su estado inicial de datos, no da de baja la cuenta. Tampoco toca
    `actividad`, que es append-only por trigger (SQLSTATE 55000): el rastro de
    auditoría sobrevive al reset a propósito.
    """
    # Los blobs se borran antes que las filas: sin `blob_path` no hay forma de
    # localizar el archivo en el storage y quedaría huérfano para siempre.
    archivos = list(db.scalars(select(DocumentoArchivo).where(
        DocumentoArchivo.company_id == cid)))
    storage = get_storage()
    blobs = 0
    for a in archivos:
        try:
            storage.delete(a.blob_path)
            blobs += 1
        except Exception:                                   # noqa: BLE001
            logger.warning("no se pudo borrar el blob %s", a.blob_path)

    contratos = list(db.scalars(select(Contrato.id).where(
        Contrato.company_id == cid)))
    revisiones = list(db.scalars(select(IaReview.id).where(
        IaReview.company_id == cid)))

    conteos: dict[str, int] = {"archivos": len(archivos), "blobs": blobs}

    def borrar(clave: str, modelo, *filtros) -> None:
        conteos[clave] = db.execute(
            delete(modelo).where(*filtros)).rowcount or 0

    if revisiones:
        borrar("ia_hallazgos", IaHallazgo, IaHallazgo.review_id.in_(revisiones))
    else:
        conteos["ia_hallazgos"] = 0
    borrar("ia_reviews", IaReview, IaReview.company_id == cid)
    borrar("eventos_calendario", EventoCalendario,
           EventoCalendario.company_id == cid)
    borrar("alertas", Alerta, Alerta.company_id == cid)
    borrar("licencias_internas", LicenciaInterna, LicenciaInterna.company_id == cid)
    borrar("documento_archivos", DocumentoArchivo,
           DocumentoArchivo.company_id == cid)
    borrar("documentos", Documento, Documento.company_id == cid)
    borrar("sujetos", Sujeto, Sujeto.company_id == cid)
    borrar("credenciales", PlataformaCredencial,
           PlataformaCredencial.company_id == cid)
    borrar("contrato_plataformas", ContratoPlataforma,
           ContratoPlataforma.company_id == cid)
    borrar("contrato_requisitos", ContratoRequisito,
           ContratoRequisito.company_id == cid)
    borrar("plantilla_overrides", ContratoPlantillaOverride,
           ContratoPlantillaOverride.company_id == cid)
    borrar("company_faena_plataformas", CompanyFaenaPlataforma,
           CompanyFaenaPlataforma.company_id == cid)
    borrar("snapshots", CumplimientoSnapshot, CumplimientoSnapshot.company_id == cid)
    # `users.contrato_id` cae por CASCADE al borrar el contrato, así que los
    # administradores de contrato se borran explícitamente y se informan: de otro
    # modo desaparecerían sin dejar constancia en la respuesta.
    if contratos:
        borrar("usuarios_contract_admin", User, User.company_id == cid,
               User.contrato_id.in_(contratos))
    else:
        conteos["usuarios_contract_admin"] = 0
    borrar("contratos", Contrato, Contrato.company_id == cid)
    db.flush()
    return conteos


def _sembrar_demo(db: Session, company: Company, user_id: uuid.UUID) -> dict:
    """Vuelve a crear la dotación de la empresa demo.

    Las faenas se buscan por nombre en el catálogo que siembra `app/seeds.py`, y
    los checklists se instancian con `checklist.instanciar_docs` y
    `crear_expediente_conductor`: la demo se construye exactamente con las mismas
    reglas que un alta real, no con inserciones a mano. Deja parte de los
    documentos en `falta` para que la demo tenga pendientes que mostrar.
    """
    from ..services.checklist import resolver_cargo

    nombres_faena = [c[2] for c in DEMO_CONTRATOS]
    faenas = {f.nombre: f for f in db.scalars(select(Faena).where(
        Faena.nombre.in_(nombres_faena)))}
    if not faenas:
        # El catálogo lo siembra `seeds.run` al arrancar; sin faenas no hay nada
        # a lo que colgar un contrato.
        raise err(409, "CATALOGO_VACIO",
                  "El catálogo de faenas está vacío; siembre los datos base antes")

    hoy = date.today()
    creados = {"contratos": 0, "personal": 0, "equipos": 0, "documentos": 0,
               "expedientes_emsipor": 0, "cargos": 0}
    contratos: list[Contrato] = []

    for nombre, codigo, faena_nombre in DEMO_CONTRATOS:
        faena = faenas.get(faena_nombre) or next(iter(faenas.values()))
        c = Contrato(company_id=company.id, faena_id=faena.id, nombre=nombre,
                     codigo=codigo, fecha_inicio=hoy - timedelta(days=120),
                     fecha_termino=hoy + timedelta(days=610),
                     renovacion_automatica=False, estado="vigente")
        db.add(c)
        db.flush()
        creados["documentos"] += instanciar_docs(
            db, company.id, "empresa", contrato_id=c.id, faena_id=faena.id,
            contrato_plantilla_id=c.id)
        contratos.append(c)
        creados["contratos"] += 1

    for i, (nombre, cargo_texto, conductor) in enumerate(DEMO_PERSONAL):
        c = contratos[i % len(contratos)]
        cargo, nuevo = resolver_cargo(db, company.id, cargo_id=None,
                                      cargo_texto=cargo_texto)
        if nuevo:
            creados["cargos"] += 1
        s = Sujeto(company_id=company.id, contrato_id=c.id, tipo="trabajador",
                   nombre=nombre, rut=_rut_demo(11_000_000 + i * 137_017),
                   cargo=cargo.nombre if cargo else cargo_texto,
                   cargo_id=cargo.id if cargo else None,
                   es_conductor=conductor)
        db.add(s)
        db.flush()
        creados["documentos"] += instanciar_docs(
            db, company.id, "personal", sujeto_id=s.id, faena_id=c.faena_id,
            cargo_id=s.cargo_id, contrato_plantilla_id=c.id)
        if conductor:
            n = crear_expediente_conductor(db, s)
            creados["documentos"] += n
            creados["expedientes_emsipor"] += 1
        creados["personal"] += 1

    for i, (tipo_equipo, marca, modelo, anio) in enumerate(DEMO_EQUIPOS):
        c = contratos[i % len(contratos)]
        s = Sujeto(company_id=company.id, contrato_id=c.id, tipo="equipo",
                   nombre=f"{marca} {modelo}", patente=_patente_demo(i),
                   tipo_equipo=tipo_equipo, marca=marca, modelo=modelo, anio=anio)
        db.add(s)
        db.flush()
        creados["documentos"] += instanciar_docs(
            db, company.id, "equipo", sujeto_id=s.id, faena_id=c.faena_id,
            contrato_plantilla_id=c.id)
        creados["equipos"] += 1

    db.flush()

    # Se marcan ok dos de cada tres documentos, con el vencimiento derivado de la
    # vigencia de su plantilla. Sin esto la demo arrancaría al 0 % y no habría
    # nada que ver en el dashboard ni en la matriz.
    docs = list(db.scalars(select(Documento).where(
        Documento.company_id == company.id).order_by(Documento.created_at,
                                                     Documento.id)))
    ok = 0
    for i, d in enumerate(docs):
        if i % DEMO_UNO_DE_CADA != 0:
            d.estado = "ok"
            d.vence = vencimiento_por_plantilla(db, d,
                                                desde=hoy - timedelta(days=60))
            ok += 1
        # `estado_calc` es derivado y NO lo recalcula la BD: sin esto los
        # documentos quedarían 'ok' con estado_calc='falta' y el dashboard
        # mostraría 0 % sobre datos que sí están completos.
        d.estado_calc = calc_estado_doc(d)
    creados["documentos_ok"] = ok

    # Y el estado del sujeto se deriva de sus documentos, con la misma regla que
    # el job nocturno de vencimientos.
    por_sujeto: dict[uuid.UUID, list[Documento]] = {}
    for d in docs:
        if d.sujeto_id:
            por_sujeto.setdefault(d.sujeto_id, []).append(d)
    for sujeto in db.scalars(select(Sujeto).where(Sujeto.company_id == company.id)):
        sujeto.estado = calc_estado_sujeto(por_sujeto.get(sujeto.id, []),
                                           sujeto.estado)

    actividad.log(db, company.id, "creacion", "empresa",
                  f"Datos demo regenerados: {creados['contratos']} contratos, "
                  f"{creados['personal']} trabajadores, {creados['equipos']} equipos",
                  user_id=user_id, entidad_tipo="empresa", entidad_id=company.id)
    return creados


@router.post("/companies/{company_id}/reset-demo")
def reset_demo(company_id: uuid.UUID, db: Session = Depends(get_db),
               admin: User = Depends(require_admin)):
    """Reinicia los datos de operación de una empresa demo. Irreversible.

    Solo se permite si `companies.es_demo`: la operación borra contratos,
    sujetos, documentos y archivos sin confirmación por elemento, y sobre una
    empresa real sería una pérdida de datos irrecuperable. `actividad` no se
    borra (append-only), así que el reset queda registrado en el historial.
    """
    c = _get_company(db, company_id)
    if not c.es_demo:
        raise err(409, "NO_ES_DEMO",
                  "Solo se puede reiniciar una empresa marcada como demo")

    try:
        borrados = _borrar_datos_empresa(db, c.id)
    except DBAPIError as e:                                 # noqa: BLE001
        db.rollback()
        # Las tablas append-only (credencial_versiones) rechazan el DELETE con
        # SQLSTATE 55000, incluso en cascada.
        logger.error("reset-demo bloqueado por la BD en empresa %s: %s", c.id, e)
        raise err(409, "RESET_BLOQUEADO",
                  "La base de datos rechazó el borrado de un histórico "
                  "inmutable; revise las credenciales de plataforma rotadas")

    creados = _sembrar_demo(db, c, admin.id)
    db.commit()
    logger.info("empresa demo %s reiniciada por admin=%s", c.id, admin.id)

    recuento = _docs_por_estado(db, c.id)
    return {
        "ok": True, "company_id": str(c.id), "nombre": c.nombre,
        "borrados": borrados, "creados": creados,
        "documentos_por_estado": recuento,
        "nota": ("La tabla 'actividad' es append-only y no se borra: el "
                 "historial de auditoría anterior al reset se conserva."),
    }
