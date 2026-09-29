"""Autenticación, sesión y autoservicio de la cuenta (§4 de la especificación).

Tres decisiones que condicionan el contrato de este módulo:

* **El olvido de contraseña nunca revela si el email existe.** `POST
  /auth/password/forgot` responde siempre 200 con el mismo mensaje. Enumerar
  cuentas de una plataforma de acreditación equivale a enumerar los contratistas
  de una faena, así que el endpoint es deliberadamente opaco.
* **Los tokens de reset se guardan hasheados.** En `password_reset_tokens` solo
  vive el SHA-256; el valor en claro se envía por email y no queda en la BD. Al
  emitir uno nuevo se invalidan los anteriores no usados de ese usuario.
* **Cambiar la contraseña cierra las sesiones.** Tanto el reset como el cambio
  desde `PATCH /auth/me` revocan los refresh tokens: si la contraseña se cambió
  porque alguien la conocía, dejar sus refresh vivos anula el cambio.

Las rutas que resuelven credenciales sin tenant (`forgot`, `reset`) usan
`auth_session()`: hay que poder buscar en `users` por email antes de saber a qué
empresa pertenece la cuenta.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, EmailStr
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import auth_session, get_db
from ..deps import err, get_current_user
from ..models import Company, Contrato, PasswordResetToken, RefreshToken, User
from ..security import (
    create_access_token, hash_password, hash_refresh, new_refresh_token,
    validar_rut, verify_password,
)
from ..services import actividad

logger = logging.getLogger("acredittia.auth")

router = APIRouter(prefix="/auth", tags=["auth"])

# Respuesta única de /password/forgot: idéntica exista o no la cuenta.
MSG_FORGOT = ("Si el email corresponde a una cuenta activa, enviaremos un "
              "enlace para restablecer la contraseña.")

MSG_PASSWORD = "Mínimo 8 caracteres, una mayúscula y un dígito"


class RegisterIn(BaseModel):
    empresa: str
    rut: str
    email: EmailStr
    password: str


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class ForgotIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str
    password: str


class MePatch(BaseModel):
    nombre: str | None = None
    email: EmailStr | None = None
    password: str | None = None
    password_actual: str | None = None
    # Refresh token de la sesión en curso, para no cerrarla al cambiar la
    # contraseña. El access token no permite identificarla (los refresh viven en
    # otra tabla y no van en el JWT), así que si no se envía se revocan todos.
    refresh_token: str | None = None


# ------------------------------------------------------------------- helpers
def _valida_password(plain: str, *, status: int = 400) -> None:
    """Política mínima de contraseña (§4). Sube 400 en reset y 422 en registro."""
    if (len(plain or "") < 8 or not any(c.isupper() for c in plain)
            or not any(c.isdigit() for c in plain)):
        raise err(status, "PASSWORD_DEBIL", MSG_PASSWORD)


def _hash_reset(raw: str) -> str:
    """Solo el hash llega a la BD; el token en claro viaja por email."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _revocar_refresh(db: Session, user_id, *, excepto_hash: str | None = None) -> int:
    """Revoca los refresh tokens vivos del usuario. Devuelve cuántos cerró."""
    ahora = datetime.now(timezone.utc)
    filas = db.scalars(select(RefreshToken).where(
        RefreshToken.user_id == user_id,
        RefreshToken.revoked_at.is_(None),
    )).all()
    n = 0
    for rt in filas:
        if excepto_hash and rt.token_hash == excepto_hash:
            continue
        rt.revoked_at = ahora
        n += 1
    return n


def _contrato_out(db: Session, u: User) -> dict | None:
    """Contrato asignado a un contract_admin, para que el frontend lo titule."""
    if u.role != "contract_admin" or not u.contrato_id:
        return None
    c = db.get(Contrato, u.contrato_id)
    if not c:
        return None
    return {"id": str(c.id), "nombre": c.nombre, "codigo": c.codigo}


def _user_out(u: User, contrato: dict | None = None) -> dict:
    return {
        "id": str(u.id), "email": u.email, "role": u.role,
        "nombre": u.nombre, "activo": u.activo,
        "contrato_id": str(u.contrato_id) if u.contrato_id else None,
        "contrato": contrato,
        "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None,
        "company": {
            "id": str(u.company.id), "nombre": u.company.nombre,
            "rut": u.company.rut,
        } if u.company else None,
    }


def _email_ocupado(email: str, excepto_id) -> bool:
    """Unicidad global de `users.email`.

    Se consulta con `auth_session()` a propósito: bajo RLS la sesión de un
    usuario de empresa no vería un email ya usado en otra empresa y el conflicto
    aparecería como IntegrityError al hacer commit.
    """
    with auth_session() as adb:
        otro = adb.scalar(select(User.id).where(User.email == email))
    return otro is not None and otro != excepto_id


# ------------------------------------------------------------------- registro
@router.post("/register", status_code=201)
def register(body: RegisterIn):
    """Alta de una empresa y de su primer usuario, en estado `pending`.

    Usa `auth_session()` y no `get_db()`: el registro es anónimo, así que no hay
    tenant en el contexto y las políticas de RLS de `companies` y `users`
    (`company_id = app_company_id()`) rechazarían tanto la comprobación de
    duplicados como el propio INSERT.
    """
    _valida_password(body.password, status=422)
    if not validar_rut(body.rut):
        raise err(422, "RUT_INVALIDO", "RUT inválido; use formato 76.543.210-9")
    with auth_session() as db:
        if db.scalar(select(User).where(User.email == body.email)):
            raise err(409, "EMAIL_EN_USO", "El email ya está registrado")
        if db.scalar(select(Company).where(Company.rut == body.rut)):
            raise err(409, "RUT_EN_USO", "El RUT ya está registrado")
        company = Company(nombre=body.empresa, rut=body.rut, email=body.email)
        db.add(company)
        db.flush()
        user = User(email=body.email, password_hash=hash_password(body.password),
                    role="company", company_id=company.id)
        db.add(user)
        db.commit()
        salida = {"user_id": str(user.id), "company_id": str(company.id)}
    salida.update(status="pending",
                  message="Solicitud en revisión. Te notificaremos al ser aprobada.")
    return salida


# ---------------------------------------------------------------------- login
@router.post("/login")
def login(body: LoginIn):
    """Emite el par de tokens. Rechaza cuentas no aprobadas o desactivadas.

    `activo=False` es distinto de `status='rejected'`: la empresa desactivó al
    usuario desde `/company/usuarios` sin borrarlo, y debe poder reactivarlo.

    Igual que el registro, se resuelve con `auth_session()`: hay que localizar
    al usuario por email antes de saber a qué empresa pertenece, y bajo RLS una
    sesión sin tenant no ve ninguna fila de `users`.
    """
    with auth_session() as db:
        user = db.scalar(select(User).where(User.email == body.email))
        if not user or not verify_password(body.password, user.password_hash):
            raise err(401, "CREDENCIALES_INVALIDAS", "Email o contraseña incorrectos")
        if user.status == "pending":
            raise err(403, "ACCOUNT_PENDING", "Tu solicitud está en revisión")
        if user.status == "rejected":
            raise err(403, "ACCOUNT_REJECTED",
                      "Solicitud rechazada. Contacta a soporte@acredittia.cl")
        if not user.activo:
            raise err(403, "CUENTA_DESACTIVADA",
                      "Tu cuenta está desactivada. Contacta al administrador de tu empresa")
        raw, h, exp = new_refresh_token()
        db.add(RefreshToken(user_id=user.id, token_hash=h, expires_at=exp))
        user.last_login_at = datetime.now(timezone.utc)
        db.commit()
        return {
            "access_token": create_access_token(user.id, user.role, user.company_id),
            "refresh_token": raw,
            "token_type": "bearer",
            "user": _user_out(user, _contrato_out(db, user)),
        }


@router.post("/refresh")
def refresh(body: RefreshIn):
    """Rota el refresh token. Sin tenant en contexto: ver `login`."""
    with auth_session() as db:
        h = hash_refresh(body.refresh_token)
        rt = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == h))
        now = datetime.now(timezone.utc)
        if not rt or rt.revoked_at or rt.expires_at < now:
            raise err(401, "REFRESH_INVALIDO", "Refresh token inválido o expirado")
        user = db.get(User, rt.user_id)
        if not user or user.status != "approved" or not user.activo:
            raise err(401, "REFRESH_INVALIDO", "Usuario no válido o desactivado")
        rt.revoked_at = now  # rotación
        raw, h2, exp = new_refresh_token()
        db.add(RefreshToken(user_id=user.id, token_hash=h2, expires_at=exp))
        db.commit()
        return {
            "access_token": create_access_token(user.id, user.role, user.company_id),
            "refresh_token": raw,
            "token_type": "bearer",
        }


@router.post("/logout")
def logout(body: RefreshIn, db: Session = Depends(get_db),
           user: User = Depends(get_current_user)):
    rt = db.scalar(select(RefreshToken).where(
        RefreshToken.token_hash == hash_refresh(body.refresh_token)))
    if rt and rt.user_id == user.id:
        rt.revoked_at = datetime.now(timezone.utc)
        db.commit()
    return {"ok": True}


# ------------------------------------------------------ recuperar contraseña
@router.post("/password/forgot")
def password_forgot(body: ForgotIn):
    """Emite un token de restablecimiento. Respuesta idéntica exista o no la cuenta.

    Solo se emite para cuentas aprobadas y activas; en cualquier otro caso el
    endpoint no hace nada y responde lo mismo, de modo que no sirva para
    enumerar usuarios ni para averiguar el estado de una solicitud.
    """
    salida: dict = {"ok": True, "message": MSG_FORGOT}

    # Sin tenant todavía: hay que buscar por email a ciegas.
    with auth_session() as db:
        user = db.scalar(select(User).where(User.email == body.email))
        if user and user.status == "approved" and user.activo:
            ahora = datetime.now(timezone.utc)
            # Un token nuevo invalida los anteriores: si el usuario pidió el
            # enlace dos veces, solo el último debe servir.
            for viejo in db.scalars(select(PasswordResetToken).where(
                    PasswordResetToken.user_id == user.id,
                    PasswordResetToken.used_at.is_(None))):
                viejo.used_at = ahora
            raw = secrets.token_urlsafe(48)
            db.add(PasswordResetToken(
                user_id=user.id, token_hash=_hash_reset(raw),
                expires_at=ahora + timedelta(minutes=settings.reset_ttl_min),
            ))
            db.commit()
            # TODO(notificaciones §17): enviar el email con el enlace
            # {frontend}/reset?token=<raw> usando SMTP_* y `email_from`. Hoy no
            # hay transporte configurado: sin el bloque de desarrollo de abajo el
            # token quedaría inalcanzable.
            logger.info("token de reset emitido para user=%s", user.id)
            if settings.jwt_secret.startswith("dev-"):
                # Solo con el secreto de desarrollo por defecto. En producción
                # JWT_SECRET es otro y esta clave no aparece nunca: devolver el
                # token en la respuesta HTTP haría inútil el envío por email.
                salida["_dev_token"] = raw
                salida["_dev_nota"] = ("Solo en desarrollo (JWT_SECRET dev-*): "
                                       "no hay SMTP configurado para enviar el enlace")
    return salida


@router.post("/password/reset")
def password_reset(body: ResetIn):
    """Consume un token de restablecimiento y cambia la contraseña.

    Al cambiarla se revocan TODOS los refresh tokens del usuario: el reset se
    pide precisamente cuando se sospecha que la contraseña anterior está
    comprometida, y un refresh vivo mantendría abierta la sesión del atacante.
    """
    _valida_password(body.password)

    with auth_session() as db:
        prt = db.scalar(select(PasswordResetToken).where(
            PasswordResetToken.token_hash == _hash_reset(body.token or "")))
        ahora = datetime.now(timezone.utc)
        if not prt or prt.used_at or prt.expires_at < ahora:
            raise err(400, "TOKEN_INVALIDO", "El enlace es inválido o expiró")
        user = db.get(User, prt.user_id)
        if not user or user.status != "approved" or not user.activo:
            raise err(400, "TOKEN_INVALIDO", "El enlace es inválido o expiró")

        user.password_hash = hash_password(body.password)
        user.updated_at = ahora
        prt.used_at = ahora
        sesiones = _revocar_refresh(db, user.id)
        if user.company_id:
            actividad.log(db, user.company_id, "actualizacion", "auth",
                          "Contraseña restablecida con enlace de recuperación",
                          user_id=user.id, entidad_tipo="usuario",
                          entidad_id=user.id)
        db.commit()
        logger.info("contraseña restablecida user=%s sesiones_cerradas=%s",
                    user.id, sesiones)
    return {"ok": True, "sesiones_cerradas": sesiones,
            "message": "Contraseña actualizada. Inicia sesión nuevamente."}


# ------------------------------------------------------------------- mi cuenta
@router.get("/me")
def me(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Perfil del usuario autenticado.

    Para un contract_admin se incluye el contrato al que está acotado: el
    frontend lo necesita para rotular la cabecera sin pedir `/contratos`, que
    para ese rol devolvería una sola fila.
    """
    return _user_out(user, _contrato_out(db, user))


@router.patch("/me")
def editar_me(body: MePatch, db: Session = Depends(get_db),
              user: User = Depends(get_current_user)):
    """Autoservicio del perfil: nombre, email y contraseña.

    Cambiar la contraseña exige la actual (401 si no coincide) y revoca los
    demás refresh tokens; el rol, la empresa y el contrato asignado NO se editan
    aquí, eso es competencia de `/company/usuarios` y de `/admin`.
    """
    data = body.model_dump(exclude_unset=True)
    if not any(k in data for k in ("nombre", "email", "password")):
        raise err(400, "SIN_CAMBIOS",
                  "Indique al menos nombre, email o password")

    u = db.get(User, user.id)
    if not u:                       # el usuario se borró entre el token y aquí
        raise err(401, "TOKEN_INVALIDO", "Usuario no válido")

    cambios: list[str] = []
    sesiones = 0

    if "nombre" in data:
        u.nombre = (data["nombre"] or "").strip() or None
        cambios.append("nombre")

    if "email" in data and data["email"] and data["email"] != u.email:
        nuevo = str(data["email"]).strip().lower()
        if _email_ocupado(nuevo, u.id):
            raise err(409, "EMAIL_EN_USO", "El email ya está registrado")
        u.email = nuevo
        cambios.append("email")

    if data.get("password"):
        if not body.password_actual:
            raise err(401, "PASSWORD_ACTUAL_REQUERIDA",
                      "Debe indicar la contraseña actual para cambiarla")
        if not verify_password(body.password_actual, u.password_hash):
            raise err(401, "PASSWORD_ACTUAL_INVALIDA",
                      "La contraseña actual no coincide")
        _valida_password(data["password"])
        u.password_hash = hash_password(data["password"])
        sesiones = _revocar_refresh(
            db, u.id,
            excepto_hash=(hash_refresh(body.refresh_token)
                          if body.refresh_token else None))
        cambios.append("contraseña")

    u.updated_at = datetime.now(timezone.utc)
    if u.company_id:
        actividad.log(db, u.company_id, "actualizacion", "auth",
                      f"Perfil actualizado ({', '.join(cambios)})",
                      user_id=u.id, entidad_tipo="usuario", entidad_id=u.id)
    db.commit()
    out = _user_out(u, _contrato_out(db, u))
    out["cambios"] = cambios
    out["sesiones_cerradas"] = sesiones
    return out
