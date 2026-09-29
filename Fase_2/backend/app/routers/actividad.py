"""Registro de actividad de la empresa (audit log de solo lectura).

`actividad` es append-only: no hay POST, PATCH ni DELETE aquí. Las filas las
escriben los demás módulos con `services.actividad.log`, y este router solo las
expone con el usuario resuelto.

Dos decisiones de rendimiento que condicionan la API:

* El orden por defecto es `-id`. La tabla crece en orden estricto y tiene índice
  `(company_id, id DESC)`, así que ordenar por id equivale a ordenar por fecha y
  evita tocar el BRIN de `created_at`.
* `user_id` no tiene relación mapeada en el modelo (es una columna uuid suelta
  con FK ON DELETE SET NULL en la BD), así que el usuario se resuelve con un
  LEFT JOIN explícito. Cuando es NULL la acción la ejecutó el sistema —la
  revisión IA, los jobs de vencimientos, las sincronizaciones— y se devuelve
  `{"nombre": "Sistema IA"}` sin id ni email.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from ..deps import (Page, aplicar_orden, contrato_scope, err, get_company_id,
                    get_current_user, get_db, paginacion, require_contrato,
                    sobre)
from ..models import Actividad, Documento, Sujeto, User

logger = logging.getLogger("acredittia.actividad")

router = APIRouter(prefix="/actividad", tags=["actividad"])

TIPOS: tuple[str, ...] = ("creacion", "actualizacion", "subida_documento",
                          "asignacion", "alerta_ia", "visualizacion",
                          "comentario")

ORDEN_ACTIVIDAD = {"id", "created_at"}

# Etiqueta del actor cuando la fila no tiene usuario (jobs, IA, webhooks).
ACTOR_SISTEMA = {"nombre": "Sistema IA"}


# ------------------------------------------------------------------- helpers
def _usuario_out(uid, nombre, email, rol) -> dict:
    """Actor de la fila. Sin `user_id` la acción es del sistema, no de nadie."""
    if uid is None:
        return dict(ACTOR_SISTEMA)
    # El usuario pudo ser borrado (FK ON DELETE SET NULL solo cubre el borrado
    # de la fila; si el LEFT JOIN no encuentra nada se conserva el id).
    return {"id": str(uid), "nombre": nombre, "email": email, "rol": rol}


def _out(a: Actividad, nombre, email, rol) -> dict:
    return {
        "id": a.id, "tipo": a.tipo, "modulo": a.modulo,
        "descripcion": a.descripcion, "entidad_tipo": a.entidad_tipo,
        "entidad_id": str(a.entidad_id) if a.entidad_id else None,
        "plataforma": a.plataforma,
        "created_at": a.created_at.isoformat(),
        "usuario": _usuario_out(a.user_id, nombre, email, rol),
    }


def _limite_dia(d: date, *, fin: bool = False) -> datetime:
    """Frontera del día en UTC. `created_at` es timestamptz y los filtros de la
    UI son fechas; se interpretan en UTC para que el rango sea determinista."""
    if fin:
        d = d + timedelta(days=1)
    return datetime.combine(d, time.min, tzinfo=timezone.utc)


def _filtro_contrato(q, cid: uuid.UUID, contrato_id: uuid.UUID):
    """Actividad relacionada con un contrato.

    Se resuelve por `entidad_id`: la del propio contrato más las de sus sujetos y
    sus documentos, ambas como semi-join para no traer listas de ids a Python.

    LIMITACIÓN CONOCIDA: solo aparecen las filas con `entidad_id` apuntando a una
    de esas tres entidades. Una acción sobre un cargo, una plantilla o un usuario
    que afecta al contrato no se puede atribuir sin recorrer `metadata`, y las
    filas sin `entidad_id` (mensajes agregados del tipo "3 requisitos añadidos")
    quedan fuera. Se comprueba `entidad_tipo` solo para el contrato: para sujetos
    y documentos se compara únicamente el uuid, que es único de facto entre
    tablas.
    """
    sujetos = select(Sujeto.id).where(Sujeto.company_id == cid,
                                      Sujeto.contrato_id == contrato_id)
    documentos = select(Documento.id).where(
        Documento.company_id == cid,
        or_(Documento.contrato_id == contrato_id,
            Documento.sujeto_id.in_(sujetos)))
    return q.where(or_(
        and_(Actividad.entidad_tipo == "contrato",
             Actividad.entidad_id == contrato_id),
        Actividad.entidad_id.in_(sujetos),
        Actividad.entidad_id.in_(documentos),
    ))


# ------------------------------------------------------------------- listado
@router.get("")
def listar(modulo: str | None = Query(None),
           tipo: str | None = Query(None),
           user_id: uuid.UUID | None = Query(None),
           contrato_id: uuid.UUID | None = Query(None),
           desde: date | None = Query(None),
           hasta: date | None = Query(None),
           p: Page = Depends(paginacion),
           db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(get_current_user)):
    """Historial de acciones de la empresa, el más reciente primero.

    Un contract_admin solo ve la actividad de su contrato: se le aplica el mismo
    filtro que a `contrato_id` aunque no lo pida, y pedir otro contrato devuelve
    404 (no 403) para no revelar su existencia.

    `search` busca en la descripción, que es el texto que ya compusieron los
    módulos al registrar la acción.
    """
    if tipo and tipo not in TIPOS:
        raise err(400, "TIPO_INVALIDO",
                  f"Tipo debe ser uno de: {', '.join(TIPOS)}")
    if desde and hasta and hasta < desde:
        raise err(400, "RANGO_INVALIDO", "'hasta' no puede ser anterior a 'desde'")

    scope = contrato_scope(user)
    if contrato_id:
        require_contrato(contrato_id, user)
    filtro_contrato = contrato_id or scope

    q = (select(Actividad, User.nombre, User.email, User.role)
         .select_from(Actividad)
         .outerjoin(User, User.id == Actividad.user_id)
         .where(Actividad.company_id == cid))
    if modulo:
        q = q.where(Actividad.modulo == modulo)
    if tipo:
        q = q.where(Actividad.tipo == tipo)
    if user_id:
        q = q.where(Actividad.user_id == user_id)
    if desde:
        q = q.where(Actividad.created_at >= _limite_dia(desde))
    if hasta:
        q = q.where(Actividad.created_at < _limite_dia(hasta, fin=True))
    if p.search:
        q = q.where(Actividad.descripcion.ilike(f"%{p.search}%"))
    if filtro_contrato:
        q = _filtro_contrato(q, cid, filtro_contrato)

    # count(*) sobre la subconsulta: el LEFT JOIN no multiplica filas (User.id es
    # la PK), así que el total es exacto.
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, Actividad, p.sort, ORDEN_ACTIVIDAD, "-id")
    filas = db.execute(q.offset(p.offset).limit(p.page_size)).all()
    items = [_out(f[0], f.nombre, f.email, f.role) for f in filas]
    return sobre(items, total, p)
