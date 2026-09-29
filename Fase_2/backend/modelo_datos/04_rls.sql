-- ============================================================================
-- ACREDITTIA — 04_rls.sql
-- Roles de conexión y Row Level Security (aislamiento multi-tenant).
--
-- Modelo de sesión: la aplicación (FastAPI) se conecta con el rol
-- acredittia_app y, al inicio de cada request/transacción, fija:
--   SET LOCAL app.company_id = '<uuid de la empresa del token>';
--   SET LOCAL app.is_admin   = 'true' | 'false';
--   SET LOCAL app.user_id    = '<uuid del usuario>';   -- para la bitácora
-- Las migraciones usan acredittia_owner (dueño de los objetos).
-- Nota Azure Flexible Server: crear los roles con el usuario administrador
-- del servidor; no existe superusuario.
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. Roles
-- ----------------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'acredittia_owner') THEN
    CREATE ROLE acredittia_owner NOLOGIN;
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'acredittia_app') THEN
    CREATE ROLE acredittia_app LOGIN PASSWORD 'CAMBIAR_EN_KEY_VAULT' NOBYPASSRLS;
  END IF;
END $$;

GRANT USAGE ON SCHEMA public TO acredittia_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO acredittia_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO acredittia_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO acredittia_app;

-- ----------------------------------------------------------------------------
-- 2. Funciones de contexto de sesión
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app_company_id() RETURNS uuid
LANGUAGE sql STABLE AS
$$ SELECT NULLIF(current_setting('app.company_id', true), '')::uuid $$;

CREATE OR REPLACE FUNCTION app_is_admin() RETURNS boolean
LANGUAGE sql STABLE AS
$$ SELECT COALESCE(current_setting('app.is_admin', true), 'false')::boolean $$;

-- ----------------------------------------------------------------------------
-- 3. RLS en tablas multi-tenant (todas las que tienen company_id)
--    FORCE: aplica también al dueño de la tabla (defensa en profundidad).
--    Política única FOR ALL: mismo predicado para leer y escribir.
-- ----------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOR t IN
    SELECT c.table_name
    FROM information_schema.columns c
    JOIN information_schema.tables tb
      ON tb.table_name = c.table_name AND tb.table_schema = 'public' AND tb.table_type = 'BASE TABLE'
    WHERE c.table_schema = 'public' AND c.column_name = 'company_id'
      AND c.table_name NOT IN ('users')            -- users se maneja aparte (admin: company_id NULL)
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format($p$
      CREATE POLICY p_tenant ON %I
        FOR ALL
        USING (app_is_admin() OR company_id = app_company_id())
        WITH CHECK (app_is_admin() OR company_id = app_company_id())
    $p$, t);
  END LOOP;
END $$;

-- users: cada empresa ve sus usuarios; el admin ve todos
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
CREATE POLICY p_tenant_users ON users
  FOR ALL
  USING (app_is_admin() OR company_id = app_company_id())
  WITH CHECK (app_is_admin() OR company_id = app_company_id());

-- ----------------------------------------------------------------------------
-- 4. Catálogos globales: lectura para todos, escritura sólo admin
--    (faenas, faena_plataformas, doc_ejemplos, requisito_templates,
--     requisitos_terreno, proveedores_catalogo, planes)
-- ----------------------------------------------------------------------------
DO $$
DECLARE t text;
BEGIN
  FOREACH t IN ARRAY ARRAY['faenas','faena_plataformas','doc_ejemplos',
                           'requisito_templates','requisitos_terreno',
                           'proveedores_catalogo','planes']
  LOOP
    EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
    EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
    EXECUTE format('CREATE POLICY p_catalogo_read  ON %I FOR SELECT USING (true)', t);
    EXECUTE format('CREATE POLICY p_catalogo_write ON %I FOR INSERT WITH CHECK (app_is_admin())', t);
    EXECUTE format('CREATE POLICY p_catalogo_upd   ON %I FOR UPDATE USING (app_is_admin()) WITH CHECK (app_is_admin())', t);
    EXECUTE format('CREATE POLICY p_catalogo_del   ON %I FOR DELETE USING (app_is_admin())', t);
  END LOOP;
END $$;

-- ----------------------------------------------------------------------------
-- 5. Tablas técnicas sin company_id directo
-- ----------------------------------------------------------------------------
-- refresh/reset tokens: acceso restringido vía el user_id de la sesión de app;
-- se protegen por unión con users (el rol app igualmente los necesita completos
-- para login, donde aún no hay tenant en contexto) → sin RLS, pero sin GRANT a
-- otros roles y con token_hash (nunca el token en claro).

-- bitacora_cambios: sólo lectura para el admin; inserción vía triggers
ALTER TABLE bitacora_cambios ENABLE ROW LEVEL SECURITY;
CREATE POLICY p_bitacora_read ON bitacora_cambios
  FOR SELECT USING (app_is_admin() OR company_id = app_company_id());
CREATE POLICY p_bitacora_ins ON bitacora_cambios
  FOR INSERT WITH CHECK (true);   -- los triggers insertan en contexto del request
-- Nota: sin FORCE aquí; los triggers corren como dueño de la tabla.

-- ia_hallazgos y sync_logs heredan el tenant por su FK; se protegen igual:
-- (ia_hallazgos no tiene company_id: se restringe vía review)
ALTER TABLE ia_hallazgos ENABLE ROW LEVEL SECURITY;
ALTER TABLE ia_hallazgos FORCE ROW LEVEL SECURITY;
CREATE POLICY p_hallazgos ON ia_hallazgos
  FOR ALL
  USING (EXISTS (SELECT 1 FROM ia_reviews r WHERE r.id = review_id
                 AND (app_is_admin() OR r.company_id = app_company_id())))
  WITH CHECK (EXISTS (SELECT 1 FROM ia_reviews r WHERE r.id = review_id
                 AND (app_is_admin() OR r.company_id = app_company_id())));
