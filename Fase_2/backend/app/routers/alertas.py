"""Alertas de la empresa: vencimientos, IA, integraciones y sistema (§13).

Las alertas no se crean desde la API: las emiten el job de vencimientos
(`services/vencimientos.py`), la revisión IA (`services/tasks.py`) y las
sincronizaciones. Aquí solo se consultan y se cierran, que es lo que hace un
usuario: leer, atender y resolver.

`leida` y `resuelta` son marcas de tiempo y no banderas: `leida_at` y
`resuelta_at`. Se exponen como booleanos porque es lo que necesita la UI, pero la
BD conserva el cuándo, que es lo que permite medir el tiempo de respuesta.

Un contract_admin ve solo las alertas de su contrato.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import (Page, aplicar_orden, contrato_scope, err, get_company_id,
                    get_current_user, paginacion, require_contrato, sobre)
from ..models import Alerta, User
from ..services import actividad

logger = logging.getLogger("acredittia.alertas")

router = APIRouter(prefix="/alertas", tags=["alertas"])

SEVERIDADES: tuple[str, ...] = ("critica", "alta", "media", "baja",
                                "advertencia", "informativa")
ORIGENES: tuple[str, ...] = ("vencimiento", "ia", "integracion", "sistema")
ESTADOS: tuple[str, ...] = ("nueva", "en_progreso", "bloqueante", "informativa",
                            "resuelta")

ORDEN_ALERTAS = {"created_at", "updated_at", "severidad", "estado", "titulo",
                 "leida_at", "resuelta_at"}


class AlertaPatch(BaseModel):
    leida: bool | None = None
    resuelta: bool | None = None
    estado: str | None = None


class MarcarIn(BaseModel):
    ids: list[uuid.UUID] | None = None


def _out(a: Alerta) -> dict:
    """Serializador de alerta. Lo importa `contratos.py` para su vista filtrada."""
    return {
        "id": str(a.id), "severidad": a.severidad, "estado": a.estado,
        "origen": a.origen, "titulo": a.titulo, "descripcion": a.descripcion,
        "plataforma": a.plataforma,
        "documento_id": str(a.documento_id) if a.documento_id else None,
        "sujeto_id": str(a.sujeto_id) if a.sujeto_id else None,
        "contrato_id": str(a.contrato_id) if a.contrato_id else None,
        "leida": a.leida_at is not None, "resuelta": a.resuelta_at is not None,
        "leida_at": a.leida_at.isoformat() if a.leida_at else None,
        "resuelta_at": a.resuelta_at.isoformat() if a.resuelta_at else None,
        "created_at": a.created_at.isoformat(),
    }


def _get_alerta(db: Session, cid: uuid.UUID, alerta_id: uuid.UUID,
                user: User) -> Alerta:
    a = db.get(Alerta, alerta_id)
    if not a or a.company_id != cid:
        raise err(404, "NO_ENCONTRADO", "Alerta no existe")
    if a.contrato_id:
        require_contrato(a.contrato_id, user)
    elif contrato_scope(user):
        # Una alerta sin contrato es de alcance empresa: un contract_admin no la
        # ve, y se responde 404 y no 403 para no revelar su existencia.
        raise err(404, "NO_ENCONTRADO", "Alerta no existe")
    return a


def _base(cid: uuid.UUID, scope: uuid.UUID | None):
    q = select(Alerta).where(Alerta.company_id == cid)
    return q.where(Alerta.contrato_id == scope) if scope else q


@router.get("")
def listar(severidad: str | None = Query(None),
           origen: str | None = Query(None),
           estado: str | None = Query(None),
           contrato_id: uuid.UUID | None = Query(None),
           sujeto_id: uuid.UUID | None = Query(None),
           leida: bool | None = Query(None),
           solo_activas: bool = Query(True),
           p: Page = Depends(paginacion),
           db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(get_current_user)):
    """Alertas de la empresa, la más reciente primero."""
    for valor, permitidos, code in ((severidad, SEVERIDADES, "SEVERIDAD_INVALIDA"),
                                    (origen, ORIGENES, "ORIGEN_INVALIDO"),
                                    (estado, ESTADOS, "ESTADO_INVALIDO")):
        if valor and valor not in permitidos:
            raise err(400, code, f"Valor debe ser uno de: {', '.join(permitidos)}")
    if contrato_id:
        require_contrato(contrato_id, user)

    q = _base(cid, contrato_id or contrato_scope(user))
    if solo_activas:
        q = q.where(Alerta.resuelta_at.is_(None))
    if severidad:
        q = q.where(Alerta.severidad == severidad)
    if origen:
        q = q.where(Alerta.origen == origen)
    if estado:
        q = q.where(Alerta.estado == estado)
    if sujeto_id:
        q = q.where(Alerta.sujeto_id == sujeto_id)
    if leida is not None:
        q = q.where(Alerta.leida_at.is_not(None) if leida
                    else Alerta.leida_at.is_(None))
    if p.search:
        like = f"%{p.search}%"
        q = q.where(or_(Alerta.titulo.ilike(like), Alerta.descripcion.ilike(like)))

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, Alerta, p.sort, ORDEN_ALERTAS, "-created_at")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))
    return sobre([_out(a) for a in filas], total, p)


@router.get("/resumen")
def resumen(db: Session = Depends(get_db),
            cid: uuid.UUID = Depends(get_company_id),
            user: User = Depends(get_current_user)):
    """Contadores de la campana de notificaciones."""
    scope = contrato_scope(user)
    base = select(func.count()).select_from(Alerta).where(Alerta.company_id == cid)
    if scope:
        base = base.where(Alerta.contrato_id == scope)
    hace30 = datetime.now(timezone.utc) - timedelta(days=30)
    return {
        "criticas": db.scalar(base.where(Alerta.severidad == "critica",
                                         Alerta.resuelta_at.is_(None))) or 0,
        "advertencias": db.scalar(base.where(
            Alerta.severidad.in_(("advertencia", "alta")),
            Alerta.resuelta_at.is_(None))) or 0,
        "informativas": db.scalar(base.where(Alerta.severidad == "informativa",
                                             Alerta.resuelta_at.is_(None))) or 0,
        "resueltas_30d": db.scalar(base.where(Alerta.resuelta_at >= hace30)) or 0,
        "no_leidas": db.scalar(base.where(Alerta.leida_at.is_(None),
                                          Alerta.resuelta_at.is_(None))) or 0,
        "activas": db.scalar(base.where(Alerta.resuelta_at.is_(None))) or 0,
    }


@router.patch("/{alerta_id}")
def editar(alerta_id: uuid.UUID, body: AlertaPatch,
           db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(get_current_user)):
    """Marca la alerta como leída, en progreso o resuelta.

    Resolver pone `resuelta_at` y `estado='resuelta'`; reabrir los limpia y
    devuelve la alerta a `nueva`. `estado` permite además dejarla `en_progreso`
    sin cerrarla, que es cómo se reparte el trabajo en un equipo.
    """
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a modificar")
    if data.get("estado") and data["estado"] not in ESTADOS:
        raise err(400, "ESTADO_INVALIDO",
                  f"Estado debe ser uno de: {', '.join(ESTADOS)}")

    a = _get_alerta(db, cid, alerta_id, user)
    now = datetime.now(timezone.utc)
    cambios: list[str] = []
    if body.leida is not None:
        a.leida_at = now if body.leida else None
        cambios.append("leída" if body.leida else "no leída")
    if body.resuelta is not None:
        a.resuelta_at = now if body.resuelta else None
        a.estado = "resuelta" if body.resuelta else "nueva"
        cambios.append("resuelta" if body.resuelta else "reabierta")
    if data.get("estado"):
        a.estado = data["estado"]
        # Coherencia: el estado 'resuelta' arrastra la marca de tiempo, y
        # cualquier otro la limpia. Si no, quedarían filas resueltas sin cuándo.
        if data["estado"] == "resuelta" and a.resuelta_at is None:
            a.resuelta_at = now
        elif data["estado"] != "resuelta":
            a.resuelta_at = None
        cambios.append(f"estado={data['estado']}")
    a.updated_at = now

    actividad.log(db, cid, "actualizacion", "alertas",
                  f"Alerta '{a.titulo}' → {', '.join(cambios)}", user_id=user.id,
                  entidad_tipo="alerta", entidad_id=a.id,
                  plataforma=a.plataforma)
    db.commit()
    return _out(a)


@router.post("/marcar-leidas")
def marcar_leidas(body: MarcarIn, db: Session = Depends(get_db),
                  cid: uuid.UUID = Depends(get_company_id),
                  user: User = Depends(get_current_user)):
    """Marca como leídas las alertas indicadas, o todas las no leídas."""
    q = _base(cid, contrato_scope(user)).where(Alerta.leida_at.is_(None))
    if body.ids:
        q = q.where(Alerta.id.in_(body.ids))
    now = datetime.now(timezone.utc)
    n = 0
    for a in db.scalars(q):
        a.leida_at = now
        a.updated_at = now
        n += 1
    if n:
        actividad.log(db, cid, "visualizacion", "alertas",
                      f"{n} alertas marcadas como leídas", user_id=user.id)
    db.commit()
    return {"marcadas": n}
