"""Conectores con plataformas del mandante y servicios externos (§16).

Tres piezas y una tarea:

* `SecretWriter` — escritura y borrado de secretos. La API **nunca** guarda las
  credenciales de una integración en la base: `integraciones.credenciales_ref`
  solo contiene una referencia lógica (`kv://<company_id>/<tipo>`) y el secreto
  vive en Azure Key Vault. Backends por `JWE_BACKEND` (keyvault | local), igual
  que el resto de la infraestructura de secretos.
* `Conector` — interfaz común de sincronización. Cada corrida devuelve
  `(registros_procesados, mensaje)`. Los seis conectores del enum
  `integracion_tipo` están declarados pero **no implementados**: levantan
  `ConectorNoImplementado` con el nombre de la plataforma. Es deliberado: un
  conector que finge sincronizar es peor que uno que dice que no existe, porque
  dejaría `sync_logs` con éxitos falsos y la integración en `activa`.
* `resolver_credencial_plataforma` — la lectura del vault de §8.2 desde el
  worker. Es la única función del proyecto que descifra un JWE de credencial.
* `sincronizar_integracion` — la tarea que ejecuta el conector, escribe el
  `SyncLog` y actualiza el estado de la integración.

Regla transversal: ni el secreto de una integración ni el payload de un JWE
entran nunca en `logging`, en una excepción o en una respuesta. De las
credenciales solo se registran la referencia (`credenciales_ref`) y el `jti`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import settings
from ..database import worker_session
from ..models import (Actividad, Alerta, ContratoPlataforma, Integracion,
                      PlataformaCredencial, SyncLog)
from . import actividad
from .crypto import CredencialError, descifrar_credencial
from .jobs import tarea

logger = logging.getLogger("acredittia.integraciones")

# Tipos del enum `integracion_tipo`. El orden es el de la pantalla de
# integraciones; `routers/plataformas.py` usa la misma tupla para saber si una
# plataforma del mandante tiene canal automático.
INTEGRACION_TIPOS: tuple[str, ...] = ("siga", "workmate", "metacontratas",
                                      "webcontrol", "whatsapp", "gdrive")

# Nombre visible de cada plataforma o servicio. Se usa en mensajes de error y en
# los títulos de las alertas: 'metacontratas' no es lo que el usuario reconoce.
NOMBRE_PLATAFORMA: dict[str, str] = {
    "siga": "SIGA",
    "workmate": "WorkMate",
    "metacontratas": "MetaContratas",
    "webcontrol": "Webcontrol",
    "whatsapp": "WhatsApp Business API",
    "gdrive": "Google Drive",
}

# Frecuencias admitidas en `config.frecuencia` para la sync programada (§16).
FRECUENCIAS: tuple[str, ...] = ("hourly", "daily")

# Código de alerta cuando el fallo apunta a la credencial y no al conector. Se
# distingue porque la acción del usuario es distinta: rotar la clave, no
# reintentar.
CODIGO_CREDENCIAL_EXPIRADA = "CREDENCIAL_EXPIRADA"

# Pistas de que el fallo es de credencial. Se buscan en el mensaje del conector,
# que es texto libre venido de un sistema ajeno.
PISTAS_CREDENCIAL = ("token expirado", "token expirada", "expired token",
                     "token has expired", "credencial expirada",
                     "credential expired", "sesión expirada", "sesion expirada",
                     "401", "unauthorized", "no autorizado", "invalid_grant",
                     "invalid grant", "authentication failed",
                     "autenticación fallida", "autenticacion fallida")


# ============================================================================
# Escritura de secretos
# ============================================================================
def nombre_secreto(ref: str) -> str:
    """Convierte una referencia lógica en un nombre de secreto válido.

    Key Vault solo admite `[0-9a-zA-Z-]` de hasta 127 caracteres en el nombre
    del secreto, así que `kv://<uuid>/<tipo>` se traduce reemplazando todo lo
    que no sea alfanumérico por `-`. El guion sí se conserva porque los UUID lo
    llevan y son la parte que identifica a la empresa.
    """
    limpio = re.sub(r"[^0-9a-zA-Z-]", "-", ref or "").strip("-")
    return (limpio or "secreto")[:127]


class SecretWriter(ABC):
    """Escribe y borra el secreto de una integración fuera de la base."""

    @abstractmethod
    def write(self, ref: str, secreto: dict) -> None:
        """Guarda `secreto` bajo `ref`. Sobrescribe si ya existía (rotación)."""

    @abstractmethod
    def delete(self, ref: str) -> None:
        """Borra el secreto de `ref`. No falla si ya no existe (idempotente)."""


class KeyVaultSecretWriter(SecretWriter):
    """Azure Key Vault con `DefaultAzureCredential` (producción).

    El import del SDK es perezoso para que la API arranque sin las librerías de
    Azure cuando `JWE_BACKEND=local`, igual que en `services/crypto.py`.
    """

    def __init__(self, vault_url: str):
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        self._cliente = SecretClient(vault_url=vault_url,
                                     credential=DefaultAzureCredential())

    def write(self, ref: str, secreto: dict) -> None:
        # El valor va serializado en JSON: una integración puede necesitar
        # varias piezas (usuario, clave, api_key, refresh_token) y Key Vault
        # guarda un único string por secreto.
        self._cliente.set_secret(nombre_secreto(ref),
                                 json.dumps(secreto, separators=(",", ":")))
        logger.info("secreto escrito en Key Vault (ref=%s)", ref)

    def delete(self, ref: str) -> None:
        try:
            self._cliente.begin_delete_secret(nombre_secreto(ref)).wait()
            logger.info("secreto borrado de Key Vault (ref=%s)", ref)
        except Exception as e:      # noqa: BLE001  — puede no existir ya
            logger.warning("no se pudo borrar el secreto %s: %s", ref,
                           type(e).__name__)


class LocalSecretWriter(SecretWriter):
    """Archivo JSON en `KEYS_DIR/secrets.json`.

    **SOLO desarrollo y tests.** Los secretos quedan en claro en el disco del
    contenedor; no hay rotación, ni auditoría, ni borrado seguro. En cualquier
    entorno compartido hay que usar `JWE_BACKEND=keyvault`.
    """

    def __init__(self, keys_dir: str):
        self.dir = keys_dir
        os.makedirs(keys_dir, exist_ok=True)
        logger.warning("LocalSecretWriter activo: los secretos de integración "
                       "quedan en claro en %s. Solo para desarrollo.", self.ruta)

    @property
    def ruta(self) -> str:
        return os.path.join(self.dir, "secrets.json")

    def _cargar(self) -> dict:
        if not os.path.exists(self.ruta):
            return {}
        try:
            with open(self.ruta, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            logger.warning("secrets.json ilegible; se reinicia vacío")
            return {}

    def _guardar(self, datos: dict) -> None:
        with open(self.ruta, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
        os.chmod(self.ruta, 0o600)

    def write(self, ref: str, secreto: dict) -> None:
        datos = self._cargar()
        datos[ref] = secreto
        self._guardar(datos)

    def delete(self, ref: str) -> None:
        datos = self._cargar()
        if datos.pop(ref, None) is not None:
            self._guardar(datos)


_writer: SecretWriter | None = None


def get_secret_writer() -> SecretWriter:
    """Backend de secretos según `JWE_BACKEND` (keyvault | local)."""
    global _writer
    if _writer is None:
        if settings.jwe_backend == "keyvault":
            if not settings.azure_keyvault_url:
                raise CredencialError("AZURE_KEYVAULT_URL no configurada")
            _writer = KeyVaultSecretWriter(settings.azure_keyvault_url)
        else:
            _writer = LocalSecretWriter(settings.keys_dir)
    return _writer


def reset_secret_writer() -> None:
    """Para tests: fuerza la reconstrucción del writer."""
    global _writer
    _writer = None


def ref_de_integracion(company_id, tipo: str) -> str:
    """Referencia lógica del secreto de una integración.

    Es determinista sobre `(company_id, tipo)` —la misma clave única que tiene
    la tabla—, así que una rotación reescribe el mismo secreto y no deja huérfano
    el anterior.
    """
    return f"kv://{company_id}/{tipo}"


# ============================================================================
# Conectores
# ============================================================================
class ConectorNoImplementado(RuntimeError):
    """El conector existe como interfaz pero todavía no habla con el sistema."""


class Conector(ABC):
    """Interfaz común de sincronización (§16).

    Una corrida hace dos cosas: **empuja** hacia la plataforma del mandante el
    estado de acreditación de la empresa (personal y equipos con sus documentos
    vigentes) y **trae** de vuelta las observaciones que el mandante haya
    registrado, para convertirlas en alertas y en documentos rechazados.

    `sincronizar` devuelve `(registros_procesados, mensaje)`. Puede escribir en
    `db` —es la sesión del worker, con el tenant ya fijado— pero **no** debe
    hacer `commit`: quien lo hace es la tarea, después de escribir el `SyncLog`,
    para que el resultado de la corrida y su registro entren en la misma
    transacción.
    """

    #: clave del enum `integracion_tipo` que atiende este conector
    TIPO: str = ""

    @abstractmethod
    def sincronizar(self, db: Session, integracion: Integracion) -> tuple[int, str]:
        """Ejecuta una corrida. Devuelve (registros_procesados, mensaje)."""

    def _pendiente(self, detalle: str) -> ConectorNoImplementado:
        """Excepción con el nombre de la plataforma, no con el del enum."""
        nombre = NOMBRE_PLATAFORMA.get(self.TIPO, self.TIPO or "la plataforma")
        return ConectorNoImplementado(
            f"El conector de {nombre} todavía no está implementado: {detalle}")


class SigaConector(Conector):
    """SIGA (portal de contratistas de varias faenas mineras).

    Lo que hará: autenticarse con la credencial del vault, publicar en SIGA el
    estado de acreditación de cada trabajador y equipo del contrato (documento a
    documento, con su fecha de vencimiento) y descargar el listado de
    observaciones y rechazos del mandante para volcarlo en `alertas` y en el
    estado de los `documentos` afectados.

    Pendiente: SIGA no expone API pública documentada; el acceso real es por
    formulario web con sesión, así que el conector necesita un cliente HTTP con
    manejo de sesión y CSRF, o un acuerdo de integración con el mandante.
    """

    TIPO = "siga"

    def sincronizar(self, db: Session, integracion: Integracion) -> tuple[int, str]:
        raise self._pendiente(
            "falta el cliente de sesión del portal y el acuerdo de integración "
            "con el mandante")


class WorkmateConector(Conector):
    """WorkMate (gestión documental de contratistas).

    Lo que hará: empujar los expedientes de personal y equipo con sus
    documentos, y traer el resultado de la validación de WorkMate (aprobado,
    observado, rechazado) para reflejarlo en el estado de cada documento y
    generar las alertas correspondientes.

    Pendiente: el contrato de la API de WorkMate (endpoints, esquema de
    autenticación y mapeo de tipos de documento) no está disponible.
    """

    TIPO = "workmate"

    def sincronizar(self, db: Session, integracion: Integracion) -> tuple[int, str]:
        raise self._pendiente(
            "falta el contrato de la API y el mapeo de tipos de documento")


class MetacontratasConector(Conector):
    """MetaContratas (control de acceso y documentación de contratistas).

    Lo que hará: sincronizar la nómina habilitada —quién puede entrar a faena— y
    traer los motivos de bloqueo por documento faltante o vencido, que son los
    que hoy la empresa consulta a mano en el portal.

    Pendiente: la API de MetaContratas exige convenio por mandante; sin
    credenciales de integración no hay forma de validar el mapeo.
    """

    TIPO = "metacontratas"

    def sincronizar(self, db: Session, integracion: Integracion) -> tuple[int, str]:
        raise self._pendiente(
            "falta el convenio de integración y las credenciales de servicio")


class WebcontrolConector(Conector):
    """Webcontrol (control documental y de acceso vehicular).

    Lo que hará: publicar el estado de la flota (permiso de circulación, SOAP,
    revisión técnica, mantención) y traer las observaciones de la inspección de
    equipos, que es la fuente habitual de rechazos en portería.

    Pendiente: sin especificación de la API ni entorno de pruebas.
    """

    TIPO = "webcontrol"

    def sincronizar(self, db: Session, integracion: Integracion) -> tuple[int, str]:
        raise self._pendiente("falta la especificación de la API y un entorno "
                              "de pruebas")


class WhatsappConector(Conector):
    """WhatsApp Business API (canal de notificaciones salientes).

    Lo que hará: enviar por WhatsApp los avisos que hoy salen por email
    (vencimientos a 30/15/7 días, alertas críticas, fallos de sincronización)
    según `notificacion_preferencias.canal_whatsapp`, y registrar los estados de
    entrega que devuelva el webhook de Meta.

    A diferencia del resto, aquí la «sincronización» no trae datos del mandante:
    la entrada la empuja Meta contra `POST /webhooks/whatsapp`. Una corrida
    manual solo servirá para verificar el token y la plantilla de mensaje.

    Pendiente: falta el envío con plantillas aprobadas y la cola de reintentos.
    """

    TIPO = "whatsapp"

    def sincronizar(self, db: Session, integracion: Integracion) -> tuple[int, str]:
        raise self._pendiente(
            "falta el envío con plantillas aprobadas por Meta; la recepción "
            "ya llega por POST /webhooks/whatsapp")


class GdriveConector(Conector):
    """Google Drive (respaldo y consumo de carpetas compartidas).

    Lo que hará: leer la carpeta compartida del contrato para dar de alta como
    archivos los documentos que la empresa deja ahí, y depositar copia de los
    expedientes generados para que el mandante los descargue.

    Pendiente: falta el flujo OAuth de consentimiento (el token del usuario no
    se puede pedir desde un worker) y la política de mapeo carpeta→requisito.
    """

    TIPO = "gdrive"

    def sincronizar(self, db: Session, integracion: Integracion) -> tuple[int, str]:
        raise self._pendiente(
            "falta el consentimiento OAuth y el mapeo de carpeta a requisito")


#: Registro por `integracion_tipo`. La tarea resuelve el conector aquí, así que
#: implementar uno de verdad es sustituir su clase sin tocar el router.
CONECTORES: dict[str, type[Conector]] = {
    "siga": SigaConector,
    "workmate": WorkmateConector,
    "metacontratas": MetacontratasConector,
    "webcontrol": WebcontrolConector,
    "whatsapp": WhatsappConector,
    "gdrive": GdriveConector,
}


# ============================================================================
# §8.2 — Resolución de credenciales de plataforma (solo worker)
# ============================================================================
def resolver_credencial_plataforma(db: Session, credencial_id) -> dict:
    """Descifra la credencial de una plataforma del mandante para usarla.

    **Solo el worker.** La consulta pide `plataforma_credenciales.credencial_jwe`,
    columna cuyo SELECT está revocado para `acredittia_app`: llamada con el rol
    de la API, PostgreSQL responde `permission denied for column credencial_jwe`
    y eso es el comportamiento correcto, no un error a sortear. El worker se
    conecta con `acredittia_worker`, que sí tiene ese GRANT (§10.2 del modelo de
    datos).

    Se validan `sub`, `cid` y `ctr` contra la fila leída: si no coinciden, el
    JWE fue copiado de otra credencial manipulando la base directamente.

    Cada resolución deja rastro en `actividad` con `tipo='visualizacion'` y el
    `jti` del token —nunca el payload— y actualiza `last_used_at`, de modo que
    una credencial que se usa sin que nadie lo haya pedido sea visible en el
    historial de la cuenta. La transacción se confirma aquí: la auditoría del
    descifrado no debe depender de que la sincronización termine bien.

    Devuelve `{usuario, password}`. El valor devuelto no se guarda, no se
    serializa y no se registra en ningún log.
    """
    cred_id = uuid.UUID(str(credencial_id))

    fila = db.execute(
        select(PlataformaCredencial.company_id,
               PlataformaCredencial.usuario,
               PlataformaCredencial.estado,
               PlataformaCredencial.expira_at,
               PlataformaCredencial.credencial_jwe,
               ContratoPlataforma.contrato_id.label("contrato_id"),
               ContratoPlataforma.nombre.label("plataforma"))
        .join(ContratoPlataforma,
              ContratoPlataforma.id == PlataformaCredencial.contrato_plataforma_id)
        .where(PlataformaCredencial.id == cred_id)).first()
    if fila is None:
        raise CredencialError(f"La credencial {cred_id} no existe")
    if fila.estado != "activa":
        raise CredencialError(
            f"La credencial {cred_id} está '{fila.estado}' y no se puede usar")

    # `descifrar_credencial` valida además iss, aud y exp, y jamás incluye el
    # secreto en el mensaje de sus excepciones.
    payload = descifrar_credencial(
        fila.credencial_jwe, esperado_sub=cred_id, esperado_cid=fila.company_id,
        esperado_ctr=fila.contrato_id)
    jti = payload.get("jti")

    ahora = datetime.now(timezone.utc)
    # UPDATE de Core: la fila no entra en el mapa de identidad de la sesión, así
    # que ningún refresh posterior intentará releer la columna revocada.
    db.execute(
        update(PlataformaCredencial)
        .where(PlataformaCredencial.id == cred_id)
        .values(last_used_at=ahora)
        .execution_options(synchronize_session=False))
    # Se escribe la fila a mano porque `actividad.log` no acepta `metadata`, y
    # aquí lo único que interesa guardar es el jti.
    db.add(Actividad(
        company_id=fila.company_id, user_id=None, tipo="visualizacion",
        modulo="integraciones",
        descripcion=(f"Credencial '{fila.usuario}' de '{fila.plataforma}' "
                     f"descifrada por el worker de integración"),
        entidad_tipo="plataforma_credencial", entidad_id=cred_id,
        plataforma=fila.plataforma, meta={"jti": jti},
    ))
    db.commit()
    logger.info("credencial %s resuelta (jti=%s)", cred_id, jti)

    return {"usuario": payload.get("usr"), "password": payload.get("pwd")}


# ============================================================================
# §16 — Tarea de sincronización
# ============================================================================
def sugiere_credencial_expirada(mensaje: str) -> bool:
    """¿El fallo apunta a la credencial y no al conector?

    Heurística por palabras clave sobre un mensaje de texto libre. Se usa solo
    para elegir el código del título de la alerta: un falso positivo hace que el
    usuario revise la credencial antes que el conector, no que se pierda nada.
    """
    texto = (mensaje or "").lower()
    return any(p in texto for p in PISTAS_CREDENCIAL)


def _titulo_alerta(tipo: str, mensaje: str) -> str:
    nombre = NOMBRE_PLATAFORMA.get(tipo, tipo)
    if sugiere_credencial_expirada(mensaje):
        return f"{CODIGO_CREDENCIAL_EXPIRADA}: credencial de {nombre} rechazada"
    return f"Falló la sincronización con {nombre}"


@tarea("sincronizar_integracion")
def sincronizar_integracion(integracion_id: str, company_id: str,
                            corrida: str | None = None) -> None:
    """Ejecuta una corrida de sincronización y deja constancia del resultado.

    El `SyncLog` se escribe **al terminar**, con el resultado real: un log
    creado al empezar y corregido después dejaría corridas colgadas en un estado
    provisional cada vez que el worker se reinicie a mitad de trabajo.

    La corrida nunca propaga la excepción: un fallo marca la integración
    `con_error`, escribe el log y genera una `Alerta` de origen `integracion`
    con severidad `alta`. Si el mensaje apunta a la credencial, el título lleva
    el código `CREDENCIAL_EXPIRADA`, porque la acción del usuario es rotar la
    clave y no reintentar.
    """
    cid = uuid.UUID(company_id)
    iid = uuid.UUID(integracion_id)
    inicio = datetime.now(timezone.utc)

    with worker_session(company_id=cid) as db:
        integ = db.get(Integracion, iid)
        if integ is None or integ.company_id != cid:
            logger.error("integración %s inexistente o de otra empresa", iid)
            return
        if integ.estado == "desconectada":
            # Puede pasar con la sync programada: la integración se desconectó
            # entre la planificación y la ejecución. No es un error de corrida.
            logger.info("integración %s desconectada; corrida omitida", iid)
            return

        tipo = integ.tipo
        registros, mensaje, ok = 0, "", False
        try:
            clase = CONECTORES.get(tipo)
            if clase is None:
                raise ConectorNoImplementado(
                    f"No hay conector registrado para el tipo '{tipo}'")
            registros, mensaje = clase().sincronizar(db, integ)
            ok = True
        except ConectorNoImplementado as e:
            mensaje = str(e)
            logger.warning("integración %s (%s): %s", iid, tipo, mensaje)
        except Exception as e:      # noqa: BLE001
            mensaje = f"{type(e).__name__}: {e}"
            logger.exception("integración %s (%s) falló", iid, tipo)

        fin = datetime.now(timezone.utc)
        try:
            if not ok:
                # Se descarta lo que el conector hubiera dejado a medias antes
                # de escribir el log; si no, una corrida fallida podría
                # confirmar cambios parciales junto con su propio SyncLog.
                db.rollback()
                integ = db.get(Integracion, iid)
                if integ is None:
                    return

            db.add(SyncLog(
                integracion_id=iid, company_id=cid,
                status="exito" if ok else "error",
                mensaje=mensaje[:2000] if mensaje else None,
                registros_procesados=registros, started_at=inicio,
                finished_at=fin,
            ))
            integ.estado = "activa" if ok else "con_error"
            integ.updated_at = fin
            if ok:
                integ.ultima_sync_at = fin

            if ok:
                actividad.log(
                    db, cid, "actualizacion", "integraciones",
                    f"Sincronización con {NOMBRE_PLATAFORMA.get(tipo, tipo)} "
                    f"completada ({registros} registros)", user_id=None,
                    entidad_tipo="integracion", entidad_id=iid, plataforma=tipo)
            else:
                db.add(Alerta(
                    company_id=cid, severidad="alta", estado="nueva",
                    origen="integracion", titulo=_titulo_alerta(tipo, mensaje),
                    descripcion=mensaje[:1000], plataforma=tipo,
                ))
                actividad.log(
                    db, cid, "actualizacion", "integraciones",
                    f"Sincronización con {NOMBRE_PLATAFORMA.get(tipo, tipo)} "
                    f"falló: {mensaje[:200]}", user_id=None,
                    entidad_tipo="integracion", entidad_id=iid, plataforma=tipo)
            db.commit()
            logger.info("integración %s: corrida %s cerrada como %s "
                        "(%s registros)", iid, corrida or "-",
                        "exito" if ok else "error", registros)
        except Exception:           # noqa: BLE001
            # Si ni el log se puede escribir, queda en logging y nada más: la
            # tarea no propaga para que el worker no la reintente en bucle.
            logger.exception("no se pudo cerrar la corrida de la integración %s",
                             iid)
            db.rollback()
