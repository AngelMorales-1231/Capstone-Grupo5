"""Perfil de la empresa del token y gestión de sus usuarios.

La empresa administra sus propias cuentas: invita usuarios `company` (alcance
empresa completa) y `contract_admin` (acotados a un contrato). Dos invariantes
gobiernan todo el módulo:

1. `users` exige `(role='contract_admin') = (contrato_id IS NOT NULL)`; se valida
   antes de tocar la BD para devolver 400 con un mensaje útil.
2. Toda empresa debe conservar al menos un usuario `company` activo, o se
   quedaría sin nadie que pueda administrarla.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, EmailStr
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..deps import (Page, aplicar_orden, contrato_scope, err, get_company_id,
                    get_current_user, get_db, paginacion, require_company, sobre)
from ..models import (Company, Contrato, Documento, PasswordResetToken,
                      RefreshToken, Sujeto, User)
from ..security import hash_password
from ..services import actividad

logger = logging.getLogger("acredittia.company")

router = APIRouter(prefix="/company", tags=["company"])

# Ventana de la invitación. Es más larga que el reset de contraseña normal
# (settings.reset_ttl_min = 60) porque quien invita no está delante del invitado
# para reenviarle el enlace si caduca.
ACTIVACION_TTL_HORAS = 72

ROLES_INVITABLES = ("company", "contract_admin")
ORDEN_USUARIOS = {"nombre", "email", "role", "created_at", "last_login_at", "activo"}


# ------------------------------------------------------------------ entradas
class CompanyPatch(BaseModel):
    """El RUT y el status son inmutables para la empresa: el primero identifica
    fiscalmente a la cuenta y el segundo solo lo mueve el admin de plataforma."""
    model_config = ConfigDict(extra="forbid")
    nombre: str | None = None
    email: EmailStr | None = None


class UsuarioIn(BaseModel):
    nombre: str
    email: EmailStr
    role: str = "company"
    contrato_id: uuid.UUID | None = None


class UsuarioPatch(BaseModel):
    nombre: str | None = None
    role: str | None = None
    contrato_id: uuid.UUID | None = None
    activo: bool | None = None


# ------------------------------------------------------------------ helpers
def _nuevo_token_activacion() -> tuple[str, str, datetime]:
    """Token de activación: (claro, hash, expiración).

    `security.py` no expone helper de reset —solo de refresh—, así que se genera
    aquí con el mismo esquema: aleatorio en claro para el enlace del email y solo
    su sha256 persistido, de modo que una fuga de la tabla no permita activar
    cuentas ajenas.
    """
    raw = secrets.token_urlsafe(48)
    h = hashlib.sha256(raw.encode()).hexdigest()
    exp = datetime.now(timezone.utc) + timedelta(hours=ACTIVACION_TTL_HORAS)
    return raw, h, exp


def _cumplimiento_empresa(db: Session, cid: uuid.UUID,
                          scope: uuid.UUID | None) -> dict:
    """Cumplimiento global: documentos obligatorios de la empresa en estado ok.

    Se cuenta sobre TODOS los documentos obligatorios no EMSIPOR de la empresa
    (los de ámbito empresa que cuelgan del contrato y los de personal y equipos
    que cuelgan del sujeto). Los EMSIPOR quedan fuera por coherencia con
    `checklist.stats_sujeto`, que los trata como expediente aparte.
    """
    stmt = (
        select(func.count().label("total"),
               func.count().filter(Documento.estado_calc == "ok").label("ok"))
        .select_from(Documento)
        .outerjoin(Sujeto, Sujeto.id == Documento.sujeto_id)
        .where(Documento.company_id == cid,
               Documento.obligatorio.is_(True),
               Documento.es_emsipor.is_(False))
    )
    if scope:
        stmt = stmt.where(or_(Documento.contrato_id == scope,
                             Sujeto.contrato_id == scope))
    fila = db.execute(stmt).one()
    total, ok = int(fila.total or 0), int(fila.ok or 0)
    return {"docs_total": total, "docs_ok": ok,
            "cumplimiento_pct": round(100 * ok / total) if total else 0}


def _company_out(db: Session, c: Company, scope: uuid.UUID | None) -> dict:
    m = _cumplimiento_empresa(db, c.id, scope)
    return {
        "id": str(c.id), "nombre": c.nombre, "rut": c.rut, "email": c.email,
        "status": c.status, "es_demo": c.es_demo,
        "created_at": c.created_at.isoformat(),
        "cumplimiento_pct": m["cumplimiento_pct"],
        "docs_ok": m["docs_ok"], "docs_total": m["docs_total"],
    }


def _usuario_out(u: User, contrato_nombre: str | None = None) -> dict:
    return {
        "id": str(u.id), "nombre": u.nombre, "email": u.email, "role": u.role,
        "contrato_id": str(u.contrato_id) if u.contrato_id else None,
        "contrato_nombre": contrato_nombre,
        "activo": u.activo, "status": u.status,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "created_at": u.created_at.isoformat(),
    }


def _get_company(db: Session, cid: uuid.UUID) -> Company:
    c = db.get(Company, cid)
    if not c or c.id != cid:
        raise err(404, "NO_ENCONTRADO", "Empresa no existe")
    return c


def _get_usuario(db: Session, cid: uuid.UUID, uid: uuid.UUID) -> User:
    """Un usuario de otra empresa devuelve 404, no 403 (§3.3)."""
    u = db.get(User, uid)
    if not u or u.company_id != cid or u.role == "admin":
        raise err(404, "NO_ENCONTRADO", "Usuario no existe")
    return u


def _valida_contrato(db: Session, cid: uuid.UUID,
                     contrato_id: uuid.UUID) -> Contrato:
    """El contrato del contract_admin tiene que ser de la propia empresa."""
    c = db.get(Contrato, contrato_id)
    if not c or c.company_id != cid:
        raise err(400, "CONTRATO_INVALIDO",
                  "El contrato indicado no pertenece a la empresa")
    return c


def _valida_rol(role: str, contrato_id: uuid.UUID | None) -> None:
    """Refleja el CHECK de `users`: el contrato es obligatorio y exclusivo del
    contract_admin."""
    if role not in ROLES_INVITABLES:
        raise err(400, "ROL_INVALIDO",
                  "role debe ser 'company' o 'contract_admin'")
    if role == "contract_admin" and not contrato_id:
        raise err(400, "CONTRATO_REQUERIDO",
                  "Un administrador de contrato necesita contrato_id")
    if role == "company" and contrato_id:
        raise err(400, "CONTRATO_NO_APLICA",
                  "Un usuario de empresa no puede estar acotado a un contrato")


def _otros_admins_activos(db: Session, cid: uuid.UUID,
                          excluir: uuid.UUID) -> int:
    """Cuántos usuarios `company` activos quedarían aparte del indicado."""
    return db.scalar(
        select(func.count()).select_from(User).where(
            User.company_id == cid, User.role == "company",
            User.activo.is_(True), User.id != excluir)
    ) or 0


def _proteger_ultimo_admin(db: Session, cid: uuid.UUID, u: User) -> None:
    if u.role != "company" or not u.activo:
        return
    if _otros_admins_activos(db, cid, u.id) == 0:
        raise err(400, "ULTIMO_ADMINISTRADOR",
                  "La empresa debe conservar al menos un usuario 'company' "
                  "activo; asigna otro administrador antes de este cambio")


# ------------------------------------------------------------------- perfil
@router.get("")
def perfil(db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(get_current_user)):
    """Perfil de la empresa del token con su cumplimiento global.

    Para un contract_admin el porcentaje se calcula solo con los documentos de
    su contrato: no debe ver el estado consolidado de la empresa.
    """
    c = _get_company(db, cid)
    return _company_out(db, c, contrato_scope(user))


@router.patch("")
def editar_perfil(body: CompanyPatch, db: Session = Depends(get_db),
                  cid: uuid.UUID = Depends(get_company_id),
                  user: User = Depends(require_company)):
    """Actualiza los datos de contacto. RUT y status no son editables aquí."""
    c = _get_company(db, cid)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a actualizar")
    for k, v in data.items():
        setattr(c, k, v)
    c.updated_at = datetime.now(timezone.utc)
    actividad.log(db, cid, "actualizacion", "empresa",
                  f"Perfil de empresa actualizado ({', '.join(sorted(data))})",
                  user_id=user.id, entidad_tipo="empresa", entidad_id=c.id)
    db.commit()
    return _company_out(db, c, contrato_scope(user))


# ----------------------------------------------------------------- usuarios
@router.get("/usuarios")
def listar_usuarios(role: str | None = Query(None),
                    activo: bool | None = Query(None),
                    p: Page = Depends(paginacion),
                    db: Session = Depends(get_db),
                    cid: uuid.UUID = Depends(get_company_id),
                    user: User = Depends(get_current_user)):
    """Usuarios de la empresa. Un contract_admin solo ve los de su contrato."""
    q = (select(User, Contrato.nombre.label("contrato_nombre"))
         .outerjoin(Contrato, Contrato.id == User.contrato_id)
         .where(User.company_id == cid, User.role != "admin"))
    scope = contrato_scope(user)
    if scope:
        q = q.where(User.contrato_id == scope)
    if role:
        q = q.where(User.role == role)
    if activo is not None:
        q = q.where(User.activo.is_(activo))
    if p.search:
        like = f"%{p.search}%"
        q = q.where(or_(User.nombre.ilike(like), User.email.ilike(like)))

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, User, p.sort, ORDEN_USUARIOS, "nombre")
    filas = db.execute(q.offset(p.offset).limit(p.page_size)).all()
    items = [_usuario_out(f[0], f.contrato_nombre) for f in filas]
    return sobre(items, total, p)


@router.post("/usuarios", status_code=201)
def invitar_usuario(body: UsuarioIn, db: Session = Depends(get_db),
                    cid: uuid.UUID = Depends(get_company_id),
                    user: User = Depends(require_company)):
    """Invita un usuario a la empresa.

    No se fija contraseña: se guarda un secreto aleatorio inservible y se emite
    un token de activación de un solo uso, que el invitado canjea por su clave.
    El usuario hereda el `status` de la empresa para que la aprobación de la
    cuenta siga siendo una decisión única del admin de plataforma.
    """
    c = _get_company(db, cid)
    if c.status != "approved":
        raise err(403, "EMPRESA_NO_APROBADA",
                  "La empresa debe estar aprobada para invitar usuarios")
    _valida_rol(body.role, body.contrato_id)
    if body.contrato_id:
        _valida_contrato(db, cid, body.contrato_id)
    # El email es único a nivel de plataforma. La comprobación previa solo
    # alcanza a los usuarios visibles por RLS (la propia empresa); para el resto
    # el 409 lo produce el IntegrityError de más abajo.
    if db.scalar(select(User).where(User.email == body.email)):
        raise err(409, "USUARIO_DUPLICADO", "El email ya está registrado")

    u = User(
        email=str(body.email), nombre=body.nombre, role=body.role,
        company_id=cid, contrato_id=body.contrato_id,
        # Contraseña aleatoria que nadie conoce: la cuenta solo se puede usar
        # después de canjear el token de activación.
        password_hash=hash_password(secrets.token_urlsafe(32)),
        activo=True, status=c.status,
    )
    db.add(u)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "USUARIO_DUPLICADO", "El email ya está registrado")

    _, token_hash, expira = _nuevo_token_activacion()
    db.add(PasswordResetToken(user_id=u.id, token_hash=token_hash,
                              expires_at=expira))
    actividad.log(db, cid, "creacion", "usuarios",
                  f"Usuario {u.email} invitado con rol {u.role}",
                  user_id=user.id, entidad_tipo="usuario", entidad_id=u.id)
    db.commit()
    # TODO(coordinador): enviar el email de invitación con el token en claro
    # (`raw` de _nuevo_token_activacion) cuando exista services/notificaciones.
    # El token en claro NO se devuelve en la respuesta a propósito.
    logger.info("usuario invitado id=%s role=%s", u.id, u.role)
    return {"id": str(u.id), "email": u.email, "role": u.role,
            "activacion_expira_at": expira.isoformat()}


@router.patch("/usuarios/{uid}")
def editar_usuario(uid: uuid.UUID, body: UsuarioPatch,
                   db: Session = Depends(get_db),
                   cid: uuid.UUID = Depends(get_company_id),
                   user: User = Depends(require_company)):
    """Cambia nombre, rol, contrato o actividad de un usuario.

    Pasar a `company` libera el contrato; pasar a `contract_admin` lo exige. Se
    rechaza cualquier cambio que deje a la empresa sin administrador activo.
    """
    u = _get_usuario(db, cid, uid)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a actualizar")

    role = data.get("role", u.role)
    contrato_id = data.get("contrato_id", u.contrato_id)
    if role == "company":
        contrato_id = None          # el ámbito empresa no se acota a contratos
    _valida_rol(role, contrato_id)
    if contrato_id and contrato_id != u.contrato_id:
        _valida_contrato(db, cid, contrato_id)

    degrada = role != "company"
    desactiva = data.get("activo") is False
    if degrada or desactiva:
        _proteger_ultimo_admin(db, cid, u)

    if "nombre" in data:
        u.nombre = data["nombre"]
    if "activo" in data:
        u.activo = data["activo"]
    u.role = role
    u.contrato_id = contrato_id
    u.updated_at = datetime.now(timezone.utc)

    actividad.log(db, cid, "actualizacion", "usuarios",
                  f"Usuario {u.email} actualizado ({', '.join(sorted(data))})",
                  user_id=user.id, entidad_tipo="usuario", entidad_id=u.id)
    db.commit()
    nombre_contrato = None
    if u.contrato_id:
        c = db.get(Contrato, u.contrato_id)
        nombre_contrato = c.nombre if c else None
    return _usuario_out(u, nombre_contrato)


@router.delete("/usuarios/{uid}")
def desactivar_usuario(uid: uuid.UUID, db: Session = Depends(get_db),
                       cid: uuid.UUID = Depends(get_company_id),
                       user: User = Depends(require_company)):
    """Baja lógica: el usuario nunca se borra para no perder la trazabilidad de
    `actividad`, que referencia su id. Se revocan sus refresh tokens para que la
    sesión abierta muera en el siguiente refresco."""
    u = _get_usuario(db, cid, uid)
    _proteger_ultimo_admin(db, cid, u)
    ahora = datetime.now(timezone.utc)
    u.activo = False
    u.updated_at = ahora
    revocados = 0
    for rt in db.scalars(select(RefreshToken).where(
            RefreshToken.user_id == u.id, RefreshToken.revoked_at.is_(None))):
        rt.revoked_at = ahora
        revocados += 1
    actividad.log(db, cid, "actualizacion", "usuarios",
                  f"Usuario {u.email} desactivado", user_id=user.id,
                  entidad_tipo="usuario", entidad_id=u.id)
    db.commit()
    return {"ok": True, "id": str(u.id), "activo": False,
            "tokens_revocados": revocados}
