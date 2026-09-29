"""Calendario de cumplimiento: vencimientos proyectados + eventos manuales.

Los vencimientos **no se guardan** como filas de `eventos_calendario`. Se
proyectan al vuelo desde `documentos.vence` cada vez que se pide un rango, y por
eso llegan con `id=null`: materializarlos crearía dos verdades sobre la misma
fecha y un evento huérfano cada vez que alguien renueva un documento o corrige
su vigencia. `eventos_calendario` guarda únicamente lo que la empresa anota a
mano (mantenciones, capacitaciones, entregas…).

De ahí la asimetría del módulo: se lee de dos fuentes y se escribe en una sola.
`POST` rechaza `categoria='vencimiento'` con 400 para que esa frontera no se
cruce por accidente, y el listado descarta cualquier evento manual que apunte a
un documento ya proyectado, de modo que un vencimiento nunca aparece dos veces.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import settings
from ..deps import (contrato_scope, err, get_company_id, get_current_user,
                    get_db, require_company)
from ..models import Contrato, Documento, EventoCalendario, Faena, Sujeto, User
from ..services import actividad

logger = logging.getLogger("acredittia.calendario")

router = APIRouter(prefix="/calendario", tags=["calendario"])

CATEGORIAS: tuple[str, ...] = ("vencimiento", "mantencion", "capacitacion",
                               "administrativo", "entrega", "otro")
# Categorías que la empresa puede crear: 'vencimiento' se proyecta, no se anota.
CATEGORIAS_MANUALES = tuple(c for c in CATEGORIAS if c != "vencimiento")

# Ventana máxima consultable. El endpoint no pagina (una vista de calendario
# necesita el rango completo, no una página), así que el rango es el límite.
VENTANA_MAX_DIAS = 366

CategoriaManual = Literal["mantencion", "capacitacion", "administrativo",
                          "entrega", "otro"]


# ------------------------------------------------------------------ entradas
class EventoIn(BaseModel):
    titulo: str
    # `str` y no `CategoriaManual`: la validación la hace
    # `_valida_categoria_manual`, que distingue 400 CATEGORIA_RESERVADA (pedir
    # 'vencimiento', que es una categoría proyectada) de 400 CATEGORIA_INVALIDA.
    # Con el Literal, Pydantic devolvía 422 antes de llegar ahí y esos dos
    # códigos documentados eran inalcanzables.
    categoria: str = "otro"
    fecha: date
    descripcion: str | None = None


class EventoPatch(BaseModel):
    titulo: str | None = None
    categoria: str | None = None
    fecha: date | None = None
    descripcion: str | None = None
    completado: bool | None = None


# ------------------------------------------------------------------- helpers
def _sujeto_out(s: Sujeto | None) -> dict | None:
    if s is None:
        return None
    return {
        "id": str(s.id), "nombre": s.nombre, "tipo": s.tipo,
        "rut": s.rut, "patente": s.patente, "cargo": s.cargo,
        "estado": s.estado,
    }


def _contrato_out(c: Contrato | None, faena: Faena | None) -> dict | None:
    if c is None:
        return None
    return {"id": str(c.id), "nombre": c.nombre, "codigo": c.codigo,
            "faena": faena.nombre if faena else None}


def _vencimientos(db: Session, cid: uuid.UUID, desde: date, hasta: date,
                  scope: uuid.UUID | None) -> tuple[list[dict], bool]:
    """Vencimientos del rango proyectados desde los documentos de la empresa.

    Solo se proyectan los documentos en estado `ok`: un documento en `falta`
    puede conservar una fecha de la versión anterior, y anunciar el vencimiento
    de algo que no está presentado confundiría dos problemas distintos (lo que
    falta ya sale en alertas y en el checklist).

    El dueño del documento es el contrato (ámbito empresa) o el sujeto; el
    contrato del sujeto se resuelve con COALESCE para que toda fila lleve su
    contrato y su faena.
    """
    hoy = date.today()
    dueno_contrato = func.coalesce(Documento.contrato_id, Sujeto.contrato_id)
    q = (select(Documento, Sujeto, Contrato, Faena)
         .select_from(Documento)
         .outerjoin(Sujeto, Sujeto.id == Documento.sujeto_id)
         .outerjoin(Contrato, Contrato.id == dueno_contrato)
         .outerjoin(Faena, Faena.id == Contrato.faena_id)
         .where(Documento.company_id == cid,
                Documento.vence.is_not(None),
                Documento.vence >= desde,
                Documento.vence <= hasta,
                Documento.estado == "ok"))
    if scope:
        q = q.where(or_(Documento.contrato_id == scope,
                        Sujeto.contrato_id == scope))
    # Cota dura: el rango ya está limitado, pero una empresa grande puede tener
    # miles de documentos venciendo en un año. Se avisa con `truncado`.
    tope = settings.export_filas_max
    filas = db.execute(q.order_by(Documento.vence).limit(tope + 1)).all()
    truncado = len(filas) > tope
    items = []
    for d, s, c, f in filas[:tope]:
        items.append({
            "id": None,                     # no existe en eventos_calendario
            "categoria": "vencimiento",
            "titulo": d.titulo,
            "fecha": d.vence.isoformat(),
            "descripcion": None,
            "completado": False,
            "documento_id": str(d.id),
            "estado_calc": d.estado_calc,
            "dias": (d.vence - hoy).days,
            "sujeto": _sujeto_out(s),
            "contrato": _contrato_out(c, f),
            # Se administra renovando el documento, no editando el calendario.
            "editable": False,
            "created_at": None,
        })
    return items, truncado


def _manuales(db: Session, cid: uuid.UUID, desde: date, hasta: date,
              proyectados: set[str]) -> list[dict]:
    """Eventos anotados por la empresa, sin los que duplican un vencimiento."""
    filas = db.scalars(select(EventoCalendario).where(
        EventoCalendario.company_id == cid,
        EventoCalendario.fecha >= desde,
        EventoCalendario.fecha <= hasta,
    ).order_by(EventoCalendario.fecha)).all()
    return [_out(e) for e in filas
            if not (e.documento_id and str(e.documento_id) in proyectados)]


def _out(e: EventoCalendario) -> dict:
    """Serializador del evento manual. Mismo contrato de campos que la
    proyección de vencimientos para que el frontend pinte una sola lista."""
    return {
        "id": str(e.id),
        "categoria": e.categoria,
        "titulo": e.titulo,
        "fecha": e.fecha.isoformat(),
        "descripcion": e.descripcion,
        "completado": e.completado,
        "documento_id": str(e.documento_id) if e.documento_id else None,
        "estado_calc": None,
        "dias": (e.fecha - date.today()).days,
        "sujeto": None,
        "contrato": None,
        "editable": True,
        "created_at": e.created_at.isoformat(),
    }


def _get_evento(db: Session, cid: uuid.UUID,
                evento_id: uuid.UUID) -> EventoCalendario:
    e = db.get(EventoCalendario, evento_id)
    if not e or e.company_id != cid:
        raise err(404, "NO_ENCONTRADO", "Evento no existe")
    return e


def _valida_categoria_manual(categoria: str) -> None:
    if categoria == "vencimiento":
        raise err(400, "CATEGORIA_RESERVADA",
                  "Los vencimientos se proyectan desde los documentos y no se "
                  "crean a mano: usa la fecha de vigencia del documento")
    if categoria not in CATEGORIAS_MANUALES:
        raise err(400, "CATEGORIA_INVALIDA",
                  f"Categoría debe ser una de: {', '.join(CATEGORIAS_MANUALES)}")


# ------------------------------------------------------------------- listado
@router.get("/eventos")
def listar_eventos(desde: date = Query(...), hasta: date = Query(...),
                   categoria: str | None = Query(None),
                   db: Session = Depends(get_db),
                   cid: uuid.UUID = Depends(get_company_id),
                   user: User = Depends(get_current_user)):
    """Eventos del rango: vencimientos proyectados + anotaciones de la empresa.

    `desde` y `hasta` son obligatorios porque los vencimientos se calculan sobre
    la marcha: sin rango habría que recorrer todos los documentos históricos de
    la empresa para dibujar un mes. Un contract_admin ve solo los vencimientos
    de su contrato; los eventos manuales son de la empresa y no cuelgan de
    ningún contrato, así que los ve todos.
    """
    if hasta < desde:
        raise err(400, "RANGO_INVALIDO", "'hasta' no puede ser anterior a 'desde'")
    if (hasta - desde).days > VENTANA_MAX_DIAS:
        raise err(400, "RANGO_DEMASIADO_AMPLIO",
                  f"El rango no puede exceder {VENTANA_MAX_DIAS} días")
    if categoria and categoria not in CATEGORIAS:
        raise err(400, "CATEGORIA_INVALIDA",
                  f"Categoría debe ser una de: {', '.join(CATEGORIAS)}")

    items: list[dict] = []
    truncado = False
    if categoria in (None, "vencimiento"):
        items, truncado = _vencimientos(db, cid, desde, hasta,
                                        contrato_scope(user))
    proyectados = {i["documento_id"] for i in items if i["documento_id"]}
    if categoria != "vencimiento":
        manuales = _manuales(db, cid, desde, hasta, proyectados)
        items += [m for m in manuales
                  if not categoria or m["categoria"] == categoria]

    items.sort(key=lambda i: (i["fecha"], i["categoria"], i["titulo"].lower()))
    por_categoria = {c: 0 for c in CATEGORIAS}
    for i in items:
        por_categoria[i["categoria"]] = por_categoria.get(i["categoria"], 0) + 1
    return {
        "items": items, "total": len(items), "por_categoria": por_categoria,
        "desde": desde.isoformat(), "hasta": hasta.isoformat(),
        "truncado": truncado,
    }


# ----------------------------------------------------------------- mutaciones
@router.post("/eventos", status_code=201)
def crear_evento(body: EventoIn, db: Session = Depends(get_db),
                 cid: uuid.UUID = Depends(get_company_id),
                 user: User = Depends(require_company)):
    """Anota un evento propio de la empresa en el calendario.

    `categoria='vencimiento'` se rechaza con 400: esa categoría la produce la
    proyección de `documentos.vence` y una fila manual con ese valor quedaría
    duplicada o, peor, sobreviviría a la renovación del documento.
    """
    _valida_categoria_manual(body.categoria)
    titulo = (body.titulo or "").strip()
    if not titulo:
        raise err(400, "TITULO_REQUERIDO", "El título del evento es obligatorio")

    e = EventoCalendario(company_id=cid, titulo=titulo,
                         categoria=body.categoria, fecha=body.fecha,
                         descripcion=body.descripcion)
    db.add(e)
    db.flush()
    actividad.log(db, cid, "creacion", "calendario",
                  f"Evento '{e.titulo}' ({e.categoria}) agendado para "
                  f"{e.fecha.isoformat()}", user_id=user.id,
                  entidad_tipo="evento_calendario", entidad_id=e.id)
    db.commit()
    return _out(e)


@router.patch("/eventos/{evento_id}")
def editar_evento(evento_id: uuid.UUID, body: EventoPatch,
                  db: Session = Depends(get_db),
                  cid: uuid.UUID = Depends(get_company_id),
                  user: User = Depends(require_company)):
    """Edita un evento manual. La categoría sigue sin poder ser 'vencimiento'."""
    e = _get_evento(db, cid, evento_id)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a actualizar")

    if "categoria" in data and data["categoria"] is not None:
        _valida_categoria_manual(data["categoria"])
        e.categoria = data["categoria"]
    if "titulo" in data:
        titulo = (data["titulo"] or "").strip()
        if not titulo:
            raise err(400, "TITULO_REQUERIDO", "El título no puede estar vacío")
        e.titulo = titulo
    if "fecha" in data and data["fecha"] is not None:
        e.fecha = data["fecha"]
    if "descripcion" in data:
        e.descripcion = data["descripcion"]
    if "completado" in data and data["completado"] is not None:
        e.completado = data["completado"]
    e.updated_at = datetime.now(timezone.utc)

    actividad.log(db, cid, "actualizacion", "calendario",
                  f"Evento '{e.titulo}' actualizado ({', '.join(sorted(data))})",
                  user_id=user.id, entidad_tipo="evento_calendario",
                  entidad_id=e.id)
    db.commit()
    return _out(e)


@router.delete("/eventos/{evento_id}")
def eliminar_evento(evento_id: uuid.UUID, db: Session = Depends(get_db),
                    cid: uuid.UUID = Depends(get_company_id),
                    user: User = Depends(require_company)):
    """Elimina un evento manual. Los vencimientos no se pueden borrar: no son
    filas, desaparecen del calendario cuando el documento cambia de fecha."""
    e = _get_evento(db, cid, evento_id)
    titulo, fecha = e.titulo, e.fecha
    db.delete(e)
    actividad.log(db, cid, "actualizacion", "calendario",
                  f"Evento '{titulo}' del {fecha.isoformat()} eliminado",
                  user_id=user.id, entidad_tipo="evento_calendario",
                  entidad_id=evento_id)
    db.commit()
    return {"ok": True, "id": str(evento_id), "titulo": titulo}
