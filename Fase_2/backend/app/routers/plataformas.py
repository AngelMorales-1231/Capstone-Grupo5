"""Plataformas del mandante por contrato (§8.1) y vault de credenciales (§8.2).

Herencia de plataformas
-----------------------
Un contrato **hereda** las plataformas de su faena (`faena_plataformas`) con el
estado de acceso que la empresa tenga en `company_faena_plataformas`. En cuanto
el contrato necesita una lista propia (porque alguien añade una plataforma
manual), la herencia se **materializa**: se copian las heredadas a
`contrato_plataformas` conservando su origen (`faena_plataforma_id`) y su estado.
Nunca se pierde una plataforma del mandante por añadir una manual.

Vault de credenciales
---------------------
`plataforma_credenciales.credencial_jwe` y `.kid` tienen el SELECT **revocado**
para el rol de la API (§10.2 del modelo de datos): la API cifra y escribe, pero
no puede releer el secreto ni el identificador de clave. Por eso:

* ningún serializador de este módulo toca esas dos columnas (están `deferred`,
  así que un `select(PlataformaCredencial)` no las pide);
* el `kid` solo se conoce en el instante en que se cifra, y en ese momento se
  registra en `actividad.metadata`. En una revocación o un borrado no se puede
  informar (no hay forma de leerlo) y viaja como `null`;
* la rotación se hace con un UPDATE de Core para que el JWE no entre nunca en
  el mapa de identidad de la sesión, y `version`/`rotada_at` los avanza el
  trigger `trg_archivar_credencial`, que además archiva la versión anterior.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..deps import (Page, aplicar_orden, err, get_company_id, get_current_user,
                    get_db, paginacion, require_contrato, sobre)
from ..models import (Actividad, CompanyFaenaPlataforma, Contrato,
                      ContratoPlataforma, ContratoRequisito, FaenaPlataforma,
                      Integracion, PlataformaCredencial,
                      PlataformaCredencialVersion, SyncLog, User)
from ..services import actividad
from ..services.checklist import normalizar
from ..services.crypto import CredencialError, cifrar_credencial
from ..services.jobs import enqueue

logger = logging.getLogger("acredittia.plataformas")

# El router lleva el contrato en el prefijo: todo lo de este módulo vive dentro
# de un contrato y se valida con require_contrato().
router = APIRouter(prefix="/contratos/{contrato_id}", tags=["plataformas"])

PlataformaEstado = Literal["activa", "solicitada", "sin_acceso"]

# Tipos de integración disponibles (enum integracion_tipo). El nombre de la
# plataforma se normaliza a este vocabulario para saber si hay un canal
# automático por el que pedir el acceso.
INTEGRACION_TIPOS = ("siga", "workmate", "metacontratas", "webcontrol",
                     "whatsapp", "gdrive")

PASSWORD_MIN = 8
MSG_VAULT = ("No se pudo cifrar la credencial: el vault no está disponible. "
             "No se guarda ningún secreto en claro.")

ORDEN_CREDENCIALES = {"nombre", "usuario", "estado", "version", "created_at",
                      "expira_at", "rotada_at", "last_used_at"}


# --------------------------------------------------------------------- utilidades
def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def slug_plataforma(nombre: str | None) -> str:
    """Clave de comparación entre el nombre visible y el enum de integraciones.

    'Meta Contratas' y 'metacontratas' son la misma plataforma.
    """
    return re.sub(r"[^a-z0-9]", "", normalizar(nombre or ""))


def contrato_de_empresa(db: Session, cid: uuid.UUID, contrato_id: uuid.UUID,
                        user: User) -> Contrato:
    """Contrato de la empresa en curso, o 404.

    `require_contrato` acota al contract_admin a su propio contrato y devuelve
    404 (no 403) para no filtrar la existencia de los demás.
    """
    require_contrato(contrato_id, user)
    c = db.get(Contrato, contrato_id)
    if not c or c.company_id != cid:
        raise err(404, "NO_ENCONTRADO", "Contrato no existe")
    return c


def _cuentas_activas(db: Session, cid: uuid.UUID,
                     plataforma_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Conteo real de credenciales activas por plataforma (nunca estimado)."""
    if not plataforma_ids:
        return {}
    filas = db.execute(
        select(PlataformaCredencial.contrato_plataforma_id,
               func.count(PlataformaCredencial.id))
        .where(PlataformaCredencial.company_id == cid,
               PlataformaCredencial.contrato_plataforma_id.in_(plataforma_ids),
               PlataformaCredencial.estado == "activa")
        .group_by(PlataformaCredencial.contrato_plataforma_id)
    ).all()
    return {f[0]: int(f[1]) for f in filas}


def _out(p: ContratoPlataforma, cuentas: int = 0) -> dict:
    """Serializador de una plataforma materializada del contrato."""
    return {
        "id": str(p.id),
        "faena_plataforma_id": (str(p.faena_plataforma_id)
                                if p.faena_plataforma_id else None),
        "nombre": p.nombre, "descripcion": p.descripcion, "url": p.url,
        "color": p.color, "estado": p.estado, "nota": p.nota, "orden": p.orden,
        "es_custom": p.es_custom,
        # Heredada = conserva el origen en la faena, aunque ya esté materializada.
        "heredada": p.faena_plataforma_id is not None,
        "materializada": True,
        "solicitado_at": _iso(p.solicitado_at),
        "habilitado_at": _iso(p.habilitado_at),
        "ultima_sync_at": _iso(p.ultima_sync_at),
        "cuentas": cuentas,
    }


def _estado_heredado(cfp: CompanyFaenaPlataforma | None) -> dict:
    """Estado de acceso de la empresa a una plataforma del mandante.

    Sin fila en `company_faena_plataformas` la plataforma está `sin_acceso`: la
    empresa todavía no la ha solicitado. Se rellenan las marcas de tiempo que el
    CHECK de `contrato_plataformas` exigirá al materializar.
    """
    if cfp is None:
        return {"estado": "sin_acceso", "nota": None,
                "solicitado_at": None, "habilitado_at": None}
    ahora = datetime.now(timezone.utc)
    solicitado = cfp.solicitado_at
    habilitado = cfp.habilitado_at
    if cfp.estado == "solicitada" and solicitado is None:
        solicitado = ahora
    if cfp.estado == "activa" and habilitado is None:
        habilitado = ahora
    return {"estado": cfp.estado, "nota": cfp.nota,
            "solicitado_at": solicitado, "habilitado_at": habilitado}


def _out_heredada(fp: FaenaPlataforma, acceso: dict) -> dict:
    """Plataforma heredada de la faena que el contrato aún no ha materializado."""
    return {
        "id": None,                     # todavía no existe fila propia
        "faena_plataforma_id": str(fp.id),
        "nombre": fp.nombre, "descripcion": fp.descripcion, "url": fp.url,
        "color": None, "estado": acceso["estado"],
        "nota": acceso["nota"] or fp.nota, "orden": fp.orden,
        "es_custom": False, "heredada": True, "materializada": False,
        "solicitado_at": _iso(acceso["solicitado_at"]),
        "habilitado_at": _iso(acceso["habilitado_at"]),
        "ultima_sync_at": None,
        # Sin fila propia no puede haber credenciales colgando de ella.
        "cuentas": 0,
    }


def _filas_del_contrato(db: Session, cid: uuid.UUID,
                        contrato_id: uuid.UUID) -> list[ContratoPlataforma]:
    return list(db.scalars(
        select(ContratoPlataforma)
        .where(ContratoPlataforma.company_id == cid,
               ContratoPlataforma.contrato_id == contrato_id)
        .order_by(ContratoPlataforma.orden, ContratoPlataforma.nombre)))


def _heredables(db: Session, cid: uuid.UUID, faena_id: uuid.UUID
                ) -> list[tuple[FaenaPlataforma, dict]]:
    """Plataformas de la faena con el estado de acceso de la empresa."""
    fps = list(db.scalars(
        select(FaenaPlataforma)
        .where(FaenaPlataforma.faena_id == faena_id)
        .order_by(FaenaPlataforma.orden, FaenaPlataforma.nombre)))
    if not fps:
        return []
    accesos = {a.faena_plataforma_id: a for a in db.scalars(
        select(CompanyFaenaPlataforma).where(
            CompanyFaenaPlataforma.company_id == cid,
            CompanyFaenaPlataforma.faena_plataforma_id.in_([f.id for f in fps])))}
    return [(f, _estado_heredado(accesos.get(f.id))) for f in fps]


def plataformas_efectivas(db: Session, cid: uuid.UUID,
                          contrato: Contrato) -> list[dict]:
    """Plataformas efectivas del contrato (regla de herencia de §8.1).

    Si el contrato tiene filas en `contrato_plataformas`, esa lista es la
    efectiva y reemplaza por completo a la de la faena. Si no tiene ninguna,
    hereda las de su faena con el estado de acceso de la empresa; esas entradas
    llegan con `id=null` porque todavía no existe fila propia.
    """
    filas = _filas_del_contrato(db, cid, contrato.id)
    if filas:
        cuentas = _cuentas_activas(db, cid, [f.id for f in filas])
        return [_out(f, cuentas.get(f.id, 0)) for f in filas]
    return [_out_heredada(fp, acceso)
            for fp, acceso in _heredables(db, cid, contrato.faena_id)]


def materializar_heredadas(db: Session, cid: uuid.UUID,
                           contrato: Contrato) -> int:
    """Copia las plataformas de la faena a `contrato_plataformas`.

    Es la operación previa e imprescindible antes de añadir la primera
    plataforma manual: sin ella, las filas propias reemplazarían silenciosamente
    a toda la lista del mandante. No hace nada si el contrato ya tiene filas.
    """
    if _filas_del_contrato(db, cid, contrato.id):
        return 0
    n = 0
    for fp, acceso in _heredables(db, cid, contrato.faena_id):
        db.add(ContratoPlataforma(
            company_id=cid, contrato_id=contrato.id, faena_plataforma_id=fp.id,
            nombre=fp.nombre, descripcion=fp.descripcion, url=fp.url,
            nota=acceso["nota"] or fp.nota, orden=fp.orden,
            estado=acceso["estado"], solicitado_at=acceso["solicitado_at"],
            habilitado_at=acceso["habilitado_at"],
            es_custom=False,            # el CHECK exige es_custom = (fp_id IS NULL)
        ))
        n += 1
    if n:
        db.flush()
        logger.info("contrato %s: %s plataformas heredadas materializadas",
                    contrato.id, n)
    return n


def resolver_plataforma(db: Session, cid: uuid.UUID, contrato: Contrato,
                        pid: uuid.UUID, *, materializar: bool = False
                        ) -> tuple[ContratoPlataforma, int]:
    """Resuelve `pid` a una fila de `contrato_plataformas`.

    Acepta también el id de una `faena_plataforma` heredada que el contrato aún
    no ha materializado: la UI recibe ese id en `GET /plataformas` cuando la
    plataforma todavía no tiene fila propia. Con `materializar=True` se
    materializa la herencia completa y se devuelve la fila correspondiente.
    Devuelve (plataforma, heredadas_materializadas).
    """
    p = db.get(ContratoPlataforma, pid)
    if p is not None:
        if p.company_id != cid or p.contrato_id != contrato.id:
            raise err(404, "NO_ENCONTRADO", "Plataforma no existe en el contrato")
        return p, 0

    fp = db.get(FaenaPlataforma, pid)
    if fp is None or fp.faena_id != contrato.faena_id:
        raise err(404, "NO_ENCONTRADO", "Plataforma no existe en el contrato")
    if not materializar:
        raise err(409, "PLATAFORMA_NO_MATERIALIZADA",
                  "La plataforma se hereda de la faena y el contrato aún no "
                  "tiene lista propia")
    n = materializar_heredadas(db, cid, contrato)
    p = db.scalar(select(ContratoPlataforma).where(
        ContratoPlataforma.company_id == cid,
        ContratoPlataforma.contrato_id == contrato.id,
        ContratoPlataforma.faena_plataforma_id == fp.id))
    if p is None:
        raise err(404, "NO_ENCONTRADO", "Plataforma no existe en el contrato")
    return p, n


# ------------------------------------------------------------------- entradas
class PlataformaIn(BaseModel):
    nombre: str
    descripcion: str | None = None
    url: str | None = None
    color: str | None = None
    nota: str | None = None


class PlataformaPatch(BaseModel):
    nombre: str | None = None
    descripcion: str | None = None
    url: str | None = None
    color: str | None = None
    nota: str | None = None
    orden: int | None = None
    estado: PlataformaEstado | None = None


class SolicitudIn(BaseModel):
    nota: str | None = None


class CredencialIn(BaseModel):
    nombre: str
    usuario: str
    password: str


class CredencialPatch(BaseModel):
    nombre: str | None = None
    usuario: str | None = None
    # La única transición admitida es la revocación; expirar es cosa del cron.
    estado: Literal["revocada"] | None = None
    # Se declara solo para poder rechazarlo con 400: la rotación tiene endpoint.
    password: str | None = None


class RotacionIn(BaseModel):
    password: str


# =============================================================================
# §8.1 — Plataformas del contrato
# =============================================================================
@router.get("/plataformas")
def listar_plataformas(contrato_id: uuid.UUID, db: Session = Depends(get_db),
                       cid: uuid.UUID = Depends(get_company_id),
                       user: User = Depends(get_current_user)):
    """Plataformas efectivas del contrato, heredadas de la faena o propias.

    No se pagina a propósito: la lista es un conjunto cerrado que la UI muestra
    como tablero, y `heredado` le dice si todavía está viendo la herencia.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    items = plataformas_efectivas(db, cid, c)
    heredado = not bool(_filas_del_contrato(db, cid, c.id))
    return {"items": items, "total": len(items), "heredado": heredado}


@router.post("/plataformas", status_code=201)
def crear_plataforma(contrato_id: uuid.UUID, body: PlataformaIn,
                     db: Session = Depends(get_db),
                     cid: uuid.UUID = Depends(get_company_id),
                     user: User = Depends(get_current_user)):
    """Añade una plataforma manual al contrato, materializando antes la herencia.

    Insertar la primera fila propia convierte la lista del contrato en la
    efectiva, así que primero se copian todas las heredadas de la faena (con su
    estado de acceso) y solo después se inserta la manual. Es la única forma de
    que añadir una plataforma no borre las del mandante.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    nombre = (body.nombre or "").strip()
    if not nombre:
        raise err(400, "NOMBRE_REQUERIDO", "El nombre de la plataforma es obligatorio")

    efectivas = plataformas_efectivas(db, cid, c)
    if any(slug_plataforma(e["nombre"]) == slug_plataforma(nombre) for e in efectivas):
        raise err(409, "PLATAFORMA_DUPLICADA",
                  "El contrato ya tiene una plataforma con ese nombre")

    materializadas = materializar_heredadas(db, cid, c)
    orden = max([e["orden"] for e in efectivas], default=-1) + 1

    p = ContratoPlataforma(
        company_id=cid, contrato_id=c.id, faena_plataforma_id=None,
        nombre=nombre, descripcion=body.descripcion, url=body.url,
        color=body.color, nota=body.nota, orden=orden,
        estado="sin_acceso", es_custom=True,
    )
    db.add(p)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "PLATAFORMA_DUPLICADA",
                  "El contrato ya tiene una plataforma con ese nombre")

    actividad.log(db, cid, "creacion", "plataformas",
                  f"Plataforma '{p.nombre}' añadida al contrato '{c.nombre}'",
                  user_id=user.id, entidad_tipo="contrato_plataforma",
                  entidad_id=p.id, plataforma=p.nombre)
    db.commit()
    out = _out(p, 0)
    out["heredadas_materializadas"] = materializadas
    return out


@router.patch("/plataformas/{pid}")
def editar_plataforma(contrato_id: uuid.UUID, pid: uuid.UUID,
                      body: PlataformaPatch, db: Session = Depends(get_db),
                      cid: uuid.UUID = Depends(get_company_id),
                      user: User = Depends(get_current_user)):
    """Edita una plataforma del contrato.

    Los CHECK `ck_cplat_solicitud` y `ck_cplat_habilitada` exigen la marca de
    tiempo correspondiente al estado, así que se rellena aquí antes del UPDATE:
    pasar a `solicitada` fija `solicitado_at` y pasar a `activa` fija
    `habilitado_at`, si estaban nulos.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a actualizar")
    # Editar una heredada implica que el contrato pasa a tener lista propia.
    p, materializadas = resolver_plataforma(db, cid, c, pid, materializar=True)

    if "nombre" in data:
        nombre = (data["nombre"] or "").strip()
        if not nombre:
            raise err(400, "NOMBRE_REQUERIDO", "El nombre no puede estar vacío")
        choque = db.scalar(select(func.count(ContratoPlataforma.id)).where(
            ContratoPlataforma.company_id == cid,
            ContratoPlataforma.contrato_id == c.id,
            ContratoPlataforma.id != p.id,
            func.lower(ContratoPlataforma.nombre) == nombre.lower()))
        if choque:
            raise err(409, "PLATAFORMA_DUPLICADA",
                      "El contrato ya tiene una plataforma con ese nombre")
        p.nombre = nombre

    for campo in ("descripcion", "url", "color", "nota"):
        if campo in data:
            setattr(p, campo, data[campo])
    if data.get("orden") is not None:      # la columna es NOT NULL
        p.orden = data["orden"]

    ahora = datetime.now(timezone.utc)
    if data.get("estado"):
        p.estado = data["estado"]
        if p.estado == "solicitada" and p.solicitado_at is None:
            p.solicitado_at = ahora
        if p.estado == "activa" and p.habilitado_at is None:
            p.habilitado_at = ahora

    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "PLATAFORMA_DUPLICADA",
                  "El contrato ya tiene una plataforma con ese nombre")

    actividad.log(db, cid, "actualizacion", "plataformas",
                  f"Plataforma '{p.nombre}' actualizada "
                  f"({', '.join(sorted(data))})", user_id=user.id,
                  entidad_tipo="contrato_plataforma", entidad_id=p.id,
                  plataforma=p.nombre)
    db.commit()
    out = _out(p, _cuentas_activas(db, cid, [p.id]).get(p.id, 0))
    out["heredadas_materializadas"] = materializadas
    return out


@router.delete("/plataformas/{pid}")
def eliminar_plataforma(contrato_id: uuid.UUID, pid: uuid.UUID,
                        confirm: bool = Query(False),
                        db: Session = Depends(get_db),
                        cid: uuid.UUID = Depends(get_company_id),
                        user: User = Depends(get_current_user)):
    """Elimina una plataforma manual del contrato y todo lo que cuelga de ella.

    Solo se borran las manuales: una plataforma del mandante no desaparece
    porque la empresa deje de usarla, se desactiva con `estado='sin_acceso'`.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    if not confirm:
        raise err(400, "REQUIERE_CONFIRMACION",
                  "Eliminar la plataforma borra sus requisitos y credenciales; "
                  "repita con confirm=true")

    p = db.get(ContratoPlataforma, pid)
    if p is None or p.company_id != cid or p.contrato_id != c.id:
        fp = db.get(FaenaPlataforma, pid)
        if p is None and fp is not None and fp.faena_id == c.faena_id:
            raise err(409, "PLATAFORMA_HEREDADA",
                      "Es una plataforma heredada de la faena: no se elimina, "
                      "se desactiva con estado='sin_acceso'")
        raise err(404, "NO_ENCONTRADO", "Plataforma no existe en el contrato")
    if not p.es_custom:
        raise err(409, "PLATAFORMA_HEREDADA",
                  "Es una plataforma heredada de la faena: no se elimina, "
                  "se desactiva con estado='sin_acceso'")

    n_reqs = db.scalar(select(func.count(ContratoRequisito.id)).where(
        ContratoRequisito.company_id == cid,
        ContratoRequisito.vinculo_ref == p.id)) or 0
    n_creds = db.scalar(select(func.count(PlataformaCredencial.id)).where(
        PlataformaCredencial.company_id == cid,
        PlataformaCredencial.contrato_plataforma_id == p.id)) or 0

    # La BD ya cascadea, pero se borra explícitamente para informar los conteos
    # y no depender del orden de las cascadas.
    db.execute(delete(ContratoRequisito).where(
        ContratoRequisito.company_id == cid,
        ContratoRequisito.vinculo_ref == p.id))
    db.execute(delete(PlataformaCredencial).where(
        PlataformaCredencial.company_id == cid,
        PlataformaCredencial.contrato_plataforma_id == p.id))
    nombre = p.nombre
    db.delete(p)

    actividad.log(db, cid, "actualizacion", "plataformas",
                  f"Plataforma '{nombre}' eliminada del contrato "
                  f"({n_reqs} requisitos, {n_creds} credenciales)",
                  user_id=user.id, entidad_tipo="contrato_plataforma",
                  entidad_id=pid, plataforma=nombre)
    db.commit()
    return {"ok": True, "id": str(pid), "nombre": nombre,
            "requisitos_eliminados": n_reqs, "credenciales_eliminadas": n_creds}


@router.post("/plataformas/{pid}/solicitar-acceso")
def solicitar_acceso(contrato_id: uuid.UUID, pid: uuid.UUID,
                     body: SolicitudIn | None = None,
                     db: Session = Depends(get_db),
                     cid: uuid.UUID = Depends(get_company_id),
                     user: User = Depends(get_current_user)):
    """Marca la plataforma como solicitada y avisa por la integración si existe.

    Si la empresa tiene una integración activa del mismo tipo que la plataforma
    (SIGA, WorkMate, MetaContratas…), la solicitud se encola para que el worker
    la curse; si no, queda registrada para seguimiento manual.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    p, materializadas = resolver_plataforma(db, cid, c, pid, materializar=True)
    if p.estado == "activa":
        raise err(409, "PLATAFORMA_ACTIVA",
                  "El acceso a la plataforma ya está habilitado")

    nota = (body.nota if body else None)
    p.estado = "solicitada"
    p.solicitado_at = datetime.now(timezone.utc)
    if nota:
        p.nota = nota

    tipo = slug_plataforma(p.nombre)
    integracion = None
    if tipo in INTEGRACION_TIPOS:
        integracion = db.scalar(select(Integracion).where(
            Integracion.company_id == cid, Integracion.tipo == tipo,
            Integracion.estado == "activa"))

    actividad.log(db, cid, "actualizacion", "plataformas",
                  f"Acceso solicitado a '{p.nombre}'"
                  + (" (vía integración)" if integracion else ""),
                  user_id=user.id, entidad_tipo="contrato_plataforma",
                  entidad_id=p.id, plataforma=p.nombre)
    db.commit()

    # Se encola después del commit: con QUEUE_BACKEND=inproc la tarea corre en
    # el acto y abre su propia sesión, que solo ve lo ya confirmado.
    if integracion:
        enqueue("notificar_solicitud_acceso",
                company_id=str(cid), contrato_id=str(c.id),
                plataforma_id=str(p.id), plataforma=p.nombre,
                integracion_id=str(integracion.id), nota=nota or "")

    out = _out(p, _cuentas_activas(db, cid, [p.id]).get(p.id, 0))
    out["heredadas_materializadas"] = materializadas
    out["notificado"] = integracion is not None
    return out


# =============================================================================
# §8.2 — Vault de credenciales
# =============================================================================
def _cifrar(*, credencial_id, company_id, contrato_id, plataforma: str,
            usuario: str, password: str) -> tuple[str, str, datetime]:
    """Envuelve `cifrar_credencial` traduciendo el fallo del vault a 503.

    Nunca hay plan B: si el vault no responde no se guarda nada, porque la
    alternativa sería persistir el secreto sin cifrar.
    """
    try:
        return cifrar_credencial(
            credencial_id=credencial_id, company_id=company_id,
            contrato_id=contrato_id, plataforma=plataforma,
            usuario=usuario, password=password)
    except CredencialError as e:
        detalle = str(e).lower()
        if "key vault" in detalle or "keyvault" in detalle or "vault" in detalle:
            raise err(503, "VAULT_NO_DISPONIBLE", MSG_VAULT)
        logger.error("cifrado de credencial rechazado: %s", e)
        raise err(503, "CIFRADO_NO_DISPONIBLE",
                  "No se pudo cifrar la credencial. No se guarda ningún "
                  "secreto en claro.")
    except Exception as e:
        # Fallos del SDK del vault (red, identidad, permisos): mismo trato.
        logger.error("cifrado de credencial falló: %s", type(e).__name__)
        raise err(503, "VAULT_NO_DISPONIBLE", MSG_VAULT)


def _log_cred(db: Session, cid: uuid.UUID, tipo: str, descripcion: str, *,
              user_id: uuid.UUID | None, credencial_id: uuid.UUID,
              version: int, kid: str | None, plataforma: str | None = None) -> None:
    """Traza de credenciales en `actividad`.

    Se escribe la fila a mano porque `actividad.log` no acepta `metadata`, y
    aquí es justo lo que interesa: solo `kid` y `version`, nunca el secreto. En
    revocaciones y borrados `kid` viaja como null: la API tiene el SELECT de esa
    columna revocado y no puede leerlo.
    """
    db.add(Actividad(
        company_id=cid, user_id=user_id, tipo=tipo, modulo="integraciones",
        descripcion=descripcion, entidad_tipo="plataforma_credencial",
        entidad_id=credencial_id, plataforma=plataforma,
        meta={"kid": kid, "version": version},
    ))


def _cred_out(c: PlataformaCredencial, ultimo_uso_resultado: str | None = None) -> dict:
    """Metadatos de una cuenta. Jamás `credencial_jwe` ni `kid` (SELECT revocado)."""
    return {
        "id": str(c.id), "nombre": c.nombre, "usuario": c.usuario,
        "estado": c.estado,
        "password_set": True,           # la columna es NOT NULL: siempre hay secreto
        "version": c.version,
        "rotada_at": _iso(c.rotada_at), "expira_at": _iso(c.expira_at),
        "last_used_at": _iso(c.last_used_at),
        "created_by": str(c.created_by) if c.created_by else None,
        "created_at": _iso(c.created_at),
        "ultimo_uso_resultado": ultimo_uso_resultado,
    }


def _ultimo_uso(db: Session, cid: uuid.UUID, nombre_plataforma: str) -> str | None:
    """Resultado del último `SyncLog` de la integración de esa plataforma."""
    tipo = slug_plataforma(nombre_plataforma)
    if tipo not in INTEGRACION_TIPOS:
        return None
    fila = db.execute(
        select(SyncLog.status)
        .join(Integracion, Integracion.id == SyncLog.integracion_id)
        .where(SyncLog.company_id == cid, Integracion.tipo == tipo)
        .order_by(SyncLog.started_at.desc()).limit(1)).first()
    return fila[0] if fila else None


def _get_credencial(db: Session, cid: uuid.UUID, plataforma: ContratoPlataforma,
                    uid: uuid.UUID) -> PlataformaCredencial:
    c = db.get(PlataformaCredencial, uid)
    if not c or c.company_id != cid or c.contrato_plataforma_id != plataforma.id:
        raise err(404, "NO_ENCONTRADO", "La cuenta no existe en esta plataforma")
    return c


def _usuario_duplicado(db: Session, cid: uuid.UUID, plataforma_id: uuid.UUID,
                       usuario: str, excluir: uuid.UUID | None = None) -> bool:
    q = select(func.count(PlataformaCredencial.id)).where(
        PlataformaCredencial.company_id == cid,
        PlataformaCredencial.contrato_plataforma_id == plataforma_id,
        func.lower(PlataformaCredencial.usuario) == usuario.lower())
    if excluir:
        q = q.where(PlataformaCredencial.id != excluir)
    return bool(db.scalar(q))


@router.get("/plataformas/{pid}/usuarios")
def listar_usuarios(contrato_id: uuid.UUID, pid: uuid.UUID,
                    estado: str | None = Query(None),
                    p: Page = Depends(paginacion),
                    db: Session = Depends(get_db),
                    cid: uuid.UUID = Depends(get_company_id),
                    user: User = Depends(get_current_user)):
    """Cuentas de acceso de la plataforma, sin secretos.

    `select(PlataformaCredencial)` no pide `credencial_jwe` ni `kid` porque están
    `deferred`, que es exactamente lo que permite el GRANT por columnas.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    plat = db.get(ContratoPlataforma, pid)
    if plat is None or plat.company_id != cid or plat.contrato_id != c.id:
        fp = db.get(FaenaPlataforma, pid)
        if plat is None and fp is not None and fp.faena_id == c.faena_id:
            # Heredada sin materializar: no puede tener cuentas todavía.
            return sobre([], 0, p)
        raise err(404, "NO_ENCONTRADO", "Plataforma no existe en el contrato")

    filtros = [PlataformaCredencial.company_id == cid,
               PlataformaCredencial.contrato_plataforma_id == plat.id]
    if estado:
        filtros.append(PlataformaCredencial.estado == estado)
    if p.search:
        like = f"%{p.search}%"
        filtros.append(PlataformaCredencial.usuario.ilike(like)
                       | PlataformaCredencial.nombre.ilike(like))

    # El total se cuenta con `count(id)` y NO con `select_from(q.subquery())`:
    # la subconsulta de una entidad ORM materializa TODAS sus columnas, incluidas
    # las deferred, y `credencial_jwe` tiene el SELECT revocado para el rol de la
    # API. Con la subconsulta el endpoint reventaba con «permission denied for
    # table plataforma_credenciales».
    total = db.scalar(select(func.count(PlataformaCredencial.id))
                      .where(*filtros)) or 0
    q = select(PlataformaCredencial).where(*filtros)
    q = aplicar_orden(q, PlataformaCredencial, p.sort, ORDEN_CREDENCIALES,
                      "-created_at")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))
    resultado = _ultimo_uso(db, cid, plat.nombre)
    return sobre([_cred_out(f, resultado) for f in filas], total, p)


@router.post("/plataformas/{pid}/usuarios", status_code=201)
def crear_usuario(contrato_id: uuid.UUID, pid: uuid.UUID, body: CredencialIn,
                  db: Session = Depends(get_db),
                  cid: uuid.UUID = Depends(get_company_id),
                  user: User = Depends(get_current_user)):
    """Guarda una cuenta de plataforma cifrando el secreto en un JWE.

    El id de la credencial se genera antes de cifrar porque viaja dentro del
    payload (claim `sub`): así el worker puede comprobar que el JWE que descifra
    es el de la fila que leyó, y una copia entre filas se detecta.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    plat, materializadas = resolver_plataforma(db, cid, c, pid, materializar=True)

    usuario = (body.usuario or "").strip()
    nombre = (body.nombre or "").strip()
    if not usuario or not nombre:
        raise err(400, "DATOS_INCOMPLETOS",
                  "El nombre del titular y el usuario son obligatorios")
    if len(body.password or "") < PASSWORD_MIN:
        raise err(400, "PASSWORD_DEBIL",
                  f"La contraseña debe tener al menos {PASSWORD_MIN} caracteres")
    if _usuario_duplicado(db, cid, plat.id, usuario):
        raise err(409, "USUARIO_DUPLICADO",
                  "Esa plataforma ya tiene una cuenta con ese usuario")

    cred_id = uuid.uuid4()
    jwe, kid, expira = _cifrar(
        credencial_id=cred_id, company_id=cid, contrato_id=c.id,
        plataforma=plat.nombre, usuario=usuario, password=body.password)
    body.password = ""      # descartado en cuanto está cifrado

    ahora = datetime.now(timezone.utc)
    cred = PlataformaCredencial(
        id=cred_id, company_id=cid, contrato_plataforma_id=plat.id,
        nombre=nombre, usuario=usuario, credencial_jwe=jwe, kid=kid,
        version=1, estado="activa", expira_at=expira, rotada_at=ahora,
        created_by=user.id,
    )
    db.add(cred)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "USUARIO_DUPLICADO",
                  "Esa plataforma ya tiene una cuenta con ese usuario")

    _log_cred(db, cid, "creacion",
              f"Credencial '{usuario}' creada en '{plat.nombre}'",
              user_id=user.id, credencial_id=cred.id, version=1, kid=kid,
              plataforma=plat.nombre)
    db.commit()
    out = _cred_out(cred, _ultimo_uso(db, cid, plat.nombre))
    out["heredadas_materializadas"] = materializadas
    return out


@router.patch("/plataformas/{pid}/usuarios/{uid}")
def editar_usuario(contrato_id: uuid.UUID, pid: uuid.UUID, uid: uuid.UUID,
                   body: CredencialPatch, db: Session = Depends(get_db),
                   cid: uuid.UUID = Depends(get_company_id),
                   user: User = Depends(get_current_user)):
    """Edita los metadatos de la cuenta o la revoca.

    La contraseña no se edita por aquí: cambiarla es una rotación y tiene su
    propio endpoint, que es el que deja rastro en el historial de versiones.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    plat, _ = resolver_plataforma(db, cid, c, pid)
    cred = _get_credencial(db, cid, plat, uid)

    data = body.model_dump(exclude_unset=True)
    if "password" in data:
        raise err(400, "PASSWORD_NO_EDITABLE",
                  "La contraseña se cambia con POST .../usuarios/{id}/rotar")
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a actualizar")

    if data.get("usuario"):
        nuevo = data["usuario"].strip()
        if not nuevo:
            raise err(400, "DATOS_INCOMPLETOS", "El usuario no puede estar vacío")
        if _usuario_duplicado(db, cid, plat.id, nuevo, excluir=cred.id):
            raise err(409, "USUARIO_DUPLICADO",
                      "Esa plataforma ya tiene una cuenta con ese usuario")
        cred.usuario = nuevo
    if data.get("nombre"):
        cred.nombre = data["nombre"].strip()

    revocada = False
    if data.get("estado"):
        if cred.estado == "revocada":
            raise err(409, "CREDENCIAL_REVOCADA", "La cuenta ya está revocada")
        cred.estado = "revocada"
        revocada = True

    if revocada:
        # kid=None: la API no puede leer esa columna (SELECT revocado).
        _log_cred(db, cid, "actualizacion",
                  f"Credencial '{cred.usuario}' revocada en '{plat.nombre}'",
                  user_id=user.id, credencial_id=cred.id,
                  version=cred.version, kid=None, plataforma=plat.nombre)
    else:
        actividad.log(db, cid, "actualizacion", "integraciones",
                      f"Credencial '{cred.usuario}' actualizada en "
                      f"'{plat.nombre}'", user_id=user.id,
                      entidad_tipo="plataforma_credencial", entidad_id=cred.id,
                      plataforma=plat.nombre)
    db.commit()
    return _cred_out(cred, _ultimo_uso(db, cid, plat.nombre))


@router.post("/plataformas/{pid}/usuarios/{uid}/rotar")
def rotar_usuario(contrato_id: uuid.UUID, pid: uuid.UUID, uid: uuid.UUID,
                  body: RotacionIn, db: Session = Depends(get_db),
                  cid: uuid.UUID = Depends(get_company_id),
                  user: User = Depends(get_current_user)):
    """Sustituye el secreto por un JWE nuevo.

    La API solo actualiza `credencial_jwe`, `kid` y `expira_at`: el trigger
    `trg_archivar_credencial` archiva la versión anterior en
    `plataforma_credencial_versiones` y avanza `version` y `rotada_at`. Tocar
    esos dos campos a mano rompería la numeración del historial.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    plat, _ = resolver_plataforma(db, cid, c, pid)
    cred = _get_credencial(db, cid, plat, uid)
    if cred.estado == "revocada":
        raise err(409, "CREDENCIAL_REVOCADA",
                  "Una credencial revocada no se rota; cree una cuenta nueva")
    if len(body.password or "") < PASSWORD_MIN:
        raise err(400, "PASSWORD_DEBIL",
                  f"La contraseña debe tener al menos {PASSWORD_MIN} caracteres")

    jwe, kid, expira = _cifrar(
        credencial_id=cred.id, company_id=cid, contrato_id=c.id,
        plataforma=plat.nombre, usuario=cred.usuario, password=body.password)
    body.password = ""      # descartado en cuanto está cifrado

    # UPDATE de Core: el JWE no entra en el mapa de identidad de la sesión, así
    # que ningún refresh posterior puede intentar releer las columnas revocadas.
    db.execute(
        update(PlataformaCredencial)
        .where(PlataformaCredencial.id == cred.id,
               PlataformaCredencial.company_id == cid)
        .values(credencial_jwe=jwe, kid=kid, expira_at=expira)
        .execution_options(synchronize_session=False))

    # El trigger ya corrió dentro de esta transacción: la version nueva es legible.
    version = db.scalar(select(PlataformaCredencial.version).where(
        PlataformaCredencial.id == cred.id)) or cred.version + 1
    _log_cred(db, cid, "actualizacion",
              f"Credencial '{cred.usuario}' rotada en '{plat.nombre}' "
              f"(v{version})", user_id=user.id, credencial_id=cred.id,
              version=version, kid=kid, plataforma=plat.nombre)
    db.commit()

    # Relectura tras el commit: se piden solo las columnas concedidas.
    fila = db.execute(
        select(PlataformaCredencial.version, PlataformaCredencial.rotada_at,
               PlataformaCredencial.expira_at, PlataformaCredencial.estado)
        .where(PlataformaCredencial.id == cred.id)).one()
    out = _cred_out(cred, _ultimo_uso(db, cid, plat.nombre))
    out.update({"version": fila.version, "rotada_at": _iso(fila.rotada_at),
                "expira_at": _iso(fila.expira_at), "estado": fila.estado})
    return out


@router.delete("/plataformas/{pid}/usuarios/{uid}")
def eliminar_usuario(contrato_id: uuid.UUID, pid: uuid.UUID, uid: uuid.UUID,
                     db: Session = Depends(get_db),
                     cid: uuid.UUID = Depends(get_company_id),
                     user: User = Depends(get_current_user)):
    """Elimina la cuenta. El historial de versiones cae por cascada de la BD."""
    c = contrato_de_empresa(db, cid, contrato_id, user)
    plat, _ = resolver_plataforma(db, cid, c, pid)
    cred = _get_credencial(db, cid, plat, uid)
    usuario, version = cred.usuario, cred.version

    n_versiones = db.scalar(select(func.count(PlataformaCredencialVersion.id))
                            .where(PlataformaCredencialVersion.company_id == cid,
                                   PlataformaCredencialVersion.credencial_id == cred.id)) or 0
    db.delete(cred)
    _log_cred(db, cid, "actualizacion",
              f"Credencial '{usuario}' eliminada de '{plat.nombre}'",
              user_id=user.id, credencial_id=uid, version=version, kid=None,
              plataforma=plat.nombre)
    db.commit()
    return {"ok": True, "id": str(uid), "usuario": usuario,
            "versiones_eliminadas": n_versiones}


@router.get("/plataformas/{pid}/usuarios/{uid}/usos")
def usos_usuario(contrato_id: uuid.UUID, pid: uuid.UUID, uid: uuid.UUID,
                 limite: int = Query(100, ge=1, le=500),
                 db: Session = Depends(get_db),
                 cid: uuid.UUID = Depends(get_company_id),
                 user: User = Depends(get_current_user)):
    """Historial de la cuenta: rotaciones archivadas y actividad registrada.

    De las versiones archivadas se devuelven los metadatos (`kid` sí está
    concedido en esa tabla) pero nunca `credencial_jwe`, que solo lee el worker.
    """
    c = contrato_de_empresa(db, cid, contrato_id, user)
    plat, _ = resolver_plataforma(db, cid, c, pid)
    cred = _get_credencial(db, cid, plat, uid)

    versiones = db.execute(
        select(PlataformaCredencialVersion.version,
               PlataformaCredencialVersion.kid,
               PlataformaCredencialVersion.vigente_desde,
               PlataformaCredencialVersion.vigente_hasta,
               PlataformaCredencialVersion.rotada_por,
               PlataformaCredencialVersion.created_at)
        .where(PlataformaCredencialVersion.company_id == cid,
               PlataformaCredencialVersion.credencial_id == cred.id)
        .order_by(PlataformaCredencialVersion.version.desc())).all()

    usos = db.scalars(
        select(Actividad)
        .where(Actividad.company_id == cid,
               Actividad.modulo == "integraciones",
               Actividad.entidad_id == cred.id)
        .order_by(Actividad.created_at.desc()).limit(limite)).all()

    return {
        "credencial": _cred_out(cred, _ultimo_uso(db, cid, plat.nombre)),
        "versiones": [{
            "version": v.version, "kid": v.kid,
            "vigente_desde": _iso(v.vigente_desde),
            "vigente_hasta": _iso(v.vigente_hasta),
            "rotada_por": str(v.rotada_por) if v.rotada_por else None,
            "created_at": _iso(v.created_at),
        } for v in versiones],
        "usos": [{
            "id": a.id, "tipo": a.tipo, "descripcion": a.descripcion,
            "user_id": str(a.user_id) if a.user_id else None,
            "plataforma": a.plataforma, "meta": a.meta or {},
            "created_at": _iso(a.created_at),
        } for a in usos],
    }
