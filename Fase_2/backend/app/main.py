"""Arranque de la API.

El esquema NO se aplica aquí: lo hace el job de migración (`python -m migrate.run`)
bajo `pg_advisory_lock`, de modo que varias réplicas de Container Apps arrancando
a la vez no compitan por el DDL. La API solo verifica la versión y falla rápido
si no coincide, para que un despliegue con esquema desfasado se detecte en el
health check y no a mitad de un request.

El cron tampoco vive aquí cuando hay Celery: `celery beat` lo ejecuta. Con
`QUEUE_BACKEND=inproc` (desarrollo) se mantiene APScheduler dentro del proceso.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .database import (SessionLocal, apply_schema, assert_schema_version,
                       reset_ctx, schema_version, set_ctx, wait_for_db)
from .routers import (actividad, admin, alertas, auth, blobs, calendario, cargos,
                      company, contrato_requisitos, contratos, dashboard,
                      documentos, faenas, ia, integraciones, licencia_interna,
                      notificaciones, personas, plataformas, reportes,
                      requisitos, sujetos, suscripcion, webhooks)
from .routers import admin_ops
from .services import ops
# Importar los módulos de tareas registra las entradas de TAREAS que usa la cola.
from .services import integraciones as _svc_integraciones  # noqa: F401
from .services import tasks as _svc_tasks  # noqa: F401
from .services.vencimientos import recalcular_documentos

log = logging.getLogger("acredittia")


def _job_vencimientos() -> None:
    """Cron de desarrollo. En producción lo dispara celery beat."""
    from .database import worker_session
    with worker_session(is_admin=True) as db:
        recalcular_documentos(db)


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_db()

    if settings.db_apply_schema_on_start:
        # Solo desarrollo y tests. En Azure lo hace el Container Apps Job.
        v = apply_schema()
        log.info("Esquema aplicado, versión %s", v)
    assert_schema_version(settings.schema_version_esperada)

    from . import seeds
    with SessionLocal() as db:
        set_ctx(is_admin=True)          # las semillas tocan catálogos globales
        try:
            seeds.run(db)
            recalcular_documentos(db)
        finally:
            reset_ctx()

    scheduler = None
    if settings.queue_backend != "celery":
        from apscheduler.schedulers.background import BackgroundScheduler
        scheduler = BackgroundScheduler(timezone="America/Santiago")
        scheduler.add_job(_job_vencimientos, "cron", hour=0, minute=30)
        scheduler.start()
        log.info("Cron en proceso activo (QUEUE_BACKEND=%s)", settings.queue_backend)

    yield

    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(title="Acredittia API", version="1.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def limpiar_contexto(request: Request, call_next):
    """Cada request empieza sin tenant.

    Es la red de seguridad del aislamiento: si un endpoint consultara la base
    sin pasar por la dependencia de autenticación, RLS no encontraría
    `app.company_id` y devolvería vacío en lugar de datos de otra empresa.
    """
    reset_ctx()
    try:
        return await call_next(request)
    finally:
        reset_ctx()


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, dict) else {
        "code": "ERROR", "message": str(exc.detail)}
    return JSONResponse(status_code=exc.status_code, content={"error": detail},
                        headers=getattr(exc, "headers", None))


API = "/api/v1"

# --- Autenticación y administración de la plataforma ---------------------
app.include_router(auth.router, prefix=API)
app.include_router(admin.router, prefix=API)
app.include_router(ops.router, prefix=API)
app.include_router(cargos.router_admin, prefix=API)
app.include_router(suscripcion.router_admin, prefix=API)

# --- Empresa y catálogos --------------------------------------------------
app.include_router(company.router, prefix=API)
app.include_router(faenas.router, prefix=API)
app.include_router(cargos.router, prefix=API)
app.include_router(requisitos.router, prefix=API)

# --- Contratos: el router base primero, para conservar el orden de rutas ---
app.include_router(contratos.router, prefix=API)
app.include_router(plataformas.router, prefix=API)
app.include_router(contrato_requisitos.router, prefix=API)

# --- Sujetos e identidad --------------------------------------------------
app.include_router(sujetos.router, prefix=API)
app.include_router(licencia_interna.router, prefix=API)
app.include_router(personas.router, prefix=API)
app.include_router(personas.router_flota, prefix=API)

# --- Documentos, IA y almacenamiento -------------------------------------
app.include_router(documentos.router, prefix=API)
app.include_router(ia.router, prefix=API)
app.include_router(blobs.router, prefix=API)     # SAS local; en Azure no se usa

# --- Operación ------------------------------------------------------------
app.include_router(alertas.router, prefix=API)
app.include_router(dashboard.router, prefix=API)
app.include_router(calendario.router, prefix=API)
app.include_router(actividad.router, prefix=API)
app.include_router(notificaciones.router, prefix=API)
app.include_router(reportes.router, prefix=API)
app.include_router(reportes.router_export, prefix=API)
app.include_router(integraciones.router, prefix=API)

# --- Comercial y webhooks -------------------------------------------------
app.include_router(suscripcion.router_planes, prefix=API)
app.include_router(suscripcion.router, prefix=API)
app.include_router(suscripcion.router_facturas, prefix=API)
app.include_router(webhooks.router, prefix=API)


@app.get("/health")
def health():
    """Liveness. Container Apps lo usa como probe de arranque."""
    return {"status": "ok", "version": app.version}


@app.get("/health/esquema")
def health_esquema():
    """Readiness estricta: comprueba que la BD esté en la versión esperada."""
    v = schema_version()
    esperada = settings.schema_version_esperada
    if v != esperada:
        return JSONResponse(
            status_code=503,
            content={"error": {"code": "ESQUEMA_DESFASADO",
                               "message": f"Esquema {v}, se esperaba {esperada}"}},
        )
    return {"status": "ok", "schema_version": v}
