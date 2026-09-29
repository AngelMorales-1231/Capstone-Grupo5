"""Dependencias transversales: autenticación, tenant, roles y paginación."""
from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, Query
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session, joinedload

from .config import settings
from .database import auth_session, get_db, set_ctx
from .models import User
from .security import decode_access_token

oauth2 = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


def err(status: int, code: str, message: str, details: list | None = None):
    detail = {"code": code, "message": message}
    if details:
        detail["details"] = details
    return HTTPException(status_code=status, detail=detail)


# ------------------------------------------------------------------ usuario
def get_current_user(token: str | None = Depends(oauth2)) -> User:
    """Resuelve el usuario y fija el contexto de tenant para el resto del request.

    Usa `auth_session()` porque leer `users` exige poder consultar sin tenant.
    Inmediatamente después fija el contexto real, que es el que verán las
    sesiones de negocio (`get_db`) al abrir su transacción.
    """
    if not token:
        raise err(401, "TOKEN_AUSENTE", "Falta el header Authorization")
    try:
        payload = decode_access_token(token)
    except Exception:
        raise err(401, "TOKEN_INVALIDO", "Token inválido o expirado")

    with auth_session() as db:
        # `company` se carga con joinedload porque el User se devuelve DESLIGADO
        # de la sesión: los serializadores (`auth._user_out`) leen
        # `user.company.nombre` ya fuera de ella y una carga diferida ahí
        # levantaría DetachedInstanceError.
        user = db.get(User, uuid.UUID(payload["sub"]),
                      options=[joinedload(User.company)])
        if not user or user.status != "approved" or not user.activo:
            raise err(401, "TOKEN_INVALIDO", "Usuario no válido o desactivado")
        db.expunge(user)

    # Contexto por defecto: la propia empresa del usuario. El admin lo ajusta
    # con X-Company-Id en get_company_id().
    set_ctx(company_id=user.company_id, is_admin=(user.role == "admin"), user_id=user.id)
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise err(403, "SOLO_ADMIN", "Requiere rol administrador")
    return user


def require_company(user: User = Depends(get_current_user)) -> User:
    """Escritura de datos de empresa: rol company o admin impersonando.

    `contract_admin` queda fuera de las mutaciones de alcance empresa; dentro
    de su contrato se le permite operar y eso se verifica con `require_contrato`.
    """
    if user.role not in ("company", "admin"):
        raise err(403, "ROL_INSUFICIENTE",
                  "El administrador de contrato no puede realizar esta operación")
    return user


def get_company_id(
    user: User = Depends(get_current_user),
    x_company_id: str | None = Header(default=None, alias="X-Company-Id"),
) -> uuid.UUID:
    """company_id efectivo. Para el admin proviene del header X-Company-Id."""
    if user.role in ("company", "contract_admin"):
        return user.company_id
    if x_company_id:
        try:
            cid = uuid.UUID(x_company_id)
        except ValueError:
            raise err(400, "COMPANY_ID_INVALIDO", "X-Company-Id no es un UUID")
        # El admin conserva is_admin=true en RLS pero opera sobre esta empresa.
        set_ctx(company_id=cid, is_admin=True, user_id=user.id)
        return cid
    raise err(400, "COMPANY_ID_REQUERIDO",
              "El admin debe indicar X-Company-Id para operar datos de empresa")


def require_contrato(contrato_id: uuid.UUID, user: User) -> None:
    """Un contract_admin solo puede tocar el contrato que tiene asignado.

    Devuelve 404 y no 403 para no filtrar la existencia del recurso (§3.3).
    """
    if user.role == "contract_admin" and user.contrato_id != contrato_id:
        raise err(404, "NO_ENCONTRADO", "Recurso inexistente")


def contrato_scope(user: User) -> uuid.UUID | None:
    """Contrato al que está acotado el usuario, o None si ve todos."""
    return user.contrato_id if user.role == "contract_admin" else None


# --------------------------------------------------------------- paginación
@dataclass
class Page:
    page: int
    page_size: int
    sort: str | None
    search: str | None

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def paginacion(
    page: int = Query(1, ge=1),
    page_size: int = Query(default=settings.page_size_default, ge=1,
                           le=settings.page_size_max),
    sort: str | None = Query(None, description="campo; prefijo - para descendente"),
    search: str | None = Query(None),
) -> Page:
    return Page(page=page, page_size=page_size, sort=sort, search=search)


def sobre(items: list, total: int, p: Page) -> dict:
    """Envoltura estándar de listados (§3.2 de la especificación)."""
    return {
        "items": items,
        "page": p.page,
        "page_size": p.page_size,
        "total": total,
        "total_pages": (total + p.page_size - 1) // p.page_size if p.page_size else 0,
    }


def aplicar_orden(stmt, modelo, sort: str | None, permitidos: set[str], defecto: str):
    """Traduce `sort=-created_at` a ORDER BY, restringido a campos permitidos."""
    campo = sort or defecto
    desc = campo.startswith("-")
    campo = campo.lstrip("-")
    if campo not in permitidos:
        campo, desc = defecto.lstrip("-"), defecto.startswith("-")
    col = getattr(modelo, campo)
    return stmt.order_by(col.desc() if desc else col.asc())


__all__ = [
    "err", "get_db", "get_current_user", "require_admin", "require_company",
    "get_company_id", "require_contrato", "contrato_scope",
    "Page", "paginacion", "sobre", "aplicar_orden", "Session",
]
