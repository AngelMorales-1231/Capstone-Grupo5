"""Contratos: alta, ficha, dotación, alertas, historial y matriz (§8).

El contrato es la unidad de acreditación: la empresa se acredita ANTE UNA FAENA
POR UN CONTRATO, y de él cuelgan las plataformas del mandante, los requisitos
efectivos, el personal y los equipos. Por eso este módulo agrega vistas que
técnicamente viven en otras tablas (`/personal`, `/equipos`, `/alertas`,
`/historial`): son la misma información acotada al contrato, y se delegan en los
helpers de sus módulos en vez de duplicar los filtros.

La **matriz de cumplimiento** (§8.4) es la vista que justifica el diseño disperso
del checklist: cada sujeto tiene solo los documentos que le aplican, así que la
cuadrícula sujeto × requisito tiene huecos. Los huecos se omiten (no se emiten
celdas nulas) y significan «este requisito no aplica a este sujeto», que no es lo
mismo que «falta».
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..deps import (Page, aplicar_orden, contrato_scope, err, get_company_id,
                    get_current_user, paginacion, require_contrato, sobre)
from ..models import (
    Actividad, Alerta, Contrato, Documento, DocumentoArchivo, Faena, IaReview,
    Sujeto, User,
)
from ..services import actividad
from ..services.checklist import instanciar_docs, normalizar, stats_sujeto
from ..services.storage import get_storage

logger = logging.getLogger("acredittia.contratos")

router = APIRouter(prefix="/contratos", tags=["contratos"])

ESTADOS: tuple[str, ...] = ("vigente", "en_evaluacion", "terminado")

ORDEN_CONTRATOS = {"nombre", "codigo", "estado", "fecha_inicio", "fecha_termino",
                   "created_at", "updated_at"}


# ============================================================================
# Entradas
# ============================================================================
class ContratoIn(BaseModel):
    nombre: str
    codigo: str | None = None
    faena_id: uuid.UUID
    fecha_inicio: date | None = None
    fecha_termino: date | None = None
    renovacion_automatica: bool = False
    # Trazabilidad del alta asistida por IA (§12.1): el job de extracción que
    # pre-llenó el formulario. No obliga a nada; solo deja el rastro.
    ia_review_id: uuid.UUID | None = None


class ContratoPatch(BaseModel):
    nombre: str | None = None
    codigo: str | None = None
    fecha_inicio: date | None = None
    fecha_termino: date | None = None
    renovacion_automatica: bool | None = None
    estado: str | None = None


class AnalizarIn(BaseModel):
    blob_path: str
    filename: str


# ============================================================================
# Helpers
# ============================================================================
def _get_contrato(db: Session, cid: uuid.UUID, contrato_id: uuid.UUID,
                  user: User | None = None) -> Contrato:
    """Contrato de la empresa, o 404. Acota al contract_admin a su contrato."""
    if user is not None:
        require_contrato(contrato_id, user)
    c = db.get(Contrato, contrato_id)
    if not c or c.company_id != cid:
        raise err(404, "NO_ENCONTRADO", "Contrato no existe")
    return c


def _stats(db: Session, c: Contrato) -> dict:
    """Cumplimiento del contrato: media de los tres frentes que lo componen.

    Documentos de empresa, personal acreditado y equipos acreditados pesan igual,
    y los frentes vacíos no cuentan: un contrato sin equipos no debe quedar al
    66 % por no tener flota. Lo reutilizan `dashboard.py` y `reportes.py`.
    """
    def contar(tipo: str, estado: str | None = None) -> int:
        q = select(func.count()).select_from(Sujeto).where(
            Sujeto.company_id == c.company_id,
            Sujeto.contrato_id == c.id, Sujeto.tipo == tipo,
            Sujeto.estado != "baja")
        if estado:
            q = q.where(Sujeto.estado == estado)
        return db.scalar(q) or 0

    docs_emp = list(db.scalars(select(Documento).where(
        Documento.company_id == c.company_id, Documento.contrato_id == c.id)))
    oblig = [d for d in docs_emp if d.obligatorio]
    ok_emp = sum(1 for d in oblig if d.estado_calc == "ok")
    alertas = db.scalar(select(func.count()).select_from(Alerta).where(
        Alerta.company_id == c.company_id, Alerta.contrato_id == c.id,
        Alerta.resuelta_at.is_(None))) or 0

    p_tot, p_ok = contar("trabajador"), contar("trabajador", "ok")
    e_tot, e_ok = contar("equipo"), contar("equipo", "ok")
    partes = []
    if oblig:
        partes.append(ok_emp / len(oblig))
    if p_tot:
        partes.append(p_ok / p_tot)
    if e_tot:
        partes.append(e_ok / e_tot)
    pct = round(100 * sum(partes) / len(partes)) if partes else 0
    return {
        "cumplimiento_pct": pct,
        "personal": {"total": p_tot, "acreditados": p_ok},
        "equipos": {"total": e_tot, "acreditados": e_ok},
        "docs_empresa": {"total": len(docs_emp), "ok": ok_emp},
        "alertas_activas": alertas,
    }


def _out(db: Session, c: Contrato, con_stats: bool = True) -> dict:
    f = c.faena
    d = {
        "id": str(c.id), "nombre": c.nombre, "codigo": c.codigo,
        "estado": c.estado,
        "fecha_inicio": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
        "fecha_termino": c.fecha_termino.isoformat() if c.fecha_termino else None,
        "renovacion_automatica": c.renovacion_automatica,
        "origen_ia_review_id": (str(c.origen_ia_review_id)
                                if c.origen_ia_review_id else None),
        "faena": {"id": str(f.id), "nombre": f.nombre, "mandante": f.mandante,
                  "grupo": f.grupo, "region": f.region, "color": f.color},
    }
    if con_stats:
        d["stats"] = _stats(db, c)
    return d


# ============================================================================
# §8.1 — CRUD
# ============================================================================
@router.get("")
def listar(faena_id: uuid.UUID | None = Query(None),
           estado: str | None = Query(None),
           p: Page = Depends(paginacion),
           db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(get_current_user)):
    """Contratos de la empresa. Un contract_admin ve únicamente el suyo."""
    if estado and estado not in ESTADOS:
        raise err(400, "ESTADO_INVALIDO",
                  f"Estado debe ser uno de: {', '.join(ESTADOS)}")
    q = select(Contrato).where(Contrato.company_id == cid)
    scope = contrato_scope(user)
    if scope:
        q = q.where(Contrato.id == scope)
    if faena_id:
        q = q.where(Contrato.faena_id == faena_id)
    if estado:
        q = q.where(Contrato.estado == estado)
    if p.search:
        like = f"%{p.search}%"
        q = q.where(or_(Contrato.nombre.ilike(like), Contrato.codigo.ilike(like)))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, Contrato, p.sort, ORDEN_CONTRATOS, "-created_at")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))
    return sobre([_out(db, c) for c in filas], total, p)


@router.post("", status_code=201)
def crear(body: ContratoIn, db: Session = Depends(get_db),
          cid: uuid.UUID = Depends(get_company_id),
          user: User = Depends(get_current_user)):
    """Crea el contrato e instancia su checklist de ámbito empresa.

    Con `ia_review_id` se guarda en `origen_ia_review_id` el job de extracción
    que pre-llenó el formulario: la IA nunca crea el contrato, solo propone los
    campos, y este es el rastro de que el alta vino de ahí (§12.1).
    """
    faena = db.get(Faena, body.faena_id)
    if not faena:
        raise err(404, "NO_ENCONTRADO", "Faena no existe")
    if not faena.activa:
        raise err(400, "FAENA_INACTIVA", "La faena aún no está disponible")
    if body.codigo and db.scalar(select(Contrato.id).where(
            Contrato.company_id == cid, Contrato.codigo == body.codigo)):
        raise err(409, "CODIGO_DUPLICADO", "Ya existe un contrato con ese código")

    origen = None
    if body.ia_review_id:
        review = db.get(IaReview, body.ia_review_id)
        if not review or review.company_id != cid:
            raise err(404, "NO_ENCONTRADO", "El job de IA no existe")
        origen = review.id

    c = Contrato(company_id=cid, faena_id=body.faena_id, nombre=body.nombre.strip(),
                 codigo=body.codigo, fecha_inicio=body.fecha_inicio,
                 fecha_termino=body.fecha_termino,
                 renovacion_automatica=body.renovacion_automatica,
                 origen_ia_review_id=origen)
    db.add(c)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "CODIGO_DUPLICADO", "Ya existe un contrato con ese código")

    n = instanciar_docs(db, cid, "empresa", contrato_id=c.id,
                        faena_id=faena.id, contrato_plantilla_id=c.id)
    actividad.log(db, cid, "creacion", "contrato",
                  f"Contrato '{c.nombre}' creado en {faena.nombre}"
                  + (" a partir de una extracción IA" if origen else ""),
                  user_id=user.id, entidad_tipo="contrato", entidad_id=c.id)
    db.commit()
    out = _out(db, c, con_stats=False)
    out["documentos_creados"] = n
    return out


@router.get("/{contrato_id}")
def detalle(contrato_id: uuid.UUID, db: Session = Depends(get_db),
            cid: uuid.UUID = Depends(get_company_id),
            user: User = Depends(get_current_user)):
    return _out(db, _get_contrato(db, cid, contrato_id, user))


@router.patch("/{contrato_id}")
def editar(contrato_id: uuid.UUID, body: ContratoPatch,
           db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(get_current_user)):
    c = _get_contrato(db, cid, contrato_id, user)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a modificar")
    if data.get("estado") and data["estado"] not in ESTADOS:
        raise err(400, "ESTADO_INVALIDO",
                  f"Estado debe ser uno de: {', '.join(ESTADOS)}")
    if data.get("codigo") and data["codigo"] != c.codigo and db.scalar(
            select(Contrato.id).where(Contrato.company_id == cid,
                                      Contrato.codigo == data["codigo"],
                                      Contrato.id != c.id)):
        raise err(409, "CODIGO_DUPLICADO", "Ya existe un contrato con ese código")
    for k, v in data.items():
        setattr(c, k, v)
    c.updated_at = datetime.now(timezone.utc)
    actividad.log(db, cid, "actualizacion", "contrato",
                  f"Contrato '{c.nombre}' actualizado", user_id=user.id,
                  entidad_tipo="contrato", entidad_id=c.id)
    db.commit()
    return _out(db, c)


@router.delete("/{contrato_id}")
def eliminar(contrato_id: uuid.UUID, confirm: bool = Query(False),
             db: Session = Depends(get_db),
             cid: uuid.UUID = Depends(get_company_id),
             user: User = Depends(get_current_user)):
    """Borra el contrato y todo lo que cuelga de él. Exige confirm con dotación."""
    c = _get_contrato(db, cid, contrato_id, user)
    n_sujetos = db.scalar(select(func.count()).select_from(Sujeto).where(
        Sujeto.company_id == cid, Sujeto.contrato_id == c.id)) or 0
    if n_sujetos and not confirm:
        raise err(400, "REQUIERE_CONFIRMACION",
                  f"El contrato tiene {n_sujetos} sujetos asociados; "
                  "repita con confirm=true")
    # Los blobs se purgan antes de borrar las filas: después no habría forma de
    # saber qué rutas quedaron huérfanas en el storage.
    archivos = list(db.scalars(
        select(DocumentoArchivo)
        .join(Documento, Documento.id == DocumentoArchivo.documento_id)
        .where(DocumentoArchivo.company_id == cid,
               or_(Documento.contrato_id == c.id,
                   Documento.sujeto_id.in_(select(Sujeto.id).where(
                       Sujeto.company_id == cid, Sujeto.contrato_id == c.id))))))
    storage = get_storage()
    for a in archivos:
        storage.delete(a.blob_path)
    nombre = c.nombre
    db.delete(c)      # cascadas: sujetos, documentos, archivos, alertas, plataformas
    actividad.log(db, cid, "actualizacion", "contrato",
                  f"Contrato '{nombre}' eliminado", user_id=user.id)
    db.commit()
    return {"ok": True, "sujetos_eliminados": n_sujetos,
            "archivos_eliminados": len(archivos)}


# ============================================================================
# §8.2 — Vistas del contrato
# ============================================================================
@router.get("/{contrato_id}/documentos")
def docs_empresa(contrato_id: uuid.UUID, p: Page = Depends(paginacion),
                 db: Session = Depends(get_db),
                 cid: uuid.UUID = Depends(get_company_id),
                 user: User = Depends(get_current_user)):
    """Checklist de ámbito empresa del contrato (los que cuelgan del contrato)."""
    from .documentos import doc_out

    c = _get_contrato(db, cid, contrato_id, user)
    q = select(Documento).where(Documento.company_id == cid,
                                Documento.contrato_id == c.id)
    if p.search:
        q = q.where(Documento.titulo.ilike(f"%{p.search}%"))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    filas = list(db.scalars(q.order_by(Documento.titulo)
                            .offset(p.offset).limit(p.page_size)))
    return sobre([doc_out(d) for d in filas], total, p)


@router.get("/{contrato_id}/personal")
def personal_contrato(contrato_id: uuid.UUID,
                      estado: str | None = Query(None),
                      cargo_id: uuid.UUID | None = Query(None),
                      cargo: str | None = Query(None),
                      es_conductor: bool | None = Query(None),
                      p: Page = Depends(paginacion),
                      db: Session = Depends(get_db),
                      cid: uuid.UUID = Depends(get_company_id),
                      user: User = Depends(get_current_user)):
    """Personal del contrato. Mismos filtros y formato que `GET /personal`."""
    from .sujetos import listar_sujetos

    c = _get_contrato(db, cid, contrato_id, user)
    return listar_sujetos(db, cid, "trabajador", p, contrato_id=c.id,
                          estado=estado, cargo_id=cargo_id, cargo=cargo,
                          es_conductor=es_conductor)


@router.get("/{contrato_id}/equipos")
def equipos_contrato(contrato_id: uuid.UUID,
                     estado: str | None = Query(None),
                     tipo_equipo: str | None = Query(None),
                     p: Page = Depends(paginacion),
                     db: Session = Depends(get_db),
                     cid: uuid.UUID = Depends(get_company_id),
                     user: User = Depends(get_current_user)):
    """Flota del contrato. Mismos filtros y formato que `GET /equipos`."""
    from .sujetos import listar_sujetos

    c = _get_contrato(db, cid, contrato_id, user)
    return listar_sujetos(db, cid, "equipo", p, contrato_id=c.id,
                          estado=estado, tipo_equipo=tipo_equipo)


@router.get("/{contrato_id}/alertas")
def alertas_contrato(contrato_id: uuid.UUID,
                     severidad: str | None = Query(None),
                     origen: str | None = Query(None),
                     solo_activas: bool = Query(True),
                     p: Page = Depends(paginacion),
                     db: Session = Depends(get_db),
                     cid: uuid.UUID = Depends(get_company_id),
                     user: User = Depends(get_current_user)):
    """Alertas del contrato, en el mismo formato que `GET /alertas`.

    Solo las que tienen `contrato_id` puesto. Una alerta de un sujeto lo lleva
    porque quien la emite (vencimientos, IA) copia el contrato del documento.
    """
    from .alertas import _out as alerta_out

    c = _get_contrato(db, cid, contrato_id, user)
    q = select(Alerta).where(Alerta.company_id == cid, Alerta.contrato_id == c.id)
    if solo_activas:
        q = q.where(Alerta.resuelta_at.is_(None))
    if severidad:
        q = q.where(Alerta.severidad == severidad)
    if origen:
        q = q.where(Alerta.origen == origen)
    if p.search:
        q = q.where(Alerta.titulo.ilike(f"%{p.search}%"))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    filas = list(db.scalars(q.order_by(Alerta.created_at.desc())
                            .offset(p.offset).limit(p.page_size)))
    return sobre([alerta_out(a) for a in filas], total, p)


@router.get("/{contrato_id}/historial")
def historial_contrato(contrato_id: uuid.UUID,
                       modulo: str | None = Query(None),
                       tipo: str | None = Query(None),
                       p: Page = Depends(paginacion),
                       db: Session = Depends(get_db),
                       cid: uuid.UUID = Depends(get_company_id),
                       user: User = Depends(get_current_user)):
    """Actividad relacionada con el contrato, la más reciente primero.

    Reutiliza el filtro de `routers/actividad.py`, que resuelve la pertenencia
    por `entidad_id` del propio contrato más la de sus sujetos y documentos, con
    sus mismas limitaciones: una acción sin `entidad_id` no se puede atribuir.
    """
    from .actividad import (ORDEN_ACTIVIDAD, TIPOS, _filtro_contrato,
                            _out as actividad_out)

    c = _get_contrato(db, cid, contrato_id, user)
    if tipo and tipo not in TIPOS:
        raise err(400, "TIPO_INVALIDO", f"Tipo debe ser uno de: {', '.join(TIPOS)}")

    q = (select(Actividad, User.nombre, User.email, User.role)
         .select_from(Actividad)
         .outerjoin(User, User.id == Actividad.user_id)
         .where(Actividad.company_id == cid))
    if modulo:
        q = q.where(Actividad.modulo == modulo)
    if tipo:
        q = q.where(Actividad.tipo == tipo)
    if p.search:
        q = q.where(Actividad.descripcion.ilike(f"%{p.search}%"))
    q = _filtro_contrato(q, cid, c.id)

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, Actividad, p.sort, ORDEN_ACTIVIDAD, "-id")
    filas = db.execute(q.offset(p.offset).limit(p.page_size)).all()
    items = [actividad_out(f[0], f.nombre, f.email, f.role) for f in filas]
    return sobre(items, total, p)


# ============================================================================
# §12.1 — Alta asistida: analizar el contrato firmado
# ============================================================================
@router.post("/analizar", status_code=202)
def analizar(body: AnalizarIn, db: Session = Depends(get_db),
             cid: uuid.UUID = Depends(get_company_id),
             user: User = Depends(get_current_user)):
    """Encola la extracción IA de un contrato ya subido por SAS.

    Alias de `POST /ia/extraer-contrato`: delega en el mismo helper para que la
    validación, el job y el encolado no puedan divergir. No crea el contrato;
    devuelve el `job_id` que luego se pasa a `POST /contratos` como
    `ia_review_id`.
    """
    from .ia import encolar_extraccion_contrato

    return encolar_extraccion_contrato(db, cid, user, body.blob_path,
                                       body.filename)


# ============================================================================
# §8.4 — Matriz de cumplimiento
# ============================================================================
@router.get("/{contrato_id}/matriz")
def matriz(contrato_id: uuid.UUID,
           tipo: str = Query("personal"),
           incluir_opcionales: bool = Query(False),
           cargo_id: uuid.UUID | None = Query(None),
           page: int = Query(1, ge=1),
           page_size: int | None = Query(None, ge=1),
           db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(get_current_user)):
    """Matriz dispersa sujeto × requisito del contrato (§8.4).

    Las columnas son los requisitos que aparecen en AL MENOS un sujeto de la
    página, no la plantilla teórica: dos trabajadores de cargos distintos tienen
    checklists distintos y la cuadrícula debe reflejarlo. El orden de columnas es
    estable y determinista —ámbito (empresa, personal, equipo, emsipor), luego
    obligatorio primero, luego título alfabético— para que la columna N sea
    siempre la misma entre páginas y coincida con la exportación a Excel.

    Las celdas AUSENTES significan «el requisito no aplica a este sujeto». No se
    emiten celdas nulas: rellenarlas con `null` se confundiría con `falta`, que
    es un incumplimiento y no una exención.
    """
    from .reportes import ORDEN_AMBITO, _ambito_doc, _docs_por_sujeto

    c = _get_contrato(db, cid, contrato_id, user)
    if tipo not in ("personal", "equipo"):
        raise err(400, "TIPO_INVALIDO", "tipo debe ser 'personal' o 'equipo'")
    if cargo_id and tipo != "personal":
        raise err(400, "CARGO_SOLO_PERSONAL",
                  "El filtro por cargo solo aplica a la matriz de personal")

    # El tope de filas es propio de la matriz: cada fila arrastra sus celdas y
    # una página grande se convierte en un payload enorme.
    limite = settings.matriz_filas_max
    tam = min(page_size or min(settings.page_size_default, limite), limite)

    subtipo = "trabajador" if tipo == "personal" else "equipo"
    q = select(Sujeto).where(Sujeto.company_id == cid,
                             Sujeto.contrato_id == c.id,
                             Sujeto.tipo == subtipo,
                             Sujeto.estado != "baja")
    if cargo_id:
        q = q.where(Sujeto.cargo_id == cargo_id)
    total_filas = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    orden = Sujeto.nombre if tipo == "personal" else Sujeto.patente
    sujetos = list(db.scalars(q.order_by(orden, Sujeto.id)
                              .offset((page - 1) * tam).limit(tam)))
    docs = _docs_por_sujeto(db, cid, [s.id for s in sujetos])

    # Columnas: clave -> (peso_ambito, peso_obligatorio, orden_titulo, titulo, ambito)
    # `orden_titulo` es el título normalizado (minúsculas y sin tildes) y no el
    # título crudo: ordenar por el crudo mezcla mayúsculas y acentos con el
    # orden de code points y produce secuencias que no son alfabéticas en
    # español («IRL Mina» antes de «Inducción», «Cédula» después de
    # «Certificado»). `reportes._filas_matriz` usa exactamente la misma clave,
    # que es lo que sostiene la promesa de que la columna N de la pantalla es la
    # columna N del Excel.
    columnas: dict[str, tuple] = {}
    for s in sujetos:
        for d in docs.get(s.id, []):
            if not d.obligatorio and not incluir_opcionales:
                continue
            clave = d.titulo.strip().lower()
            if clave not in columnas:
                ambito = _ambito_doc(d, s)
                columnas[clave] = (ORDEN_AMBITO.get(ambito, 9),
                                   0 if d.obligatorio else 1,
                                   normalizar(d.titulo), d.titulo, ambito)
    ordenadas = sorted(columnas.items(), key=lambda kv: kv[1][:3])
    claves = [k for k, _ in ordenadas]
    indice = {k: i for i, k in enumerate(claves)}
    cols_out = [{"titulo": v[3], "obligatorio": v[1] == 0, "ambito": v[4]}
                for _, v in ordenadas]

    filas = []
    for s in sujetos:
        propios = docs.get(s.id, [])
        celdas = []
        for d in propios:
            i = indice.get(d.titulo.strip().lower())
            if i is None:                      # opcional excluido de las columnas
                continue
            celdas.append({"col": i, "estado_calc": d.estado_calc,
                           "vence": d.vence.isoformat() if d.vence else None})
        celdas.sort(key=lambda x: x["col"])
        filas.append({
            "sujeto_id": str(s.id),
            "nombre": s.nombre if tipo == "personal" else (s.patente or s.nombre),
            "rut": s.rut or s.patente,
            "cargo": s.cargo if tipo == "personal" else s.tipo_equipo,
            "cumplimiento_pct": stats_sujeto(propios)["cumplimiento_pct"],
            "celdas": celdas,
        })

    return {
        "tipo": tipo, "contrato_id": str(c.id),
        "incluir_opcionales": incluir_opcionales,
        "columnas": cols_out, "filas": filas,
        "page": page, "page_size": tam, "total_filas": total_filas,
        "total_pages": (total_filas + tam - 1) // tam if tam else 0,
        "nota": ("Matriz dispersa: una celda ausente significa que el requisito "
                 "no aplica a ese sujeto, no que falte."),
    }
