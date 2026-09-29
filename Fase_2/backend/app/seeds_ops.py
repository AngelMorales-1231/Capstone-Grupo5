"""Script de Seeders para la Consola Interna de Operaciones (/admin/ops).

Puebla con datos de prueba realistas e idempotentes las tablas mapeadas
en `app/models_ops.py`:
- ops_componentes (Componentes de infraestructura y servicios)
- ops_jobs (Catálogo de jobs programados y continuos)
- ops_job_runs (Historial de ejecuciones)
- ops_incidentes (Incidentes abiertos, monitoreados y resueltos)
- ops_disponibilidad_diaria (Consolidado de uptime y latencia diaria)

Idempotencia:
- Verifica la existencia previa de registros por clave primaria o campos únicos.
- Si el registro ya existe, no se duplica ni altera datos existentes.
"""
from __future__ import annotations

import logging
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal, reset_ctx, set_ctx
from app.models import User
from app.models_ops import (
    OpsCompEstado,
    OpsComponente,
    OpsDisponibilidadDiaria,
    OpsIncEstado,
    OpsIncidente,
    OpsJob,
    OpsJobRun,
    OpsJobTipo,
    OpsRunStatus,
    OpsRunTrigger,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("acredittia.seeds_ops")


def seed_ops(db: Session) -> dict[str, int]:
    """Inserta registros iniciales de operaciones de forma idempotente."""
    conteo = {
        "componentes_creados": 0,
        "jobs_creados": 0,
        "runs_creados": 0,
        "incidentes_creados": 0,
        "disponibilidad_creada": 0,
    }

    # -------------------------------------------------------------------------
    # 0. Usuario administrador (para asociar a incidentes o acciones de ops)
    # -------------------------------------------------------------------------
    admin_user = db.scalar(
        select(User).where(User.role == "admin").order_by(User.created_at.asc()).limit(1)
    )
    admin_id = admin_user.id if admin_user else None

    # -------------------------------------------------------------------------
    # 1. Componentes del sistema (ops_componentes)
    # -------------------------------------------------------------------------
    componentes_data = [
        {
            "clave": "api",
            "nombre": "API Principal",
            "descripcion": "FastAPI en Azure Container Apps · Endpoints REST, Auth y RLS",
            "orden": 1,
            "activo": True,
        },
        {
            "clave": "db",
            "nombre": "Base de Datos",
            "descripcion": "PostgreSQL 16 · Esquema multi-tenant, RLS y particiones",
            "orden": 2,
            "activo": True,
        },
        {
            "clave": "workers",
            "nombre": "Workers · Celery",
            "descripcion": "Procesamiento asíncrono en segundo plano y jobs nocturnos",
            "orden": 3,
            "activo": True,
        },
        {
            "clave": "storage",
            "nombre": "Storage de Documentos",
            "descripcion": "Azure Blob Storage / almacenamiento local para evidencias",
            "orden": 4,
            "activo": True,
        },
        {
            "clave": "ia",
            "nombre": "Servicio de Verificación IA",
            "descripcion": "Motor OCR y validación automática de requisitos documentales",
            "orden": 5,
            "activo": True,
        },
        {
            "clave": "integraciones",
            "nombre": "Integraciones Externas",
            "descripcion": "Sincronización con SIGA, WebControl, Metacontratas y Workmate",
            "orden": 6,
            "activo": True,
        },
    ]

    for c_info in componentes_data:
        comp = db.get(OpsComponente, c_info["clave"])
        if not comp:
            db.add(OpsComponente(**c_info))
            conteo["componentes_creados"] += 1
            log.info("Componente creado: %s (%s)", c_info["nombre"], c_info["clave"])
        else:
            log.debug("Componente existente: %s", c_info["clave"])

    db.flush()

    # -------------------------------------------------------------------------
    # 2. Catálogo de Procesos / Jobs (ops_jobs)
    # -------------------------------------------------------------------------
    jobs_data = [
        {
            "clave": "mantenimiento_bd",
            "nombre": "Mantenimiento y Vacuum de Base de Datos",
            "tipo": OpsJobTipo.programado,
            "cron_expr": "0 3 * * 0",  # Semanal: domingos a las 03:00 UTC
            "componente": "db",
            "activo": True,
            "timeout_s": 1800,
        },
        {
            "clave": "vencimientos",
            "nombre": "Cálculo de vencimientos y snapshots",
            "tipo": OpsJobTipo.programado,
            "cron_expr": "30 0 * * *",
            "componente": "workers",
            "activo": True,
            "timeout_s": 3600,
        },
        {
            "clave": "purga_temporales",
            "nombre": "Purga de blobs temporales IA",
            "tipo": OpsJobTipo.programado,
            "cron_expr": "0 4 * * *",
            "componente": "storage",
            "activo": True,
            "timeout_s": 900,
        },
        {
            "clave": "ia_revision",
            "nombre": "Verificación IA de documentos",
            "tipo": OpsJobTipo.continuo,
            "cron_expr": None,
            "componente": "ia",
            "activo": True,
            "timeout_s": 3600,
        },
    ]

    for j_info in jobs_data:
        job = db.get(OpsJob, j_info["clave"])
        if not job:
            db.add(OpsJob(**j_info))
            conteo["jobs_creados"] += 1
            log.info("Job creado: %s (%s)", j_info["nombre"], j_info["clave"])
        else:
            log.debug("Job existente: %s", j_info["clave"])

    db.flush()

    # -------------------------------------------------------------------------
    # 3. Historial de ejecuciones de Jobs (ops_job_runs)
    # -------------------------------------------------------------------------
    ahora = datetime.now(timezone.utc)
    runs_data = [
        {
            "job_clave": "mantenimiento_bd",
            "status": OpsRunStatus.ok,
            "disparo": OpsRunTrigger.cron,
            "params": {"vacuum_full": False, "reindex": True},
            "started_at": ahora - timedelta(days=1, hours=2),
            "finished_at": ahora - timedelta(days=1, hours=1, minutes=58),
            "items_procesados": 24,
            "mensaje": "Vacuum analyze y optimización de estadísticas completados en 24 tablas.",
        },
        {
            "job_clave": "vencimientos",
            "status": OpsRunStatus.ok,
            "disparo": OpsRunTrigger.cron,
            "params": {},
            "started_at": ahora - timedelta(hours=5),
            "finished_at": ahora - timedelta(hours=4, minutes=58),
            "items_procesados": 182,
            "mensaje": "182 documentos recalculados, 15 alertas de próximo vencimiento emitidas.",
        },
    ]

    for r_info in runs_data:
        # Idempotencia: no volver a insertar si ya existe un run con el mismo mensaje para el job
        run_existe = db.scalar(
            select(OpsJobRun.id).where(
                OpsJobRun.job_clave == r_info["job_clave"],
                OpsJobRun.mensaje == r_info["mensaje"],
            ).limit(1)
        )
        if not run_existe:
            db.add(OpsJobRun(**r_info))
            conteo["runs_creados"] += 1
            log.info("Job run registrado para: %s", r_info["job_clave"])

    db.flush()

    # -------------------------------------------------------------------------
    # 4. Incidentes de Operaciones (ops_incidentes)
    # -------------------------------------------------------------------------
    incidentes_data = [
        {
            "componente": "db",
            "severidad": OpsCompEstado.degradado,
            "estado": OpsIncEstado.resuelto,
            "origen": "auto",
            "titulo": "Pico de conexiones simultáneas y latencia en consultas transaccionales",
            "descripcion": (
                "Se detectó un incremento de latencia p95 > 900ms debido a consultas "
                "concurrentes durante la sincronización masiva de dotación."
            ),
            "resolucion": (
                "Se optimizó el pool de conexiones en SQLAlchemy y se agregaron "
                "índices de cobertura en la tabla de contratos."
            ),
            "abierto_at": ahora - timedelta(days=2),
            "resuelto_at": ahora - timedelta(days=2, hours=-1),
            "creado_por": None,
        },
        {
            "componente": "api",
            "severidad": OpsCompEstado.degradado,
            "estado": OpsIncEstado.monitoreando,
            "origen": "manual",
            "titulo": "Latencia intermitente en exportación de matriz de cumplimiento",
            "descripcion": (
                "Clientes de gran envergadura reportan demoras de hasta 4 segundos al generar "
                "reportes consolidados de faena con más de 500 contratistas."
            ),
            "resolucion": None,
            "abierto_at": ahora - timedelta(hours=3),
            "resuelto_at": None,
            "creado_por": admin_id,
        },
    ]

    for inc_info in incidentes_data:
        # Idempotencia: verificar por título y componente
        inc_existe = db.scalar(
            select(OpsIncidente.id).where(
                OpsIncidente.componente == inc_info["componente"],
                OpsIncidente.titulo == inc_info["titulo"],
            ).limit(1)
        )
        if not inc_existe:
            db.add(OpsIncidente(**inc_info))
            conteo["incidentes_creados"] += 1
            log.info("Incidente creado: [%s] %s", inc_info["componente"], inc_info["titulo"])
        else:
            log.debug("Incidente existente: %s", inc_info["titulo"])

    db.flush()

    # -------------------------------------------------------------------------
    # 5. Disponibilidad diaria reciente (ops_disponibilidad_diaria)
    # -------------------------------------------------------------------------
    hoy = date.today()
    for dias_atras in range(1, 8):
        fecha_disp = hoy - timedelta(days=dias_atras)
        for clave_comp, p95_lat in [("api", 48), ("db", 22), ("workers", 110)]:
            pk = (clave_comp, fecha_disp)
            disp_existente = db.get(OpsDisponibilidadDiaria, pk)
            if not disp_existente:
                db.add(
                    OpsDisponibilidadDiaria(
                        componente=clave_comp,
                        fecha=fecha_disp,
                        estado=OpsCompEstado.operativo,
                        uptime_pct=100.00 if dias_atras != 2 or clave_comp != "db" else 99.45,
                        latencia_p95_ms=p95_lat,
                        checks_total=1440,
                        checks_fallidos=0 if dias_atras != 2 or clave_comp != "db" else 8,
                    )
                )
                conteo["disponibilidad_creada"] += 1

    return conteo


def main() -> int:
    log.info("Iniciando script de seeds para /admin/ops...")
    with SessionLocal() as db:
        # Es indispensable fijar is_admin=True para satisfacer la política RLS (p_ops_solo_admin)
        set_ctx(is_admin=True)
        try:
            resumen = seed_ops(db)
            db.commit()
            log.info("¡Seeder ejecutado exitosamente!")
            for clave, valor in resumen.items():
                log.info("  * %s: %d", clave, valor)
            return 0
        except Exception as exc:
            db.rollback()
            log.exception("Error crítico ejecutando el seeder de operaciones: %s", exc)
            return 1
        finally:
            reset_ctx()


if __name__ == "__main__":
    sys.exit(main())
