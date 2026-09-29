"""Webhooks entrantes: WhatsApp Business y pasarela de pagos (§16 y §17).

Estos endpoints son **públicos pero firmados**. No usan `get_current_user` ni
`get_company_id`, y eso es correcto: son un canal servidor-a-servidor. Quien
llama es Meta o la pasarela, no una persona con sesión, así que no hay token de
usuario que presentar ni empresa en el contexto del request. La autenticación es
la **firma HMAC-SHA256 del cuerpo exacto** con el secreto compartido; sin firma
válida no se lee el contenido ni se toca la base.

Por eso la sesión se abre con `worker_session(is_admin=True)`: la empresa se
deduce del contenido del webhook (el `phone_number_id` de WhatsApp, el
`gateway_ref` de la factura) y para poder deducirla hay que poder consultar
antes de saber de quién es la fila. Es el mismo razonamiento que `auth_session()`
en el login: sin tenant no hay forma de resolver el tenant. El alcance se acota
de inmediato —una vez identificada la empresa se filtra por su `company_id`— y
no se acepta ningún `company_id` que venga en el cuerpo.

Tres reglas de comportamiento:

* **La firma se compara con `hmac.compare_digest`**, nunca con `==`.
* **Sin secreto configurado se responde 503**, no se acepta el webhook. Un
  secreto vacío haría que cualquiera pudiera firmar.
* **Un error de negocio no es un 5xx.** Con la firma válida se responde 200
  aunque el contenido no aplique: la pasarela y Meta reintentan ante cualquier
  respuesta que no sea 2xx, y un reintento infinito por un evento que nunca va a
  poder procesarse es peor que registrarlo y seguir. La única excepción es la
  factura inexistente, que devuelve 404 (sin excepción hacia el cliente) porque
  ahí el reintento sí puede tener sentido para la pasarela.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import date, datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import settings
from ..database import worker_session
from ..models import Actividad, Factura, Integracion, Suscripcion
from .suscripcion import sumar_mes

logger = logging.getLogger("acredittia.webhooks")

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Claves donde cada pasarela suele poner su identificador de transacción. Se
# aceptan varias porque el nombre cambia entre Transbank (buy_order) y Stripe
# (payment_intent), y la factura se localiza por `facturas.gateway_ref`.
CLAVES_REF = ("gateway_ref", "reference", "referencia", "buy_order",
              "payment_intent", "transaction_id", "token_ws", "id")

# Valores que indican pago rechazado. Un webhook de fallo no debe marcar la
# factura pagada.
ESTADOS_FALLIDOS = ("fallida", "failed", "rejected", "rechazado", "rechazada",
                    "declined", "canceled", "cancelled", "error")

# Valores que indican pago conciliado.
ESTADOS_PAGADOS = ("pagada", "paid", "succeeded", "success", "aprobado",
                   "aprobada", "authorized", "completed", "captured")


# ============================================================================
# Verificación de firma
# ============================================================================
def firma_hmac(secreto: str, cuerpo: bytes) -> str:
    """HMAC-SHA256 hexadecimal del cuerpo tal como llegó (sin reserializar)."""
    return hmac.new(secreto.encode("utf-8"), cuerpo, hashlib.sha256).hexdigest()


def firma_valida(secreto: str, cuerpo: bytes, recibida: str | None) -> bool:
    """Compara la firma recibida con la calculada, en tiempo constante.

    Se admite tanto `sha256=<hex>` (formato de Meta) como el hex a secas, que es
    lo que envían algunas pasarelas. La comparación es siempre
    `hmac.compare_digest`: un `==` sobre la firma filtra información por el
    tiempo de respuesta.
    """
    if not recibida:
        return False
    valor = recibida.strip()
    if "=" in valor:
        esquema, _, resto = valor.partition("=")
        if esquema.strip().lower() != "sha256":
            return False
        valor = resto.strip()
    return hmac.compare_digest(firma_hmac(secreto, cuerpo), valor.lower())


def _exigir_firma(nombre_secreto: str, secreto: str, cuerpo: bytes,
                  recibida: str | None) -> None:
    """503 si no hay secreto configurado, 401 si la firma no valida."""
    if not secreto:
        logger.error("webhook rechazado: %s no está configurado", nombre_secreto)
        raise _respuesta_error(
            503, "WEBHOOK_NO_CONFIGURADO",
            "El canal de webhooks no está configurado en este entorno")
    if not firma_valida(secreto, cuerpo, recibida):
        logger.warning("webhook rechazado: firma inválida (%s bytes)", len(cuerpo))
        raise _respuesta_error(401, "FIRMA_INVALIDA",
                              "La firma del webhook no es válida")


class _ErrorWebhook(Exception):
    """Error con respuesta ya formada. Se traduce en el propio endpoint."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message

    def respuesta(self) -> JSONResponse:
        return JSONResponse(
            status_code=self.status,
            content={"error": {"code": self.code, "message": self.message}})


def _respuesta_error(status: int, code: str, message: str) -> _ErrorWebhook:
    return _ErrorWebhook(status, code, message)


def _payload(cuerpo: bytes) -> dict:
    """Cuerpo como dict. Un JSON ilegible no es motivo para fallar el webhook."""
    try:
        datos = json.loads(cuerpo or b"{}")
    except ValueError:
        logger.warning("webhook con cuerpo no JSON (%s bytes)", len(cuerpo))
        return {}
    return datos if isinstance(datos, dict) else {}


# ============================================================================
# §16 — WhatsApp Business
# ============================================================================
def _datos_whatsapp(payload: dict) -> dict:
    """Resumen del evento: identificadores, nunca el texto de los mensajes.

    El contenido de un mensaje de WhatsApp es dato personal y no aporta nada al
    registro de actividad, así que solo se conservan los identificadores y el
    tipo de cada mensaje.
    """
    resumen: dict = {"phone_number_id": None, "waba_id": None,
                     "mensajes": [], "estados": []}
    for entrada in (payload.get("entry") or []):
        if isinstance(entrada, dict):
            resumen["waba_id"] = resumen["waba_id"] or entrada.get("id")
            for cambio in (entrada.get("changes") or []):
                valor = (cambio or {}).get("value") or {}
                meta = valor.get("metadata") or {}
                resumen["phone_number_id"] = (resumen["phone_number_id"]
                                              or meta.get("phone_number_id"))
                for m in (valor.get("messages") or []):
                    resumen["mensajes"].append(
                        {"id": m.get("id"), "tipo": m.get("type")})
                for e in (valor.get("statuses") or []):
                    resumen["estados"].append(
                        {"id": e.get("id"), "status": e.get("status")})
    return resumen


def _integracion_whatsapp(db: Session, resumen: dict) -> Integracion | None:
    """Integración de WhatsApp cuya `config` coincide con el número receptor.

    El emparejamiento se hace contra `config.phone_number_id` (y como respaldo
    `config.waba_id`), que es lo que la empresa configura al conectar el canal.
    Sin coincidencia no hay empresa a la que atribuir el evento.
    """
    for clave in ("phone_number_id", "waba_id"):
        valor = resumen.get(clave)
        if not valor:
            continue
        integ = db.scalar(
            select(Integracion).where(
                Integracion.tipo == "whatsapp",
                Integracion.config[clave].astext == str(valor)))
        if integ is not None:
            return integ
    return None


def _registrar_whatsapp(payload: dict) -> dict:
    """Registra el evento en `actividad` si se puede resolver la empresa.

    Si no hay integración que empareje, el evento queda solo en `logging`: no
    existe empresa a la que atribuir la fila y `actividad.company_id` es NOT
    NULL. Devolver un error tampoco serviría de nada, porque Meta reintentaría
    el mismo evento indefinidamente.
    """
    resumen = _datos_whatsapp(payload)
    # is_admin=True: la empresa se deduce del contenido, así que hay que poder
    # consultar `integraciones` antes de conocer el tenant. Ver la cabecera.
    with worker_session(is_admin=True) as db:
        integ = _integracion_whatsapp(db, resumen)
        if integ is None:
            logger.info("webhook whatsapp sin integración asociada "
                        "(phone_number_id=%s, %s mensajes, %s estados)",
                        resumen["phone_number_id"], len(resumen["mensajes"]),
                        len(resumen["estados"]))
            return {"empresa_resuelta": False}
        try:
            db.add(Actividad(
                company_id=integ.company_id, user_id=None, tipo="actualizacion",
                modulo="integraciones",
                descripcion=(f"Evento entrante de WhatsApp: "
                             f"{len(resumen['mensajes'])} mensajes, "
                             f"{len(resumen['estados'])} estados de entrega"),
                entidad_tipo="integracion", entidad_id=integ.id,
                plataforma="whatsapp", meta=resumen,
            ))
            db.commit()
        except Exception:       # noqa: BLE001
            logger.exception("no se pudo registrar el evento de WhatsApp")
            db.rollback()
            return {"empresa_resuelta": True, "registrado": False}
        return {"empresa_resuelta": True, "registrado": True,
                "company_id": str(integ.company_id)}


@router.get("/whatsapp")
def verificar_whatsapp(
    hub_mode: str | None = Query(None, alias="hub.mode"),
    hub_verify_token: str | None = Query(None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(None, alias="hub.challenge"),
):
    """Handshake de verificación de Meta.

    Meta llama una vez al configurar el webhook y espera el `hub.challenge`
    devuelto **en texto plano** (no JSON) con 200. Si el token no coincide se
    responde 403, que es lo que la plataforma interpreta como «no verificado».
    El token se compara con `compare_digest`.
    """
    esperado = settings.whatsapp_verify_token
    if not esperado:
        return _respuesta_error(
            503, "WEBHOOK_NO_CONFIGURADO",
            "WHATSAPP_VERIFY_TOKEN no está configurado").respuesta()
    if hub_mode != "subscribe" or not hmac.compare_digest(
            esperado, hub_verify_token or ""):
        logger.warning("handshake de WhatsApp rechazado (mode=%s)", hub_mode)
        return _respuesta_error(
            403, "VERIFICACION_INVALIDA",
            "El token de verificación no coincide").respuesta()
    logger.info("handshake de WhatsApp verificado")
    return PlainTextResponse(hub_challenge or "")


@router.post("/whatsapp")
async def whatsapp_entrante(request: Request):
    """Recibe eventos de WhatsApp Business firmados por Meta.

    La firma va en `X-Hub-Signature-256` con formato `sha256=<hex>` y se calcula
    sobre el cuerpo **crudo**: hay que leer los bytes tal como llegaron, porque
    reserializar el JSON cambiaría el HMAC.

    Con la firma válida siempre se responde 200, incluso si el evento no se puede
    atribuir a ninguna empresa: los webhooks no deben reintentarse por errores de
    negocio.
    """
    cuerpo = await request.body()
    try:
        _exigir_firma("WHATSAPP_TOKEN", settings.whatsapp_token, cuerpo,
                      request.headers.get("X-Hub-Signature-256"))
    except _ErrorWebhook as e:
        return e.respuesta()

    payload = _payload(cuerpo)
    # El endpoint es async para poder leer el cuerpo crudo; el trabajo de base de
    # datos es síncrono y se manda al threadpool para no bloquear el event loop.
    resultado = await run_in_threadpool(_registrar_whatsapp, payload)
    return {"received": True, **resultado}


# ============================================================================
# §17 — Conciliación de pagos
# ============================================================================
def _ref_de_pago(payload: dict) -> str | None:
    """Identificador de la transacción, buscando en las claves habituales.

    Se mira también dentro de `data`/`data.object`, que es donde Stripe anida el
    objeto del evento.
    """
    candidatos = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        candidatos.append(data)
        obj = data.get("object")
        if isinstance(obj, dict):
            candidatos.append(obj)
    for fuente in candidatos:
        for clave in CLAVES_REF:
            valor = fuente.get(clave)
            if isinstance(valor, (str, int)) and str(valor).strip():
                return str(valor).strip()
    return None


def _estado_de_pago(payload: dict) -> str:
    """`pagado`, `fallido` o `desconocido` a partir del cuerpo del webhook."""
    for clave in ("status", "estado", "event", "type", "tipo"):
        valor = payload.get(clave)
        if not isinstance(valor, str):
            continue
        v = valor.strip().lower()
        if any(f in v for f in ESTADOS_FALLIDOS):
            return "fallido"
        if any(p in v for p in ESTADOS_PAGADOS):
            return "pagado"
    # Sin estado explícito se asume conciliación: es lo que envía una pasarela
    # que solo notifica pagos exitosos.
    return "desconocido"


def _fecha_pago(payload: dict) -> datetime:
    valor = payload.get("pagada_at") or payload.get("paid_at")
    if isinstance(valor, str):
        try:
            fecha = datetime.fromisoformat(valor.replace("Z", "+00:00"))
            return (fecha if fecha.tzinfo
                    else fecha.replace(tzinfo=timezone.utc))
        except ValueError:
            logger.info("fecha de pago no interpretable: %r", valor)
    return datetime.now(timezone.utc)


def _conciliar(payload: dict) -> tuple[int, dict]:
    """Aplica el pago sobre la factura y su suscripción. Devuelve (status, cuerpo).

    Idempotente: una factura ya `pagada` se responde 200 sin cambios, porque las
    pasarelas reenvían el mismo evento cuando no reciben el 2xx a tiempo.
    """
    ref = _ref_de_pago(payload)
    if not ref:
        logger.warning("webhook de pagos sin referencia de transacción")
        return 200, {"received": True, "conciliado": False,
                     "motivo": "sin_referencia"}

    estado_pago = _estado_de_pago(payload)
    # is_admin=True: la factura se localiza por `gateway_ref`, que no dice de qué
    # empresa es. Ver la cabecera del módulo.
    with worker_session(is_admin=True) as db:
        f = db.scalar(select(Factura).where(Factura.gateway_ref == ref))
        if f is None:
            logger.warning("webhook de pagos: factura con gateway_ref=%s "
                           "no encontrada", ref)
            return 404, {"received": True, "conciliado": False,
                         "motivo": "factura_no_encontrada", "gateway_ref": ref}

        if f.estado == "pagada":
            logger.info("webhook de pagos: factura %s ya estaba pagada", f.id)
            return 200, {"received": True, "conciliado": True,
                         "sin_cambios": True, "factura_id": str(f.id),
                         "estado": f.estado}

        ahora = datetime.now(timezone.utc)
        if estado_pago == "fallido":
            # Un pago rechazado no marca la factura pagada ni reactiva nada. La
            # transición a `morosa` la decide el proceso de cobro, no el webhook:
            # aquí solo se refleja el rechazo en la factura.
            f.estado = "fallida"
            f.updated_at = ahora
            db.add(Actividad(
                company_id=f.company_id, user_id=None, tipo="actualizacion",
                modulo="suscripcion",
                descripcion=f"Pago rechazado de la factura {f.folio or f.id}",
                entidad_tipo="factura", entidad_id=f.id,
                meta={"gateway_ref": ref, "estado_pago": estado_pago}))
            db.commit()
            logger.info("factura %s marcada fallida por webhook", f.id)
            return 200, {"received": True, "conciliado": False,
                         "factura_id": str(f.id), "estado": f.estado}

        f.estado = "pagada"
        f.pagada_at = _fecha_pago(payload)
        f.updated_at = ahora

        s = db.get(Suscripcion, f.suscripcion_id)
        reactivada = False
        if s is not None and s.estado == "morosa":
            # Reactivación: el periodo avanza un mes desde donde terminó el
            # anterior (o desde hoy si ya quedó atrás), para no regalar meses ni
            # cobrar dos veces el mismo tramo.
            hoy = date.today()
            base = s.periodo_actual_hasta or hoy
            if base < hoy:
                base = hoy
            s.estado = "activa"
            s.periodo_actual_desde = base
            s.periodo_actual_hasta = sumar_mes(base)
            s.updated_at = ahora
            reactivada = True

        db.add(Actividad(
            company_id=f.company_id, user_id=None, tipo="actualizacion",
            modulo="suscripcion",
            descripcion=(f"Pago conciliado de la factura {f.folio or f.id}"
                         + (" y suscripción reactivada" if reactivada else "")),
            entidad_tipo="factura", entidad_id=f.id,
            meta={"gateway_ref": ref, "reactivada": reactivada}))
        db.commit()
        logger.info("factura %s pagada por webhook (reactivada=%s)", f.id,
                    reactivada)
        return 200, {
            "received": True, "conciliado": True, "factura_id": str(f.id),
            "estado": f.estado, "pagada_at": f.pagada_at.isoformat(),
            "suscripcion_reactivada": reactivada,
            "periodo_actual_hasta": (s.periodo_actual_hasta.isoformat()
                                     if s and s.periodo_actual_hasta else None),
        }


@router.post("/pagos")
async def pagos_entrante(request: Request):
    """Concilia un pago notificado por la pasarela (§17).

    Firma HMAC-SHA256 del cuerpo crudo en `X-Signature`. Con la firma válida se
    responde 200 salvo que la factura no exista, caso en el que se devuelve 404
    con un cuerpo controlado —sin excepción ni traza hacia la pasarela— para que
    pueda decidir si reintenta.
    """
    cuerpo = await request.body()
    try:
        _exigir_firma("PAGOS_WEBHOOK_SECRET", settings.pagos_webhook_secret,
                      cuerpo, request.headers.get("X-Signature"))
    except _ErrorWebhook as e:
        return e.respuesta()

    payload = _payload(cuerpo)
    status, contenido = await run_in_threadpool(_conciliar, payload)
    if status == 200:
        return contenido
    return JSONResponse(status_code=status, content=contenido)
