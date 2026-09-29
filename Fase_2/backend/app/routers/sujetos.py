"""Personal y equipos: alta, edición, baja y checklist del sujeto (§9).

`sujetos` es una sola tabla con dos caras (`tipo = trabajador | equipo`) y dos
familias de rutas, `/personal` y `/equipos`, porque el formulario, los filtros y
el checklist son distintos aunque el registro sea el mismo.

Tres reglas de negocio que este módulo concentra:

* **El cargo manda sobre el checklist.** Al dar de alta a un trabajador se
  resuelve su `cargo_id` con `checklist.resolver_cargo` (por id, o por texto
  creando el cargo si hace falta) y ese cargo determina qué requisitos se
  instancian: `cargo_requisitos` prevalece sobre la plantilla de la faena (§8.3).
* **El expediente EMSIPOR no depende de `es_conductor` a secas.** Se decide con
  `checklist.requiere_emsipor`, que consulta `cargos.requiere_emsipor` y admite
  `sujetos.es_conductor` como override por trabajador. Cambiar el cargo en un
  PATCH puede crear el expediente que antes no correspondía.
* **Los overrides del contrato se aplican siempre.** `instanciar_docs` recibe
  `contrato_plantilla_id` con el contrato del sujeto, aunque los documentos
  cuelguen del sujeto y no del contrato: es lo que activa
  `contrato_plantilla_overrides` y los `contrato_requisitos` custom.

Un contract_admin solo opera dentro de su contrato: los listados se filtran con
`contrato_scope` y cada sujeto se valida con `require_contrato`.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from ..config import TIPOS_EQUIPO
from ..database import get_db
from ..deps import (Page, aplicar_orden, contrato_scope, err, get_company_id,
                    get_current_user, paginacion, require_contrato, sobre)
from ..models import Cargo, Contrato, Documento, LicenciaInterna, Sujeto, User
from ..security import validar_rut
from ..services import actividad
from ..services.checklist import (crear_expediente_conductor, instanciar_docs,
                                  requiere_emsipor, resolver_cargo,
                                  stats_sujeto)

logger = logging.getLogger("acredittia.sujetos")

router = APIRouter(tags=["sujetos"])

# `cargo` no se ordena por la columna del sujeto sino por su texto denormalizado,
# que es lo que ve el usuario en la tabla.
ORDEN_SUJETOS = {"nombre", "rut", "patente", "estado", "cargo", "tipo_equipo",
                 "marca", "anio", "created_at", "updated_at"}

TIPO_POR_RUTA = {"personal": "trabajador", "equipos": "equipo"}


# ============================================================================
# Entradas
# ============================================================================
class TrabajadorIn(BaseModel):
    contrato_id: uuid.UUID
    nombre: str
    rut: str
    # `cargo_id` es la forma recomendada (referencia al catálogo). `cargo` como
    # texto se acepta porque el alta por extracción IA y las importaciones traen
    # el cargo escrito a mano; se resuelve o se crea en el catálogo.
    cargo_id: uuid.UUID | None = None
    cargo: str | None = None
    es_conductor: bool = False


class EquipoIn(BaseModel):
    contrato_id: uuid.UUID
    patente: str
    tipo_equipo: str
    marca: str | None = None
    modelo: str | None = None
    anio: int | None = None


class TrabajadorPatch(BaseModel):
    nombre: str | None = None
    cargo_id: uuid.UUID | None = None
    cargo: str | None = None
    es_conductor: bool | None = None


class EquipoPatch(BaseModel):
    nombre: str | None = None
    marca: str | None = None
    modelo: str | None = None
    anio: int | None = None
    tipo_equipo: str | None = None


# ============================================================================
# Helpers reutilizables por otros routers
# ============================================================================
def _get_sujeto(db: Session, cid: uuid.UUID, sid: uuid.UUID, tipo: str,
                user: User | None = None) -> Sujeto:
    s = db.get(Sujeto, sid)
    if not s or s.company_id != cid or s.tipo != tipo:
        raise err(404, "NO_ENCONTRADO", "No existe")
    if user is not None:
        require_contrato(s.contrato_id, user)
    return s


def _validar_contrato(db: Session, cid: uuid.UUID, contrato_id: uuid.UUID,
                      user: User | None = None) -> Contrato:
    if user is not None:
        require_contrato(contrato_id, user)
    c = db.get(Contrato, contrato_id)
    if not c or c.company_id != cid:
        raise err(404, "NO_ENCONTRADO", "Contrato no existe")
    return c


def docs_de_sujeto(db: Session, s: Sujeto) -> list[Documento]:
    return list(db.scalars(
        select(Documento).where(Documento.company_id == s.company_id,
                                Documento.sujeto_id == s.id)
        .order_by(Documento.es_emsipor, Documento.created_at)))


def _out(db: Session, s: Sujeto, con_docs: bool = False,
         docs: list[Documento] | None = None) -> dict:
    """Serializador del sujeto. Lo importan `contratos.py` y `personas.py`."""
    docs = docs if docs is not None else docs_de_sujeto(db, s)
    d = {
        "id": str(s.id), "tipo": s.tipo, "estado": s.estado, "nombre": s.nombre,
        "rut": s.rut, "cargo": s.cargo,
        "cargo_id": str(s.cargo_id) if s.cargo_id else None,
        "es_conductor": s.es_conductor,
        "patente": s.patente, "tipo_equipo": s.tipo_equipo, "marca": s.marca,
        "modelo": s.modelo, "anio": s.anio,
        "contrato": {"id": str(s.contrato.id), "nombre": s.contrato.nombre,
                     "faena_id": str(s.contrato.faena_id),
                     "faena": s.contrato.faena.nombre},
        "stats": stats_sujeto(docs),
    }
    if con_docs:
        from .documentos import doc_out
        d["documentos"] = [doc_out(x) for x in docs if not x.es_emsipor]
        d["documentos_emsipor"] = [doc_out(x) for x in docs if x.es_emsipor]
        lim = db.scalar(select(LicenciaInterna).where(
            LicenciaInterna.company_id == s.company_id,
            LicenciaInterna.sujeto_id == s.id))
        d["licencia_interna"] = {
            "numero": lim.numero, "estado": lim.estado,
            "vence": lim.vence.isoformat() if lim.vence else None,
            "emsipor_estado": lim.emsipor_estado,
        } if lim else None
    return d


def checklist_out(db: Session, s: Sujeto) -> dict:
    """Checklist del sujeto como recurso propio.

    El detalle del sujeto ya embebe los documentos; este envoltorio existe para
    `GET /personal/{id}/documentos`, que es lo que consulta la pantalla del
    expediente cuando refresca solo la lista tras subir un archivo.
    """
    from .documentos import doc_out
    docs = docs_de_sujeto(db, s)
    core = [d for d in docs if not d.es_emsipor]
    emsipor = [d for d in docs if d.es_emsipor]
    return {
        "sujeto": {"id": str(s.id), "tipo": s.tipo, "nombre": s.nombre,
                   "rut": s.rut, "patente": s.patente, "estado": s.estado,
                   "cargo": s.cargo,
                   "contrato_id": str(s.contrato_id)},
        "items": [doc_out(d) for d in core],
        "documentos_emsipor": [doc_out(d) for d in emsipor],
        "total": len(core),
        "stats": stats_sujeto(docs),
    }


def listar_sujetos(db: Session, cid: uuid.UUID, tipo: str, p: Page, *,
                   scope: uuid.UUID | None = None,
                   contrato_id: uuid.UUID | None = None,
                   estado: str | None = None,
                   faena_id: uuid.UUID | None = None,
                   cargo_id: uuid.UUID | None = None,
                   cargo: str | None = None,
                   es_conductor: bool | None = None,
                   tipo_equipo: str | None = None) -> dict:
    """Listado paginado de sujetos de un tipo. Lo reutiliza `contratos.py`.

    `faena_id` obliga a unir con `contratos`: la faena no está denormalizada en
    el sujeto y filtrar en Python rompería la paginación.
    """
    q = (select(Sujeto).join(Contrato, Contrato.id == Sujeto.contrato_id)
         .where(Sujeto.company_id == cid, Sujeto.tipo == tipo))
    efectivo = contrato_id or scope
    if efectivo:
        q = q.where(Sujeto.contrato_id == efectivo)
    if faena_id:
        q = q.where(Contrato.faena_id == faena_id)
    if estado:
        q = q.where(Sujeto.estado == estado)
    if cargo_id:
        q = q.where(Sujeto.cargo_id == cargo_id)
    if cargo:
        q = q.where(Sujeto.cargo.ilike(f"%{cargo}%"))
    if es_conductor is not None:
        q = q.where(Sujeto.es_conductor.is_(es_conductor))
    if tipo_equipo:
        q = q.where(Sujeto.tipo_equipo == tipo_equipo)
    if p.search:
        like = f"%{p.search}%"
        q = q.where(or_(Sujeto.nombre.ilike(like), Sujeto.rut.ilike(like),
                        Sujeto.patente.ilike(like)))

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    defecto = "nombre" if tipo == "trabajador" else "patente"
    q = aplicar_orden(q, Sujeto, p.sort, ORDEN_SUJETOS, defecto)
    # El contrato y su faena van en cada fila: sin el eager load serían dos
    # consultas por sujeto solo para rotular la columna «faena».
    q = q.options(joinedload(Sujeto.contrato).joinedload(Contrato.faena))
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)).unique())

    # Los documentos de toda la página en una sola consulta: `stats_sujeto` los
    # necesita para cada fila y pedirlos uno a uno es un N+1 por sujeto.
    from .reportes import _docs_por_sujeto
    docs = _docs_por_sujeto(db, cid, [s.id for s in filas])
    return sobre([_out(db, s, docs=docs.get(s.id, [])) for s in filas], total, p)


def _resolver_cargo_entrada(db: Session, cid: uuid.UUID,
                            cargo_id: uuid.UUID | None,
                            cargo_texto: str | None) -> tuple[Cargo | None, bool]:
    """Resuelve el cargo del formulario. 400 si el id no es visible para la empresa."""
    cargo, creado = resolver_cargo(db, cid, cargo_id=cargo_id,
                                   cargo_texto=cargo_texto)
    if cargo_id and cargo is None:
        raise err(400, "CARGO_INEXISTENTE",
                  "El cargo indicado no existe o no pertenece a la empresa")
    return cargo, creado


def _sincronizar_emsipor(db: Session, s: Sujeto) -> int:
    """Crea el expediente EMSIPOR si el cargo (o el override) lo exige.

    Es idempotente: `crear_expediente_conductor` devuelve 0 si ya existe la
    licencia interna. Al revés no actúa: dejar de exigir EMSIPOR NO borra un
    expediente ya iniciado, porque puede tener archivos aprobados por el
    mandante. Para eso está `POST /personal/{id}/licencia-interna/reset`.
    """
    if s.tipo != "trabajador" or not requiere_emsipor(db, s):
        return 0
    return crear_expediente_conductor(db, s)


# ============================================================================
# §9.1 — Personal
# ============================================================================
@router.get("/personal")
def listar_personal(contrato_id: uuid.UUID | None = Query(None),
                    faena_id: uuid.UUID | None = Query(None),
                    estado: str | None = Query(None),
                    cargo_id: uuid.UUID | None = Query(None),
                    cargo: str | None = Query(None),
                    es_conductor: bool | None = Query(None),
                    p: Page = Depends(paginacion),
                    db: Session = Depends(get_db),
                    cid: uuid.UUID = Depends(get_company_id),
                    user: User = Depends(get_current_user)):
    """Dotación de personal de la empresa."""
    if contrato_id:
        require_contrato(contrato_id, user)
    return listar_sujetos(db, cid, "trabajador", p, scope=contrato_scope(user),
                          contrato_id=contrato_id, estado=estado,
                          faena_id=faena_id, cargo_id=cargo_id, cargo=cargo,
                          es_conductor=es_conductor)


@router.post("/personal", status_code=201)
def crear_trabajador(body: TrabajadorIn, db: Session = Depends(get_db),
                     cid: uuid.UUID = Depends(get_company_id),
                     user: User = Depends(get_current_user)):
    """Da de alta un trabajador e instancia su checklist.

    El cargo se acepta como `cargo_id` (recomendado) o como texto libre; con
    texto se busca en el catálogo de la empresa y en el base de Acredittia, y si
    no aparece se crea con categoría `otro` y se avisa con `cargo_creado: true`
    para que alguien lo clasifique. El expediente EMSIPOR se crea si el cargo lo
    exige o si el trabajador viene marcado como conductor.
    """
    c = _validar_contrato(db, cid, body.contrato_id, user)
    if not validar_rut(body.rut):
        raise err(422, "RUT_INVALIDO", "RUT inválido; use formato 12.345.678-5")
    if db.scalar(select(Sujeto.id).where(
            Sujeto.company_id == cid, Sujeto.contrato_id == c.id,
            Sujeto.rut == body.rut, Sujeto.tipo == "trabajador",
            Sujeto.estado != "baja")):
        raise err(409, "RUT_DUPLICADO",
                  "El trabajador ya está activo en este contrato")

    cargo, cargo_creado = _resolver_cargo_entrada(db, cid, body.cargo_id,
                                                  body.cargo)
    s = Sujeto(company_id=cid, contrato_id=c.id, tipo="trabajador",
               nombre=body.nombre.strip(), rut=body.rut,
               cargo=cargo.nombre if cargo else (body.cargo or None),
               cargo_id=cargo.id if cargo else None,
               es_conductor=body.es_conductor)
    db.add(s)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "RUT_DUPLICADO", "El trabajador ya existe en este contrato")

    n = instanciar_docs(db, cid, "personal", sujeto_id=s.id,
                        faena_id=c.faena_id, cargo_id=s.cargo_id,
                        contrato_plantilla_id=c.id)
    n_emsipor = _sincronizar_emsipor(db, s)
    actividad.log(db, cid, "creacion", "personal",
                  f"Trabajador {s.nombre} agregado a '{c.nombre}'"
                  + (f" con cargo '{s.cargo}'" if s.cargo else ""),
                  user_id=user.id, entidad_tipo="sujeto", entidad_id=s.id)
    db.commit()
    out = _out(db, s)
    out["documentos_creados"] = n + n_emsipor
    out["expediente_emsipor_creado"] = n_emsipor > 0
    out["cargo_creado"] = cargo_creado
    return out


@router.get("/personal/{sid}")
def detalle_trabajador(sid: uuid.UUID, db: Session = Depends(get_db),
                       cid: uuid.UUID = Depends(get_company_id),
                       user: User = Depends(get_current_user)):
    return _out(db, _get_sujeto(db, cid, sid, "trabajador", user), con_docs=True)


@router.get("/personal/{sid}/documentos")
def docs_trabajador(sid: uuid.UUID, db: Session = Depends(get_db),
                    cid: uuid.UUID = Depends(get_company_id),
                    user: User = Depends(get_current_user)):
    """Checklist del trabajador (el detalle del sujeto ya lo embebe)."""
    return checklist_out(db, _get_sujeto(db, cid, sid, "trabajador", user))


@router.patch("/personal/{sid}")
def editar_trabajador(sid: uuid.UUID, body: TrabajadorPatch,
                      db: Session = Depends(get_db),
                      cid: uuid.UUID = Depends(get_company_id),
                      user: User = Depends(get_current_user)):
    """Edita al trabajador y recalcula el expediente EMSIPOR si el cargo cambió.

    Cambiar el cargo NO reinstancia el checklist de personal: los documentos ya
    subidos siguen siendo válidos y borrarlos perdería archivos aprobados. Los
    requisitos del cargo nuevo que falten se añaden desde
    `PATCH /cargos/{id}` o al crear el requisito en el contrato.
    """
    s = _get_sujeto(db, cid, sid, "trabajador", user)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a modificar")

    cargo_creado = False
    if "cargo_id" in data or "cargo" in data:
        cargo, cargo_creado = _resolver_cargo_entrada(
            db, cid, data.get("cargo_id"), data.get("cargo"))
        s.cargo_id = cargo.id if cargo else None
        s.cargo = cargo.nombre if cargo else (data.get("cargo") or None)
        data.pop("cargo_id", None)
        data.pop("cargo", None)

    for k, v in data.items():
        setattr(s, k, v)
    s.updated_at = datetime.now(timezone.utc)

    n_emsipor = _sincronizar_emsipor(db, s)
    actividad.log(db, cid, "actualizacion", "personal",
                  f"{s.nombre} actualizado", user_id=user.id,
                  entidad_tipo="sujeto", entidad_id=s.id)
    db.commit()
    out = _out(db, s, con_docs=True)
    out["cargo_creado"] = cargo_creado
    out["expediente_emsipor_creado"] = n_emsipor > 0
    out["documentos_creados"] = n_emsipor
    return out


# ============================================================================
# §9.2 — Equipos
# ============================================================================
@router.get("/equipos")
def listar_equipos(contrato_id: uuid.UUID | None = Query(None),
                   faena_id: uuid.UUID | None = Query(None),
                   estado: str | None = Query(None),
                   tipo_equipo: str | None = Query(None),
                   p: Page = Depends(paginacion),
                   db: Session = Depends(get_db),
                   cid: uuid.UUID = Depends(get_company_id),
                   user: User = Depends(get_current_user)):
    """Flota de la empresa."""
    if contrato_id:
        require_contrato(contrato_id, user)
    return listar_sujetos(db, cid, "equipo", p, scope=contrato_scope(user),
                          contrato_id=contrato_id, estado=estado,
                          faena_id=faena_id, tipo_equipo=tipo_equipo)


@router.post("/equipos", status_code=201)
def crear_equipo(body: EquipoIn, db: Session = Depends(get_db),
                 cid: uuid.UUID = Depends(get_company_id),
                 user: User = Depends(get_current_user)):
    """Da de alta un equipo o vehículo e instancia su checklist."""
    c = _validar_contrato(db, cid, body.contrato_id, user)
    if body.tipo_equipo not in TIPOS_EQUIPO:
        raise err(422, "TIPO_INVALIDO", "Tipo de equipo no reconocido")
    patente = body.patente.strip().upper()
    if db.scalar(select(Sujeto.id).where(
            Sujeto.company_id == cid, Sujeto.contrato_id == c.id,
            Sujeto.patente == patente, Sujeto.tipo == "equipo",
            Sujeto.estado != "baja")):
        raise err(409, "PATENTE_DUPLICADA",
                  "El equipo ya está activo en este contrato")
    s = Sujeto(company_id=cid, contrato_id=c.id, tipo="equipo",
               nombre=f"{body.marca or ''} {body.modelo or ''}".strip() or patente,
               patente=patente, tipo_equipo=body.tipo_equipo,
               marca=body.marca, modelo=body.modelo, anio=body.anio)
    db.add(s)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "PATENTE_DUPLICADA", "El equipo ya existe en este contrato")

    n = instanciar_docs(db, cid, "equipo", sujeto_id=s.id, faena_id=c.faena_id,
                        contrato_plantilla_id=c.id)
    actividad.log(db, cid, "creacion", "equipos",
                  f"Equipo {patente} ({body.tipo_equipo}) agregado a '{c.nombre}'",
                  user_id=user.id, entidad_tipo="sujeto", entidad_id=s.id)
    db.commit()
    out = _out(db, s)
    out["documentos_creados"] = n
    return out


@router.get("/equipos/{sid}")
def detalle_equipo(sid: uuid.UUID, db: Session = Depends(get_db),
                   cid: uuid.UUID = Depends(get_company_id),
                   user: User = Depends(get_current_user)):
    return _out(db, _get_sujeto(db, cid, sid, "equipo", user), con_docs=True)


@router.get("/equipos/{sid}/documentos")
def docs_equipo(sid: uuid.UUID, db: Session = Depends(get_db),
                cid: uuid.UUID = Depends(get_company_id),
                user: User = Depends(get_current_user)):
    """Checklist del equipo (el detalle del sujeto ya lo embebe)."""
    return checklist_out(db, _get_sujeto(db, cid, sid, "equipo", user))


@router.patch("/equipos/{sid}")
def editar_equipo(sid: uuid.UUID, body: EquipoPatch,
                  db: Session = Depends(get_db),
                  cid: uuid.UUID = Depends(get_company_id),
                  user: User = Depends(get_current_user)):
    s = _get_sujeto(db, cid, sid, "equipo", user)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a modificar")
    if data.get("tipo_equipo") and data["tipo_equipo"] not in TIPOS_EQUIPO:
        raise err(422, "TIPO_INVALIDO", "Tipo de equipo no reconocido")
    for k, v in data.items():
        setattr(s, k, v)
    s.updated_at = datetime.now(timezone.utc)
    actividad.log(db, cid, "actualizacion", "equipos",
                  f"{s.nombre} ({s.patente}) actualizado", user_id=user.id,
                  entidad_tipo="sujeto", entidad_id=s.id)
    db.commit()
    return _out(db, s, con_docs=True)


# ============================================================================
# Baja y borrado (comunes a los dos tipos)
# ============================================================================
def _baja(ruta: str, sid: uuid.UUID, db: Session, cid: uuid.UUID, user: User):
    """Baja lógica: el sujeto deja de contar pero conserva su expediente."""
    s = _get_sujeto(db, cid, sid, TIPO_POR_RUTA[ruta], user)
    s.estado = "baja"
    s.updated_at = datetime.now(timezone.utc)
    actividad.log(db, cid, "actualizacion", ruta,
                  f"{s.nombre} dado de baja", user_id=user.id,
                  entidad_tipo="sujeto", entidad_id=s.id)
    db.commit()
    return {"ok": True, "id": str(s.id), "estado": "baja"}


def _eliminar(ruta: str, sid: uuid.UUID, db: Session, cid: uuid.UUID, user: User):
    """Borrado físico. Las cascadas de la BD arrastran documentos y archivos.

    Los blobs se borran antes de la fila: con la fila ya eliminada no quedaría
    forma de saber qué `blob_path` purgar.
    """
    from ..services.storage import get_storage
    from ..models import DocumentoArchivo

    s = _get_sujeto(db, cid, sid, TIPO_POR_RUTA[ruta], user)
    nombre = s.nombre
    archivos = list(db.scalars(
        select(DocumentoArchivo)
        .join(Documento, Documento.id == DocumentoArchivo.documento_id)
        .where(DocumentoArchivo.company_id == cid, Documento.sujeto_id == s.id)))
    storage = get_storage()
    for a in archivos:
        storage.delete(a.blob_path)
    db.delete(s)
    actividad.log(db, cid, "actualizacion", ruta,
                  f"{nombre} eliminado ({len(archivos)} archivos purgados)",
                  user_id=user.id)
    db.commit()
    return {"ok": True, "archivos_eliminados": len(archivos)}


@router.post("/personal/{sid}/baja")
def baja_trabajador(sid: uuid.UUID, db: Session = Depends(get_db),
                    cid: uuid.UUID = Depends(get_company_id),
                    user: User = Depends(get_current_user)):
    return _baja("personal", sid, db, cid, user)


@router.post("/equipos/{sid}/baja")
def baja_equipo(sid: uuid.UUID, db: Session = Depends(get_db),
                cid: uuid.UUID = Depends(get_company_id),
                user: User = Depends(get_current_user)):
    return _baja("equipos", sid, db, cid, user)


@router.delete("/personal/{sid}")
def eliminar_trabajador(sid: uuid.UUID, db: Session = Depends(get_db),
                        cid: uuid.UUID = Depends(get_company_id),
                        user: User = Depends(get_current_user)):
    return _eliminar("personal", sid, db, cid, user)


@router.delete("/equipos/{sid}")
def eliminar_equipo(sid: uuid.UUID, db: Session = Depends(get_db),
                    cid: uuid.UUID = Depends(get_company_id),
                    user: User = Depends(get_current_user)):
    return _eliminar("equipos", sid, db, cid, user)
