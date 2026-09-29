import uuid
from datetime import datetime, date

from sqlalchemy import (
    Text, Boolean, Date, DateTime, ForeignKey, BigInteger, Integer,
    Numeric, Identity, SmallInteger,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB, ARRAY, ENUM as PGENUM
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


_ENUM_VALUES = {
    "user_role": ("admin", "company", "contract_admin"),
    "account_status": ("pending", "approved", "rejected"),
    "subject_type": ("trabajador", "equipo"),
    "subject_status": ("ok", "falta", "venc", "proc", "baja"),
    "doc_estado": ("ok", "falta"),
    "doc_estado_calc": ("ok", "porvenc", "venc", "falta"),
    "lim_estado": ("vigente", "por_vencer", "pendiente"),
    "emsipor_estado": ("aprobado", "parcial", "pendiente"),
    "req_ambito": ("empresa", "personal", "equipo", "emsipor"),
    "req_tipo": ("legal", "medico", "capacitacion", "certificacion", "tecnico", "medioambiental"),
    "contrato_estado": ("vigente", "en_evaluacion", "terminado"),
    "ia_context": ("empresa", "personal", "equipo", "contrato", "emsipor",
                   "cedula", "padron", "carpeta_arranque"),
    # --- v1.1 ---
    "plataforma_estado": ("activa", "solicitada", "sin_acceso"),
    "vinculo_tipo": ("plataforma", "arranque", "otro"),
    "requisito_origen": ("base", "custom", "arranque"),
    "cargo_categoria": ("conduccion", "operacion", "supervision", "mantencion",
                        "administracion", "otro"),
    "credencial_estado": ("activa", "expirada", "revocada"),
    "evento_categoria": ("vencimiento", "mantencion", "capacitacion",
                         "administrativo", "entrega", "otro"),
    "reporte_tipo": ("estado_acreditacion", "cumplimiento_requisitos",
                     "personal_acreditado", "equipos_vehiculos", "vencimientos"),
    "reporte_formato": ("pdf", "excel"),
    "integracion_tipo": ("siga", "workmate", "metacontratas", "webcontrol",
                         "whatsapp", "gdrive"),
    "integracion_estado": ("activa", "con_error", "desconectada"),
    "sync_status": ("exito", "error"),
    "suscripcion_estado": ("trial", "activa", "morosa", "cancelada"),
    "factura_estado": ("pendiente", "pagada", "fallida", "anulada"),
    "terreno_nivel": ("critico", "importante", "informativo"),
    "ia_resultado": ("validado", "con_observaciones", "con_errores"),
    "hallazgo_tipo": ("error", "warning", "info"),
    "job_status": ("queued", "processing", "done", "failed"),
    "alerta_severidad": ("critica", "alta", "media", "baja", "advertencia", "informativa"),
    "alerta_estado": ("nueva", "en_progreso", "bloqueante", "informativa", "resuelta"),
    "alerta_origen": ("vencimiento", "ia", "integracion", "sistema"),
    "actividad_tipo": ("creacion", "actualizacion", "subida_documento", "asignacion",
                       "alerta_ia", "visualizacion", "comentario"),
}


def pg_enum(name: str) -> PGENUM:
    """Referencia a un ENUM ya creado por los scripts SQL (01_esquema.sql)."""
    return PGENUM(*_ENUM_VALUES[name], name=name, create_type=False)


class Base(DeclarativeBase):
    pass


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    nombre: Mapped[str] = mapped_column(Text)
    rut: Mapped[str] = mapped_column(Text, unique=True)
    email: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(pg_enum('account_status'), default="pending")
    approved_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    es_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(pg_enum('user_role'), default="company")
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id"))
    # Solo para role=contract_admin: acota el acceso a un único contrato.
    contrato_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contratos.id"))
    nombre: Mapped[str | None] = mapped_column(Text)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(pg_enum('account_status'), default="pending")
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    company: Mapped[Company | None] = relationship()


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Faena(Base):
    __tablename__ = "faenas"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    nombre: Mapped[str] = mapped_column(Text, unique=True)
    mandante: Mapped[str] = mapped_column(Text)
    grupo: Mapped[str | None] = mapped_column(Text)
    region: Mapped[str | None] = mapped_column(Text)
    sector: Mapped[str] = mapped_column(Text, default="mineria")
    activa: Mapped[bool] = mapped_column(Boolean, default=True)
    logo_url: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Numeric(9, 6))
    lng: Mapped[float | None] = mapped_column(Numeric(9, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class FaenaPlataforma(Base):
    __tablename__ = "faena_plataformas"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    faena_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("faenas.id"))
    nombre: Mapped[str] = mapped_column(Text)
    descripcion: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    nota: Mapped[str | None] = mapped_column(Text)
    orden: Mapped[int] = mapped_column(SmallInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class CompanyFaenaPlataforma(Base):
    """Estado de acceso de una empresa a una plataforma del mandante."""
    __tablename__ = "company_faena_plataformas"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    faena_plataforma_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("faena_plataformas.id"))
    estado: Mapped[str] = mapped_column(pg_enum('plataforma_estado'), default="sin_acceso")
    nota: Mapped[str | None] = mapped_column(Text)
    solicitado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    habilitado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class RequisitoTerreno(Base):
    """Requisitos no documentales de solo lectura (Reglamento de Tránsito MLP)."""
    __tablename__ = "requisitos_terreno"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    ambito: Mapped[str] = mapped_column(Text)          # conductor | equipo
    titulo: Mapped[str] = mapped_column(Text)
    descripcion: Mapped[str | None] = mapped_column(Text)
    nivel: Mapped[str] = mapped_column(pg_enum('terreno_nivel'))
    icono: Mapped[str | None] = mapped_column(Text)
    referencia: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ProveedorCatalogo(Base):
    """Laboratorios médicos, talleres homologados y proveedores GPS."""
    __tablename__ = "proveedores_catalogo"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    categoria: Mapped[str] = mapped_column(Text)       # laboratorio | taller | gps
    nombre: Mapped[str] = mapped_column(Text)
    localidad: Mapped[str | None] = mapped_column(Text)
    certificacion: Mapped[str | None] = mapped_column(Text)
    faena_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("faenas.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class DocEjemplo(Base):
    __tablename__ = "doc_ejemplos"
    clave: Mapped[str] = mapped_column(Text, primary_key=True)
    nombre: Mapped[str] = mapped_column(Text)
    referencia: Mapped[str | None] = mapped_column(Text)
    campos_clave: Mapped[list] = mapped_column(JSONB, default=list)
    notas: Mapped[list] = mapped_column(JSONB, default=list)
    tip: Mapped[str | None] = mapped_column(Text)
    pdf_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class RequisitoTemplate(Base):
    __tablename__ = "requisito_templates"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    ambito: Mapped[str] = mapped_column(pg_enum('req_ambito'))
    titulo: Mapped[str] = mapped_column(Text)
    codigo: Mapped[str | None] = mapped_column(Text, unique=True)
    tipo: Mapped[str | None] = mapped_column(pg_enum('req_tipo'))
    obligatorio: Mapped[bool] = mapped_column(Boolean, default=True)
    ejemplo_clave: Mapped[str | None] = mapped_column(ForeignKey("doc_ejemplos.clave"))
    faena_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("faenas.id"))
    vigencia_meses: Mapped[int | None] = mapped_column(Integer)
    plataforma: Mapped[str | None] = mapped_column(Text)
    aplica_a: Mapped[str | None] = mapped_column(Text)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Contrato(Base):
    __tablename__ = "contratos"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    faena_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("faenas.id"))
    nombre: Mapped[str] = mapped_column(Text)
    codigo: Mapped[str | None] = mapped_column(Text)
    fecha_inicio: Mapped[date | None] = mapped_column(Date)
    fecha_termino: Mapped[date | None] = mapped_column(Date)
    renovacion_automatica: Mapped[bool] = mapped_column(Boolean, default=False)
    estado: Mapped[str] = mapped_column(pg_enum('contrato_estado'), default="vigente")
    origen_ia_review_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    faena: Mapped[Faena] = relationship()


class Sujeto(Base):
    __tablename__ = "sujetos"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    contrato_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contratos.id"))
    tipo: Mapped[str] = mapped_column(pg_enum('subject_type'))
    estado: Mapped[str] = mapped_column(pg_enum('subject_status'), default="proc")
    nombre: Mapped[str] = mapped_column(Text)
    rut: Mapped[str | None] = mapped_column(Text)
    cargo: Mapped[str | None] = mapped_column(Text)          # derivado de cargos.nombre
    cargo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cargos.id"))
    es_conductor: Mapped[bool] = mapped_column(Boolean, default=False)
    patente: Mapped[str | None] = mapped_column(Text)
    tipo_equipo: Mapped[str | None] = mapped_column(Text)
    marca: Mapped[str | None] = mapped_column(Text)
    modelo: Mapped[str | None] = mapped_column(Text)
    anio: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    contrato: Mapped[Contrato] = relationship()
    # passive_deletes: la FK es ON DELETE CASCADE en la BD. Sin esto, al borrar
    # el sujeto la unidad de trabajo carga los documentos y les pone
    # sujeto_id=NULL antes del DELETE, lo que viola `ck_documentos_dueno` y el
    # trigger `fn_check_doc_tenant` («no coincide con el dueño (NULL)») y deja el
    # borrado imposible. Se delega la cascada al motor.
    documentos: Mapped[list["Documento"]] = relationship(
        back_populates="sujeto", passive_deletes=True)


class Documento(Base):
    __tablename__ = "documentos"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("requisito_templates.id"))
    sujeto_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sujetos.id"))
    contrato_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contratos.id"))
    titulo: Mapped[str] = mapped_column(Text)
    obligatorio: Mapped[bool] = mapped_column(Boolean, default=True)
    estado: Mapped[str] = mapped_column(pg_enum('doc_estado'), default="falta")
    vence: Mapped[date | None] = mapped_column(Date)
    estado_calc: Mapped[str] = mapped_column(pg_enum('doc_estado_calc'), default="falta")
    es_emsipor: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    sujeto: Mapped[Sujeto | None] = relationship(back_populates="documentos")
    template: Mapped[RequisitoTemplate | None] = relationship()
    # ON DELETE CASCADE en la BD: ver la nota de `Sujeto.documentos`.
    archivos: Mapped[list["DocumentoArchivo"]] = relationship(
        back_populates="documento", passive_deletes=True)


class DocumentoArchivo(Base):
    __tablename__ = "documento_archivos"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    documento_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("documentos.id"))
    filename: Mapped[str] = mapped_column(Text)
    blob_path: Mapped[str] = mapped_column(Text, unique=True)
    content_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    ia_review_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    documento: Mapped[Documento] = relationship(back_populates="archivos")


class LicenciaInterna(Base):
    __tablename__ = "licencias_internas"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    sujeto_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sujetos.id"), unique=True)
    numero: Mapped[str | None] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(pg_enum('lim_estado'), default="pendiente")
    vence: Mapped[date | None] = mapped_column(Date)
    emsipor_estado: Mapped[str] = mapped_column(pg_enum('emsipor_estado'), default="pendiente")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class IaReview(Base):
    __tablename__ = "ia_reviews"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    archivo_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    context: Mapped[str] = mapped_column(pg_enum('ia_context'))
    status: Mapped[str] = mapped_column(pg_enum('job_status'), default="queued")
    resultado: Mapped[str | None] = mapped_column(pg_enum('ia_resultado'))
    confianza: Mapped[float | None] = mapped_column(Numeric(4, 3))
    campos_extraidos: Mapped[dict] = mapped_column(JSONB, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    hallazgos: Mapped[list["IaHallazgo"]] = relationship(
        back_populates="review", passive_deletes=True)


class IaHallazgo(Base):
    __tablename__ = "ia_hallazgos"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    review_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ia_reviews.id"))
    tipo: Mapped[str] = mapped_column(pg_enum('hallazgo_tipo'))
    codigo: Mapped[str] = mapped_column(Text)
    mensaje: Mapped[str] = mapped_column(Text)
    campo: Mapped[str | None] = mapped_column(Text)
    valor_detectado: Mapped[str | None] = mapped_column(Text)
    valor_esperado: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    review: Mapped[IaReview] = relationship(back_populates="hallazgos")


class Alerta(Base):
    __tablename__ = "alertas"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    severidad: Mapped[str] = mapped_column(pg_enum('alerta_severidad'))
    estado: Mapped[str] = mapped_column(pg_enum('alerta_estado'), default="nueva")
    origen: Mapped[str] = mapped_column(pg_enum('alerta_origen'))
    titulo: Mapped[str] = mapped_column(Text)
    descripcion: Mapped[str | None] = mapped_column(Text)
    plataforma: Mapped[str | None] = mapped_column(Text)
    documento_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documentos.id"))
    sujeto_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sujetos.id"))
    contrato_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contratos.id"))
    leida_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resuelta_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Actividad(Base):
    __tablename__ = "actividad"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    tipo: Mapped[str] = mapped_column(pg_enum('actividad_tipo'))
    modulo: Mapped[str] = mapped_column(Text)
    descripcion: Mapped[str] = mapped_column(Text)
    entidad_tipo: Mapped[str | None] = mapped_column(Text)
    entidad_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    plataforma: Mapped[str | None] = mapped_column(Text)
    meta: Mapped[dict] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


# ============================================================================
# v1.1 — Catálogo de cargos
# ============================================================================
class Cargo(Base):
    """company_id NULL = catálogo base de Acredittia (solo admin lo edita)."""
    __tablename__ = "cargos"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("companies.id"))
    nombre: Mapped[str] = mapped_column(Text)
    nombre_normalizado: Mapped[str] = mapped_column(Text)
    categoria: Mapped[str] = mapped_column(pg_enum('cargo_categoria'), default="otro")
    requiere_emsipor: Mapped[bool] = mapped_column(Boolean, default=False)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    requisitos: Mapped[list["CargoRequisito"]] = relationship(
        back_populates="cargo", passive_deletes=True)


class CargoRequisito(Base):
    __tablename__ = "cargo_requisitos"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    cargo_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cargos.id"))
    requisito_template_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("requisito_templates.id"))
    obligatorio: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    cargo: Mapped[Cargo] = relationship(back_populates="requisitos")
    template: Mapped[RequisitoTemplate] = relationship()


# ============================================================================
# v1.1 — Plataformas por contrato y vault de credenciales
# ============================================================================
class ContratoPlataforma(Base):
    """Plataformas efectivas del contrato. Si existen filas, reemplazan la
    lista heredada de faena_plataformas para ese contrato."""
    __tablename__ = "contrato_plataformas"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    contrato_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contratos.id"))
    faena_plataforma_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("faena_plataformas.id"))
    nombre: Mapped[str] = mapped_column(Text)
    descripcion: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str | None] = mapped_column(Text)
    color: Mapped[str | None] = mapped_column(Text)
    estado: Mapped[str] = mapped_column(pg_enum('plataforma_estado'), default="sin_acceso")
    nota: Mapped[str | None] = mapped_column(Text)
    orden: Mapped[int] = mapped_column(SmallInteger, default=0)
    es_custom: Mapped[bool] = mapped_column(Boolean, default=True)
    solicitado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    habilitado_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ultima_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class PlataformaCredencial(Base):
    """Cuenta de acceso a una plataforma del mandante.

    ATENCIÓN: `credencial_jwe` no debe leerse desde la API. El rol
    acredittia_app tiene el SELECT revocado sobre esa columna (§7.3 del modelo
    de datos); solo el worker de integración puede leerla. Para listar usa
    `defer(PlataformaCredencial.credencial_jwe)` o la vista
    v_plataforma_credenciales.
    """
    __tablename__ = "plataforma_credenciales"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    contrato_plataforma_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("contrato_plataformas.id"))
    nombre: Mapped[str] = mapped_column(Text)
    usuario: Mapped[str] = mapped_column(Text)
    credencial_jwe: Mapped[str] = mapped_column(Text, deferred=True)
    kid: Mapped[str] = mapped_column(Text, deferred=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    estado: Mapped[str] = mapped_column(pg_enum('credencial_estado'), default="activa")
    expira_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rotada_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class PlataformaCredencialVersion(Base):
    """Historial append-only de rotaciones. Lo escribe un trigger en la BD."""
    __tablename__ = "plataforma_credencial_versiones"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    credencial_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("plataforma_credenciales.id"))
    version: Mapped[int] = mapped_column(Integer)
    credencial_jwe: Mapped[str] = mapped_column(Text, deferred=True)
    kid: Mapped[str] = mapped_column(Text)
    vigente_desde: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    vigente_hasta: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    rotada_por: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


# ============================================================================
# v1.1 — Requisitos por contrato
# ============================================================================
class ContratoRequisito(Base):
    __tablename__ = "contrato_requisitos"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    contrato_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contratos.id"))
    vinculo_tipo: Mapped[str] = mapped_column(pg_enum('vinculo_tipo'), default="otro")
    vinculo_ref: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contrato_plataformas.id"))
    ambito: Mapped[str] = mapped_column(pg_enum('req_ambito'))
    titulo: Mapped[str] = mapped_column(Text)
    obligatorio: Mapped[bool] = mapped_column(Boolean, default=True)
    cargo_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cargos.id"))
    origen: Mapped[str] = mapped_column(pg_enum('requisito_origen'), default="custom")
    requisito_template_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("requisito_templates.id"))
    ejemplo_clave: Mapped[str | None] = mapped_column(ForeignKey("doc_ejemplos.clave"))
    vigencia_meses: Mapped[int | None] = mapped_column(SmallInteger)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ContratoPlantillaOverride(Base):
    __tablename__ = "contrato_plantilla_overrides"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    contrato_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contratos.id"))
    ambito: Mapped[str] = mapped_column(pg_enum('req_ambito'))
    requisito_template_ids: Mapped[list] = mapped_column(
        ARRAY(UUID(as_uuid=True)), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


# ============================================================================
# v1.1 — Serie histórica de cumplimiento
# ============================================================================
class CumplimientoSnapshot(Base):
    """contrato_id NULL = fila agregada de la empresa. Inmutable (trigger)."""
    __tablename__ = "cumplimiento_snapshots"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    contrato_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("contratos.id"))
    fecha: Mapped[date] = mapped_column(Date)
    cumplimiento_pct: Mapped[int] = mapped_column(SmallInteger)
    docs_ok: Mapped[int] = mapped_column(Integer, default=0)
    docs_total: Mapped[int] = mapped_column(Integer, default=0)
    personal_acreditados: Mapped[int] = mapped_column(Integer, default=0)
    personal_total: Mapped[int] = mapped_column(Integer, default=0)
    equipos_acreditados: Mapped[int] = mapped_column(Integer, default=0)
    equipos_total: Mapped[int] = mapped_column(Integer, default=0)
    alertas_criticas: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


# ============================================================================
# Operación: calendario, reportes, integraciones, notificaciones
# ============================================================================
class EventoCalendario(Base):
    __tablename__ = "eventos_calendario"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    titulo: Mapped[str] = mapped_column(Text)
    categoria: Mapped[str] = mapped_column(pg_enum('evento_categoria'), default="otro")
    fecha: Mapped[date] = mapped_column(Date)
    descripcion: Mapped[str | None] = mapped_column(Text)
    documento_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documentos.id"))
    completado: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Reporte(Base):
    __tablename__ = "reportes"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    nombre: Mapped[str] = mapped_column(Text)
    tipo: Mapped[str] = mapped_column(pg_enum('reporte_tipo'))
    formato: Mapped[str] = mapped_column(pg_enum('reporte_formato'))
    status: Mapped[str] = mapped_column(pg_enum('job_status'), default="queued")
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    blob_path: Mapped[str | None] = mapped_column(Text)
    generado_por: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class ReporteProgramado(Base):
    __tablename__ = "reportes_programados"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    nombre: Mapped[str] = mapped_column(Text)
    tipo: Mapped[str] = mapped_column(pg_enum('reporte_tipo'))
    formato: Mapped[str] = mapped_column(pg_enum('reporte_formato'))
    params: Mapped[dict] = mapped_column(JSONB, default=dict)
    cron_expr: Mapped[str] = mapped_column(Text)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    ultimo_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Integracion(Base):
    __tablename__ = "integraciones"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    tipo: Mapped[str] = mapped_column(pg_enum('integracion_tipo'))
    estado: Mapped[str] = mapped_column(pg_enum('integracion_estado'), default="desconectada")
    credenciales_ref: Mapped[str | None] = mapped_column(Text)   # referencia a Key Vault
    config: Mapped[dict] = mapped_column(JSONB, default=dict)
    ultima_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class SyncLog(Base):
    __tablename__ = "sync_logs"
    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    integracion_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("integraciones.id"))
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    status: Mapped[str] = mapped_column(pg_enum('sync_status'))
    mensaje: Mapped[str | None] = mapped_column(Text)
    registros_procesados: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class NotificacionPreferencia(Base):
    __tablename__ = "notificacion_preferencias"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    evento: Mapped[str] = mapped_column(Text)
    canal_email: Mapped[bool] = mapped_column(Boolean, default=True)
    canal_whatsapp: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


# ============================================================================
# Comercial
# ============================================================================
class Plan(Base):
    __tablename__ = "planes"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    nombre: Mapped[str] = mapped_column(Text, unique=True)
    precio: Mapped[float] = mapped_column(Numeric(12, 2))
    moneda: Mapped[str] = mapped_column(Text, default="UF")
    periodo: Mapped[str] = mapped_column(Text, default="mensual")
    limites: Mapped[dict] = mapped_column(JSONB, default=dict)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)


class Suscripcion(Base):
    __tablename__ = "suscripciones"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"), unique=True)
    plan_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("planes.id"))
    estado: Mapped[str] = mapped_column(pg_enum('suscripcion_estado'), default="trial")
    trial_hasta: Mapped[date | None] = mapped_column(Date)
    periodo_actual_desde: Mapped[date | None] = mapped_column(Date)
    periodo_actual_hasta: Mapped[date | None] = mapped_column(Date)
    medio_pago_ref: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    plan: Mapped[Plan] = relationship()


class Factura(Base):
    __tablename__ = "facturas"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    suscripcion_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("suscripciones.id"))
    company_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("companies.id"))
    folio: Mapped[str | None] = mapped_column(Text, unique=True)
    monto: Mapped[float] = mapped_column(Numeric(12, 2))
    moneda: Mapped[str] = mapped_column(Text, default="CLP")
    estado: Mapped[str] = mapped_column(pg_enum('factura_estado'), default="pendiente")
    gateway_ref: Mapped[str | None] = mapped_column(Text)
    emitida_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    pagada_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_blob_path: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
