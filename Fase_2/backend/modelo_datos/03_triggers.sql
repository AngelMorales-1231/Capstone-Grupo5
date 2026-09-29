-- ============================================================================
-- ACREDITTIA — 03_triggers.sql
-- Triggers: updated_at automático, bitácora técnica de cambios en tablas
-- críticas, y derivaciones de estado (EMSIPOR/LIM, estado del sujeto).
-- Estrategia híbrida: la actividad de NEGOCIO la escribe la aplicación;
-- la BD garantiza lo técnico y las derivaciones de consistencia.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. updated_at automático en todas las tablas que lo poseen
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_touch_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END $$;

DO $$
DECLARE t text;
BEGIN
  FOR t IN
    SELECT c.table_name
    FROM information_schema.columns c
    JOIN information_schema.tables tb
      ON tb.table_name = c.table_name AND tb.table_schema = 'public' AND tb.table_type = 'BASE TABLE'
    WHERE c.table_schema = 'public' AND c.column_name = 'updated_at'
  LOOP
    EXECUTE format(
      'CREATE OR REPLACE TRIGGER trg_touch_%I
         BEFORE UPDATE ON %I
         FOR EACH ROW EXECUTE FUNCTION fn_touch_updated_at()', t, t);
  END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- 2. Bitácora técnica de cambios (tablas críticas: documentos, sujetos,
--    companies, licencias_internas, suscripciones)
--    Registra sólo los campos que cambiaron, con valor antes/después.
--    La app puede identificarse con: SET LOCAL app.user_id = '<uuid>';
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_bitacora() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  cambios jsonb := '{}'::jsonb;
  k text;
  v_old jsonb; v_new jsonb;
  v_company uuid;
  v_id uuid;
BEGIN
  IF TG_OP = 'UPDATE' THEN
    v_old := to_jsonb(OLD); v_new := to_jsonb(NEW);
    FOR k IN SELECT jsonb_object_keys(v_new) LOOP
      IF k NOT IN ('updated_at') AND (v_old -> k) IS DISTINCT FROM (v_new -> k) THEN
        cambios := cambios || jsonb_build_object(k, jsonb_build_object('antes', v_old -> k, 'despues', v_new -> k));
      END IF;
    END LOOP;
    IF cambios = '{}'::jsonb THEN RETURN NEW; END IF;   -- nada relevante cambió
    v_company := (v_new ->> 'company_id')::uuid;
    v_id := (v_new ->> 'id')::uuid;
  ELSIF TG_OP = 'DELETE' THEN
    v_old := to_jsonb(OLD);
    cambios := jsonb_build_object('_registro', v_old);
    v_company := (v_old ->> 'company_id')::uuid;
    v_id := (v_old ->> 'id')::uuid;
  ELSE  -- INSERT
    v_new := to_jsonb(NEW);
    cambios := jsonb_build_object('_registro', v_new);
    v_company := (v_new ->> 'company_id')::uuid;
    v_id := (v_new ->> 'id')::uuid;
  END IF;

  INSERT INTO bitacora_cambios (tabla, registro_id, company_id, operacion, campos, app_user)
  VALUES (TG_TABLE_NAME, v_id, v_company, TG_OP, cambios,
          current_setting('app.user_id', true));
  RETURN COALESCE(NEW, OLD);
END $$;

CREATE TRIGGER trg_bitacora_documentos
  AFTER INSERT OR UPDATE OR DELETE ON documentos
  FOR EACH ROW EXECUTE FUNCTION fn_bitacora();
CREATE TRIGGER trg_bitacora_sujetos
  AFTER UPDATE OR DELETE ON sujetos
  FOR EACH ROW EXECUTE FUNCTION fn_bitacora();
CREATE TRIGGER trg_bitacora_companies
  AFTER UPDATE ON companies
  FOR EACH ROW EXECUTE FUNCTION fn_bitacora();
CREATE TRIGGER trg_bitacora_lim
  AFTER UPDATE ON licencias_internas
  FOR EACH ROW EXECUTE FUNCTION fn_bitacora();
CREATE TRIGGER trg_bitacora_suscripciones
  AFTER UPDATE ON suscripciones
  FOR EACH ROW EXECUTE FUNCTION fn_bitacora();

-- ----------------------------------------------------------------------------
-- 3. Derivación EMSIPOR: al cambiar un documento del expediente (es_emsipor),
--    recalcular emsipor_estado de la licencia interna del sujeto.
--    pendiente (0 ok) → parcial (algunos) → aprobado (todos los obligatorios ok)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_sync_emsipor() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_sujeto uuid := COALESCE(NEW.sujeto_id, OLD.sujeto_id);
  n_oblig int; n_ok int; nuevo emsipor_estado;
BEGIN
  IF v_sujeto IS NULL THEN RETURN COALESCE(NEW, OLD); END IF;

  SELECT count(*) FILTER (WHERE obligatorio),
         count(*) FILTER (WHERE obligatorio AND estado = 'ok')
    INTO n_oblig, n_ok
    FROM documentos
   WHERE sujeto_id = v_sujeto AND es_emsipor;

  nuevo := CASE
             WHEN n_ok = 0 THEN 'pendiente'
             WHEN n_ok < n_oblig THEN 'parcial'
             ELSE 'aprobado'
           END;

  UPDATE licencias_internas
     SET emsipor_estado = nuevo
   WHERE sujeto_id = v_sujeto AND emsipor_estado IS DISTINCT FROM nuevo;

  RETURN COALESCE(NEW, OLD);
END $$;

CREATE TRIGGER trg_sync_emsipor
  AFTER INSERT OR UPDATE OF estado OR DELETE ON documentos
  FOR EACH ROW
  WHEN (pg_trigger_depth() = 0)
  EXECUTE FUNCTION fn_sync_emsipor();

-- ----------------------------------------------------------------------------
-- 4. Consistencia de company_id: el documento debe pertenecer a la misma
--    empresa que su sujeto/contrato (evita cruces entre tenants por bug de app)
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_check_doc_tenant() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_owner uuid;
BEGIN
  IF NEW.sujeto_id IS NOT NULL THEN
    SELECT company_id INTO v_owner FROM sujetos WHERE id = NEW.sujeto_id;
  ELSE
    SELECT company_id INTO v_owner FROM contratos WHERE id = NEW.contrato_id;
  END IF;
  IF v_owner IS NULL OR v_owner <> NEW.company_id THEN
    RAISE EXCEPTION 'documentos.company_id (%) no coincide con el dueño (%)', NEW.company_id, v_owner
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER trg_doc_tenant
  BEFORE INSERT OR UPDATE OF sujeto_id, contrato_id, company_id ON documentos
  FOR EACH ROW EXECUTE FUNCTION fn_check_doc_tenant();

-- Misma verificación para sujetos vs su contrato
CREATE OR REPLACE FUNCTION fn_check_sujeto_tenant() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE v_owner uuid;
BEGIN
  SELECT company_id INTO v_owner FROM contratos WHERE id = NEW.contrato_id;
  IF v_owner IS NULL OR v_owner <> NEW.company_id THEN
    RAISE EXCEPTION 'sujetos.company_id (%) no coincide con el contrato (%)', NEW.company_id, v_owner
      USING ERRCODE = '23514';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER trg_sujeto_tenant
  BEFORE INSERT OR UPDATE OF contrato_id, company_id ON sujetos
  FOR EACH ROW EXECUTE FUNCTION fn_check_sujeto_tenant();

-- ----------------------------------------------------------------------------
-- 5. Protección append-only de la actividad y la bitácora
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION fn_solo_insert() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'La tabla % es de sólo inserción (append-only)', TG_TABLE_NAME
    USING ERRCODE = '55000';
END $$;

CREATE TRIGGER trg_actividad_inmutable
  BEFORE UPDATE OR DELETE ON actividad
  FOR EACH ROW EXECUTE FUNCTION fn_solo_insert();
CREATE TRIGGER trg_bitacora_inmutable
  BEFORE UPDATE OR DELETE ON bitacora_cambios
  FOR EACH ROW EXECUTE FUNCTION fn_solo_insert();
