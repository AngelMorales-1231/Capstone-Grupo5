"""Preferencias de notificación de la empresa.

`notificacion_preferencias` es una tabla de excepciones, no un espejo de la
configuración: mientras nadie toque nada no hay filas y valen los valores por
defecto de `EVENTOS_NOTIFICABLES`. El GET compone la vista completa (defecto +
configurado) **sin escribir en la base**; crear las filas en una lectura
convertiría un GET en mutación y dejaría cada empresa con siete filas inútiles
que además habría que migrar cada vez que se añade un evento nuevo.

`user_id NULL` es la preferencia de toda la empresa; una fila con `user_id` es la
excepción de esa persona y se devuelve además de la de empresa —el envío decide
cuál usa, no la API—. La clave única de la tabla es
`(company_id, user_id, evento)`, y sobre ella hace upsert el PATCH.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..deps import (contrato_scope, err, get_company_id, get_current_user,
                    get_db, require_company)
from ..models import NotificacionPreferencia, User
from ..services import actividad

logger = logging.getLogger("acredittia.notificaciones")

router = APIRouter(prefix="/notificaciones", tags=["notificaciones"])

# Catálogo de eventos notificables con su etiqueta legible. Es la única fuente
# de esos textos: el frontend los recibe en la respuesta del GET en lugar de
# duplicarlos, de modo que añadir un evento aquí lo hace visible en la interfaz
# sin tocar el cliente. El orden es el de la pantalla de configuración.
EVENTOS_NOTIFICABLES: dict[str, str] = {
    "vencimiento_30": "Documento por vencer en 30 días",
    "vencimiento_15": "Documento por vencer en 15 días",
    "vencimiento_7": "Documento por vencer en 7 días",
    "doc_rechazado": "Documento rechazado en la revisión",
    "alerta_critica": "Alerta crítica de cumplimiento",
    "sync_fallida": "Falló la sincronización con una plataforma",
    "credencial_por_expirar": "Credencial de plataforma por expirar",
}

# Valores por defecto de un evento sin fila propia: aviso por email y nada por
# WhatsApp, que es un canal de coste por mensaje y se activa a mano.
DEFECTO_EMAIL = True
DEFECTO_WHATSAPP = False


# ------------------------------------------------------------------ entradas
class PreferenciaIn(BaseModel):
    evento: str
    canal_email: bool = DEFECTO_EMAIL
    canal_whatsapp: bool = DEFECTO_WHATSAPP
    # NULL/ausente = preferencia de toda la empresa.
    user_id: uuid.UUID | None = None


class PreferenciasIn(BaseModel):
    preferencias: list[PreferenciaIn]


# ------------------------------------------------------------------- helpers
def _out(evento: str, *, user_id: uuid.UUID | None, canal_email: bool,
         canal_whatsapp: bool, origen: str, fila_id: uuid.UUID | None = None,
         updated_at: datetime | None = None) -> dict:
    return {
        "id": str(fila_id) if fila_id else None,
        "evento": evento,
        "descripcion": EVENTOS_NOTIFICABLES.get(evento),
        "user_id": str(user_id) if user_id else None,
        "canal_email": canal_email,
        "canal_whatsapp": canal_whatsapp,
        "origen": origen,
        "updated_at": updated_at.isoformat() if updated_at else None,
    }


def _filas(db: Session, cid: uuid.UUID,
           solo_usuario: uuid.UUID | None) -> list[NotificacionPreferencia]:
    """Filas configuradas de la empresa.

    `solo_usuario` acota a las de empresa más las del propio usuario: un
    contract_admin no tiene por qué ver las excepciones de sus compañeros.
    """
    q = select(NotificacionPreferencia).where(
        NotificacionPreferencia.company_id == cid)
    if solo_usuario:
        q = q.where(or_(NotificacionPreferencia.user_id.is_(None),
                        NotificacionPreferencia.user_id == solo_usuario))
    return list(db.scalars(q))


def _conjunto(db: Session, cid: uuid.UUID,
              solo_usuario: uuid.UUID | None) -> list[dict]:
    """Vista completa: un defecto por evento conocido más lo configurado.

    La fila de empresa (`user_id NULL`) sustituye al defecto del mismo evento;
    las de usuario se añaden como entradas propias. Se conservan los eventos
    desconocidos que hubiera en la tabla (por ejemplo, de una versión anterior)
    marcados como `configurado` y con `descripcion` nula, para que se vean y se
    puedan limpiar en lugar de desaparecer en silencio.
    """
    filas = _filas(db, cid, solo_usuario)
    empresa = {f.evento: f for f in filas if f.user_id is None}

    items: list[dict] = []
    for evento in EVENTOS_NOTIFICABLES:
        f = empresa.get(evento)
        if f is not None:
            items.append(_out(evento, user_id=None, canal_email=f.canal_email,
                              canal_whatsapp=f.canal_whatsapp,
                              origen="configurado", fila_id=f.id,
                              updated_at=f.updated_at))
        else:
            items.append(_out(evento, user_id=None, canal_email=DEFECTO_EMAIL,
                              canal_whatsapp=DEFECTO_WHATSAPP,
                              origen="defecto"))
    for f in filas:
        if f.user_id is None and f.evento in EVENTOS_NOTIFICABLES:
            continue                    # ya está arriba
        items.append(_out(f.evento, user_id=f.user_id,
                          canal_email=f.canal_email,
                          canal_whatsapp=f.canal_whatsapp,
                          origen="configurado", fila_id=f.id,
                          updated_at=f.updated_at))

    orden = list(EVENTOS_NOTIFICABLES)
    items.sort(key=lambda i: (orden.index(i["evento"])
                              if i["evento"] in orden else len(orden),
                              i["user_id"] or ""))
    return items


def _valida_usuario(db: Session, cid: uuid.UUID, uid: uuid.UUID) -> User:
    """El usuario de la preferencia tiene que ser de la empresa (404 si no)."""
    u = db.get(User, uid)
    if not u or u.company_id != cid or u.role == "admin":
        raise err(404, "NO_ENCONTRADO", "Usuario no existe en la empresa")
    return u


def _respuesta(db: Session, cid: uuid.UUID,
               solo_usuario: uuid.UUID | None) -> dict:
    items = _conjunto(db, cid, solo_usuario)
    return {
        "items": items,
        "total": len(items),
        # Catálogo de etiquetas para la interfaz; ver EVENTOS_NOTIFICABLES.
        "eventos": [{"evento": e, "descripcion": d}
                    for e, d in EVENTOS_NOTIFICABLES.items()],
        "defectos": {"canal_email": DEFECTO_EMAIL,
                     "canal_whatsapp": DEFECTO_WHATSAPP},
    }


# --------------------------------------------------------------------- lectura
@router.get("/preferencias")
def listar_preferencias(db: Session = Depends(get_db),
                        cid: uuid.UUID = Depends(get_company_id),
                        user: User = Depends(get_current_user)):
    """Preferencias efectivas de la empresa, con su origen.

    Si la empresa nunca configuró nada se devuelve el conjunto por defecto
    (`origen='defecto'`) sin crear ninguna fila: la tabla solo guarda
    excepciones. No se pagina —son siete eventos— y va acompañado del catálogo
    de etiquetas para que el frontend no duplique los textos.
    """
    # El contract_admin ve la configuración de la empresa y sus propias
    # excepciones, no las del resto del personal.
    solo_usuario = user.id if contrato_scope(user) else None
    return _respuesta(db, cid, solo_usuario)


# ------------------------------------------------------------------- escritura
@router.patch("/preferencias")
def editar_preferencias(body: PreferenciasIn, db: Session = Depends(get_db),
                        cid: uuid.UUID = Depends(get_company_id),
                        user: User = Depends(require_company)):
    """Upsert de preferencias por `(company_id, user_id, evento)`.

    Es un PATCH parcial: lo que no viene en el cuerpo no se toca, y lo que viene
    crea o actualiza la fila. Un evento desconocido es 400 con la lista de
    válidos —no se acepta configurar avisos que nadie enviará— y un `user_id`
    ajeno a la empresa es 404.

    Si el mismo `(user_id, evento)` llega repetido en la petición gana la última
    entrada, en vez de rechazar el lote: el cliente suele enviar el formulario
    completo y un duplicado es un descuido, no un conflicto de datos.
    """
    if not body.preferencias:
        raise err(400, "SIN_DATOS", "No se envió ninguna preferencia")

    desconocidos = sorted({p.evento for p in body.preferencias
                           if p.evento not in EVENTOS_NOTIFICABLES})
    if desconocidos:
        raise err(400, "EVENTO_INVALIDO",
                  "Evento de notificación no reconocido. Válidos: "
                  + ", ".join(EVENTOS_NOTIFICABLES),
                  details=desconocidos)

    # Última entrada por clave.
    entradas: dict[tuple[uuid.UUID | None, str], PreferenciaIn] = {}
    for p in body.preferencias:
        entradas[(p.user_id, p.evento)] = p
    for uid in {k[0] for k in entradas if k[0] is not None}:
        _valida_usuario(db, cid, uid)

    existentes = {(f.user_id, f.evento): f for f in _filas(db, cid, None)}
    ahora = datetime.now(timezone.utc)
    creadas = actualizadas = 0
    for (uid, evento), p in entradas.items():
        f = existentes.get((uid, evento))
        if f is None:
            db.add(NotificacionPreferencia(
                company_id=cid, user_id=uid, evento=evento,
                canal_email=p.canal_email, canal_whatsapp=p.canal_whatsapp))
            creadas += 1
        else:
            f.canal_email = p.canal_email
            f.canal_whatsapp = p.canal_whatsapp
            f.updated_at = ahora
            actualizadas += 1
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "PREFERENCIA_DUPLICADA",
                  "Ya existe una preferencia para ese usuario y evento")

    actividad.log(db, cid, "actualizacion", "notificaciones",
                  f"Preferencias de notificación actualizadas "
                  f"({creadas} nuevas, {actualizadas} modificadas)",
                  user_id=user.id, entidad_tipo="notificacion_preferencia")
    db.commit()
    logger.info("empresa %s: preferencias actualizadas (%s nuevas, %s modificadas)",
                cid, creadas, actualizadas)

    salida = _respuesta(db, cid, None)
    salida["creadas"] = creadas
    salida["actualizadas"] = actualizadas
    return salida
