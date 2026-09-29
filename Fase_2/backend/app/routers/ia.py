"""Revisión y extracción IA (§12 y §12.1).

Todo en este módulo es **asíncrono**: el endpoint valida, crea la fila de
`ia_reviews` en estado `queued`, encola la tarea y responde `202 {job_id,
status}`. El resultado se consulta siempre con `GET /ia/revisiones/{job_id}`,
tanto para la revisión de un documento cargado como para las tres extracciones
que pre-llenan formularios. Un solo recurso de polling para todo: el frontend no
necesita saber qué tipo de trabajo pidió, solo el `job_id`.

Dos consecuencias de que `ia_reviews` sea la única tabla de jobs:

* **No tiene `contrato_id`.** La trazabilidad de las extracciones (contrato al
  que apuntan, blob temporal y nombre original) va dentro de `campos_extraidos`.
  Es la convención que ya usa `contrato_requisitos.py` y se respeta aquí.
* **`campos_extraidos` mezcla datos de entrada y de salida.** Las claves internas
  se prefijan con `_` (`_accion_aplicada`) y se excluyen del payload público:
  la especificación las expone como campos hermanos, no dentro del diccionario
  de valores detectados.

Las extracciones no crean entidades. Devuelven valores propuestos con confianza
por campo; el alta ocurre en el endpoint del recurso, que acepta `ia_review_id`
para dejar el rastro (§12.1).
"""
from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import (ALLOWED_EXTENSIONS, ALLOWED_EXTENSIONS_ARRANQUE,
                      ALLOWED_EXTENSIONS_CONTRATO, settings)
from ..deps import (Page, aplicar_orden, contrato_scope, err, get_company_id,
                    get_current_user, get_db, paginacion, require_company,
                    require_contrato, sobre)
from ..models import (Documento, DocumentoArchivo, IaHallazgo, IaReview, Sujeto,
                      User)
from ..services import actividad
from ..services.jobs import enqueue
from ..services.storage import get_storage, make_tmp_path
# Importar el módulo de tareas registra TAREAS: sin él `enqueue` no resolvería
# 'revisar_documento' ni las extracciones y solo dejaría un error en el log.
from ..services.tasks import contexto_de_documento

logger = logging.getLogger("acredittia.ia")

router = APIRouter(prefix="/ia", tags=["ia"])

RESULTADOS: tuple[str, ...] = ("validado", "con_observaciones", "con_errores")
CONTEXTOS: tuple[str, ...] = ("empresa", "personal", "equipo", "contrato",
                              "emsipor", "cedula", "padron", "carpeta_arranque")
ESTADOS: tuple[str, ...] = ("queued", "processing", "done", "failed")

ORDEN_REVISIONES = {"created_at", "started_at", "finished_at", "status",
                    "confianza"}

NOTA_EXTRACCION = ("La extracción no crea entidades: confirme el formulario en "
                   "el endpoint del recurso indicando ia_review_id.")


# ------------------------------------------------------------------- entradas
class RevisionIn(BaseModel):
    archivo_id: uuid.UUID


class ExtraerSujetoIn(BaseModel):
    blob_path: str
    filename: str
    tipo: Literal["cedula", "padron"]


class ExtraerContratoIn(BaseModel):
    blob_path: str
    filename: str


class ExtraerArranqueIn(BaseModel):
    blob_path: str
    filename: str
    contrato_id: uuid.UUID


# ------------------------------------------------------------------- helpers
def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _publicos(campos: dict | None) -> dict:
    """Campos detectados sin las claves internas (prefijo `_`)."""
    return {k: v for k, v in (campos or {}).items() if not k.startswith("_")}


def _valida_extension(filename: str, permitidas: set[str]) -> None:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in permitidas:
        raise err(400, "EXTENSION_NO_PERMITIDA",
                  f"Extensiones permitidas: {', '.join(sorted(permitidas))}")


def _valida_blob(blob_path: str) -> str:
    """El archivo ya se subió por SAS; aquí solo llega su ruta (§10)."""
    ruta = (blob_path or "").strip()
    if not ruta:
        raise err(400, "BLOB_PATH_REQUERIDO",
                  "Falta la ruta del archivo ya subido")
    return ruta


def _hallazgo_out(h: IaHallazgo) -> dict:
    return {
        "tipo": h.tipo, "codigo": h.codigo, "mensaje": h.mensaje,
        "campo": h.campo, "valor_detectado": h.valor_detectado,
        "valor_esperado": h.valor_esperado,
    }


def review_out(r: IaReview, hallazgos: list[IaHallazgo] | None = None) -> dict:
    """Serializador de un job de IA (§12). Lo reutilizan otros routers."""
    campos = r.campos_extraidos or {}
    filas = hallazgos if hallazgos is not None else list(r.hallazgos)
    return {
        "job_id": str(r.id), "status": r.status, "context": r.context,
        "resultado": r.resultado,
        "confianza": float(r.confianza) if r.confianza is not None else None,
        "campos_extraidos": _publicos(campos),
        "hallazgos": [_hallazgo_out(h) for h in filas],
        "archivo_id": str(r.archivo_id) if r.archivo_id else None,
        "documento_id": campos.get("documento_id"),
        "contrato_id": campos.get("contrato_id"),
        "started_at": _iso(r.started_at), "finished_at": _iso(r.finished_at),
        "error": r.error,
        # Qué se hizo con el documento a partir del veredicto. Lo escribe la
        # tarea; sin él el usuario ve el resultado de la IA pero no el efecto.
        "accion_aplicada": campos.get("_accion_aplicada"),
        "created_at": _iso(r.created_at),
    }


def _archivos_de(cid: uuid.UUID, *, documento_id: uuid.UUID | None = None,
                 sujeto_id: uuid.UUID | None = None,
                 contrato_id: uuid.UUID | None = None):
    """Subconsulta de `documento_archivos` que cumplen el filtro pedido.

    `ia_reviews.archivo_id` es una columna uuid suelta (sin FK mapeada), así que
    los filtros por documento, sujeto o contrato se resuelven con un semi-join y
    no con un JOIN que multiplicaría filas.
    """
    q = (select(DocumentoArchivo.id)
         .join(Documento, Documento.id == DocumentoArchivo.documento_id)
         .where(DocumentoArchivo.company_id == cid))
    if documento_id:
        q = q.where(DocumentoArchivo.documento_id == documento_id)
    if sujeto_id:
        q = q.where(Documento.sujeto_id == sujeto_id)
    if contrato_id:
        q = q.where(or_(
            Documento.contrato_id == contrato_id,
            Documento.sujeto_id.in_(select(Sujeto.id).where(
                Sujeto.company_id == cid, Sujeto.contrato_id == contrato_id)),
        ))
    return q


def _get_review(db: Session, cid: uuid.UUID, job_id: uuid.UUID) -> IaReview:
    r = db.get(IaReview, job_id)
    if not r or r.company_id != cid:
        raise err(404, "NO_ENCONTRADO", "El job de IA no existe")
    return r


def _crear_job(db: Session, cid: uuid.UUID, *, context: str,
               archivo_id: uuid.UUID | None = None,
               campos: dict | None = None) -> IaReview:
    review = IaReview(company_id=cid, archivo_id=archivo_id, context=context,
                      status="queued", campos_extraidos=campos or {})
    db.add(review)
    db.flush()
    return review


# =============================================================================
# §12 — Revisión de documentos
# =============================================================================
@router.post("/revisiones", status_code=202)
def encolar_revision(body: RevisionIn, db: Session = Depends(get_db),
                     cid: uuid.UUID = Depends(get_company_id),
                     user: User = Depends(get_current_user)):
    """Encola la revisión IA de un archivo ya subido.

    Normalmente la dispara la propia confirmación de subida; este endpoint existe
    para volver a revisar un archivo (nueva versión del modelo, revisión que
    falló) sin obligar a resubirlo.

    El `context` no lo elige el cliente: se deriva del dueño del documento, que
    es quien determina qué debe validar la IA. Un archivo de otra empresa
    responde 404 y no 403, para no revelar su existencia (§3.3).
    """
    archivo = db.get(DocumentoArchivo, body.archivo_id)
    if not archivo or archivo.company_id != cid:
        raise err(404, "NO_ENCONTRADO", "El archivo no existe")
    doc = archivo.documento
    # Un contract_admin solo puede pedir revisiones dentro de su contrato.
    contrato_dueno = doc.contrato_id or (doc.sujeto.contrato_id if doc.sujeto else None)
    if contrato_dueno:
        require_contrato(contrato_dueno, user)

    contexto = contexto_de_documento(doc)
    review = _crear_job(db, cid, context=contexto, archivo_id=archivo.id,
                        campos={"documento_id": str(doc.id),
                                "blob_path": archivo.blob_path,
                                "filename": archivo.filename})
    actividad.log(db, cid, "alerta_ia", "alertas_ia",
                  f"Revisión IA solicitada para '{doc.titulo}' "
                  f"(archivo '{archivo.filename}')", user_id=user.id,
                  entidad_tipo="documento", entidad_id=doc.id)
    db.commit()

    # Se encola después del commit: con QUEUE_BACKEND=inproc la tarea corre en
    # el acto y abre su propia sesión, que solo ve lo ya confirmado.
    enqueue("revisar_documento", archivo_id=str(archivo.id),
            company_id=str(cid), review_id=str(review.id))
    logger.info("revisión encolada review=%s archivo=%s contexto=%s",
                review.id, archivo.id, contexto)
    return {"job_id": str(review.id), "status": review.status,
            "context": contexto}


@router.get("/revisiones/{job_id}")
def detalle_revision(job_id: uuid.UUID, db: Session = Depends(get_db),
                     cid: uuid.UUID = Depends(get_company_id),
                     user: User = Depends(get_current_user)):
    """Estado y resultado de un job de IA (endpoint de polling).

    Mientras `status` es `queued` o `processing` los campos de resultado llegan
    nulos. `accion_aplicada` describe qué se hizo con el documento —marcarlo ok,
    autocompletar el vencimiento, abrir una alerta— porque el veredicto de la IA
    por sí solo no dice qué cambió en el expediente.
    """
    r = _get_review(db, cid, job_id)
    scope = contrato_scope(user)
    if scope and not _visible_para_scope(db, cid, r, scope):
        raise err(404, "NO_ENCONTRADO", "El job de IA no existe")
    return review_out(r)


def _visible_para_scope(db: Session, cid: uuid.UUID, r: IaReview,
                        scope: uuid.UUID) -> bool:
    """¿El job pertenece al contrato al que está acotado un contract_admin?

    Se acepta por dos vías: el archivo revisado cuelga de ese contrato, o el job
    es una extracción cuyo `campos_extraidos['contrato_id']` es ese contrato.
    """
    campos = r.campos_extraidos or {}
    if str(campos.get("contrato_id") or "") == str(scope):
        return True
    if r.archivo_id is None:
        return False
    return db.scalar(select(func.count()).select_from(
        _archivos_de(cid, contrato_id=scope)
        .where(DocumentoArchivo.id == r.archivo_id).subquery())) > 0


@router.get("/revisiones")
def listar_revisiones(documento_id: uuid.UUID | None = Query(None),
                      sujeto_id: uuid.UUID | None = Query(None),
                      resultado: str | None = Query(None),
                      context: str | None = Query(None),
                      status: str | None = Query(None),
                      p: Page = Depends(paginacion),
                      db: Session = Depends(get_db),
                      cid: uuid.UUID = Depends(get_company_id),
                      user: User = Depends(get_current_user)):
    """Historial de revisiones y extracciones de la empresa, la más reciente primero.

    Filtrar por `documento_id` o `sujeto_id` descarta necesariamente las
    extracciones, que no tienen archivo asociado. Un contract_admin ve solo los
    jobs de su contrato.
    """
    if resultado and resultado not in RESULTADOS:
        raise err(400, "RESULTADO_INVALIDO",
                  f"Resultado debe ser uno de: {', '.join(RESULTADOS)}")
    if context and context not in CONTEXTOS:
        raise err(400, "CONTEXT_INVALIDO",
                  f"Context debe ser uno de: {', '.join(CONTEXTOS)}")
    if status and status not in ESTADOS:
        raise err(400, "STATUS_INVALIDO",
                  f"Status debe ser uno de: {', '.join(ESTADOS)}")

    q = select(IaReview).where(IaReview.company_id == cid)
    if resultado:
        q = q.where(IaReview.resultado == resultado)
    if context:
        q = q.where(IaReview.context == context)
    if status:
        q = q.where(IaReview.status == status)
    if documento_id or sujeto_id:
        q = q.where(IaReview.archivo_id.in_(
            _archivos_de(cid, documento_id=documento_id, sujeto_id=sujeto_id)))

    scope = contrato_scope(user)
    if scope:
        q = q.where(or_(
            IaReview.archivo_id.in_(_archivos_de(cid, contrato_id=scope)),
            IaReview.campos_extraidos["contrato_id"].astext == str(scope),
        ))

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, IaReview, p.sort, ORDEN_REVISIONES, "-created_at")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))
    return sobre([review_out(r) for r in filas], total, p)


# =============================================================================
# §12.1 — Extracciones que pre-llenan formularios
# =============================================================================
@router.post("/extraer-sujeto", status_code=202)
def extraer_sujeto(body: ExtraerSujetoIn, db: Session = Depends(get_db),
                   cid: uuid.UUID = Depends(get_company_id),
                   user: User = Depends(require_company)):
    """Lee una cédula o un padrón y propone los campos del alta.

    Cédula → `{nombre, rut, cargo_sugerido}`; padrón o permiso de circulación →
    `{patente, marca, modelo, anio, tipo_equipo_sugerido}`. El `context` del job
    es el propio tipo de documento (`cedula` | `padron`), que es lo que permite
    filtrar el historial por clase de extracción.
    """
    _valida_extension(body.filename, ALLOWED_EXTENSIONS)
    blob_path = _valida_blob(body.blob_path)

    review = _crear_job(db, cid, context=body.tipo,
                        campos={"blob_path": blob_path,
                                "filename": body.filename,
                                "tipo": body.tipo})
    actividad.log(db, cid, "alerta_ia", "alertas_ia",
                  f"Extracción de {body.tipo} solicitada sobre '{body.filename}'",
                  user_id=user.id, entidad_tipo="ia_review", entidad_id=review.id)
    db.commit()

    enqueue("extraer_sujeto", review_id=str(review.id), company_id=str(cid),
            blob_path=blob_path, tipo=body.tipo)
    logger.info("extracción de sujeto encolada review=%s tipo=%s",
                review.id, body.tipo)
    return {"job_id": str(review.id), "status": review.status,
            "context": body.tipo, "nota": NOTA_EXTRACCION}


def encolar_extraccion_contrato(db: Session, cid: uuid.UUID, user: User,
                                blob_path: str, filename: str) -> dict:
    """Encola la extracción de un contrato firmado. Devuelve el sobre 202.

    Vive aquí porque `POST /ia/extraer-contrato` es el alias canónico y
    `POST /contratos/analizar` debe comportarse exactamente igual: el router de
    contratos importa esta función en vez de duplicar la validación, la creación
    del job y el encolado.
    """
    _valida_extension(filename, ALLOWED_EXTENSIONS_CONTRATO)
    ruta = _valida_blob(blob_path)

    review = _crear_job(db, cid, context="contrato",
                        campos={"blob_path": ruta, "filename": filename})
    actividad.log(db, cid, "alerta_ia", "contrato",
                  f"Contrato '{filename}' enviado a extracción IA",
                  user_id=user.id, entidad_tipo="ia_review", entidad_id=review.id)
    db.commit()

    enqueue("extraer_contrato", review_id=str(review.id), company_id=str(cid),
            blob_path=ruta)
    logger.info("extracción de contrato encolada review=%s", review.id)
    return {"job_id": str(review.id), "status": review.status,
            "context": "contrato", "nota": NOTA_EXTRACCION}


@router.post("/extraer-contrato", status_code=202)
def extraer_contrato(body: ExtraerContratoIn, db: Session = Depends(get_db),
                     cid: uuid.UUID = Depends(get_company_id),
                     user: User = Depends(require_company)):
    """Lee un contrato en PDF o Word y propone mandante, faena, fechas y servicio.

    Alias canónico de `POST /contratos/analizar` (§12.1).
    """
    return encolar_extraccion_contrato(db, cid, user, body.blob_path,
                                       body.filename)


def encolar_extraccion_arranque(db: Session, cid: uuid.UUID, user: User,
                                blob_path: str, filename: str,
                                contrato_id: uuid.UUID) -> dict:
    """Encola la extracción de una Carpeta de Arranque. Devuelve el sobre 202.

    El contrato se guarda en `campos_extraidos['contrato_id']` porque
    `ia_reviews` no tiene columna propia; es la misma convención que usa
    `POST /contratos/{id}/carpeta-arranque`.
    """
    _valida_extension(filename, ALLOWED_EXTENSIONS_ARRANQUE)
    ruta = _valida_blob(blob_path)

    from .plataformas import contrato_de_empresa      # evita un ciclo de imports
    c = contrato_de_empresa(db, cid, contrato_id, user)

    review = _crear_job(db, cid, context="carpeta_arranque",
                        campos={"contrato_id": str(c.id), "blob_path": ruta,
                                "filename": filename})
    actividad.log(db, cid, "alerta_ia", "requisitos",
                  f"Carpeta de Arranque '{filename}' enviada a extracción "
                  f"para el contrato '{c.nombre}'", user_id=user.id,
                  entidad_tipo="ia_review", entidad_id=review.id)
    db.commit()

    enqueue("extraer_carpeta_arranque", review_id=str(review.id),
            company_id=str(cid), contrato_id=str(c.id), blob_path=ruta)
    logger.info("extracción de carpeta de arranque encolada review=%s contrato=%s",
                review.id, c.id)
    return {
        "job_id": str(review.id), "status": review.status,
        "context": "carpeta_arranque", "contrato_id": str(c.id),
        "nota": ("La extracción propone requisitos; confírmelos con "
                 "POST /contratos/{id}/requisitos?bulk=true"),
    }


@router.post("/extraer-carpeta-arranque", status_code=202)
def extraer_carpeta_arranque(body: ExtraerArranqueIn,
                             db: Session = Depends(get_db),
                             cid: uuid.UUID = Depends(get_company_id),
                             user: User = Depends(require_company)):
    """Lee una planilla o PDF de carpeta de arranque y propone requisitos por ámbito.

    Alias canónico de `POST /contratos/{id}/carpeta-arranque` (§12.1). La
    extracción por sí sola no crea requisitos.
    """
    return encolar_extraccion_arranque(db, cid, user, body.blob_path,
                                       body.filename, body.contrato_id)


# ============================================================================
# SAS temporal para las extracciones
# ============================================================================
class TmpUploadIn(BaseModel):
    filename: str
    content_type: str | None = None
    size_bytes: int | None = None
    proposito: Literal["contrato", "cedula", "padron", "carpeta_arranque"]


@router.post("/upload-url")
def upload_url_temporal(body: TmpUploadIn,
                        cid: uuid.UUID = Depends(get_company_id),
                        user: User = Depends(get_current_user)):
    """SAS de escritura para un blob temporal de extracción.

    Las tres extracciones de §12.1 y `POST /contratos/analizar` reciben un
    `blob_path` ya subido, pero el emisor de SAS de `/documentos/{id}/upload-url`
    exige un documento existente y estas extracciones son justo lo que ocurre
    ANTES de que exista la entidad: analizar el contrato para pre-llenar el
    formulario de creación, o leer una cédula para dar de alta al trabajador.

    El blob cae en `tmp/{company_id}/…` y la tarea `purgar_temporales` lo elimina
    a las 24 horas: contiene datos personales (cédulas) y no queda referenciado
    por ninguna fila, así que no puede vivir indefinidamente.
    """
    permitidas = {
        "contrato": ALLOWED_EXTENSIONS_CONTRATO,
        "carpeta_arranque": ALLOWED_EXTENSIONS_ARRANQUE,
        "cedula": ALLOWED_EXTENSIONS,
        "padron": ALLOWED_EXTENSIONS,
    }[body.proposito]
    _valida_extension(body.filename, permitidas)

    maximo = settings.max_upload_mb * 1024 * 1024
    if body.size_bytes is not None and body.size_bytes > maximo:
        raise err(400, "ARCHIVO_DEMASIADO_GRANDE",
                  f"El tamaño máximo es {settings.max_upload_mb} MB")

    blob_path = make_tmp_path(cid, body.proposito, body.filename)
    sas = get_storage().upload_url(blob_path, body.content_type)
    return {
        "upload_url": sas.upload_url,
        "blob_path": sas.blob_path,
        "expires_at": sas.expires_at.isoformat(),
        "headers": sas.headers,
        "temporal": True,
        "purga_horas": 24,
    }
