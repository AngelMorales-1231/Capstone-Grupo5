-- ============================================================================
-- ACREDITTIA — Modelo de datos v1.0
-- 01_esquema.sql — Extensiones, tipos enumerados y tablas
-- Motor: PostgreSQL 16 (Azure Database for PostgreSQL Flexible Server)
-- Orden de ejecución: 01 → 02 → 03 → 04 → 05
-- Nota Azure: habilitar previamente las extensiones en el parámetro de
-- servidor azure.extensions = CITEXT,PG_TRGM  (portal o CLI).
-- ============================================================================

-- En Docker/on-premise: CREATE DATABASE acredittia ENCODING 'UTF8'
--   LC_COLLATE 'es-CL-x-icu' LC_CTYPE 'es-CL-x-icu' TEMPLATE template0;
-- En Azure la BD se crea desde el portal/CLI; ejecutar este script conectado a ella.

SET client_encoding = 'UTF8';
SET timezone = 'America/Santiago';

CREATE EXTENSION IF NOT EXISTS citext;    -- emails case-insensitive
CREATE EXTENSION IF NOT EXISTS pg_trgm;   -- búsqueda por similitud (nombres, RUT, patentes)

-- ============================================================================
-- 1. TIPOS ENUMERADOS
-- ============================================================================
CREATE TYPE user_role          AS ENUM ('admin','company');
CREATE TYPE account_status     AS ENUM ('pending','approved','rejected');
CREATE TYPE subject_type       AS ENUM ('trabajador','equipo');
CREATE TYPE subject_status     AS ENUM ('ok','falta','venc','proc','baja');
CREATE TYPE doc_estado         AS ENUM ('ok','falta');
CREATE TYPE doc_estado_calc    AS ENUM ('ok','porvenc','venc','falta');
CREATE TYPE lim_estado         AS ENUM ('vigente','por_vencer','pendiente');
CREATE TYPE emsipor_estado     AS ENUM ('aprobado','parcial','pendiente');
CREATE TYPE req_ambito         AS ENUM ('empresa','personal','equipo','emsipor');
CREATE TYPE req_tipo           AS ENUM ('legal','medico','capacitacion','certificacion','tecnico','medioambiental');
CREATE TYPE terreno_nivel      AS ENUM ('critico','importante','informativo');
CREATE TYPE alerta_severidad   AS ENUM ('critica','alta','media','baja','advertencia','informativa');
CREATE TYPE alerta_estado      AS ENUM ('nueva','en_progreso','bloqueante','informativa','resuelta');
CREATE TYPE alerta_origen      AS ENUM ('vencimiento','ia','integracion','sistema');
CREATE TYPE ia_context         AS ENUM ('empresa','personal','equipo','contrato','emsipor');
CREATE TYPE ia_resultado       AS ENUM ('validado','con_observaciones','con_errores');
CREATE TYPE hallazgo_tipo      AS ENUM ('error','warning','info');
CREATE TYPE job_status         AS ENUM ('queued','processing','done','failed');
CREATE TYPE contrato_estado    AS ENUM ('vigente','en_evaluacion','terminado');
CREATE TYPE plataforma_estado  AS ENUM ('activa','solicitada','sin_acceso');
CREATE TYPE integracion_tipo   AS ENUM ('siga','workmate','metacontratas','webcontrol','whatsapp','gdrive');
CREATE TYPE integracion_estado AS ENUM ('activa','con_error','desconectada');
CREATE TYPE sync_status        AS ENUM ('exito','error');
CREATE TYPE reporte_tipo       AS ENUM ('estado_acreditacion','cumplimiento_requisitos','personal_acreditado','equipos_vehiculos','vencimientos');
CREATE TYPE reporte_formato    AS ENUM ('pdf','excel');
CREATE TYPE evento_categoria   AS ENUM ('vencimiento','mantencion','capacitacion','administrativo','entrega','otro');
CREATE TYPE actividad_tipo     AS ENUM ('creacion','actualizacion','subida_documento','asignacion','alerta_ia','visualizacion','comentario');
CREATE TYPE suscripcion_estado AS ENUM ('trial','activa','morosa','cancelada');
CREATE TYPE factura_estado     AS ENUM ('pendiente','pagada','fallida','anulada');

-- Dominio para RUT chileno con puntos y dígito verificador (formato 76.543.210-9)
CREATE DOMAIN rut_chileno AS text
  CHECK (VALUE ~ '^\d{1,2}\.\d{3}\.\d{3}-[\dkK]$');

-- ============================================================================
-- 2. CUENTAS Y AUTENTICACIÓN
-- ============================================================================
CREATE TABLE companies (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nombre           text NOT NULL,
  rut              rut_chileno NOT NULL UNIQUE,
  email            citext NOT NULL,
  status           account_status NOT NULL DEFAULT 'pending',
  approved_by      uuid,                    -- FK a users (se agrega abajo, dependencia circular)
  approved_at      timestamptz,
  rejection_reason text,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_companies_rechazo CHECK (status <> 'rejected' OR rejection_reason IS NOT NULL)
);

CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email         citext NOT NULL UNIQUE,
  password_hash text NOT NULL,
  role          user_role NOT NULL DEFAULT 'company',
  company_id    uuid REFERENCES companies(id) ON DELETE CASCADE,
  status        account_status NOT NULL DEFAULT 'pending',
  last_login_at timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_users_company CHECK ( (role = 'admin') = (company_id IS NULL) )
);

ALTER TABLE companies
  ADD CONSTRAINT fk_companies_approved_by FOREIGN KEY (approved_by) REFERENCES users(id) ON DELETE SET NULL;

CREATE TABLE refresh_tokens (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  revoked_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE password_reset_tokens (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id    uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash text NOT NULL UNIQUE,
  expires_at timestamptz NOT NULL,
  used_at    timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- ============================================================================
-- 3. CATÁLOGOS GLOBALES (sin company_id: administrados por Acredittia)
-- ============================================================================
CREATE TABLE faenas (
  id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nombre    text NOT NULL UNIQUE,
  mandante  text NOT NULL,
  grupo     text,                      -- AMSA | Lundin | Codelco | ...
  region    text,
  sector    text NOT NULL DEFAULT 'mineria',   -- mineria | eolica | solar | ...
  activa    boolean NOT NULL DEFAULT true,     -- false = "próximamente"
  logo_url  text,
  color     text,
  lat       numeric(9,6),
  lng       numeric(9,6),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE faena_plataformas (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  faena_id    uuid NOT NULL REFERENCES faenas(id) ON DELETE CASCADE,
  nombre      text NOT NULL,           -- SIGA, DIRECTIC, SGES, EMSIPOR, WEBCONTROL...
  descripcion text,
  url         text,
  nota        text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (faena_id, nombre)
);

CREATE TABLE doc_ejemplos (
  clave       text PRIMARY KEY,        -- contrato, examen, altura, induccion, soap...
  nombre      text NOT NULL,
  referencia  text,                    -- norma o reglamento de origen
  campos_clave jsonb NOT NULL DEFAULT '[]'::jsonb,  -- [["Empleador","..."],...]
  notas       jsonb NOT NULL DEFAULT '[]'::jsonb,
  tip         text,
  pdf_url     text,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE requisito_templates (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ambito         req_ambito NOT NULL,
  titulo         text NOT NULL,
  codigo         text UNIQUE,          -- REQ-001...
  tipo           req_tipo,
  obligatorio    boolean NOT NULL DEFAULT true,
  ejemplo_clave  text REFERENCES doc_ejemplos(clave) ON DELETE SET NULL,
  faena_id       uuid REFERENCES faenas(id) ON DELETE CASCADE,  -- NULL = estándar general
  vigencia_meses smallint CHECK (vigencia_meses IS NULL OR vigencia_meses BETWEEN 1 AND 120),
  plataforma     text,                 -- EMSIPOR: SIGA | DIRECTIC | Academia MLP | EMSIPOR
  aplica_a       text,                 -- condición, ej: 'camioneta'
  activo         boolean NOT NULL DEFAULT true,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE requisitos_terreno (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  ambito      text NOT NULL CHECK (ambito IN ('conductor','equipo')),
  titulo      text NOT NULL,
  descripcion text,
  nivel       terreno_nivel NOT NULL,
  icono       text,
  referencia  text,                    -- Reglamento de Tránsito MLP v.16
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE proveedores_catalogo (    -- laboratorios, talleres y proveedores GPS
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  categoria     text NOT NULL CHECK (categoria IN ('laboratorio','taller','gps')),
  nombre        text NOT NULL,
  localidad     text,
  certificacion text,
  faena_id      uuid REFERENCES faenas(id) ON DELETE CASCADE,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (categoria, nombre, faena_id)
);

CREATE TABLE planes (
  id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nombre     text NOT NULL UNIQUE,
  precio     numeric(12,2) NOT NULL CHECK (precio >= 0),
  moneda     text NOT NULL DEFAULT 'UF',
  periodo    text NOT NULL DEFAULT 'mensual',
  limites    jsonb NOT NULL DEFAULT '{}'::jsonb,
  activo     boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- ============================================================================
-- 4. NEGOCIO POR EMPRESA (multi-tenant: todas llevan company_id)
-- ============================================================================
CREATE TABLE company_faena_plataformas (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id          uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  faena_plataforma_id uuid NOT NULL REFERENCES faena_plataformas(id) ON DELETE CASCADE,
  estado              plataforma_estado NOT NULL DEFAULT 'sin_acceso',
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, faena_plataforma_id)
);

CREATE TABLE contratos (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id            uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  faena_id              uuid NOT NULL REFERENCES faenas(id) ON DELETE RESTRICT,
  nombre                text NOT NULL,
  codigo                text,
  fecha_inicio          date,
  fecha_termino         date,
  renovacion_automatica boolean NOT NULL DEFAULT false,
  estado                contrato_estado NOT NULL DEFAULT 'vigente',
  origen_ia_review_id   uuid,          -- FK a ia_reviews (se agrega abajo)
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_contratos_fechas CHECK (fecha_termino IS NULL OR fecha_inicio IS NULL OR fecha_termino >= fecha_inicio),
  CONSTRAINT ux_contratos_codigo UNIQUE (company_id, codigo)
);

CREATE TABLE sujetos (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  contrato_id  uuid NOT NULL REFERENCES contratos(id) ON DELETE CASCADE,
  tipo         subject_type NOT NULL,
  estado       subject_status NOT NULL DEFAULT 'proc',
  nombre       text NOT NULL,
  -- Trabajador
  rut          rut_chileno,
  cargo        text,
  es_conductor boolean NOT NULL DEFAULT false,
  -- Equipo
  patente      text,
  tipo_equipo  text,
  marca        text,
  modelo       text,
  anio         smallint CHECK (anio IS NULL OR anio BETWEEN 1990 AND 2035),
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  -- Coherencia por tipo: un trabajador tiene RUT y no patente; un equipo al revés
  CONSTRAINT ck_sujetos_trabajador CHECK (tipo <> 'trabajador' OR (rut IS NOT NULL AND patente IS NULL AND tipo_equipo IS NULL)),
  CONSTRAINT ck_sujetos_equipo     CHECK (tipo <> 'equipo'     OR (patente IS NOT NULL AND tipo_equipo IS NOT NULL AND rut IS NULL AND es_conductor = false))
);

CREATE TABLE documentos (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  template_id  uuid REFERENCES requisito_templates(id) ON DELETE SET NULL,
  sujeto_id    uuid REFERENCES sujetos(id) ON DELETE CASCADE,
  contrato_id  uuid REFERENCES contratos(id) ON DELETE CASCADE,  -- docs de empresa
  titulo       text NOT NULL,
  obligatorio  boolean NOT NULL DEFAULT true,
  estado       doc_estado NOT NULL DEFAULT 'falta',
  vence        date,
  estado_calc  doc_estado_calc NOT NULL DEFAULT 'falta',
  es_emsipor   boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  -- Exactamente un dueño: sujeto XOR contrato
  CONSTRAINT ck_documentos_dueno CHECK ( (sujeto_id IS NOT NULL)::int + (contrato_id IS NOT NULL)::int = 1 )
);

CREATE TABLE documento_archivos (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  documento_id uuid NOT NULL REFERENCES documentos(id) ON DELETE CASCADE,
  filename     text NOT NULL,
  blob_path    text NOT NULL UNIQUE,
  content_type text,
  size_bytes   bigint CHECK (size_bytes IS NULL OR size_bytes BETWEEN 0 AND 20*1024*1024),
  uploaded_by  uuid REFERENCES users(id) ON DELETE SET NULL,
  ia_review_id uuid,                   -- FK a ia_reviews (se agrega abajo)
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE licencias_internas (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id     uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  sujeto_id      uuid NOT NULL UNIQUE REFERENCES sujetos(id) ON DELETE CASCADE,
  numero         text,                 -- LIM-2024-0341
  estado         lim_estado NOT NULL DEFAULT 'pendiente',
  vence          date,
  emsipor_estado emsipor_estado NOT NULL DEFAULT 'pendiente',
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_lim_numero CHECK (estado = 'pendiente' OR numero IS NOT NULL)
);

CREATE TABLE ia_reviews (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id       uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  archivo_id       uuid REFERENCES documento_archivos(id) ON DELETE SET NULL,
  context          ia_context NOT NULL,
  status           job_status NOT NULL DEFAULT 'queued',
  resultado        ia_resultado,
  confianza        numeric(4,3) CHECK (confianza IS NULL OR confianza BETWEEN 0 AND 1),
  campos_extraidos jsonb NOT NULL DEFAULT '{}'::jsonb,
  error            text,
  started_at       timestamptz,
  finished_at      timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_ia_done CHECK (status <> 'done' OR resultado IS NOT NULL)
);

ALTER TABLE contratos
  ADD CONSTRAINT fk_contratos_ia FOREIGN KEY (origen_ia_review_id) REFERENCES ia_reviews(id) ON DELETE SET NULL;
ALTER TABLE documento_archivos
  ADD CONSTRAINT fk_archivos_ia FOREIGN KEY (ia_review_id) REFERENCES ia_reviews(id) ON DELETE SET NULL;

CREATE TABLE ia_hallazgos (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  review_id       uuid NOT NULL REFERENCES ia_reviews(id) ON DELETE CASCADE,
  tipo            hallazgo_tipo NOT NULL,
  codigo          text NOT NULL,       -- DOC_ILEGIBLE, RUT_NO_COINCIDE, EMISION_ANTIGUA...
  mensaje         text NOT NULL,
  campo           text,
  valor_detectado text,
  valor_esperado  text,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE alertas (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  severidad    alerta_severidad NOT NULL,
  estado       alerta_estado NOT NULL DEFAULT 'nueva',
  origen       alerta_origen NOT NULL,
  titulo       text NOT NULL,
  descripcion  text,
  plataforma   text,
  documento_id uuid REFERENCES documentos(id) ON DELETE CASCADE,
  sujeto_id    uuid REFERENCES sujetos(id) ON DELETE CASCADE,
  contrato_id  uuid REFERENCES contratos(id) ON DELETE CASCADE,
  leida_at     timestamptz,
  resuelta_at  timestamptz,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE actividad (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- append-only, secuencial
  company_id   uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  user_id      uuid REFERENCES users(id) ON DELETE SET NULL,     -- NULL = Sistema IA
  tipo         actividad_tipo NOT NULL,
  modulo       text NOT NULL,
  descripcion  text NOT NULL,
  entidad_tipo text,
  entidad_id   uuid,
  plataforma   text,
  metadata     jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE eventos_calendario (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  titulo       text NOT NULL,
  categoria    evento_categoria NOT NULL DEFAULT 'otro',
  fecha        date NOT NULL,
  descripcion  text,
  documento_id uuid REFERENCES documentos(id) ON DELETE CASCADE,
  completado   boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reportes (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id   uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  nombre       text NOT NULL,
  tipo         reporte_tipo NOT NULL,
  formato      reporte_formato NOT NULL,
  status       job_status NOT NULL DEFAULT 'queued',
  params       jsonb NOT NULL DEFAULT '{}'::jsonb,
  blob_path    text,
  generado_por uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reportes_programados (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id    uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  nombre        text NOT NULL,
  tipo          reporte_tipo NOT NULL,
  formato       reporte_formato NOT NULL,
  params        jsonb NOT NULL DEFAULT '{}'::jsonb,
  cron_expr     text NOT NULL,
  activo        boolean NOT NULL DEFAULT true,
  ultimo_run_at timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE integraciones (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id       uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  tipo             integracion_tipo NOT NULL,
  estado           integracion_estado NOT NULL DEFAULT 'desconectada',
  credenciales_ref text,               -- referencia a Key Vault; NUNCA credenciales en claro
  config           jsonb NOT NULL DEFAULT '{}'::jsonb,
  ultima_sync_at   timestamptz,
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, tipo)
);

CREATE TABLE sync_logs (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  integracion_id       uuid NOT NULL REFERENCES integraciones(id) ON DELETE CASCADE,
  company_id           uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  status               sync_status NOT NULL,
  mensaje              text,
  registros_procesados integer NOT NULL DEFAULT 0,
  started_at           timestamptz NOT NULL DEFAULT now(),
  finished_at          timestamptz
);

CREATE TABLE suscripciones (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id           uuid NOT NULL UNIQUE REFERENCES companies(id) ON DELETE CASCADE,
  plan_id              uuid NOT NULL REFERENCES planes(id) ON DELETE RESTRICT,
  estado               suscripcion_estado NOT NULL DEFAULT 'trial',
  trial_hasta          date,
  periodo_actual_desde date,
  periodo_actual_hasta date,
  medio_pago_ref       text,           -- token pasarela (Transbank/Stripe)
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_susc_periodo CHECK (periodo_actual_hasta IS NULL OR periodo_actual_desde IS NULL OR periodo_actual_hasta >= periodo_actual_desde)
);

CREATE TABLE facturas (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  suscripcion_id uuid NOT NULL REFERENCES suscripciones(id) ON DELETE CASCADE,
  company_id     uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  folio          text UNIQUE,
  monto          numeric(12,2) NOT NULL CHECK (monto >= 0),
  moneda         text NOT NULL DEFAULT 'CLP',
  estado         factura_estado NOT NULL DEFAULT 'pendiente',
  gateway_ref    text,
  emitida_at     timestamptz NOT NULL DEFAULT now(),
  pagada_at      timestamptz,
  pdf_blob_path  text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE notificacion_preferencias (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id  uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  user_id     uuid REFERENCES users(id) ON DELETE CASCADE,
  evento      text NOT NULL,           -- vencimiento_30, vencimiento_7, doc_rechazado, alerta_critica, sync_fallida...
  canal_email boolean NOT NULL DEFAULT true,
  canal_whatsapp boolean NOT NULL DEFAULT false,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (company_id, user_id, evento)
);

-- ============================================================================
-- 5. BITÁCORA TÉCNICA DE CAMBIOS (poblada por triggers, ver 03_triggers.sql)
-- ============================================================================
CREATE TABLE bitacora_cambios (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tabla      text NOT NULL,
  registro_id uuid NOT NULL,
  company_id uuid,
  operacion  text NOT NULL CHECK (operacion IN ('INSERT','UPDATE','DELETE')),
  campos     jsonb NOT NULL,           -- {campo: {antes, despues}}
  usuario_bd text NOT NULL DEFAULT current_user,
  app_user   text,                     -- set por la app vía SET app.user_id
  created_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE sujetos IS 'Trabajadores y equipos (single-table, discriminador tipo). Un sujeto pertenece a un contrato.';
COMMENT ON TABLE documentos IS 'Instancias de requisitos documentales. Dueño exclusivo: sujeto (personal/equipo/emsipor) XOR contrato (empresa).';
COMMENT ON TABLE bitacora_cambios IS 'Auditoría técnica por triggers en tablas críticas. La actividad de negocio la escribe la aplicación en la tabla actividad.';
