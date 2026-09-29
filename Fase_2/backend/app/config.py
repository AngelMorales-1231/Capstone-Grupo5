from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Configuración por variables de entorno (.env soportado).

    Los adaptadores de infraestructura (storage, cola, cifrado, IA) se eligen
    por variable para que `docker compose up` funcione sin Azure y el
    despliegue en Azure use identidad administrada sin secretos en config.
    """

    # --- Base de datos -----------------------------------------------------
    # En producción se conecta con el rol acredittia_app (NOBYPASSRLS) para que
    # las políticas de RLS del modelo de datos se apliquen de verdad.
    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/acredittia"
    schema_dir: str = "/schema"
    schema_version_esperada: int = 7        # scripts 01..04 + 06 aplicados
    db_apply_schema_on_start: bool = False  # en Azure lo hace el job de migración
    db_rls_enabled: bool = True             # fija app.company_id/is_admin/user_id por request

    # --- Autenticación -----------------------------------------------------
    jwt_secret: str = "dev-secret-cambiar-en-produccion"
    access_ttl_min: int = 30
    refresh_ttl_days: int = 30
    reset_ttl_min: int = 60
    cors_origins: str = "http://localhost:3000"

    # --- Almacenamiento: local | azure ------------------------------------
    storage_backend: str = "local"
    storage_dir: str = "/data/uploads"
    azure_storage_account: str = ""        # identidad administrada (preferido)
    azure_blob_conn: str = ""              # cadena de conexión (alternativa)
    azure_blob_container: str = "docs"
    sas_upload_ttl_min: int = 15
    sas_download_ttl_min: int = 15
    public_base_url: str = "http://localhost:8000"   # para URLs locales de subida

    # --- Cola de trabajos: celery | inproc --------------------------------
    queue_backend: str = "inproc"
    redis_url: str = "redis://localhost:6379/0"

    # --- Cifrado de credenciales de plataforma: keyvault | local ----------
    jwe_backend: str = "local"
    azure_keyvault_url: str = ""
    keyvault_key_name: str = "plataforma-cred"
    keys_dir: str = "/data/keys"           # solo jwe_backend=local
    credencial_ttl_meses: int = 12

    # --- Revisión IA: simulada | claude -----------------------------------
    ia_backend: str = "simulada"
    anthropic_api_key: str = ""

    # --- Notificaciones ----------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    email_from: str = "no-reply@acredittia.cl"
    whatsapp_token: str = ""
    whatsapp_phone_id: str = ""
    whatsapp_verify_token: str = ""

    # --- Pagos -------------------------------------------------------------
    pagos_webhook_secret: str = ""

    # --- Límites -----------------------------------------------------------
    max_upload_mb: int = 20
    page_size_default: int = 25
    page_size_max: int = 100
    matriz_filas_max: int = 200
    export_filas_max: int = 5000

    class Config:
        env_file = ".env"


settings = Settings()

# Extensiones permitidas por contexto (§10 de la especificación)
ALLOWED_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png"}
ALLOWED_EXTENSIONS_CONTRATO = ALLOWED_EXTENSIONS | {".doc", ".docx"}
ALLOWED_EXTENSIONS_ARRANQUE = ALLOWED_EXTENSIONS_CONTRATO | {".xlsx", ".xls", ".csv"}

TIPOS_EQUIPO = [
    "Tracto-Camión", "Camión", "Camión Pluma", "Camión Aljibe", "Cama Baja",
    "Semirremolque", "Rampla Plana", "Camioneta", "JEEP", "Bus", "MINIBUS",
    "Furgón", "Automóvil", "Alzahombre", "Grúa", "Grúa Horquilla",
    "Retroexcavadora", "Motoniveladora", "Equipo de levante", "Equipo de producción", "Otro",
]

# Umbrales de vencimiento y acreditación
UMBRAL_PORVENC_DIAS = 30
UMBRAL_ACREDITADO_PCT = 70

# Tipos de reporte y de exportación
REPORTE_TIPOS = (
    "estado_acreditacion", "cumplimiento_requisitos", "personal_acreditado",
    "equipos_vehiculos", "vencimientos", "matriz_cumplimiento",
)
EXPORT_RECURSOS = (
    "personal", "equipos", "personas", "flota", "documentos",
    "requisitos", "alertas", "contratos", "matriz",
)
