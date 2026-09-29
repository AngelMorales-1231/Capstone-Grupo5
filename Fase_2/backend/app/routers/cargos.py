"""Catálogo de cargos y sus requisitos documentales.

`cargos.company_id IS NULL` es el catálogo base de Acredittia: lo leen todas las
empresas y solo el admin de plataforma lo edita (política `p_cargos_*` del
modelo de datos). Cada empresa añade encima sus propios cargos.

El cargo es la fuente de verdad de dos decisiones: qué documentos de ámbito
personal se instancian a un trabajador (`cargo_requisitos`, §8.3) y si necesita
el expediente EMSIPOR (`requiere_emsipor`, §9.2).
"""
from __future__ import annotations

import logging
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..deps import (Page, aplicar_orden, contrato_scope, err, get_company_id,
                    get_current_user, get_db, paginacion, require_admin,
                    require_company, sobre)
from ..models import (Cargo, CargoRequisito, Documento, RequisitoTemplate,
                      Sujeto, User)
from ..services import actividad
from ..services.checklist import normalizar, plantilla_efectiva, plantillas

logger = logging.getLogger("acredittia.cargos")

router = APIRouter(prefix="/cargos", tags=["cargos"])
router_admin = APIRouter(prefix="/admin/cargos", tags=["admin"],
                         dependencies=[Depends(require_admin)])

Categoria = Literal["conduccion", "operacion", "supervision", "mantencion",
                    "administracion", "otro"]

ORDEN_CARGOS = {"nombre", "categoria", "created_at", "updated_at", "activo"}


# ------------------------------------------------------------------ entradas
class RequisitoIn(BaseModel):
    template_id: uuid.UUID
    obligatorio: bool = True


class CargoIn(BaseModel):
    nombre: str
    categoria: Categoria = "otro"
    requiere_emsipor: bool = False
    requisitos: list[RequisitoIn] = []


class CargoPatch(BaseModel):
    nombre: str | None = None
    categoria: Categoria | None = None
    requiere_emsipor: bool | None = None
    activo: bool | None = None
    # None = no tocar la lista; [] = dejar el cargo sin requisitos propios.
    requisitos: list[RequisitoIn] | None = None


# ------------------------------------------------------------------ helpers
def _template_out(t: RequisitoTemplate, obligatorio: bool | None = None) -> dict:
    return {
        "template_id": str(t.id), "titulo": t.titulo, "ambito": t.ambito,
        "tipo": t.tipo,
        "obligatorio": t.obligatorio if obligatorio is None else obligatorio,
        "vigencia_meses": t.vigencia_meses, "plataforma": t.plataforma,
        "ejemplo_clave": t.ejemplo_clave,
    }


def _requisitos_por_cargo(db: Session,
                          cargo_ids: list[uuid.UUID]) -> dict[uuid.UUID, list]:
    if not cargo_ids:
        return {}
    filas = db.execute(
        select(CargoRequisito.cargo_id, CargoRequisito.requisito_template_id,
               CargoRequisito.obligatorio, RequisitoTemplate.titulo)
        .join(RequisitoTemplate,
              RequisitoTemplate.id == CargoRequisito.requisito_template_id)
        .where(CargoRequisito.cargo_id.in_(cargo_ids))
        .order_by(RequisitoTemplate.titulo)
    ).all()
    out: dict[uuid.UUID, list] = {}
    for f in filas:
        out.setdefault(f.cargo_id, []).append({
            "template_id": str(f.requisito_template_id),
            "titulo": f.titulo, "obligatorio": f.obligatorio,
        })
    return out


def _metricas_por_cargo(db: Session, cid: uuid.UUID, cargo_ids: list[uuid.UUID],
                        scope: uuid.UUID | None) -> dict[uuid.UUID, dict]:
    """Trabajadores activos por cargo y media de su cumplimiento individual.

    Se agrega en dos pasos dentro de SQL: primero el porcentaje de cada
    trabajador y luego el promedio por cargo, que no es lo mismo que el
    porcentaje global de documentos (un cargo con un trabajador impecable y otro
    sin nada rinde 50%, no el cociente de sus documentos).
    """
    if not cargo_ids:
        return {}
    oblig = and_(Documento.obligatorio.is_(True), Documento.es_emsipor.is_(False))
    por_sujeto = (
        select(Sujeto.cargo_id.label("cargo_id"),
               func.count(Documento.id).filter(oblig).label("tot"),
               func.count(Documento.id).filter(
                   and_(oblig, Documento.estado_calc == "ok")).label("ok"))
        .select_from(Sujeto)
        .outerjoin(Documento, Documento.sujeto_id == Sujeto.id)
        .where(Sujeto.company_id == cid, Sujeto.tipo == "trabajador",
               Sujeto.estado != "baja", Sujeto.cargo_id.in_(cargo_ids))
        .group_by(Sujeto.cargo_id, Sujeto.id)
    )
    if scope:
        por_sujeto = por_sujeto.where(Sujeto.contrato_id == scope)
    s = por_sujeto.subquery()
    filas = db.execute(
        select(s.c.cargo_id,
               func.count().label("trabajadores"),
               func.avg(case((s.c.tot > 0, 100.0 * s.c.ok / s.c.tot),
                             else_=0.0)).label("pct"))
        .group_by(s.c.cargo_id)
    ).all()
    return {f.cargo_id: {"trabajadores": int(f.trabajadores),
                         "cumplimiento_pct": round(float(f.pct or 0))}
            for f in filas}


def _out(c: Cargo, requisitos: list, metricas: dict | None = None) -> dict:
    m = metricas or {"trabajadores": 0, "cumplimiento_pct": 0}
    return {
        "id": str(c.id), "nombre": c.nombre, "categoria": c.categoria,
        "requiere_emsipor": c.requiere_emsipor, "activo": c.activo,
        "es_global": c.company_id is None,
        "requisitos": requisitos,
        "trabajadores": m["trabajadores"],
        "cumplimiento_pct": m["cumplimiento_pct"],
    }


def _get_cargo(db: Session, cid: uuid.UUID | None, cargo_id: uuid.UUID) -> Cargo:
    """Cargo visible: el de la empresa o el del catálogo base."""
    c = db.get(Cargo, cargo_id)
    if not c or c.company_id not in (None, cid):
        raise err(404, "NO_ENCONTRADO", "Cargo no existe")
    return c


def _puede_editar(c: Cargo, user: User) -> None:
    if c.company_id is None and user.role != "admin":
        raise err(403, "CARGO_GLOBAL",
                  "El catálogo base de Acredittia solo lo edita Acredittia")


def _valida_templates(db: Session, reqs: list[RequisitoIn]
                      ) -> list[tuple[RequisitoTemplate, bool]]:
    """Resuelve las plantillas del cargo, todas de ámbito personal.

    `checklist.plantilla_efectiva` solo consulta `cargo_requisitos` para el
    ámbito personal, así que colgar de un cargo una plantilla de otro ámbito
    generaría requisitos que nunca se instanciarían.
    """
    if not reqs:
        return []
    ids = [r.template_id for r in reqs]
    encontrados = {t.id: t for t in db.scalars(
        select(RequisitoTemplate).where(RequisitoTemplate.id.in_(ids)))}
    faltan = [str(i) for i in ids if i not in encontrados]
    if faltan:
        raise err(400, "TEMPLATE_INVALIDO",
                  "Plantillas de requisito inexistentes", details=faltan)
    ajenos = [t.titulo for t in encontrados.values() if t.ambito != "personal"]
    if ajenos:
        raise err(400, "AMBITO_INVALIDO",
                  "Un cargo solo admite requisitos de ámbito personal",
                  details=ajenos)
    vistos: set[uuid.UUID] = set()
    salida: list[tuple[RequisitoTemplate, bool]] = []
    for r in reqs:
        if r.template_id in vistos:
            continue
        vistos.add(r.template_id)
        salida.append((encontrados[r.template_id], r.obligatorio))
    return salida


def _reemplazar_requisitos(db: Session, cargo: Cargo,
                           reqs: list[tuple[RequisitoTemplate, bool]]) -> None:
    for fila in db.scalars(select(CargoRequisito).where(
            CargoRequisito.cargo_id == cargo.id)):
        db.delete(fila)
    db.flush()
    for t, obligatorio in reqs:
        db.add(CargoRequisito(cargo_id=cargo.id, requisito_template_id=t.id,
                              obligatorio=obligatorio))


def _duplicado(db: Session, company_id: uuid.UUID | None, norm: str,
               excluir: uuid.UUID | None = None) -> bool:
    q = select(func.count()).select_from(Cargo).where(
        Cargo.nombre_normalizado == norm)
    q = q.where(Cargo.company_id.is_(None) if company_id is None
                else Cargo.company_id == company_id)
    if excluir:
        q = q.where(Cargo.id != excluir)
    return bool(db.scalar(q))


def _trabajadores_activos(db: Session, cid: uuid.UUID, cargo_id: uuid.UUID,
                          scope: uuid.UUID | None = None) -> list[Sujeto]:
    q = select(Sujeto).where(
        Sujeto.company_id == cid, Sujeto.tipo == "trabajador",
        Sujeto.cargo_id == cargo_id, Sujeto.estado != "baja")
    if scope:
        q = q.where(Sujeto.contrato_id == scope)
    return list(db.scalars(q))


def _instanciar_faltantes(db: Session, cid: uuid.UUID, trabajadores: list[Sujeto],
                          nuevos: list[tuple[RequisitoTemplate, bool]]) -> int:
    """Crea en cada trabajador los documentos de las plantillas nuevas.

    Se replica la lógica de `checklist.instanciar_docs` pero sin volver a
    resolver la plantilla completa: solo se añade lo que falta, comparando por
    título en minúsculas para no duplicar un documento que ya existía con otro
    origen (custom del contrato, carpeta de arranque…).
    """
    if not trabajadores or not nuevos:
        return 0
    creados = 0
    for s in trabajadores:
        titulos = {t.strip().lower() for t in db.scalars(
            select(Documento.titulo).where(Documento.sujeto_id == s.id))}
        for t, obligatorio in nuevos:
            clave = t.titulo.strip().lower()
            if clave in titulos:
                continue
            titulos.add(clave)
            db.add(Documento(company_id=cid, template_id=t.id, sujeto_id=s.id,
                             titulo=t.titulo, obligatorio=obligatorio,
                             es_emsipor=False))
            creados += 1
    return creados


# ------------------------------------------------------------------ catálogo
@router.get("")
def listar(categoria: str | None = Query(None),
           requiere_emsipor: bool | None = Query(None),
           activo: bool | None = Query(None),
           p: Page = Depends(paginacion),
           db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(get_current_user)):
    """Catálogo visible: los cargos de la empresa más los globales de Acredittia.

    Los conteos de trabajadores y el cumplimiento se acotan al contrato del
    contract_admin cuando corresponde.
    """
    q = select(Cargo).where(or_(Cargo.company_id == cid,
                                Cargo.company_id.is_(None)))
    if categoria:
        q = q.where(Cargo.categoria == categoria)
    if requiere_emsipor is not None:
        q = q.where(Cargo.requiere_emsipor.is_(requiere_emsipor))
    if activo is not None:
        q = q.where(Cargo.activo.is_(activo))
    if p.search:
        q = q.where(Cargo.nombre.ilike(f"%{p.search}%"))

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, Cargo, p.sort, ORDEN_CARGOS, "nombre")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))
    ids = [c.id for c in filas]
    reqs = _requisitos_por_cargo(db, ids)
    met = _metricas_por_cargo(db, cid, ids, contrato_scope(user))
    items = [_out(c, reqs.get(c.id, []), met.get(c.id)) for c in filas]
    return sobre(items, total, p)


@router.post("", status_code=201)
def crear(body: CargoIn, db: Session = Depends(get_db),
          cid: uuid.UUID = Depends(get_company_id),
          user: User = Depends(require_company)):
    """Crea un cargo de la empresa.

    `nombre_normalizado` (minúsculas sin acentos) es la clave real de
    deduplicación: 'Operador Camión' y 'operador camion' son el mismo cargo.
    """
    norm = normalizar(body.nombre)
    if not norm:
        raise err(400, "NOMBRE_REQUERIDO", "El nombre del cargo no puede estar vacío")
    if _duplicado(db, cid, norm):
        raise err(409, "CARGO_DUPLICADO", "La empresa ya tiene un cargo con ese nombre")
    reqs = _valida_templates(db, body.requisitos)

    c = Cargo(company_id=cid, nombre=body.nombre.strip(), nombre_normalizado=norm,
              categoria=body.categoria, requiere_emsipor=body.requiere_emsipor)
    db.add(c)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "CARGO_DUPLICADO", "La empresa ya tiene un cargo con ese nombre")
    for t, obligatorio in reqs:
        db.add(CargoRequisito(cargo_id=c.id, requisito_template_id=t.id,
                              obligatorio=obligatorio))
    actividad.log(db, cid, "creacion", "cargos",
                  f"Cargo '{c.nombre}' creado con {len(reqs)} requisitos",
                  user_id=user.id, entidad_tipo="cargo", entidad_id=c.id)
    db.commit()
    return _out(c, _requisitos_por_cargo(db, [c.id]).get(c.id, []))


@router.patch("/{cargo_id}")
def editar(cargo_id: uuid.UUID, body: CargoPatch,
           aplicar_retroactivo: bool = Query(False),
           db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(require_company)):
    """Edita el cargo y, opcionalmente, propaga los requisitos nuevos.

    Cambiar la plantilla de un cargo no reescribe el expediente de quien ya
    estaba contratado: por defecto solo se informa a cuántos trabajadores
    afectaría (`trabajadores_afectados`). Con `aplicar_retroactivo=true` se
    crean, únicamente, los documentos de las plantillas recién añadidas que al
    trabajador le faltan; nunca se borra nada de un expediente existente.
    """
    c = _get_cargo(db, cid, cargo_id)
    _puede_editar(c, user)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a actualizar")

    if "nombre" in data and data["nombre"]:
        norm = normalizar(data["nombre"])
        if not norm:
            raise err(400, "NOMBRE_REQUERIDO", "El nombre del cargo no puede estar vacío")
        if _duplicado(db, c.company_id, norm, excluir=c.id):
            raise err(409, "CARGO_DUPLICADO", "Ya existe un cargo con ese nombre")
        c.nombre = data["nombre"].strip()
        c.nombre_normalizado = norm
    for campo in ("categoria", "requiere_emsipor", "activo"):
        if campo in data and data[campo] is not None:
            setattr(c, campo, data[campo])

    nuevos: list[tuple[RequisitoTemplate, bool]] = []
    if body.requisitos is not None:
        previos = set(db.scalars(select(CargoRequisito.requisito_template_id)
                                 .where(CargoRequisito.cargo_id == c.id)))
        reqs = _valida_templates(db, body.requisitos)
        nuevos = [(t, o) for t, o in reqs if t.id not in previos]
        _reemplazar_requisitos(db, c, reqs)

    scope = contrato_scope(user)
    trabajadores = _trabajadores_activos(db, cid, c.id, scope)
    salida_extra: dict = {}
    if aplicar_retroactivo:
        creados = _instanciar_faltantes(db, cid, trabajadores, nuevos)
        salida_extra["documentos_creados"] = creados
        salida_extra["trabajadores_afectados"] = len(trabajadores) if nuevos else 0
    else:
        salida_extra["documentos_creados"] = 0
        salida_extra["trabajadores_afectados"] = len(trabajadores)

    actividad.log(db, cid, "actualizacion", "cargos",
                  f"Cargo '{c.nombre}' actualizado ({', '.join(sorted(data))})"
                  + (f"; {salida_extra['documentos_creados']} documentos creados"
                     if salida_extra["documentos_creados"] else ""),
                  user_id=user.id, entidad_tipo="cargo", entidad_id=c.id)
    db.commit()
    out = _out(c, _requisitos_por_cargo(db, [c.id]).get(c.id, []),
               _metricas_por_cargo(db, cid, [c.id], scope).get(c.id))
    out.update(salida_extra)
    return out


@router.delete("/{cargo_id}")
def eliminar(cargo_id: uuid.UUID, db: Session = Depends(get_db),
             cid: uuid.UUID = Depends(get_company_id),
             user: User = Depends(require_company)):
    """Baja lógica del cargo. Con trabajadores activos asignados se rechaza:
    borrarlo dejaría sujetos apuntando a un cargo inexistente y sin plantilla."""
    c = _get_cargo(db, cid, cargo_id)
    _puede_editar(c, user)
    n = len(_trabajadores_activos(db, cid, c.id))
    if n:
        raise err(409, "CARGO_EN_USO",
                  f"El cargo tiene {n} trabajadores activos asignados")
    c.activo = False
    actividad.log(db, cid, "actualizacion", "cargos",
                  f"Cargo '{c.nombre}' desactivado", user_id=user.id,
                  entidad_tipo="cargo", entidad_id=c.id)
    db.commit()
    return {"ok": True, "id": str(c.id), "activo": False}


@router.get("/{cargo_id}/requisitos")
def requisitos_efectivos(cargo_id: uuid.UUID, db: Session = Depends(get_db),
                         cid: uuid.UUID = Depends(get_company_id)):
    """Plantilla efectiva del cargo: lo que se instanciará a un trabajador nuevo.

    Si el cargo no define requisitos propios se devuelve la plantilla estándar de
    ámbito personal, que es lo que aplicaría `checklist.plantilla_efectiva`. El
    bloque EMSIPOR aparece solo si el cargo lo exige.
    """
    c = _get_cargo(db, cid, cargo_id)
    obligatorio_por_template = dict(db.execute(
        select(CargoRequisito.requisito_template_id, CargoRequisito.obligatorio)
        .where(CargoRequisito.cargo_id == c.id)).all())
    personal = plantilla_efectiva(db, "personal", cargo_id=c.id)
    emsipor = plantillas(db, "emsipor") if c.requiere_emsipor else []
    p_out = [_template_out(t, obligatorio_por_template.get(t.id))
             for t in sorted(personal, key=lambda t: t.titulo)]
    e_out = [_template_out(t) for t in sorted(emsipor, key=lambda t: t.titulo)]
    return {
        "cargo": {"id": str(c.id), "nombre": c.nombre,
                  "requiere_emsipor": c.requiere_emsipor,
                  "es_global": c.company_id is None},
        "personal": p_out, "emsipor": e_out,
        "total": len(p_out) + len(e_out),
    }


# =========================================================================
# Catálogo base de Acredittia (company_id NULL) — solo admin de plataforma
# =========================================================================
def _get_cargo_base(db: Session, cargo_id: uuid.UUID) -> Cargo:
    c = db.get(Cargo, cargo_id)
    if not c or c.company_id is not None:
        raise err(404, "NO_ENCONTRADO", "Cargo del catálogo base no existe")
    return c


def _usos_por_cargo(db: Session, cargo_ids: list[uuid.UUID]) -> dict:
    """Cuántos sujetos activos usan cada cargo base, en todas las empresas."""
    if not cargo_ids:
        return {}
    filas = db.execute(
        select(Sujeto.cargo_id, func.count().label("n"))
        .where(Sujeto.cargo_id.in_(cargo_ids), Sujeto.estado != "baja")
        .group_by(Sujeto.cargo_id)).all()
    return {f.cargo_id: int(f.n) for f in filas}


@router_admin.get("")
def admin_listar(categoria: str | None = Query(None),
                 requiere_emsipor: bool | None = Query(None),
                 activo: bool | None = Query(None),
                 p: Page = Depends(paginacion),
                 db: Session = Depends(get_db)):
    """Catálogo base de Acredittia, el que heredan todas las empresas."""
    q = select(Cargo).where(Cargo.company_id.is_(None))
    if categoria:
        q = q.where(Cargo.categoria == categoria)
    if requiere_emsipor is not None:
        q = q.where(Cargo.requiere_emsipor.is_(requiere_emsipor))
    if activo is not None:
        q = q.where(Cargo.activo.is_(activo))
    if p.search:
        q = q.where(Cargo.nombre.ilike(f"%{p.search}%"))

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, Cargo, p.sort, ORDEN_CARGOS, "nombre")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))
    ids = [c.id for c in filas]
    reqs = _requisitos_por_cargo(db, ids)
    usos = _usos_por_cargo(db, ids)
    items = []
    for c in filas:
        d = _out(c, reqs.get(c.id, []))
        d.pop("trabajadores")
        d.pop("cumplimiento_pct")
        d["usos"] = usos.get(c.id, 0)
        items.append(d)
    return sobre(items, total, p)


@router_admin.post("", status_code=201)
def admin_crear(body: CargoIn, db: Session = Depends(get_db),
                admin: User = Depends(require_admin)):
    """Crea un cargo del catálogo base (company_id NULL)."""
    norm = normalizar(body.nombre)
    if not norm:
        raise err(400, "NOMBRE_REQUERIDO", "El nombre del cargo no puede estar vacío")
    if _duplicado(db, None, norm):
        raise err(409, "CARGO_DUPLICADO", "El catálogo base ya tiene ese cargo")
    reqs = _valida_templates(db, body.requisitos)
    c = Cargo(company_id=None, nombre=body.nombre.strip(), nombre_normalizado=norm,
              categoria=body.categoria, requiere_emsipor=body.requiere_emsipor)
    db.add(c)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "CARGO_DUPLICADO", "El catálogo base ya tiene ese cargo")
    for t, obligatorio in reqs:
        db.add(CargoRequisito(cargo_id=c.id, requisito_template_id=t.id,
                              obligatorio=obligatorio))
    # Sin registro en `actividad`: esa tabla exige company_id y el catálogo base
    # no pertenece a ninguna empresa (la traza queda en bitacora_cambios).
    logger.info("catálogo base: cargo creado id=%s por admin=%s", c.id, admin.id)
    db.commit()
    return _out(c, _requisitos_por_cargo(db, [c.id]).get(c.id, []))


@router_admin.patch("/{cargo_id}")
def admin_editar(cargo_id: uuid.UUID, body: CargoPatch,
                 db: Session = Depends(get_db),
                 admin: User = Depends(require_admin)):
    """Edita un cargo del catálogo base. No propaga documentos: los expedientes
    ya instanciados son de cada empresa y se actualizan desde `PATCH /cargos`."""
    c = _get_cargo_base(db, cargo_id)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a actualizar")
    if "nombre" in data and data["nombre"]:
        norm = normalizar(data["nombre"])
        if _duplicado(db, None, norm, excluir=c.id):
            raise err(409, "CARGO_DUPLICADO", "El catálogo base ya tiene ese cargo")
        c.nombre = data["nombre"].strip()
        c.nombre_normalizado = norm
    for campo in ("categoria", "requiere_emsipor", "activo"):
        if campo in data and data[campo] is not None:
            setattr(c, campo, data[campo])
    if body.requisitos is not None:
        _reemplazar_requisitos(db, c, _valida_templates(db, body.requisitos))
    logger.info("catálogo base: cargo actualizado id=%s por admin=%s", c.id, admin.id)
    db.commit()
    return _out(c, _requisitos_por_cargo(db, [c.id]).get(c.id, []))


@router_admin.delete("/{cargo_id}")
def admin_eliminar(cargo_id: uuid.UUID, db: Session = Depends(get_db),
                   admin: User = Depends(require_admin)):
    """Baja lógica en el catálogo base. Se rechaza si alguna empresa lo usa: el
    cargo seguiría referenciado por sus sujetos."""
    c = _get_cargo_base(db, cargo_id)
    n = _usos_por_cargo(db, [c.id]).get(c.id, 0)
    if n:
        raise err(409, "CARGO_EN_USO",
                  f"El cargo está asignado a {n} sujetos activos")
    c.activo = False
    logger.info("catálogo base: cargo desactivado id=%s por admin=%s", c.id, admin.id)
    db.commit()
    return {"ok": True, "id": str(c.id), "activo": False}
