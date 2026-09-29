"""Integraciones con plataformas del mandante y servicios externos (§16).

Una integración es el canal automático de la empresa con un sistema ajeno: SIGA,
WorkMate, MetaContratas y Webcontrol son portales del mandante; WhatsApp
Business y Google Drive son servicios. La tabla admite **una integración por
tipo y empresa** (UNIQUE (company_id, tipo)), así que conectar dos veces el
mismo tipo es 409 y no una segunda fila.

Las credenciales **no se guardan en la base**. `integraciones.credenciales_ref`
contiene solo una referencia lógica al secreto (`kv://<company_id>/<tipo>`) y el
secreto vive en Azure Key Vault, escrito por el `SecretWriter` de
`services/integraciones.py`. Mientras esa escritura siga pendiente (ver los TODO
de este módulo), lo que llega en `credenciales` se descarta en cuanto termina el
request: la integración queda registrada y `desconectada`, con
`credenciales_pendientes=true` en la respuesta para que la interfaz no dé por
hecho que ya puede sincronizar. Es preferible a persistir el secreto «temporal»
en la base, que es exactamente lo que el modelo de datos prohíbe.

El estado lo mueve el resultado de las corridas, no el usuario: `activa` cuando
una sync termina bien, `con_error` cuando falla (con su `Alerta`), y
`desconectada` cuando la empresa la apaga. Por eso el PATCH solo admite
`activa` y `desconectada`.
"""
from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..deps import (Page, aplicar_orden, err, get_company_id, get_current_user,
                    get_db, paginacion, require_company, sobre)
from ..models import Integracion, SyncLog, User
from ..services import actividad
from ..services.integraciones import (CONECTORES, FRECUENCIAS,
                                      INTEGRACION_TIPOS, NOMBRE_PLATAFORMA,
                                      ref_de_integracion)
from ..services.jobs import enqueue

logger = logging.getLogger("acredittia.integraciones")

router = APIRouter(prefix="/integraciones", tags=["integraciones"])

IntegracionEstado = Literal["activa", "desconectada"]

ESTADOS = ("activa", "con_error", "desconectada")
SYNC_STATUS = ("exito", "error")

ORDEN_INTEGRACIONES = {"tipo", "estado", "ultima_sync_at", "created_at",
                       "updated_at"}
ORDEN_LOGS = {"started_at", "finished_at", "status", "registros_procesados"}

# Clave interna de `config` donde se guarda el uuid de la última corrida
# disparada a mano. Va dentro de config y no en una columna nueva porque el
# modelo de datos no tiene dónde ponerla y el dato solo sirve para correlacionar
# la respuesta 202 con el `SyncLog` que escribirá el worker.
CLAVE_CORRIDA = "_ultima_corrida"


# ------------------------------------------------------------------ entradas
class IntegracionIn(BaseModel):
    tipo: str
    credenciales: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)


class IntegracionPatch(BaseModel):
    estado: IntegracionEstado | None = None
    config: dict | None = None
    credenciales: dict | None = None


# ------------------------------------------------------------------- helpers
def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


def _solo_empresa(user: User) -> None:
    """Las integraciones son de alcance empresa (§16: roles company y admin).

    El administrador de contrato no las administra: una integración habla por
    toda la empresa y su configuración afectaría a contratos que no ve.
    """
    if user.role == "contract_admin":
        raise err(403, "ROL_INSUFICIENTE",
                  "El administrador de contrato no accede a integraciones")


def _config_publica(config: dict | None) -> dict:
    """`config` sin las claves internas (las que empiezan por `_`).

    Se filtran para que la interfaz no tenga que conocer ni conservar los
    apuntes del backend al reenviar el objeto en un PATCH.
    """
    return {k: v for k, v in (config or {}).items() if not k.startswith("_")}


def _valida_tipo(tipo: str) -> str:
    t = (tipo or "").strip().lower()
    if t not in INTEGRACION_TIPOS:
        raise err(400, "TIPO_INVALIDO",
                  f"Tipo debe ser uno de: {', '.join(INTEGRACION_TIPOS)}")
    return t


def _valida_config(config: dict | None) -> dict:
    """Valida `config.frecuencia` para la sync programada (§16).

    El resto de `config` es libre a propósito: cada conector necesita sus
    propios parámetros (url del portal, phone_number_id de WhatsApp, id de
    carpeta de Drive) y no tiene sentido cerrar el esquema antes de tener el
    primer conector real.
    """
    cfg = dict(config or {})
    frec = cfg.get("frecuencia")
    if frec is not None and frec not in FRECUENCIAS:
        raise err(400, "FRECUENCIA_INVALIDA",
                  f"config.frecuencia debe ser una de: {', '.join(FRECUENCIAS)}")
    return cfg


def _get_integracion(db: Session, cid: uuid.UUID,
                     iid: uuid.UUID) -> Integracion:
    i = db.get(Integracion, iid)
    if not i or i.company_id != cid:
        raise err(404, "NO_ENCONTRADO", "La integración no existe")
    return i


def _agregados(db: Session, cid: uuid.UUID,
               ids: list[uuid.UUID]) -> dict[uuid.UUID, dict]:
    """Conteos de `sync_logs` por integración: éxitos, fallos y registros."""
    if not ids:
        return {}
    filas = db.execute(
        select(SyncLog.integracion_id,
               func.count(SyncLog.id).filter(SyncLog.status == "exito"),
               func.count(SyncLog.id).filter(SyncLog.status == "error"),
               func.coalesce(func.sum(SyncLog.registros_procesados), 0))
        .where(SyncLog.company_id == cid, SyncLog.integracion_id.in_(ids))
        .group_by(SyncLog.integracion_id)).all()
    return {f[0]: {"syncs_exitosas": int(f[1]), "syncs_fallidas": int(f[2]),
                   "registros_sincronizados": int(f[3])} for f in filas}


def _ultimos_errores(db: Session, cid: uuid.UUID,
                     ids: list[uuid.UUID]) -> dict[uuid.UUID, str | None]:
    """Mensaje del último `SyncLog` con status='error' por integración.

    Se resuelve con `DISTINCT ON` de PostgreSQL en vez de traer todos los logs
    de error y quedarse con el primero en Python: el historial de una integración
    activa crece sin techo.
    """
    if not ids:
        return {}
    filas = db.execute(
        select(SyncLog.integracion_id, SyncLog.mensaje)
        .where(SyncLog.company_id == cid, SyncLog.integracion_id.in_(ids),
               SyncLog.status == "error")
        .order_by(SyncLog.integracion_id, SyncLog.started_at.desc())
        .distinct(SyncLog.integracion_id)).all()
    return {f[0]: f[1] for f in filas}


def _out(i: Integracion, agregados: dict | None = None,
         ultimo_error: str | None = None) -> dict:
    """Serializador de una integración. Nunca incluye el secreto ni su valor."""
    ag = agregados or {"syncs_exitosas": 0, "syncs_fallidas": 0,
                       "registros_sincronizados": 0}
    return {
        "id": str(i.id), "tipo": i.tipo,
        "nombre": NOMBRE_PLATAFORMA.get(i.tipo, i.tipo),
        "estado": i.estado,
        # Solo la referencia lógica: el secreto vive en Key Vault.
        "credenciales_ref": i.credenciales_ref,
        "credenciales_configuradas": bool(i.credenciales_ref),
        "config": _config_publica(i.config),
        "frecuencia": (i.config or {}).get("frecuencia"),
        "ultima_sync_at": _iso(i.ultima_sync_at),
        "syncs_exitosas": ag["syncs_exitosas"],
        "syncs_fallidas": ag["syncs_fallidas"],
        "registros_sincronizados": ag["registros_sincronizados"],
        "ultimo_error": ultimo_error,
        "conector_disponible": i.tipo in CONECTORES,
        "created_at": _iso(i.created_at),
        "updated_at": _iso(i.updated_at),
    }


def _log_out(s: SyncLog) -> dict:
    """Una corrida. `duracion_seg` es None mientras no haya `finished_at`."""
    duracion = None
    if s.finished_at and s.started_at:
        duracion = round((s.finished_at - s.started_at).total_seconds(), 3)
    return {
        "id": s.id, "integracion_id": str(s.integracion_id),
        "status": s.status, "mensaje": s.mensaje,
        "registros_procesados": s.registros_procesados,
        "started_at": _iso(s.started_at), "finished_at": _iso(s.finished_at),
        "duracion_seg": duracion,
    }


def _kpis(db: Session, cid: uuid.UUID) -> dict:
    """Resumen de la cabecera. Cuenta TODAS las integraciones, no la página."""
    base = select(func.count()).select_from(Integracion).where(
        Integracion.company_id == cid)
    return {
        "activas": db.scalar(base.where(Integracion.estado == "activa")) or 0,
        "con_error": db.scalar(base.where(Integracion.estado == "con_error")) or 0,
        "desconectadas": db.scalar(
            base.where(Integracion.estado == "desconectada")) or 0,
        "ultima_sincronizacion": _iso(db.scalar(
            select(func.max(Integracion.ultima_sync_at))
            .where(Integracion.company_id == cid))),
    }


def _desde_hasta(q, columna, desde: date | None, hasta: date | None):
    """Filtro de rango sobre una columna timestamptz a partir de fechas ISO.

    `hasta` es inclusivo: se compara contra el día siguiente a medianoche, que
    es lo que espera quien pide «hasta el 31 de marzo».
    """
    if desde:
        q = q.where(columna >= datetime.combine(desde, datetime.min.time(),
                                                tzinfo=timezone.utc))
    if hasta:
        q = q.where(columna < datetime.combine(hasta + timedelta(days=1),
                                               datetime.min.time(),
                                               tzinfo=timezone.utc))
    return q


# =============================================================================
# Listado y alta
# =============================================================================
@router.get("")
def listar(tipo: str | None = Query(None), estado: str | None = Query(None),
           p: Page = Depends(paginacion), db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(get_current_user)):
    """Integraciones de la empresa con su estado y el volumen sincronizado.

    Los agregados (`syncs_exitosas`, `syncs_fallidas`,
    `registros_sincronizados`, `ultimo_error`) se calculan sobre `sync_logs` de
    toda la historia, no de la última corrida: es lo que permite ver que una
    integración «activa» viene fallando la mitad de las veces.

    Los `kpis` se cuentan sobre todas las integraciones de la empresa, con
    independencia de los filtros y de la página.
    """
    _solo_empresa(user)
    if tipo:
        tipo = _valida_tipo(tipo)
    if estado and estado not in ESTADOS:
        raise err(400, "ESTADO_INVALIDO",
                  f"Estado debe ser uno de: {', '.join(ESTADOS)}")

    q = select(Integracion).where(Integracion.company_id == cid)
    if tipo:
        q = q.where(Integracion.tipo == tipo)
    if estado:
        q = q.where(Integracion.estado == estado)

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, Integracion, p.sort, ORDEN_INTEGRACIONES, "tipo")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))

    ids = [f.id for f in filas]
    ag = _agregados(db, cid, ids)
    errores = _ultimos_errores(db, cid, ids)
    salida = sobre([_out(f, ag.get(f.id), errores.get(f.id)) for f in filas],
                   total, p)
    salida["kpis"] = _kpis(db, cid)
    # Tipos que la empresa todavía no ha conectado: la UI ofrece el alta. Se
    # consultan aparte porque la página filtrada no basta para saberlo.
    conectados = set(db.scalars(select(Integracion.tipo).where(
        Integracion.company_id == cid)))
    salida["tipos_disponibles"] = [
        {"tipo": t, "nombre": NOMBRE_PLATAFORMA.get(t, t)}
        for t in INTEGRACION_TIPOS if t not in conectados
    ]
    return salida


@router.post("", status_code=201)
def crear(body: IntegracionIn, db: Session = Depends(get_db),
          cid: uuid.UUID = Depends(get_company_id),
          user: User = Depends(require_company)):
    """Conecta una integración. El secreto NO se guarda en la base.

    Se persiste únicamente `credenciales_ref` —la referencia lógica al secreto—
    y la integración nace `desconectada`: hasta que la primera sincronización
    confirme que las credenciales sirven, no hay razón para llamarla activa.

    Las credenciales recibidas se descartan al terminar el request; la respuesta
    lo dice con `credenciales_pendientes` para que nadie asuma que ya están en
    el vault (ver el TODO de más abajo).
    """
    _solo_empresa(user)
    tipo = _valida_tipo(body.tipo)
    cfg = _valida_config(body.config)

    existente = db.scalar(select(Integracion).where(
        Integracion.company_id == cid, Integracion.tipo == tipo))
    if existente is not None:
        raise err(409, "INTEGRACION_DUPLICADA",
                  f"La empresa ya tiene una integración de tipo '{tipo}'; "
                  "modifíquela con PATCH /integraciones/{id}")

    ref = ref_de_integracion(cid, tipo)
    tiene_credenciales = bool(body.credenciales)

    # TODO(§16): escribir el secreto en el vault antes de guardar la fila:
    #   from ..services.integraciones import get_secret_writer
    #   get_secret_writer().write(ref, body.credenciales)
    # y si la escritura falla, responder 503 sin crear la integración (mismo
    # criterio que `routers/plataformas.py::_cifrar`: nunca hay plan B en claro).
    # Hasta entonces el secreto se descarta aquí mismo y la integración queda
    # registrada pero sin credenciales utilizables.
    body.credenciales = {}

    integ = Integracion(company_id=cid, tipo=tipo, estado="desconectada",
                        credenciales_ref=ref, config=cfg)
    db.add(integ)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise err(409, "INTEGRACION_DUPLICADA",
                  f"La empresa ya tiene una integración de tipo '{tipo}'")

    actividad.log(db, cid, "creacion", "integraciones",
                  f"Integración con {NOMBRE_PLATAFORMA.get(tipo, tipo)} creada",
                  user_id=user.id, entidad_tipo="integracion",
                  entidad_id=integ.id, plataforma=tipo)
    db.commit()
    logger.info("empresa %s: integración %s creada (ref=%s)", cid, tipo, ref)

    salida = _out(integ)
    salida["credenciales_pendientes"] = tiene_credenciales
    salida["nota"] = ("Las credenciales no se almacenan en la base de datos. La "
                      "escritura en Key Vault está pendiente, así que la "
                      "integración queda registrada y desconectada.")
    return salida


# =============================================================================
# Edición, desconexión y sincronización
# =============================================================================
@router.patch("/{integracion_id}")
def editar(integracion_id: uuid.UUID, body: IntegracionPatch,
           db: Session = Depends(get_db),
           cid: uuid.UUID = Depends(get_company_id),
           user: User = Depends(require_company)):
    """Activa o desactiva la integración, actualiza `config` o rota el secreto.

    Pasar a `activa` **no** toca `ultima_sync_at`: la fecha dice cuándo se
    sincronizó por última vez, no cuándo se encendió el interruptor. Quien
    quiera una corrida usa `POST /integraciones/{id}/sync`.

    `con_error` no se puede fijar a mano: lo pone la corrida que falla. Reactivar
    una integración en error es pasarla a `activa`.

    `config` se mezcla con la existente en lugar de reemplazarla, para que un
    PATCH parcial no borre parámetros que el cliente no envió.
    """
    _solo_empresa(user)
    integ = _get_integracion(db, cid, integracion_id)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise err(400, "SIN_CAMBIOS", "No se indicó ningún campo a actualizar")

    cambios: list[str] = []
    if "config" in data and data["config"] is not None:
        nueva = _valida_config(data["config"])
        # Reasignación completa del dict: JSONB no rastrea mutaciones in-place.
        integ.config = {**(integ.config or {}), **nueva}
        cambios.append("config")

    if data.get("estado"):
        if data["estado"] == "activa" and not integ.credenciales_ref:
            raise err(400, "CREDENCIALES_AUSENTES",
                      "La integración no tiene credenciales asociadas; "
                      "envíelas en el mismo PATCH antes de activarla")
        integ.estado = data["estado"]
        cambios.append(f"estado={data['estado']}")

    if "credenciales" in data and data["credenciales"] is not None:
        if not data["credenciales"]:
            raise err(400, "CREDENCIALES_VACIAS",
                      "Para rotar el secreto envíe las credenciales nuevas")
        # La referencia es determinista sobre (company_id, tipo): una rotación
        # reescribe el mismo secreto y no deja versiones huérfanas.
        integ.credenciales_ref = ref_de_integracion(cid, integ.tipo)
        # TODO(§16): rotar el secreto en el vault:
        #   get_secret_writer().write(integ.credenciales_ref, data["credenciales"])
        # Mientras siga pendiente, el valor recibido se descarta aquí y no se
        # escribe en ninguna columna.
        data["credenciales"] = {}
        cambios.append("credenciales")

    integ.updated_at = datetime.now(timezone.utc)
    actividad.log(db, cid, "actualizacion", "integraciones",
                  f"Integración con {NOMBRE_PLATAFORMA.get(integ.tipo, integ.tipo)} "
                  f"actualizada ({', '.join(cambios)})", user_id=user.id,
                  entidad_tipo="integracion", entidad_id=integ.id,
                  plataforma=integ.tipo)
    db.commit()

    ag = _agregados(db, cid, [integ.id])
    salida = _out(integ, ag.get(integ.id),
                  _ultimos_errores(db, cid, [integ.id]).get(integ.id))
    if "credenciales" in cambios:
        salida["credenciales_pendientes"] = True
    return salida


@router.delete("/{integracion_id}")
def eliminar(integracion_id: uuid.UUID, db: Session = Depends(get_db),
             cid: uuid.UUID = Depends(get_company_id),
             user: User = Depends(require_company)):
    """Desconecta la integración y borra la referencia a su secreto.

    Se elimina la fila: es lo que hace caer en cascada `sync_logs`
    (ON DELETE CASCADE) y lo que libera la clave única `(company_id, tipo)` para
    volver a conectar el mismo tipo más adelante. El historial de corridas se va
    con ella, así que el conteo se informa en la respuesta.

    Las alertas y la actividad generadas por sus fallos **no** se borran: son el
    rastro de lo que pasó y no cuelgan de la integración.
    """
    _solo_empresa(user)
    integ = _get_integracion(db, cid, integracion_id)
    tipo, ref = integ.tipo, integ.credenciales_ref

    n_logs = db.scalar(select(func.count(SyncLog.id)).where(
        SyncLog.company_id == cid,
        SyncLog.integracion_id == integ.id)) or 0

    # TODO(§16): borrar el secreto del vault antes de perder la referencia:
    #   get_secret_writer().delete(ref)
    # Un fallo del vault aquí no debe impedir la desconexión (la integración
    # deja de usarse igual), pero sí tiene que quedar en el log para poder
    # limpiar el secreto huérfano a mano.
    integ.credenciales_ref = None
    db.delete(integ)

    actividad.log(db, cid, "actualizacion", "integraciones",
                  f"Integración con {NOMBRE_PLATAFORMA.get(tipo, tipo)} "
                  f"desconectada ({n_logs} corridas eliminadas)",
                  user_id=user.id, entidad_tipo="integracion",
                  entidad_id=integracion_id, plataforma=tipo)
    db.commit()
    logger.info("empresa %s: integración %s eliminada (ref=%s, %s logs)",
                cid, tipo, ref, n_logs)
    return {"ok": True, "id": str(integracion_id), "tipo": tipo,
            "logs_eliminados": n_logs, "secreto_pendiente_de_borrado": bool(ref)}


@router.post("/{integracion_id}/sync", status_code=202)
def sincronizar(integracion_id: uuid.UUID, db: Session = Depends(get_db),
                cid: uuid.UUID = Depends(get_company_id),
                user: User = Depends(require_company)):
    """Dispara una sincronización manual (job asíncrono).

    El `SyncLog` lo escribe la tarea al terminar, con el resultado real: aquí no
    se crea ningún log provisional, porque una corrida abandonada dejaría una
    fila en un estado que el enum `sync_status` (exito | error) no sabe
    representar.

    `job_id` es `<id de la integración>:<uuid de corrida>`. El uuid queda en
    `config['_ultima_corrida']` para poder correlacionar esta respuesta con la
    corrida que el worker registrará en `GET /integraciones/{id}/logs`.
    """
    _solo_empresa(user)
    integ = _get_integracion(db, cid, integracion_id)
    if integ.estado == "desconectada":
        raise err(409, "INTEGRACION_DESCONECTADA",
                  "La integración está desconectada; actívela antes de "
                  "sincronizar")

    corrida = str(uuid.uuid4())
    integ.config = {**(integ.config or {}), CLAVE_CORRIDA: corrida}
    integ.updated_at = datetime.now(timezone.utc)
    actividad.log(db, cid, "actualizacion", "integraciones",
                  f"Sincronización manual de "
                  f"{NOMBRE_PLATAFORMA.get(integ.tipo, integ.tipo)} solicitada",
                  user_id=user.id, entidad_tipo="integracion",
                  entidad_id=integ.id, plataforma=integ.tipo)
    db.commit()

    # Después del commit: con QUEUE_BACKEND=inproc la tarea corre en el acto y
    # abre su propia sesión, que solo ve lo ya confirmado. En ese backend la
    # corrida ya habrá terminado cuando el cliente lea esta respuesta; el estado
    # real se consulta en /logs.
    enqueue("sincronizar_integracion", integracion_id=str(integ.id),
            company_id=str(cid), corrida=corrida)
    logger.info("integración %s: corrida %s encolada", integ.id, corrida)

    return {"job_id": f"{integ.id}:{corrida}", "status": "queued",
            "integracion_id": str(integ.id), "corrida": corrida}


@router.get("/{integracion_id}/logs")
def listar_logs(integracion_id: uuid.UUID, status: str | None = Query(None),
                desde: date | None = Query(None), hasta: date | None = Query(None),
                p: Page = Depends(paginacion), db: Session = Depends(get_db),
                cid: uuid.UUID = Depends(get_company_id),
                user: User = Depends(get_current_user)):
    """Historial de sincronizaciones de la integración, la más reciente primero.

    `duracion_seg` se calcula sobre `started_at`/`finished_at` y no se persiste:
    derivarla evita que una fila quede con una duración incoherente si el worker
    muere entre las dos marcas.
    """
    _solo_empresa(user)
    integ = _get_integracion(db, cid, integracion_id)
    if status and status not in SYNC_STATUS:
        raise err(400, "STATUS_INVALIDO",
                  f"Status debe ser uno de: {', '.join(SYNC_STATUS)}")
    if desde and hasta and hasta < desde:
        raise err(400, "RANGO_INVALIDO", "'hasta' no puede ser anterior a 'desde'")

    q = select(SyncLog).where(SyncLog.company_id == cid,
                              SyncLog.integracion_id == integ.id)
    if status:
        q = q.where(SyncLog.status == status)
    q = _desde_hasta(q, SyncLog.started_at, desde, hasta)
    if p.search:
        q = q.where(SyncLog.mensaje.ilike(f"%{p.search}%"))

    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    q = aplicar_orden(q, SyncLog, p.sort, ORDEN_LOGS, "-started_at")
    filas = list(db.scalars(q.offset(p.offset).limit(p.page_size)))

    salida = sobre([_log_out(f) for f in filas], total, p)
    ag = _agregados(db, cid, [integ.id])
    salida["integracion"] = _out(
        integ, ag.get(integ.id),
        _ultimos_errores(db, cid, [integ.id]).get(integ.id))
    return salida
