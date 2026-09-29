"""Worker de Celery sobre Azure Cache for Redis.

Dos procesos separados, ambos con la misma imagen que la API:

    celery -A worker.celery_app worker --loglevel=info --concurrency=4
    celery -A worker.celery_app beat   --loglevel=info

Los routers nunca importan Celery: encolan con `services.jobs.enqueue(nombre,
**kwargs)`, que aquí se traduce a una única tarea `ejecutar_tarea` que resuelve
el nombre en el registro `TAREAS`. Así añadir una tarea de negocio no obliga a
tocar la configuración de la cola ni a redespleg­ar el worker con rutas nuevas.

El worker se conecta con el rol `acredittia_worker`, que es el único con SELECT
sobre `plataforma_credenciales.credencial_jwe`. Si el worker corriera con el rol
de la API, resolver una credencial fallaría con *permission denied*, que es
exactamente el comportamiento esperado.
"""
from __future__ import annotations

import logging
import os

from celery import Celery
from celery.schedules import crontab

# El rol del worker es distinto del de la API (§7.3 del modelo de datos).
if os.environ.get("WORKER_DATABASE_URL"):
    os.environ["DATABASE_URL"] = os.environ["WORKER_DATABASE_URL"]

from app.config import settings  # noqa: E402
# Importar los módulos de tareas puebla el registro TAREAS.
from app.services import integraciones as _svc_integraciones  # noqa: E402,F401
from app.services import tasks as _svc_tasks  # noqa: E402,F401
from app.services.jobs import TAREAS  # noqa: E402

log = logging.getLogger("acredittia.worker")

celery = Celery("acredittia", broker=settings.redis_url, backend=settings.redis_url)
celery.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="America/Santiago",
    enable_utc=True,
    task_acks_late=True,               # si el worker muere, la tarea se reintenta
    worker_prefetch_multiplier=1,      # trabajos largos: no acaparar la cola
    task_time_limit=15 * 60,
    task_soft_time_limit=13 * 60,
    broker_connection_retry_on_startup=True,
)


@celery.task(name="acredittia.ejecutar_tarea", bind=True, max_retries=3)
def ejecutar_tarea(self, nombre: str, kwargs: dict | None = None):
    """Punto de entrada único: resuelve `nombre` en el registro y ejecuta.

    Las tareas de negocio capturan sus propios errores y marcan el job como
    `failed`, así que un reintento aquí solo cubre fallos de infraestructura
    (base de datos o storage momentáneamente inaccesibles).
    """
    fn = TAREAS.get(nombre)
    if fn is None:
        log.error("Tarea desconocida: %s", nombre)
        return {"ok": False, "error": "tarea_desconocida", "tarea": nombre}
    try:
        fn(**(kwargs or {}))
        return {"ok": True, "tarea": nombre}
    except Exception as exc:      # infraestructura, no negocio
        log.exception("Fallo de infraestructura en %s", nombre)
        raise self.retry(exc=exc, countdown=30)


# --------------------------------------------------------------------- cron
@celery.task(name="acredittia.cron_diario")
def cron_diario():
    """00:30 America/Santiago (§10.1 de la especificación).

    Recalcula `estado_calc` de los documentos con vencimiento, actualiza el
    estado agregado de sujetos y contratos, genera alertas, escribe los
    snapshots de cumplimiento del día y marca las credenciales expiradas.
    """
    from app.database import worker_session
    from app.services.vencimientos import (
        expirar_credenciales, escribir_snapshots, recalcular_documentos)

    with worker_session(is_admin=True) as db:
        docs = recalcular_documentos(db)
        snaps = escribir_snapshots(db)
        creds = expirar_credenciales(db)
    log.info("Cron diario: %s documentos, %s snapshots, %s credenciales expiradas",
             docs, snaps, creds)
    return {"documentos": docs, "snapshots": snaps, "credenciales": creds}


@celery.task(name="acredittia.cron_reportes_programados")
def cron_reportes_programados():
    """Cada hora: dispara los `reportes_programados` cuyo cron toca ahora."""
    from app.database import worker_session
    from app.services.programados import disparar_pendientes

    with worker_session(is_admin=True) as db:
        n = disparar_pendientes(db)
    log.info("Reportes programados disparados: %s", n)
    return {"disparados": n}


@celery.task(name="acredittia.cron_purga_temporales")
def cron_purga_temporales():
    """Purga los blobs temporales de extracción IA con más de 24 horas."""
    from app.services.tasks import purgar_temporales
    n = purgar_temporales()
    log.info("Blobs temporales purgados: %s", n)
    return {"purgados": n}


celery.conf.beat_schedule = {
    "cron-diario": {
        "task": "acredittia.cron_diario",
        "schedule": crontab(hour=0, minute=30),
    },
    "reportes-programados": {
        "task": "acredittia.cron_reportes_programados",
        "schedule": crontab(minute=5),          # cada hora, al minuto 5
    },
    "purga-temporales": {
        "task": "acredittia.cron_purga_temporales",
        "schedule": crontab(hour=4, minute=0),
    },
}
from worker.ops_tasks import registrar_ops
registrar_ops(celery)