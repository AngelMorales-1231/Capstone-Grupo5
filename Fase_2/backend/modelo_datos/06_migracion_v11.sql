-- ============================================================================
-- ACREDITTIA — Modelo de datos v1.1
-- 06_migracion_v11.sql — Migración aditiva desde el baseline v1.0 (01→05)
-- Motor: PostgreSQL 16 (Azure Database for PostgreSQL Flexible Server)
-- Orden de ejecución: 01 → 02 → 03 → 04 → 05 → 06
--
-- Incorpora las entidades de la Especificación de API Backend v1.1:
--   · plataformas por contrato y flujo de solicitud de acceso   (§8.1)
--   · vault JWE de credenciales de plataforma con historial      (§8.2)
--   · requisitos personalizados por vínculo y overrides          (§8.3)
--   · catálogo de cargos referenciable                            (§9.2)
--   · snapshots de cumplimiento por empresa y contrato            (§14.1)
--   · rol contract_admin y usuarios internos de la empresa        (§7.1)
--
-- El script es IDEMPOTENTE: puede reejecutarse sin efectos adversos.
--
-- IMPORTANTE — ejecutar en autocommit (psql -f, sin BEGIN envolvente).
-- ALTER TYPE ... ADD VALUE admite ejecutarse dentro de una transacción desde
-- PG12, pero el valor nuevo no puede USARSE en esa misma transacción, y el
-- bloque 3 crea un CHECK que referencia 'contract_admin'.
-- ============================================================================

SET client_encoding = 'UTF8';
SET timezone = 'America/Santiago';

-- ============================================================================
-- 1. TIPOS ENUMERADOS
-- ============================================================================

-- 1.1 Ampliación de enums existentes
ALTER TYPE user_role  ADD VALUE IF NOT EXISTS 'contract_admin';
ALTER TYPE ia_context ADD VALUE IF NOT EXISTS 'cedula';
ALTER TYPE ia_context ADD VALUE IF NOT EXISTS 'padron';
ALTER TYPE ia_context ADD VALUE IF NOT EXISTS 'carpeta_arranque';
-- La matriz de cumplimiento (§8.4) es exportable como reporte
ALTER TYPE reporte_tipo ADD VALUE IF NOT EXISTS 'matriz_cumplimiento';

-- 1.2 Enums nuevos (CREATE TYPE no admite IF NOT EXISTS)
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'vinculo_tipo') THEN
    CREATE TYPE vinculo_tipo AS ENUM ('plataforma','arranque','otro');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'requisito_origen') THEN
    CREATE TYPE requisito_origen AS ENUM ('base','custom','arranque');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'cargo_categoria') THEN
    CREATE TYPE cargo_categoria AS ENUM
      ('conduccion','operacion','supervision','mantencion','administracion','otro');
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'credencial_estado') THEN
    CREATE TYPE credencial_estado AS ENUM ('activa','expirada','revocada');
  END IF;
END $$;

-- ============================================================================
-- 2. CATÁLOGO DE CARGOS
--    company_id NULL = catálogo base de Acredittia (administrado por admin).
--    Reemplaza la inferencia por expresiones regulares sobre texto libre.
-- ============================================================================
CREATE TABLE IF NOT EXISTS cargos (
  id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id         uuid REFERENCES companies(id) ON DELETE CASCADE,  -- NULL = global
  nombre             text NOT NULL,
  nombre_normalizado text NOT NULL,          -- lower(unaccent-lite) para deduplicar
  categoria          cargo_categoria NOT NULL DEFAULT 'otro',
  requiere_emsipor   boolean NOT NULL DEFAULT false,
  activo             boolean NOT NULL DEFAULT true,
  created_at         timestamptz NOT NULL DEFAULT now(),
  updated_at         timestamptz NOT NULL DEFAULT now(),
  -- NULLS NOT DISTINCT (PG15+): impide dos cargos globales con el mismo nombre
  CONSTRAINT ux_cargos_nombre UNIQUE NULLS NOT DISTINCT (company_id, nombre_normalizado)
);

CREATE TABLE IF NOT EXISTS cargo_requisitos (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cargo_id              uuid NOT NULL REFERENCES cargos(id) ON DELETE CASCADE,
  requisito_template_id uuid NOT NULL REFERENCES requisito_templates(id) ON DELETE CASCADE,
  obligatorio           boolean NOT NULL DEFAULT true,
  created_at            timestamptz NOT NULL DEFAULT now(),
  UNIQUE (cargo_id, requisito_template_id)
);

-- ============================================================================
-- 3. PLATAFORMAS POR CONTRATO
--    Por defecto el contrato hereda faena_plataformas. Si tiene filas aquí,
--    esta lista es la efectiva (la API materializa antes las heredadas para
--    no perderlas: faena_plataforma_id conserva el origen).
-- ============================================================================
CREATE TABLE IF NOT EXISTS contrato_plataformas (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id          uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  contrato_id         uuid NOT NULL REFERENCES contratos(id) ON DELETE CASCADE,
  faena_plataforma_id uuid REFERENCES faena_plataformas(id) ON DELETE SET NULL,
  nombre              text NOT NULL,
  descripcion         text,
  url                 text,
  color               text,
  estado              plataforma_estado NOT NULL DEFAULT 'sin_acceso',
  nota                text,
  orden               smallint NOT NULL DEFAULT 0,
  es_custom           boolean NOT NULL DEFAULT true,
  solicitado_at       timestamptz,
  habilitado_at       timestamptz,
  ultima_sync_at      timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ux_cplat_nombre UNIQUE (contrato_id, nombre),
  -- Una plataforma heredada de la faena no es "custom" y viceversa
  CONSTRAINT ck_cplat_origen CHECK (es_custom = (faena_plataforma_id IS NULL)),
  CONSTRAINT ck_cplat_solicitud CHECK (estado <> 'solicitada' OR solicitado_at IS NOT NULL),
  CONSTRAINT ck_cplat_habilitada CHECK (estado <> 'activa' OR habilitado_at IS NOT NULL)
);

-- ============================================================================
-- 4. VAULT DE CREDENCIALES DE PLATAFORMA (JWE)
--    El secreto se persiste EXCLUSIVAMENTE como JWE compact
--    (alg=RSA-OAEP-256, enc=A256GCM); la clave privada vive en Azure Key
--    Vault y el descifrado se delega al vault (unwrapKey). No existe columna
--    en claro ni ruta de descifrado desde la API.
--
--    RIESGO RESIDUAL DOCUMENTADO — logs del servidor. PostgreSQL no permite
--    redactar columnas en los mensajes de error: una violación de NOT NULL o
--    de UNIQUE puede emitir «DETAIL: Failing row contains (...)» con el JWE
--    incluido, y ese texto va al log. Mitigaciones aplicadas:
--      · sin CHECK constraints en esta tabla (la vía de violación más probable);
--        las validaciones viven en fn_check_credencial() (§9.6).
--      · la aplicación valida formato y nulabilidad ANTES del INSERT.
--    Mitigación pendiente a nivel de servidor (Azure → parámetros del server):
--      log_error_verbosity = terse
--    Sin ella, un JWE puede quedar en postgresql.log; el log no es descifrable
--    sin Key Vault, pero debe tratarse con la misma clasificación que la BD.
-- ============================================================================
CREATE TABLE IF NOT EXISTS plataforma_credenciales (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id             uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  contrato_plataforma_id uuid NOT NULL REFERENCES contrato_plataformas(id) ON DELETE CASCADE,
  nombre                 text NOT NULL,              -- titular responsable
  usuario                text NOT NULL,              -- identificador visible
  credencial_jwe         text NOT NULL,              -- JWE vigente. NUNCA se retorna por la API
  kid                    text NOT NULL,              -- versión de clave de Key Vault
  version                integer NOT NULL DEFAULT 1 CHECK (version >= 1),
  estado                 credencial_estado NOT NULL DEFAULT 'activa',
  expira_at              timestamptz NOT NULL,       -- claim exp del JWE
  rotada_at              timestamptz NOT NULL DEFAULT now(),
  last_used_at           timestamptz,
  created_by             uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ux_credencial_usuario UNIQUE (contrato_plataforma_id, usuario)
  -- SIN CHECK constraints por diseño: ante una violación PostgreSQL emite
  -- «DETAIL: Failing row contains (...)» con la fila completa, lo que copiaría
  -- el JWE al log del servidor. El formato de las 5 partes de la serialización
  -- compacta y la coherencia expira_at > rotada_at se validan en el trigger
  -- fn_check_credencial() (§9.6), que levanta mensajes sin volcar la fila.
);

-- Historial de rotaciones: append-only. Conserva los JWE anteriores con su kid
-- para auditar qué secreto usó una sincronización pasada y permitir rollback.
CREATE TABLE IF NOT EXISTS plataforma_credencial_versiones (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id     uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  credencial_id  uuid NOT NULL REFERENCES plataforma_credenciales(id) ON DELETE CASCADE,
  version        integer NOT NULL,
  credencial_jwe text NOT NULL,
  kid            text NOT NULL,
  vigente_desde  timestamptz NOT NULL,
  vigente_hasta  timestamptz NOT NULL,
  rotada_por     uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (credencial_id, version),
  CONSTRAINT ck_credver_vigencia CHECK (vigente_hasta >= vigente_desde)
);

-- ============================================================================
-- 5. REQUISITOS PERSONALIZADOS POR CONTRATO
--    Todo requisito se ancla a un vínculo: una plataforma del contrato, la
--    Carpeta de Arranque ('arranque') u 'otro'. Las filas origen='base' se
--    proyectan desde requisito_templates y son de solo lectura para la empresa.
-- ============================================================================
CREATE TABLE IF NOT EXISTS contrato_requisitos (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id            uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  contrato_id           uuid NOT NULL REFERENCES contratos(id) ON DELETE CASCADE,
  vinculo_tipo          vinculo_tipo NOT NULL DEFAULT 'otro',
  vinculo_ref           uuid REFERENCES contrato_plataformas(id) ON DELETE CASCADE,
  ambito                req_ambito NOT NULL,
  titulo                text NOT NULL,
  obligatorio           boolean NOT NULL DEFAULT true,
  cargo_id              uuid REFERENCES cargos(id) ON DELETE SET NULL,
  origen                requisito_origen NOT NULL DEFAULT 'custom',
  requisito_template_id uuid REFERENCES requisito_templates(id) ON DELETE SET NULL,
  ejemplo_clave         text REFERENCES doc_ejemplos(clave) ON DELETE SET NULL,
  vigencia_meses        smallint CHECK (vigencia_meses IS NULL OR vigencia_meses BETWEEN 1 AND 120),
  activo                boolean NOT NULL DEFAULT true,
  created_at            timestamptz NOT NULL DEFAULT now(),
  updated_at            timestamptz NOT NULL DEFAULT now(),
  -- vinculo_ref solo aplica (y es obligatorio) cuando el vínculo es plataforma
  CONSTRAINT ck_creq_vinculo CHECK ((vinculo_tipo = 'plataforma') = (vinculo_ref IS NOT NULL)),
  -- El scope por cargo solo tiene sentido en el ámbito personal
  CONSTRAINT ck_creq_cargo CHECK (cargo_id IS NULL OR ambito = 'personal'),
  -- origen='base' exige la plantilla de origen
  CONSTRAINT ck_creq_base CHECK (origen <> 'base' OR requisito_template_id IS NOT NULL)
);

-- Reemplazo total de la plantilla estándar de un ámbito para un contrato.
CREATE TABLE IF NOT EXISTS contrato_plantilla_overrides (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id             uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  contrato_id            uuid NOT NULL REFERENCES contratos(id) ON DELETE CASCADE,
  ambito                 req_ambito NOT NULL,
  requisito_template_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
  created_at             timestamptz NOT NULL DEFAULT now(),
  updated_at             timestamptz NOT NULL DEFAULT now(),
  UNIQUE (contrato_id, ambito)
);

-- ============================================================================
-- 6. SNAPSHOTS DE CUMPLIMIENTO
--    Escritos por el cron diario de vencimientos (00:30 America/Santiago).
--    contrato_id NULL = fila agregada de la empresa.
--    Inmutables: un recálculo posterior no reescribe el histórico.
-- ============================================================================
CREATE TABLE IF NOT EXISTS cumplimiento_snapshots (
  id                    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  company_id            uuid NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  contrato_id           uuid REFERENCES contratos(id) ON DELETE CASCADE,  -- NULL = empresa
  fecha                 date NOT NULL,
  cumplimiento_pct      smallint NOT NULL CHECK (cumplimiento_pct BETWEEN 0 AND 100),
  docs_ok               integer NOT NULL DEFAULT 0 CHECK (docs_ok >= 0),
  docs_total            integer NOT NULL DEFAULT 0 CHECK (docs_total >= 0),
  personal_acreditados  integer NOT NULL DEFAULT 0 CHECK (personal_acreditados >= 0),
  personal_total        integer NOT NULL DEFAULT 0 CHECK (personal_total >= 0),
  equipos_acreditados   integer NOT NULL DEFAULT 0 CHECK (equipos_acreditados >= 0),
  equipos_total         integer NOT NULL DEFAULT 0 CHECK (equipos_total >= 0),
  alertas_criticas      integer NOT NULL DEFAULT 0 CHECK (alertas_criticas >= 0),
  created_at            timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ux_snapshot UNIQUE NULLS NOT DISTINCT (company_id, contrato_id, fecha),
  CONSTRAINT ck_snapshot_docs CHECK (docs_ok <= docs_total),
  CONSTRAINT ck_snapshot_personal CHECK (personal_acreditados <= personal_total),
  CONSTRAINT ck_snapshot_equipos CHECK (equipos_acreditados <= equipos_total)
);

-- ============================================================================
-- 7. COLUMNAS NUEVAS EN TABLAS EXISTENTES
-- ============================================================================

-- 7.1 users: rol contract_admin acotado a un contrato + gestión interna
ALTER TABLE users ADD COLUMN IF NOT EXISTS contrato_id uuid REFERENCES contratos(id) ON DELETE CASCADE;
ALTER TABLE users ADD COLUMN IF NOT EXISTS nombre text;
ALTER TABLE users ADD COLUMN IF NOT EXISTS activo boolean NOT NULL DEFAULT true;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ck_users_contrato') THEN
    ALTER TABLE users ADD CONSTRAINT ck_users_contrato
      CHECK ( (role = 'contract_admin') = (contrato_id IS NOT NULL) );
  END IF;
END $$;

-- 7.2 companies: empresa sandbox reiniciable (POST /admin/companies/{id}/reset-demo)
ALTER TABLE companies ADD COLUMN IF NOT EXISTS es_demo boolean NOT NULL DEFAULT false;

-- 7.3 sujetos: cargo referenciable. El texto libre 'cargo' se conserva por
--     compatibilidad y queda derivado de cargos.nombre.
ALTER TABLE sujetos ADD COLUMN IF NOT EXISTS cargo_id uuid REFERENCES cargos(id) ON DELETE SET NULL;

-- 7.4 company_faena_plataformas: flujo de solicitud de acceso
ALTER TABLE company_faena_plataformas ADD COLUMN IF NOT EXISTS nota text;
ALTER TABLE company_faena_plataformas ADD COLUMN IF NOT EXISTS solicitado_at timestamptz;
ALTER TABLE company_faena_plataformas ADD COLUMN IF NOT EXISTS habilitado_at timestamptz;

-- 7.5 faena_plataformas: orden de despliegue en la UI
ALTER TABLE faena_plataformas ADD COLUMN IF NOT EXISTS orden smallint NOT NULL DEFAULT 0;

-- ============================================================================
-- 8. ÍNDICES
--    Toda FK creada arriba lleva índice de soporte: el chequeo 2 de
--    05_verificacion.sql exige que no queden FK sin índice.
-- ============================================================================

-- Cargos
CREATE INDEX IF NOT EXISTS ix_cargos_company     ON cargos (company_id) WHERE activo;
CREATE INDEX IF NOT EXISTS ix_cargos_company_fk  ON cargos (company_id);
CREATE INDEX IF NOT EXISTS ix_cargos_categoria   ON cargos (categoria) WHERE activo;
CREATE INDEX IF NOT EXISTS ix_cargoreq_cargo     ON cargo_requisitos (cargo_id);
CREATE INDEX IF NOT EXISTS ix_cargoreq_template  ON cargo_requisitos (requisito_template_id);

-- Plataformas del contrato
CREATE INDEX IF NOT EXISTS ix_cplat_company      ON contrato_plataformas (company_id);
CREATE INDEX IF NOT EXISTS ix_cplat_contrato     ON contrato_plataformas (contrato_id, orden);
CREATE INDEX IF NOT EXISTS ix_cplat_fplat        ON contrato_plataformas (faena_plataforma_id);
CREATE INDEX IF NOT EXISTS ix_cplat_estado       ON contrato_plataformas (company_id, estado);

-- Credenciales
CREATE INDEX IF NOT EXISTS ix_cred_company       ON plataforma_credenciales (company_id);
CREATE INDEX IF NOT EXISTS ix_cred_plataforma    ON plataforma_credenciales (contrato_plataforma_id);
CREATE INDEX IF NOT EXISTS ix_cred_creador       ON plataforma_credenciales (created_by);
-- Cron de expiración: solo recorre las activas próximas a vencer
CREATE INDEX IF NOT EXISTS ix_cred_expiracion    ON plataforma_credenciales (expira_at) WHERE estado = 'activa';
CREATE INDEX IF NOT EXISTS ix_credver_company    ON plataforma_credencial_versiones (company_id);
CREATE INDEX IF NOT EXISTS ix_credver_credencial ON plataforma_credencial_versiones (credencial_id, version DESC);
CREATE INDEX IF NOT EXISTS ix_credver_rotada_por ON plataforma_credencial_versiones (rotada_por);
CREATE INDEX IF NOT EXISTS ix_credver_brin       ON plataforma_credencial_versiones USING brin (created_at);

-- Requisitos del contrato
CREATE INDEX IF NOT EXISTS ix_creq_company       ON contrato_requisitos (company_id);
CREATE INDEX IF NOT EXISTS ix_creq_contrato      ON contrato_requisitos (contrato_id, ambito) WHERE activo;
CREATE INDEX IF NOT EXISTS ix_creq_contrato_fk   ON contrato_requisitos (contrato_id);
CREATE INDEX IF NOT EXISTS ix_creq_vinculo       ON contrato_requisitos (vinculo_ref);
CREATE INDEX IF NOT EXISTS ix_creq_cargo         ON contrato_requisitos (cargo_id);
CREATE INDEX IF NOT EXISTS ix_creq_template      ON contrato_requisitos (requisito_template_id);
CREATE INDEX IF NOT EXISTS ix_creq_ejemplo       ON contrato_requisitos (ejemplo_clave);
-- Deduplicación por título dentro del mismo vínculo y ámbito (expresión + NULLS NOT DISTINCT)
CREATE UNIQUE INDEX IF NOT EXISTS ux_creq_titulo
  ON contrato_requisitos (contrato_id, vinculo_tipo, vinculo_ref, ambito, lower(titulo))
  NULLS NOT DISTINCT;

-- Overrides de plantilla
CREATE INDEX IF NOT EXISTS ix_covr_company       ON contrato_plantilla_overrides (company_id);
CREATE INDEX IF NOT EXISTS ix_covr_contrato      ON contrato_plantilla_overrides (contrato_id);

-- Snapshots: la serie se lee por (empresa|contrato, fecha)
CREATE INDEX IF NOT EXISTS ix_snap_company_fecha ON cumplimiento_snapshots (company_id, fecha DESC);
CREATE INDEX IF NOT EXISTS ix_snap_contrato      ON cumplimiento_snapshots (contrato_id, fecha DESC);

-- Columnas nuevas en tablas existentes
CREATE INDEX IF NOT EXISTS ix_users_contrato     ON users (contrato_id);
CREATE INDEX IF NOT EXISTS ix_sujetos_cargo      ON sujetos (cargo_id);
CREATE INDEX IF NOT EXISTS ix_companies_demo     ON companies (id) WHERE es_demo;

-- 8.1 Deuda del baseline v1.0: 15 foreign keys quedaron sin índice de soporte
--     y el chequeo 2 de 05_verificacion.sql las reportaba en cada corrida.
--     Se resuelven aquí porque son aditivas y no alteran los scripts 01–05.
--     Impacto real: acelera los ON DELETE CASCADE/SET NULL y los joins de
--     auditoría; sin ellos, borrar una empresa obliga a un seq scan por tabla.
CREATE INDEX IF NOT EXISTS ix_actividad_user      ON actividad (user_id);
CREATE INDEX IF NOT EXISTS ix_companies_approver  ON companies (approved_by);
CREATE INDEX IF NOT EXISTS ix_cfplat_fplat        ON company_faena_plataformas (faena_plataforma_id);
CREATE INDEX IF NOT EXISTS ix_contratos_ia        ON contratos (origen_ia_review_id);
CREATE INDEX IF NOT EXISTS ix_archivos_company    ON documento_archivos (company_id);
CREATE INDEX IF NOT EXISTS ix_archivos_uploader   ON documento_archivos (uploaded_by);
CREATE INDEX IF NOT EXISTS ix_archivos_ia         ON documento_archivos (ia_review_id);
CREATE INDEX IF NOT EXISTS ix_docs_template       ON documentos (template_id);
CREATE INDEX IF NOT EXISTS ix_eventos_documento   ON eventos_calendario (documento_id);
CREATE INDEX IF NOT EXISTS ix_notifpref_user      ON notificacion_preferencias (user_id);
CREATE INDEX IF NOT EXISTS ix_provcat_faena       ON proveedores_catalogo (faena_id);
CREATE INDEX IF NOT EXISTS ix_reportes_generador  ON reportes (generado_por);
CREATE INDEX IF NOT EXISTS ix_reqtpl_ejemplo      ON requisito_templates (ejemplo_clave);
CREATE INDEX IF NOT EXISTS ix_reqtpl_faena        ON requisito_templates (faena_id);
CREATE INDEX IF NOT EXISTS ix_susc_plan           ON suscripciones (plan_id);

-- ============================================================================
-- 9. TRIGGERS
-- ============================================================================

-- 9.1 updated_at en las tablas nuevas (reutiliza fn_touch_updated_at de 03)
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['cargos','contrato_plataformas','plataforma_credenciales',
                           'contrato_requisitos','contrato_plantilla_overrides']
  LOOP
    EXECUTE format(
      'CREATE OR REPLACE TRIGGER trg_touch_%I
         BEFORE UPDATE ON %I
         FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at()', t, t);
  END LOOP;
END $$;

-- 9.2 Archivado automático de la credencial anterior al rotar el secreto.
--     La API solo hace UPDATE de credencial_jwe/kid/expira_at; la BD garantiza
--     que la versión anterior queda archivada y que 'version' avanza.
CREATE OR REPLACE FUNCTION fn_archivar_credencial() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.credencial_jwe IS DISTINCT FROM OLD.credencial_jwe THEN
    INSERT INTO plataforma_credencial_versiones
      (company_id, credencial_id, version, credencial_jwe, kid,
       vigente_desde, vigente_hasta, rotada_por)
    VALUES
      (OLD.company_id, OLD.id, OLD.version, OLD.credencial_jwe, OLD.kid,
       OLD.rotada_at, now(),
       NULLIF(current_setting('app.user_id', true), '')::uuid);
    NEW.version   := OLD.version + 1;
    NEW.rotada_at := now();
  END IF;
  RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER trg_archivar_credencial
  BEFORE UPDATE OF credencial_jwe ON plataforma_credenciales
  FOR EACH ROW EXECUTE FUNCTION fn_archivar_credencial();

-- 9.3 Historial de credenciales inmutable: rechaza UPDATE (reutiliza
--     fn_solo_insert de 03). El DELETE SÍ se permite, por dos razones:
--       · al borrar la credencial, su historial debe caer en cascada; con el
--         DELETE bloqueado la cascada aborta con SQLSTATE 55000 y la credencial
--         se vuelve imborrable.
--       · la purga por retención (24 meses) no necesitaría deshabilitar el
--         trigger, lo que obligaba a un ALTER TABLE en ventana de mantención.
--     La garantía que importa es que una versión archivada no se pueda
--     REESCRIBIR: eso es lo que impide falsificar qué secreto estuvo vigente.
CREATE OR REPLACE TRIGGER trg_credver_inmutable
  BEFORE UPDATE ON plataforma_credencial_versiones
  FOR EACH ROW EXECUTE FUNCTION fn_solo_insert();

-- 9.4 Snapshots inmutables: un recálculo no reescribe el histórico.
CREATE OR REPLACE TRIGGER trg_snapshots_inmutables
  BEFORE UPDATE ON cumplimiento_snapshots
  FOR EACH ROW EXECUTE FUNCTION fn_solo_insert();

-- 9.5 Coherencia de tenant en las tablas nuevas colgadas de un contrato.
CREATE OR REPLACE FUNCTION fn_check_contrato_tenant() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_owner uuid;
BEGIN
  SELECT company_id INTO v_owner FROM contratos WHERE id = NEW.contrato_id;
  IF v_owner IS NULL OR v_owner <> NEW.company_id THEN
    RAISE EXCEPTION '%.company_id (%) no coincide con el contrato (%)',
      TG_TABLE_NAME, NEW.company_id, v_owner USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;

DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['contrato_plataformas','contrato_requisitos',
                           'contrato_plantilla_overrides']
  LOOP
    EXECUTE format(
      'CREATE OR REPLACE TRIGGER trg_%I_tenant
         BEFORE INSERT OR UPDATE OF contrato_id, company_id ON %I
         FOR EACH ROW EXECUTE FUNCTION fn_check_contrato_tenant()', t, t);
  END LOOP;
END $$;

-- 9.6 Validación de la credencial: coherencia de tenant, formato del JWE y
--     vigencia. Se hace por trigger y no con CHECK constraints porque los
--     mensajes de violación de CHECK/NOT NULL incluyen «Failing row contains»
--     con la fila completa, que iría al log del servidor con el JWE dentro.
--     Este trigger nunca menciona el valor del secreto.
CREATE OR REPLACE FUNCTION fn_check_credencial() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_owner uuid;
BEGIN
  SELECT company_id INTO v_owner FROM contrato_plataformas
   WHERE id = NEW.contrato_plataforma_id;
  IF v_owner IS NULL OR v_owner <> NEW.company_id THEN
    RAISE EXCEPTION 'plataforma_credenciales.company_id (%) no coincide con la plataforma (%)',
      NEW.company_id, v_owner USING ERRCODE = '23514';
  END IF;

  -- Serialización compacta: exactamente 5 partes separadas por punto
  IF NEW.credencial_jwe !~ '^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]*){4}$' THEN
    RAISE EXCEPTION 'credencial_jwe no tiene el formato JWE compact de 5 partes (credencial %)',
      NEW.id USING ERRCODE = '23514';
  END IF;

  IF NEW.expira_at <= NEW.rotada_at THEN
    RAISE EXCEPTION 'expira_at debe ser posterior a rotada_at (credencial %)',
      NEW.id USING ERRCODE = '23514';
  END IF;

  RETURN NEW;
END $$;

CREATE OR REPLACE TRIGGER trg_credencial_check
  BEFORE INSERT OR UPDATE ON plataforma_credenciales
  FOR EACH ROW EXECUTE FUNCTION fn_check_credencial();

-- Se ejecuta después de trg_archivar_credencial (orden alfabético de nombres:
-- trg_archivar_credencial < trg_credencial_check), de modo que valida ya la
-- versión y rotada_at recalculados por el archivado.

-- 9.7 NOTA DELIBERADA — sin bitácora técnica sobre plataforma_credenciales.
--     fn_bitacora() serializa la fila completa a jsonb en INSERT y DELETE, lo
--     que copiaría el JWE a bitacora_cambios, una tabla legible por el rol de
--     la aplicación. La trazabilidad de las credenciales vive en
--     plataforma_credencial_versiones (JWE, con acceso restringido al worker)
--     y en actividad (metadata: jti, kid, quién y cuándo).

-- ============================================================================
-- 10. ROW LEVEL SECURITY
-- ============================================================================

-- 10.1 Rol del worker de integración: único que puede leer el JWE.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'acredittia_worker') THEN
    CREATE ROLE acredittia_worker LOGIN PASSWORD 'CAMBIAR_EN_KEY_VAULT' NOBYPASSRLS;
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO acredittia_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO acredittia_worker;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO acredittia_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO acredittia_worker;

-- Las tablas creadas por este script necesitan el GRANT explícito para el rol
-- de aplicación (ALTER DEFAULT PRIVILEGES solo afecta a objetos futuros).
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO acredittia_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO acredittia_app;

-- 10.2 Separación de privilegios sobre el JWE.
--      En PostgreSQL el GRANT a nivel de tabla implica todas las columnas, así
--      que primero se revoca el SELECT de tabla y luego se concede columna a
--      columna, omitiendo credencial_jwe y kid.
REVOKE SELECT ON plataforma_credenciales FROM acredittia_app;
GRANT  SELECT (id, company_id, contrato_plataforma_id, nombre, usuario, version,
               estado, expira_at, rotada_at, last_used_at, created_by,
               created_at, updated_at)
  ON plataforma_credenciales TO acredittia_app;
-- La API escribe el secreto pero no puede releerlo (write-only real).
GRANT INSERT, UPDATE ON plataforma_credenciales TO acredittia_app;

REVOKE SELECT ON plataforma_credencial_versiones FROM acredittia_app;
GRANT  SELECT (id, company_id, credencial_id, version, kid,
               vigente_desde, vigente_hasta, rotada_por, created_at)
  ON plataforma_credencial_versiones TO acredittia_app;
-- El archivado lo hace el trigger, que corre con los privilegios del invocador
GRANT INSERT ON plataforma_credencial_versiones TO acredittia_app;

-- 10.3 Vista sin secreto para la API. security_invoker=true (PG15+) mantiene
--      la evaluación de RLS con el rol que consulta, no con el dueño de la vista.
CREATE OR REPLACE VIEW v_plataforma_credenciales
  WITH (security_invoker = true) AS
  SELECT id, company_id, contrato_plataforma_id, nombre, usuario, version,
         estado, expira_at, rotada_at, last_used_at, created_by,
         created_at, updated_at,
         true AS password_set
    FROM plataforma_credenciales;

GRANT SELECT ON v_plataforma_credenciales TO acredittia_app, acredittia_worker;

-- 10.4 Política multi-tenant en las tablas nuevas con company_id NOT NULL.
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['contrato_plataformas','plataforma_credenciales',
                           'plataforma_credencial_versiones','contrato_requisitos',
                           'contrato_plantilla_overrides','cumplimiento_snapshots']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('DROP POLICY IF EXISTS p_tenant ON %I', t);
    EXECUTE format($p$
      CREATE POLICY p_tenant ON %I
        FOR ALL
        USING     (app_is_admin() OR company_id = app_company_id())
        WITH CHECK (app_is_admin() OR company_id = app_company_id())
    $p$, t);
  END LOOP;
END $$;

-- 10.5 cargos: híbrido tenant + catálogo global (company_id NULL).
--      Todos leen el catálogo base; solo el admin lo modifica.
ALTER TABLE cargos ENABLE ROW LEVEL SECURITY;
ALTER TABLE cargos FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_cargos_read  ON cargos;
DROP POLICY IF EXISTS p_cargos_write ON cargos;
DROP POLICY IF EXISTS p_cargos_upd   ON cargos;
DROP POLICY IF EXISTS p_cargos_del   ON cargos;
CREATE POLICY p_cargos_read ON cargos
  FOR SELECT USING (company_id IS NULL OR app_is_admin() OR company_id = app_company_id());
CREATE POLICY p_cargos_write ON cargos
  FOR INSERT WITH CHECK (CASE WHEN company_id IS NULL THEN app_is_admin()
                              ELSE company_id = app_company_id() OR app_is_admin() END);
CREATE POLICY p_cargos_upd ON cargos
  FOR UPDATE USING     (CASE WHEN company_id IS NULL THEN app_is_admin()
                             ELSE company_id = app_company_id() OR app_is_admin() END)
             WITH CHECK (CASE WHEN company_id IS NULL THEN app_is_admin()
                              ELSE company_id = app_company_id() OR app_is_admin() END);
CREATE POLICY p_cargos_del ON cargos
  FOR DELETE USING (CASE WHEN company_id IS NULL THEN app_is_admin()
                         ELSE company_id = app_company_id() OR app_is_admin() END);

-- 10.6 cargo_requisitos: no porta company_id; delega en el cargo padre.
ALTER TABLE cargo_requisitos ENABLE ROW LEVEL SECURITY;
ALTER TABLE cargo_requisitos FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS p_cargoreq ON cargo_requisitos;
CREATE POLICY p_cargoreq ON cargo_requisitos
  FOR ALL
  USING (EXISTS (SELECT 1 FROM cargos c WHERE c.id = cargo_id
                 AND (c.company_id IS NULL OR app_is_admin() OR c.company_id = app_company_id())))
  WITH CHECK (EXISTS (SELECT 1 FROM cargos c WHERE c.id = cargo_id
                 AND (CASE WHEN c.company_id IS NULL THEN app_is_admin()
                           ELSE c.company_id = app_company_id() OR app_is_admin() END)));

-- ============================================================================
-- 11. COMENTARIOS DE DOCUMENTACIÓN
-- ============================================================================
COMMENT ON TABLE  cargos IS
  'Catálogo de cargos. company_id NULL = catálogo base de Acredittia (solo admin). Reemplaza la inferencia por regex sobre texto libre.';
COMMENT ON TABLE  contrato_plataformas IS
  'Plataformas efectivas de un contrato. Si el contrato tiene filas aquí, sustituyen la lista heredada de faena_plataformas.';
COMMENT ON TABLE  plataforma_credenciales IS
  'Cuentas de acceso a plataformas del mandante. El secreto vive solo como JWE (RSA-OAEP-256 + A256GCM); la clave privada está en Azure Key Vault. Sin bitácora técnica por diseño: evitaría copiar el JWE a bitacora_cambios.';
COMMENT ON COLUMN plataforma_credenciales.credencial_jwe IS
  'JWE compact. SELECT revocado para acredittia_app; solo acredittia_worker puede leerlo.';
COMMENT ON TABLE  plataforma_credencial_versiones IS
  'Historial append-only de rotaciones. Conserva los JWE anteriores para auditar qué secreto usó una sync pasada y permitir rollback. Retención 24 meses.';
COMMENT ON TABLE  contrato_requisitos IS
  'Requisitos del contrato anclados a un vínculo (plataforma | arranque | otro). origen=base se proyecta desde requisito_templates y es de solo lectura para la empresa.';
COMMENT ON TABLE  contrato_plantilla_overrides IS
  'Reemplazo total de la plantilla estándar de un ámbito para un contrato. Se aplica a sujetos nuevos; no reescribe documentos ya instanciados.';
COMMENT ON TABLE  cumplimiento_snapshots IS
  'Serie histórica diaria de cumplimiento por empresa (contrato_id NULL) y por contrato. Alimenta GET /dashboard/tendencia. Inmutable.';
COMMENT ON VIEW   v_plataforma_credenciales IS
  'Proyección de plataforma_credenciales sin el JWE, para consumo de la API. security_invoker=true preserva la evaluación de RLS.';

-- ============================================================================
-- 12. VERIFICACIÓN DE LA MIGRACIÓN
--     Cada bloque devuelve filas SOLO si hay un problema.
--     Incorporar estos chequeos a 05_verificacion.sql en el próximo baseline.
-- ============================================================================

\echo '=== 16. Tablas nuevas sin RLS habilitado (esperado: 0 filas) ==='
SELECT c.relname AS tabla_sin_rls
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity
  AND c.relname IN ('cargos','cargo_requisitos','contrato_plataformas',
                    'plataforma_credenciales','plataforma_credencial_versiones',
                    'contrato_requisitos','contrato_plantilla_overrides',
                    'cumplimiento_snapshots');

\echo '=== 17. Fuga de privilegios: acredittia_app con SELECT sobre el JWE (esperado: 0) ==='
SELECT table_name, column_name, grantee
FROM information_schema.column_privileges
WHERE table_schema = 'public'
  AND column_name IN ('credencial_jwe')
  AND privilege_type = 'SELECT'
  AND grantee <> 'acredittia_worker'
  AND grantee NOT IN (SELECT rolname FROM pg_roles WHERE rolsuper);

\echo '=== 18. Credenciales activas ya expiradas sin marcar (esperado: 0) ==='
SELECT id, usuario, expira_at
FROM plataforma_credenciales
WHERE estado = 'activa' AND expira_at <= now();

\echo '=== 19. Historial de credenciales desalineado con la versión vigente (esperado: 0) ==='
SELECT c.id, c.version, max(v.version) AS max_archivada
FROM plataforma_credenciales c
JOIN plataforma_credencial_versiones v ON v.credencial_id = c.id
GROUP BY c.id, c.version
HAVING max(v.version) >= c.version;

\echo '=== 20. Requisitos de contrato con vínculo de otra empresa (esperado: 0) ==='
SELECT r.id, r.company_id, p.company_id AS plataforma_company
FROM contrato_requisitos r
JOIN contrato_plataformas p ON p.id = r.vinculo_ref
WHERE r.company_id <> p.company_id;

\echo '=== 21. contract_admin sin contrato o con contrato de otra empresa (esperado: 0) ==='
SELECT u.id, u.email, u.company_id, c.company_id AS contrato_company
FROM users u LEFT JOIN contratos c ON c.id = u.contrato_id
WHERE u.role = 'contract_admin'
  AND (u.contrato_id IS NULL OR c.company_id IS DISTINCT FROM u.company_id);

\echo '=== 22. Snapshots incoherentes: agregado de empresa ausente (esperado: 0) ==='
SELECT DISTINCT s.company_id, s.fecha
FROM cumplimiento_snapshots s
WHERE s.contrato_id IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM cumplimiento_snapshots a
                  WHERE a.company_id = s.company_id AND a.fecha = s.fecha
                    AND a.contrato_id IS NULL);

\echo '=== 23. FK nuevas sin índice de soporte (esperado: 0 filas) ==='
SELECT conrelid::regclass AS tabla, conname AS fk
FROM pg_constraint pc
WHERE contype = 'f'
  AND connamespace = 'public'::regnamespace
  AND conrelid::regclass::text IN ('cargos','cargo_requisitos','contrato_plataformas',
      'plataforma_credenciales','plataforma_credencial_versiones','contrato_requisitos',
      'contrato_plantilla_overrides','cumplimiento_snapshots','users','sujetos')
  AND NOT EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indrelid = pc.conrelid
      AND (i.indkey::int2[])[0:cardinality(pc.conkey)-1] @> pc.conkey::int2[]
  );

\echo '=== Resumen: objetos creados por la migración v1.1 ==='
SELECT
  (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'r') AS tablas_totales,
  (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind = 'i') AS indices_totales,
  (SELECT count(*) FROM pg_policies WHERE schemaname = 'public') AS politicas_rls,
  (SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal) AS triggers;

-- ============================================================================
-- Fin de 06_migracion_v11.sql
-- ============================================================================
