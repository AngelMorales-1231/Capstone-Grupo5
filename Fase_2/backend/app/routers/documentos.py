"""Documentos y archivos: listado transversal y ciclo de subida por SAS (§10).

**Los archivos no pasan por la API.** El flujo es de tres pasos y es el mismo en
Azure y en desarrollo:

1. `POST /documentos/{id}/upload-url` → la API valida extensión y tamaño y emite
   una SAS de escritura con el `blob_path` ya calculado.
2. El navegador hace `PUT` del archivo contra `upload_url` (en Azure va directo
   al blob; con `STORAGE_BACKEND=local` va a `PUT /blobs/upload`, que es el mismo
   contrato).
3. `POST /documentos/{id}/archivos` con `{blob_path, filename}` → la API
   comprueba que el blob existe, lee su tamaño, registra el archivo, crea el job
   de revisión IA en `queued` y responde 201.

De ahí que la revisión IA sea **asíncrona**: el endpoint de confirmación no lee
el archivo ni espera al modelo. El documento sigue en `falta` hasta que la tarea
`revisar_documento` aplica el veredicto; el frontend hace polling en
`GET /ia/revisiones/{job_id}`.

`GET /documentos` es el listado transversal de la empresa: es lo que consumen el
calendario, los reportes y las vistas de vencimientos, y por eso resuelve el
dueño de cada documento (sujeto o contrato) en la propia fila.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from ..config import ALLOWED_EXTENSIONS, settings
from ..database import get_db
from ..deps import (Page, aplicar_orden, contrato_scope, err, get_company_id,
                    get_current_user, paginacion, require_contrato, sobre)
from ..models import Contrato, Documento, DocumentoArchivo, IaReview, Sujeto, User
from ..services import actividad
from ..services.checklist import calc_estado_doc, vencimiento_por_plantilla
from ..services.jobs import enqueue
from ..services.storage import get_storage, make_blob_path
from ..services.tasks import contexto_de_documento
from ..services.vencimientos import recalcular_sujeto

logger = logging.getLogger("acredittia.documentos")

router = APIRouter(prefix="/documentos", tags=["documentos"])

ESTADOS_CALC: tuple[str, ...] = ("ok", "porvenc", "venc", "falta")

ORDEN_DOCUMENTOS = {"titulo", "estado", "estado_calc", "vence", "obligatorio",
                    "created_at", "updated_at"}

MAX_BYTES = settings.max_upload_mb * 1024 * 1024


class DocPatch(BaseModel):
    estado: str | None = None      # ok | falta
    vence: date | None = None


class UploadUrlIn(BaseModel):
    filename: str
    content_type: str | None = None
    size_bytes: int | None = None


class ConfirmarArchivoIn(BaseModel):
    blob_path: str
    filename: str


# ============================================================================
# Serializadores
# ============================================================================
def doc_out(d: Documento, archivos: int | None = None) -> dict:
    """Documento con su checklist. Lo importan `sujetos.py` y `contratos.py`.

    Con `archivos` se informa solo el conteo y no la lista: en los listados
    transversales cargar los archivos de cada fila sería un N+1 por documento.
    """
    dias = (d.vence - date.today()).days if d.vence else None
    salida = {
        "id": str(d.id), "titulo": d.titulo, "obligatorio": d.obligatorio,
        "estado": d.estado, "estado_calc": d.estado_calc,
        "vence": d.vence.isoformat() if d.vence else None,
        "dias_para_vencer": dias, "es_emsipor": d.es_emsipor,
        "template_id": str(d.template_id) if d.template_id else None,
        "ejemplo_clave": d.template.ejemplo_clave if d.template else None,
        "plataforma": d.template.plataforma if d.template else None,
    }
    if archivos is None:
        salida["archivos"] = [{
            "id": str(a.id), "filename": a.filename, "size_bytes": a.size_bytes,
            "content_type": a.content_type,
            "created_at": a.created_at.isoformat() if a.created_at else None,
            "ia_review_id": str(a.ia_review_id) if a.ia_review_id else None,
        } for a in d.archivos]
        salida["archivos_count"] = len(salida["archivos"])
    else:
        salida["archivos_count"] = archivos
    return salida


def _dueno_out(d: Documento, s: Sujeto | None, c: Contrato | None) -> dict | None:
    """Dueño resuelto del documento. `sujeto_id` XOR `contrato_id` en la BD."""
    if s is not None:
        return {"tipo": s.tipo, "id": str(s.id),
                "nombre": s.nombre or s.patente or "",
                "rut": s.rut, "patente": s.patente}
    if c is not None:
        return {"tipo": "contrato", "id": str(c.id), "nombre": c.nombre,
                "codigo": c.codigo}
    return None


def _fila_out(d: Documento, s: Sujeto | None, c: Contrato | None,
              archivos: int) -> dict:
    salida = doc_out(d, archivos=archivos)
    salida["dueno"] = _dueno_out(d, s, c)
    salida["contrato_id"] = str(d.contrato_id or (s.contrato_id if s else "")) or None
    return salida


# ============================================================================
# Helpers
# ============================================================================
def _contrato_del_doc(db: Session, d: Documento) -> uuid.UUID | None:
    """Contrato al que pertenece el documento, sea su dueño o el de su sujeto."""
    if d.contrato_id:
        return d.contrato_id
    if d.sujeto_id:
        s = d.sujeto or db.get(Sujeto, d.sujeto_id)
        return s.contrato_id if s else None
    return None


def _get_doc(db: Session, cid: uuid.UUID, doc_id: uuid.UUID,
             user: User | None = None) -> Documento:
    d = db.get(Documento, doc_id)
    if not d or d.company_id != cid:
        raise err(404, "NO_ENCONTRADO", "Documento no existe")
    if user is not None:
        contrato_id = _contrato_del_doc(db, d)
        if contrato_id:
            require_contrato(contrato_id, user)
    return d


def _get_archivo(db: Session, cid: uuid.UUID, d: Documento,
                 archivo_id: uuid.UUID) -> DocumentoArchivo:
    a = db.get(DocumentoArchivo, archivo_id)
    if not a or a.company_id != cid or a.documento_id != d.id:
        raise err(404, "NO_ENCONTRADO", "Archivo no existe")
    return a


def _valida_extension(filename: str) -> str:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise err(400, "EXTENSION_NO_PERMITIDA",
                  f"Extensiones permitidas: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    return ext


def _dueno_blob(d: Documento) -> tuple[str, uuid.UUID]:
    return ("sujeto", d.sujeto_id) if d.sujeto_id else ("contrato", d.contrato_id)


# ============================================================================
# §10.1 — Listado transversal
# ============================================================================
@router.get("")
def listar(sujeto_id: uuid.UUID | None = Query(None),
           contrato_id: uuid.UUID | None = Query(None),
           estado: str | None = Query(None),
           estado_calc: str | None = Query(None),
           obligatorio: bool | None = Query(None),
           es_emsipor: bool | None = Query(None),
           vence_antes: date | None = Query(None),
           vence_despues: date | None = Query(None),
           p: Page = Depends(paginacion),
           db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(get_current_user)):
    """Todos los documentos de la empresa, con su dueño resuelto.

    Es el listado que necesitan el dashboard, el calendario y los reportes: sin
    él habría que recorrer sujeto a sujeto para responder «qué vence este mes».
    `vence_antes` y `vence_despues` son inclusivos y descartan los documentos sin
    fecha, que por definición no vencen.

    El filtro por contrato cubre las dos rutas del dueño (`documentos.contrato_id`
    para el ámbito empresa y `sujetos.contrato_id` para personal y equipos).
    """
    if estado and estado not in ("ok", "falta"):
        raise err(400, "ESTADO_INVALIDO", "Estado debe ser 'ok' o 'falta'")
    if estado_calc and estado_calc not in ESTADOS_CALC:
        raise err(400, "ESTADO_CALC_INVALIDO",
                  f"estado_calc debe ser uno de: {', '.join(ESTADOS_CALC)}")
    if vence_antes and vence_despues and vence_antes < vence_despues:
        raise err(400, "RANGO_INVALIDO",
                  "'vence_antes' no puede ser anterior a 'vence_despues'")
    if contrato_id:
        require_contrato(contrato_id, user)

    q = (select(Documento, Sujeto, Contrato)
         .select_from(Documento)
         .outerjoin(Sujeto, Sujeto.id == Documento.sujeto_id)
         .outerjoin(Contrato, Contrato.id == Documento.contrato_id)
         .options(joinedload(Documento.template))
         .where(Documento.company_id == cid))

    efectivo = contrato_id or contrato_scope(user)
    if efectivo:
        q = q.where(or_(Documento.contrato_id == efectivo,
                        Sujeto.contrato_id == efectivo))
    if sujeto_id:
        q = q.where(Documento.sujeto_id == sujeto_id)
    if estado:
        q = q.where(Documento.estado == estado)
    if estado_calc:
        q = q.where(Documento.estado_calc == estado_calc)
    if obligatorio is not None:
        q = q.where(Documento.obligatorio.is_(obligatorio))
    if es_emsipor is not None:
        q = q.where(Documento.es_emsipor.is_(es_emsipor))
    if vence_despues:
        q = q.where(Documento.vence.is_not(None), Documento.vence >= vence_despues)
    if vence_antes:
        q = q.where(Documento.vence.is_not(None), Documento.vence <= vence_antes)
    if p.search:
        q = q.where(Documento.titulo.ilike(f"%{p.search}%"))

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, Documento, p.sort, ORDEN_DOCUMENTOS, "titulo")
    filas = db.execute(q.offset(p.offset).limit(p.page_size)).unique().all()

    from .reportes import _archivos_por_documento      # conteo en una sola query
    conteos = _archivos_por_documento(db, cid, [f[0].id for f in filas])
    items = [_fila_out(f[0], f[1], f[2], conteos.get(f[0].id, 0)) for f in filas]
    return sobre(items, total, p)


@router.get("/{doc_id}")
def detalle(doc_id: uuid.UUID, db: Session = Depends(get_db),
            cid: uuid.UUID = Depends(get_company_id),
            user: User = Depends(get_current_user)):
    return doc_out(_get_doc(db, cid, doc_id, user))


@router.patch("/{doc_id}")
def editar(doc_id: uuid.UUID, body: DocPatch, db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(get_current_user)):
    """Marca el documento a mano (ok / falta) y fija o deriva su vencimiento.

    Al pasar a `ok` sin `vence` se intenta derivarlo de `vigencia_meses` de la
    plantilla del requisito: sin fecha el documento quedaría `ok` para siempre y
    los estados `porvenc` y `venc` no se activarían nunca. Si se derivó se avisa
    con `vence_derivado: true`, porque es una fecha que el usuario no escribió.
    """
    d = _get_doc(db, cid, doc_id, user)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a modificar")

    if "estado" in data and data["estado"] is not None:
        if data["estado"] not in ("ok", "falta"):
            raise err(400, "TRANSICION_INVALIDA", "Estado debe ser ok o falta")
        d.estado = data["estado"]
    if "vence" in data and data["vence"] is not None:
        d.vence = data["vence"]

    derivado = False
    if d.estado == "ok" and d.vence is None:
        d.vence = vencimiento_por_plantilla(db, d)
        derivado = d.vence is not None

    d.estado_calc = calc_estado_doc(d)
    d.updated_at = datetime.now(timezone.utc)
    if d.sujeto_id:
        recalcular_sujeto(db, d.sujeto_id)
    actividad.log(db, cid, "actualizacion", "documentos",
                  f"'{d.titulo}' marcado {d.estado}"
                  + (f"; vence derivado de la plantilla ({d.vence})" if derivado
                     else ""),
                  user_id=user.id, entidad_tipo="documento", entidad_id=d.id)
    db.commit()
    out = doc_out(d)
    out["vence_derivado"] = derivado
    if derivado:
        out["nota"] = ("El vencimiento se derivó de la vigencia de la plantilla "
                       "del requisito porque no se informó ninguna fecha.")
    return out


# ============================================================================
# §10.2 — Subida por SAS
# ============================================================================
@router.post("/{doc_id}/upload-url")
def upload_url(doc_id: uuid.UUID, body: UploadUrlIn,
               db: Session = Depends(get_db),
               cid: uuid.UUID = Depends(get_company_id),
               user: User = Depends(get_current_user)):
    """Emite la SAS de escritura para subir el archivo sin pasar por la API.

    El `blob_path` lo calcula el servidor y NO es negociable: incluye el
    `company_id`, el tipo y el id del dueño y el id del documento, que es lo que
    permite que la ruta del blob sea auditable y que un cliente no pueda escribir
    en el árbol de otra empresa.

    `size_bytes` se valida aquí para no emitir una SAS que el navegador va a usar
    en vano, pero es un dato declarado por el cliente: el tamaño real se vuelve a
    comprobar al confirmar, leyéndolo del propio storage.
    """
    d = _get_doc(db, cid, doc_id, user)
    _valida_extension(body.filename)
    if body.size_bytes is not None:
        if body.size_bytes <= 0:
            raise err(400, "ARCHIVO_VACIO", "El archivo está vacío")
        if body.size_bytes > MAX_BYTES:
            raise err(400, "ARCHIVO_MUY_GRANDE",
                      f"Máximo {settings.max_upload_mb} MB")

    tipo, dueno_id = _dueno_blob(d)
    blob_path = make_blob_path(cid, tipo, dueno_id, d.id, body.filename)
    content_type = (body.content_type
                    or mimetypes.guess_type(body.filename)[0]
                    or "application/octet-stream")
    sas = get_storage().upload_url(blob_path, content_type)
    logger.info("SAS de subida emitida documento=%s blob=%s", d.id, blob_path)
    return {
        "upload_url": sas.upload_url, "blob_path": sas.blob_path,
        "expires_at": sas.expires_at.isoformat(), "headers": sas.headers,
        "method": "PUT", "max_bytes": MAX_BYTES,
        "confirmar_en": f"/api/v1/documentos/{d.id}/archivos",
    }


@router.post("/{doc_id}/archivos", status_code=201)
def confirmar_archivo(doc_id: uuid.UUID, body: ConfirmarArchivoIn,
                      db: Session = Depends(get_db),
                      cid: uuid.UUID = Depends(get_company_id),
                      user: User = Depends(get_current_user)):
    """Confirma un archivo ya subido por SAS y encola su revisión IA.

    No recibe el contenido: recibe la ruta del blob y comprueba contra el storage
    que existe de verdad (400 si no, porque el `PUT` pudo fallar sin que el
    cliente se enterara) y cuánto pesa. El tamaño se lee del storage y no se
    acepta del cliente.

    La revisión IA queda en `queued` y la aplica la tarea `revisar_documento`: el
    documento sigue en `falta` hasta que el job termina. El frontend hace polling
    con `GET /ia/revisiones/{job_id}`.
    """
    d = _get_doc(db, cid, doc_id, user)
    _valida_extension(body.filename)

    blob_path = (body.blob_path or "").strip()
    if not blob_path:
        raise err(400, "BLOB_PATH_REQUERIDO", "Falta la ruta del archivo subido")
    # La ruta la emitió `upload-url` con el prefijo de la empresa. Aceptar
    # cualquier ruta permitiría adjuntar el blob de otro tenant a un documento
    # propio, que es una fuga de datos por confusión de rutas.
    if not blob_path.startswith(f"{cid}/"):
        raise err(400, "BLOB_NO_PERMITIDO",
                  "La ruta del blob no corresponde a esta empresa; use la que "
                  "devuelve POST /documentos/{id}/upload-url")

    storage = get_storage()
    if not storage.exists(blob_path):
        raise err(400, "BLOB_NO_ENCONTRADO",
                  "El archivo no está en el almacenamiento; vuelva a subirlo "
                  "con la URL de escritura antes de confirmar")
    size = storage.size(blob_path)
    if size is not None and size > MAX_BYTES:
        storage.delete(blob_path)
        raise err(400, "ARCHIVO_MUY_GRANDE",
                  f"Máximo {settings.max_upload_mb} MB")

    archivo = DocumentoArchivo(
        company_id=cid, documento_id=d.id, filename=body.filename,
        blob_path=blob_path,
        content_type=(mimetypes.guess_type(body.filename)[0]
                      or "application/octet-stream"),
        size_bytes=size, uploaded_by=user.id,
    )
    db.add(archivo)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "BLOB_YA_REGISTRADO",
                  "Ese blob ya está asociado a un documento")

    contexto = contexto_de_documento(d)
    review = IaReview(company_id=cid, archivo_id=archivo.id, context=contexto,
                      status="queued",
                      campos_extraidos={"documento_id": str(d.id),
                                        "blob_path": blob_path,
                                        "filename": body.filename})
    db.add(review)
    db.flush()

    actividad.log(db, cid, "subida_documento", "documentos",
                  f"Archivo '{body.filename}' subido a '{d.titulo}'; "
                  f"revisión IA encolada", user_id=user.id,
                  entidad_tipo="documento", entidad_id=d.id)
    db.commit()

    # Se encola DESPUÉS del commit: con QUEUE_BACKEND=inproc la tarea corre en el
    # acto y abre su propia sesión, que solo ve lo ya confirmado.
    enqueue("revisar_documento", archivo_id=str(archivo.id),
            company_id=str(cid), review_id=str(review.id))
    logger.info("archivo %s confirmado en documento %s; review=%s",
                archivo.id, d.id, review.id)
    return {
        "archivo": {
            "id": str(archivo.id), "filename": archivo.filename,
            "blob_path": archivo.blob_path, "size_bytes": archivo.size_bytes,
            "content_type": archivo.content_type,
            "created_at": archivo.created_at.isoformat() if archivo.created_at
            else None,
        },
        "ia_review": {"job_id": str(review.id), "status": review.status,
                      "context": contexto},
        "documento": doc_out(d),
        "nota": ("La revisión IA es asíncrona: consulte "
                 f"GET /api/v1/ia/revisiones/{review.id} para ver el veredicto "
                 "y el estado final del documento."),
    }


@router.get("/{doc_id}/archivos/{archivo_id}/download-url")
def download_url(doc_id: uuid.UUID, archivo_id: uuid.UUID,
                 db: Session = Depends(get_db),
                 cid: uuid.UUID = Depends(get_company_id),
                 user: User = Depends(get_current_user)):
    """Emite una SAS de lectura de corta vida para descargar el archivo.

    Sustituye a la descarga por streaming: el contenido no pasa por la API, así
    que un PDF de 20 MB no ocupa un worker durante toda la transferencia. La URL
    caduca en `SAS_DOWNLOAD_TTL_MIN` minutos y no debe cachearse.
    """
    d = _get_doc(db, cid, doc_id, user)
    a = _get_archivo(db, cid, d, archivo_id)
    dl = get_storage().download_url(a.blob_path, a.filename)
    return {
        "download_url": dl.download_url,
        "expires_at": dl.expires_at.isoformat(),
        "filename": a.filename,
        "content_type": a.content_type,
        "size_bytes": a.size_bytes,
    }


@router.delete("/{doc_id}/archivos/{archivo_id}")
def borrar_archivo(doc_id: uuid.UUID, archivo_id: uuid.UUID,
                   db: Session = Depends(get_db),
                   cid: uuid.UUID = Depends(get_company_id),
                   user: User = Depends(get_current_user)):
    """Borra el archivo y su blob. Sin archivos, el documento vuelve a `falta`."""
    d = _get_doc(db, cid, doc_id, user)
    a = _get_archivo(db, cid, d, archivo_id)
    get_storage().delete(a.blob_path)
    filename = a.filename
    db.delete(a)
    db.flush()
    if not db.scalar(select(func.count()).select_from(DocumentoArchivo).where(
            DocumentoArchivo.company_id == cid,
            DocumentoArchivo.documento_id == d.id)):
        d.estado = "falta"
        d.estado_calc = calc_estado_doc(d)
        if d.sujeto_id:
            recalcular_sujeto(db, d.sujeto_id)
    actividad.log(db, cid, "actualizacion", "documentos",
                  f"Archivo '{filename}' eliminado de '{d.titulo}'",
                  user_id=user.id, entidad_tipo="documento", entidad_id=d.id)
    db.commit()
    return {"ok": True, "documento": doc_out(d)}
