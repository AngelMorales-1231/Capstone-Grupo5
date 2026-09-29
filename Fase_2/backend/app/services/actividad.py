import uuid

from sqlalchemy.orm import Session

from ..models import Actividad


def log(
    db: Session, company_id: uuid.UUID, tipo: str, modulo: str, descripcion: str,
    user_id: uuid.UUID | None = None, entidad_tipo: str | None = None,
    entidad_id: uuid.UUID | None = None, plataforma: str | None = None,
) -> None:
    db.add(Actividad(
        company_id=company_id, user_id=user_id, tipo=tipo, modulo=modulo,
        descripcion=descripcion, entidad_tipo=entidad_tipo, entidad_id=entidad_id,
        plataforma=plataforma,
    ))
