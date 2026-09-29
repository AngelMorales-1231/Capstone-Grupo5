import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from pwdlib import PasswordHash

from .config import settings

pwd = PasswordHash.recommended()


def hash_password(plain: str) -> str:
    return pwd.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return pwd.verify(plain, hashed)
    except Exception:
        return False


def create_access_token(user_id: uuid.UUID, role: str, company_id: uuid.UUID | None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "role": role,
        "company_id": str(company_id) if company_id else None,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_ttl_min),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])


def new_refresh_token() -> tuple[str, str, datetime]:
    """Devuelve (token_en_claro, hash, expiración)."""
    raw = secrets.token_urlsafe(48)
    h = hashlib.sha256(raw.encode()).hexdigest()
    exp = datetime.now(timezone.utc) + timedelta(days=settings.refresh_ttl_days)
    return raw, h, exp


def hash_refresh(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def validar_rut(rut: str) -> bool:
    """Valida formato 99.999.999-K y dígito verificador (módulo 11)."""
    import re
    if not re.fullmatch(r"\d{1,2}\.\d{3}\.\d{3}-[\dkK]", rut):
        return False
    cuerpo = rut.split("-")[0].replace(".", "")
    dv = rut.split("-")[1].lower()
    suma, factor = 0, 2
    for c in reversed(cuerpo):
        suma += int(c) * factor
        factor = 2 if factor == 7 else factor + 1
    resto = 11 - (suma % 11)
    esperado = "0" if resto == 11 else "k" if resto == 10 else str(resto)
    return dv == esperado
