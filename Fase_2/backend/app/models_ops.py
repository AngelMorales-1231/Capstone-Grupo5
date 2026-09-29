"""Modelos ORM de la consola interna (/admin/ops).

Mapea las tablas de 07_consola_interna.sql sobre el MISMO `Base` de
`app.models`, de modo que comparten metadata y sesiones.

También añade `Company.industria` sin tocar `models.py`: SQLAlchemy permite
agregar columnas mapeadas a una clase declarativa después de definirla, y la
columna ya existe en la BD (07 §2).
"""
from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import ENUM as PGENUM
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models import Base, Company, Plan, User, _uuid


# ============================================================================
# 1. Tipos ENUM nativos de PostgreSQL (§4.1)
# ============================================================================

class CompanyIndustria(str, enum.Enum):
    mineria = "mineria"
    construccion = "construccion"
    energia = "energia"
    industrial = "industrial"
    otras = "otras"


class OpsJobTipo(str, enum.Enum):
    programado = "programado"
    continuo = "continuo"


class OpsRunStatus(str, enum.Enum):
    queued = "queued"
    running = "running"
    ok = "ok"
    error = "error"
    timeout = "timeout"


class OpsRunTrigger(str, enum.Enum):
    cron = "cron"
    manual = "manual"
    reintento = "reintento"


class OpsCompEstado(str, enum.Enum):
    operativo = "operativo"
    degradado = "degradado"
    caido = "caido"


class OpsIncEstado(str, enum.Enum):
    abierto = "abierto"
    monitoreando = "monitoreando"
    resuelto = "resuelto"


_OPS_ENUMS = {
    "company_industria": tuple(e.value for e in CompanyIndustria),
    "ops_job_tipo": tuple(e.value for e in OpsJobTipo),
    "ops_run_status": tuple(e.value for e in OpsRunStatus),
    "ops_run_trigger": tuple(e.value for e in OpsRunTrigger),
    "ops_comp_estado": tuple(e.value for e in OpsCompEstado),
    "ops_inc_estado": tuple(e.value for e in OpsIncEstado),
}


def ops_enum(name: str) -> PGENUM:
    """Instancia del ENUM nativo de PostgreSQL (create_type=False pues ya existe en el esquema)."""
    return PGENUM(*_OPS_ENUMS[name], name=name, create_type=False)


# --- Columna nueva sobre la tabla existente companies (§4.2) ----------------
# Se agrega dinámicamente a Company sin modificar models.py
Company.industria = Column(
    ops_enum("company_industria"),
    nullable=False,
    server_default=text("'otras'"),
    default="otras",
)


# ============================================================================
# 2. Tablas nuevas de la Consola Interna (§4.3 - §4.6)
# ============================================================================

class OpsComponente(Base):
    """Componentes monitoreados del sistema (§4.3)."""
    __tablename__ = "ops_componentes"

    clave: Mapped[str] = mapped_column(Text, primary_key=True)
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    descripcion: Mapped[str | None] = mapped_column(Text, nullable=True)
    orden: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        server_default=text("0"),
        default=0,
    )
    activo: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
        default=True,
    )

    # Relaciones
    disponibilidades: Mapped[list[OpsDisponibilidadDiaria]] = relationship(
        "OpsDisponibilidadDiaria",
        back_populates="componente_rel",
        cascade="all, delete-orphan",
    )
    jobs: Mapped[list[OpsJob]] = relationship(
        "OpsJob",
        back_populates="componente_rel",
    )
    incidentes: Mapped[list[OpsIncidente]] = relationship(
        "OpsIncidente",
        back_populates="componente_rel",
    )


class OpsDisponibilidadDiaria(Base):
    """Consolidado diario de disponibilidad por componente (§4.3)."""
    __tablename__ = "ops_disponibilidad_diaria"

    componente: Mapped[str] = mapped_column(
        Text,
        ForeignKey("ops_componentes.clave", ondelete="CASCADE"),
        primary_key=True,
    )
    fecha: Mapped[date] = mapped_column(Date, primary_key=True)
    estado: Mapped[str] = mapped_column(
        ops_enum("ops_comp_estado"),
        nullable=False,
    )
    uptime_pct: Mapped[float] = mapped_column(
        Numeric(5, 2),
        nullable=False,
    )
    latencia_p95_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    checks_total: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        default=0,
    )
    checks_fallidos: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        default=0,
    )

    # Relaciones
    componente_rel: Mapped[OpsComponente] = relationship(
        "OpsComponente",
        back_populates="disponibilidades",
    )


class OpsJob(Base):
    """Catálogo de procesos críticos y jobs programados/continuos (§4.4)."""
    __tablename__ = "ops_jobs"

    clave: Mapped[str] = mapped_column(Text, primary_key=True)
    nombre: Mapped[str] = mapped_column(Text, nullable=False)
    tipo: Mapped[str] = mapped_column(
        ops_enum("ops_job_tipo"),
        nullable=False,
    )
    cron_expr: Mapped[str | None] = mapped_column(Text, nullable=True)
    componente: Mapped[str | None] = mapped_column(
        Text,
        ForeignKey("ops_componentes.clave"),
        nullable=True,
    )
    activo: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("true"),
        default=True,
    )
    pausado_por: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
    pausado_motivo: Mapped[str | None] = mapped_column(Text, nullable=True)
    timeout_s: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("3600"),
        default=3600,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        default=datetime.utcnow,
    )

    # Relaciones
    componente_rel: Mapped[OpsComponente | None] = relationship(
        "OpsComponente",
        back_populates="jobs",
    )
    pausado_por_user: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[pausado_por],
    )
    runs: Mapped[list[OpsJobRun]] = relationship(
        "OpsJobRun",
        back_populates="job",
        cascade="all, delete-orphan",
    )


class OpsJobRun(Base):
    """Historial de ejecuciones de cada proceso crítico (§4.4)."""
    __tablename__ = "ops_job_runs"

    id: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        primary_key=True,
    )
    job_clave: Mapped[str] = mapped_column(
        Text,
        ForeignKey("ops_jobs.clave", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        ops_enum("ops_run_status"),
        nullable=False,
        server_default=text("'queued'"),
        default="queued",
    )
    disparo: Mapped[str] = mapped_column(
        ops_enum("ops_run_trigger"),
        nullable=False,
        server_default=text("'cron'"),
        default="cron",
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
    celery_task_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    params: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    items_procesados: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mensaje: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_detalle: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        default=datetime.utcnow,
    )

    # Relaciones
    job: Mapped[OpsJob] = relationship(
        "OpsJob",
        back_populates="runs",
        foreign_keys=[job_clave],
    )
    actor: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[actor_user_id],
    )


class OpsIncidente(Base):
    """Registro y gestión de incidentes del sistema (§4.5)."""
    __tablename__ = "ops_incidentes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=_uuid,
    )
    componente: Mapped[str] = mapped_column(
        Text,
        ForeignKey("ops_componentes.clave"),
        nullable=False,
    )
    severidad: Mapped[str] = mapped_column(
        ops_enum("ops_comp_estado"),
        nullable=False,
    )
    estado: Mapped[str] = mapped_column(
        ops_enum("ops_inc_estado"),
        nullable=False,
        server_default=text("'abierto'"),
        default="abierto",
    )
    origen: Mapped[str] = mapped_column(Text, nullable=False)
    titulo: Mapped[str] = mapped_column(Text, nullable=False)
    descripcion: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolucion: Mapped[str | None] = mapped_column(Text, nullable=True)
    abierto_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        default=datetime.utcnow,
    )
    resuelto_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    creado_por: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id"),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        default=datetime.utcnow,
    )

    # Relaciones
    componente_rel: Mapped[OpsComponente] = relationship(
        "OpsComponente",
        back_populates="incidentes",
        foreign_keys=[componente],
    )
    creador: Mapped[User | None] = relationship(
        "User",
        foreign_keys=[creado_por],
    )


class MetricasMensuales(Base):
    """Snapshots consolidados mensuales de indicadores de negocio (§4.6)."""
    __tablename__ = "metricas_mensuales"

    mes: Mapped[date] = mapped_column(Date, primary_key=True)
    valor_uf: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    mrr_clp: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    mrr_nuevos_clp: Mapped[float] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        server_default=text("0"),
        default=0,
    )
    mrr_expansion_clp: Mapped[float] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        server_default=text("0"),
        default=0,
    )
    mrr_contraccion_clp: Mapped[float] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        server_default=text("0"),
        default=0,
    )
    mrr_perdido_clp: Mapped[float] = mapped_column(
        Numeric(14, 2),
        nullable=False,
        server_default=text("0"),
        default=0,
    )
    clientes_activos: Mapped[int] = mapped_column(Integer, nullable=False)
    altas: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        default=0,
    )
    bajas: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default=text("0"),
        default=0,
    )
    churn_clientes_pct: Mapped[float | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
    )
    churn_ingresos_pct: Mapped[float | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
    )
    nrr_pct: Mapped[float | None] = mapped_column(
        Numeric(6, 2),
        nullable=True,
    )
    usuarios_totales: Mapped[int] = mapped_column(Integer, nullable=False)
    usuarios_activos: Mapped[int] = mapped_column(Integer, nullable=False)
    cerrado: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=text("false"),
        default=False,
    )
    calculado_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("now()"),
        default=datetime.utcnow,
    )

    # Relaciones
    desglose_planes: Mapped[list[MetricasMensualesPlan]] = relationship(
        "MetricasMensualesPlan",
        back_populates="metrica_mensual",
        cascade="all, delete-orphan",
    )


class MetricasMensualesPlan(Base):
    """Desglose de indicadores mensuales por plan comercial (§4.6)."""
    __tablename__ = "metricas_mensuales_plan"

    mes: Mapped[date] = mapped_column(
        Date,
        ForeignKey("metricas_mensuales.mes", ondelete="CASCADE"),
        primary_key=True,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("planes.id"),
        primary_key=True,
    )
    clientes: Mapped[int] = mapped_column(Integer, nullable=False)
    usuarios: Mapped[int] = mapped_column(Integer, nullable=False)
    mrr_clp: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False)
    churn_clientes_pct: Mapped[float | None] = mapped_column(
        Numeric(5, 2),
        nullable=True,
    )
    nrr_pct: Mapped[float | None] = mapped_column(
        Numeric(6, 2),
        nullable=True,
    )

    # Relaciones
    metrica_mensual: Mapped[MetricasMensuales] = relationship(
        "MetricasMensuales",
        back_populates="desglose_planes",
    )
    plan: Mapped[Plan] = relationship(
        "Plan",
        foreign_keys=[plan_id],
    )