"""Licencia Interna de Manejo y expediente EMSIPOR del conductor (§11).

En la minería chilena conducir dentro de la faena exige una licencia INTERNA que
emite el mandante, distinta de la municipal. Para obtenerla el contratista debe
completar un expediente de nueve documentos que se tramitan en cuatro
plataformas distintas (SIGA, DIRECTIC, Academia MLP y EMSIPOR), y de ahí que este
recurso exista aparte del checklist normal del trabajador: los nueve documentos
llevan `documentos.es_emsipor = true` y se excluyen del cumplimiento general
(`checklist.stats_sujeto`), porque un trabajador que no conduce está 100 %
acreditado sin ellos.

Tres reglas que este módulo concentra:

* **Solo aplica a quien lo necesita.** Se comprueba con
  `checklist.requiere_emsipor`, que consulta `cargos.requiere_emsipor` y admite
  `sujetos.es_conductor` como override por trabajador. Para el resto: 409.
* **`estado` es derivado, no editable.** Se recalcula a partir de `numero` y
  `vence` en cada escritura; el cliente no lo envía.
* **`emsipor_estado` resume el expediente**, no la licencia: `aprobado` cuando
  están todos los documentos obligatorios en `ok`, `parcial` con alguno, y
  `pendiente` sin ninguno.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import UMBRAL_PORVENC_DIAS
from ..database import get_db
from ..deps import err, get_company_id, get_current_user
from ..models import Documento, DocumentoArchivo, LicenciaInterna, Sujeto, User
from ..services import actividad
from ..services.checklist import instanciar_docs, requiere_emsipor
from ..services.storage import get_storage

logger = logging.getLogger("acredittia.licencia_interna")

# El sujeto va en el prefijo: todo el módulo opera sobre un único trabajador.
router = APIRouter(prefix="/personal/{sid}/licencia-interna",
                   tags=["licencia interna"])

MSG_NO_APLICA = ("El trabajador no requiere Licencia Interna de Manejo: su cargo "
                 "no lo exige y no está marcado como conductor")


class LicenciaPatch(BaseModel):
    numero: str | None = None
    vence: date | None = None


# ------------------------------------------------------------------- helpers
def _get_conductor(db: Session, cid: uuid.UUID, sid: uuid.UUID,
                   user: User) -> Sujeto:
    """Trabajador que requiere expediente EMSIPOR, o 404 / 409."""
    from .sujetos import _get_sujeto

    s = _get_sujeto(db, cid, sid, "trabajador", user)
    if not requiere_emsipor(db, s):
        raise err(409, "NO_REQUIERE_EMSIPOR", MSG_NO_APLICA)
    return s


def _docs_emsipor(db: Session, s: Sujeto) -> list[Documento]:
    return list(db.scalars(
        select(Documento).where(Documento.company_id == s.company_id,
                                Documento.sujeto_id == s.id,
                                Documento.es_emsipor.is_(True))
        .order_by(Documento.titulo)))


def _calc_estado(numero: str | None, vence: date | None) -> str:
    """Estado de la licencia (`lim_estado`).

    Sin número la licencia todavía no se ha emitido: `pendiente`. Con número y
    sin fecha se considera `vigente`, porque no hay nada que pueda caducar.

    LIMITACIÓN: el enum `lim_estado` no tiene un valor «vencida», así que una
    licencia ya caducada se devuelve como `por_vencer` (el umbral incluye los
    días negativos). El vencimiento real está en `vence` y el frontend puede
    distinguirlo comparando con hoy.
    """
    if not (numero or "").strip():
        return "pendiente"
    if vence is None:
        return "vigente"
    return ("por_vencer"
            if vence <= date.today() + timedelta(days=UMBRAL_PORVENC_DIAS)
            else "vigente")


def _calc_emsipor(docs: list[Documento]) -> str:
    """Estado del expediente (`emsipor_estado`) a partir de sus documentos.

    Se cuenta por `estado` ('ok' / 'falta'), NO por `estado_calc`, para coincidir
    exactamente con el trigger `fn_sync_emsipor` (03_triggers.sql §3) y con el
    chequeo 8 de `05_verificacion.sql`, que usan
    `count(*) FILTER (WHERE obligatorio AND estado = 'ok')`. Con `estado_calc` un
    documento aprobado pero próximo a vencer no sumaba, el valor que escribe la
    aplicación difería del que deriva la base y el chequeo de integridad marcaba
    la fila como desincronizada en cada corrida.
    """
    oblig = [d for d in docs if d.obligatorio] or docs
    if not oblig:
        return "pendiente"
    ok = sum(1 for d in oblig if d.estado == "ok")
    if ok == len(oblig):
        return "aprobado"
    return "parcial" if ok else "pendiente"


def _asegurar_licencia(db: Session, s: Sujeto) -> LicenciaInterna:
    """Devuelve el expediente del trabajador, creándolo si aún no existe.

    Puede faltar si el cargo pasó a exigir EMSIPOR después del alta: se crea aquí
    con los nueve documentos en vez de obligar a un PATCH previo del sujeto.
    """
    lim = db.scalar(select(LicenciaInterna).where(
        LicenciaInterna.company_id == s.company_id,
        LicenciaInterna.sujeto_id == s.id))
    if lim is not None:
        return lim
    from ..services.checklist import crear_expediente_conductor

    n = crear_expediente_conductor(db, s)
    db.flush()
    lim = db.scalar(select(LicenciaInterna).where(
        LicenciaInterna.company_id == s.company_id,
        LicenciaInterna.sujeto_id == s.id))
    if lim is None:
        # No debería ocurrir: `crear_expediente_conductor` solo omite la
        # inserción si la fila ya existe, y acabamos de comprobar que no.
        logger.error("no se pudo crear el expediente EMSIPOR del sujeto %s", s.id)
        raise err(500, "EXPEDIENTE_NO_CREADO",
                  "No se pudo inicializar el expediente EMSIPOR")
    logger.info("expediente EMSIPOR creado al vuelo para sujeto=%s (%s docs)",
                s.id, n)
    return lim


def _out(db: Session, s: Sujeto, lim: LicenciaInterna) -> dict:
    from .documentos import doc_out

    docs = _docs_emsipor(db, s)
    ok = sum(1 for d in docs if d.estado_calc == "ok")
    return {
        "sujeto": {"id": str(s.id), "nombre": s.nombre, "rut": s.rut,
                   "cargo": s.cargo, "es_conductor": s.es_conductor,
                   "contrato_id": str(s.contrato_id)},
        "numero": lim.numero,
        "estado": lim.estado,
        "vence": lim.vence.isoformat() if lim.vence else None,
        "dias_para_vencer": ((lim.vence - date.today()).days
                             if lim.vence else None),
        "emsipor_estado": lim.emsipor_estado,
        "checklist": [{
            **doc_out(d),
            # La plataforma en la que se tramita cada documento es lo que hace
            # navegable el expediente: son cuatro sistemas distintos.
            "plataforma": d.template.plataforma if d.template else None,
        } for d in docs],
        "resumen": {"total": len(docs), "ok": ok, "faltan": len(docs) - ok},
    }


def _sincronizar(db: Session, s: Sujeto, lim: LicenciaInterna) -> None:
    """Recalcula los dos estados derivados del expediente."""
    lim.estado = _calc_estado(lim.numero, lim.vence)
    lim.emsipor_estado = _calc_emsipor(_docs_emsipor(db, s))
    lim.updated_at = datetime.now(timezone.utc)


# ------------------------------------------------------------------ endpoints
@router.get("")
def detalle(sid: uuid.UUID, db: Session = Depends(get_db),
            cid: uuid.UUID = Depends(get_company_id),
            user: User = Depends(get_current_user)):
    """Licencia interna del trabajador y checklist de los nueve documentos EMSIPOR.

    Los estados derivados se recalculan al leer: los documentos pudieron cambiar
    (una subida, el job nocturno de vencimientos) sin que nadie tocara el
    expediente, y devolver un `emsipor_estado` obsoleto sería peor que escribirlo.
    """
    s = _get_conductor(db, cid, sid, user)
    lim = _asegurar_licencia(db, s)
    _sincronizar(db, s, lim)
    db.commit()
    return _out(db, s, lim)


@router.patch("")
def editar(sid: uuid.UUID, body: LicenciaPatch, db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(get_current_user)):
    """Registra el número y el vencimiento de la licencia que emitió el mandante.

    `estado` no se acepta del cliente: se deriva de `numero` y `vence`. Enviar
    `numero: null` borra el número y devuelve el expediente a `pendiente`, que es
    la forma de corregir un dato mal tecleado.
    """
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "Indique 'numero' y/o 'vence'")

    s = _get_conductor(db, cid, sid, user)
    lim = _asegurar_licencia(db, s)
    if "numero" in data:
        lim.numero = (data["numero"] or "").strip() or None
    if "vence" in data:
        lim.vence = data["vence"]
    _sincronizar(db, s, lim)

    actividad.log(db, cid, "actualizacion", "licencia_interna",
                  f"Licencia interna de {s.nombre}: número="
                  f"{lim.numero or 'sin número'}, vence="
                  f"{lim.vence.isoformat() if lim.vence else 'sin fecha'} "
                  f"({lim.estado})",
                  user_id=user.id, entidad_tipo="sujeto", entidad_id=s.id)
    db.commit()
    logger.info("licencia interna actualizada sujeto=%s estado=%s", s.id,
                lim.estado)
    return _out(db, s, lim)


@router.post("/reset")
def reset(sid: uuid.UUID, db: Session = Depends(get_db),
          cid: uuid.UUID = Depends(get_company_id),
          user: User = Depends(get_current_user)):
    """Reinicia el expediente EMSIPOR del trabajador. Irreversible.

    Borra los nueve documentos `es_emsipor` con sus archivos, los vuelve a
    instanciar en blanco y limpia número, vencimiento y estado. Es la salida
    cuando el mandante rechaza el expediente completo y hay que rehacerlo: dejar
    los documentos antiguos mezclados con los nuevos hace imposible saber cuál
    presentó el contratista.

    NO toca el resto del checklist del trabajador: su acreditación general
    (contrato, examen de altura, inducción) sobrevive intacta.
    """
    s = _get_conductor(db, cid, sid, user)
    lim = _asegurar_licencia(db, s)

    docs = _docs_emsipor(db, s)
    ids = [d.id for d in docs]

    # Los blobs se purgan antes de borrar las filas: sin `blob_path` el archivo
    # quedaría huérfano en el storage para siempre.
    blobs = 0
    if ids:
        storage = get_storage()
        for a in db.scalars(select(DocumentoArchivo).where(
                DocumentoArchivo.company_id == cid,
                DocumentoArchivo.documento_id.in_(ids))):
            storage.delete(a.blob_path)
            blobs += 1
        db.execute(delete(DocumentoArchivo).where(
            DocumentoArchivo.company_id == cid,
            DocumentoArchivo.documento_id.in_(ids)))
        db.execute(delete(Documento).where(Documento.company_id == cid,
                                           Documento.id.in_(ids)))
        db.flush()

    # Mismos argumentos que `checklist.crear_expediente_conductor`, a propósito:
    # si el reset instanciara con otro criterio (p. ej. añadiendo overrides de
    # faena) el expediente reiniciado no coincidiría con el que se creó al dar de
    # alta al trabajador, y nadie sabría por qué cambió la lista.
    n = instanciar_docs(db, cid, "emsipor", sujeto_id=s.id,
                        contrato_plantilla_id=s.contrato_id)
    lim.numero = None
    lim.vence = None
    _sincronizar(db, s, lim)

    actividad.log(db, cid, "actualizacion", "licencia_interna",
                  f"Expediente EMSIPOR de {s.nombre} reiniciado: "
                  f"{len(ids)} documentos y {blobs} archivos eliminados, "
                  f"{n} documentos recreados",
                  user_id=user.id, entidad_tipo="sujeto", entidad_id=s.id)
    db.commit()
    logger.info("expediente EMSIPOR reiniciado sujeto=%s (-%s docs, +%s docs)",
                s.id, len(ids), n)
    out = _out(db, s, lim)
    out["reset"] = {"documentos_eliminados": len(ids), "archivos_eliminados": blobs,
                    "documentos_creados": n}
    return out
