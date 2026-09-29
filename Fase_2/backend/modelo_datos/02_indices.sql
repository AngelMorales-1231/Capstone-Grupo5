-- ============================================================================
-- ACREDITTIA — 02_indices.sql
-- Índices de rendimiento. Supuesto: POCO tráfico de escritura y consultas
-- de lectura frecuentes (listados filtrados, dashboard, cron de vencimientos),
-- por lo que se indexa con generosidad: el costo de mantención es marginal.
-- ============================================================================

-- --- Autenticación ---
CREATE INDEX ix_users_company        ON users (company_id);
CREATE INDEX ix_refresh_user         ON refresh_tokens (user_id);
CREATE INDEX ix_refresh_vigentes     ON refresh_tokens (expires_at) WHERE revoked_at IS NULL;
CREATE INDEX ix_pwdreset_user        ON password_reset_tokens (user_id);

-- --- Empresas: cola de aprobación del admin ---
CREATE INDEX ix_companies_pendientes ON companies (created_at) WHERE status = 'pending';

-- --- Catálogos ---
CREATE INDEX ix_fplat_faena          ON faena_plataformas (faena_id);
CREATE INDEX ix_reqtpl_ambito        ON requisito_templates (ambito, faena_id) WHERE activo;
CREATE INDEX ix_provcat_categoria    ON proveedores_catalogo (categoria, faena_id);

-- --- Contratos y sujetos ---
CREATE INDEX ix_contratos_company    ON contratos (company_id, estado);
CREATE INDEX ix_contratos_faena      ON contratos (faena_id);
CREATE INDEX ix_sujetos_company      ON sujetos (company_id, tipo, estado);
CREATE INDEX ix_sujetos_contrato     ON sujetos (contrato_id, tipo);
-- Unicidad de identidad activa por contrato (excluye bajas para permitir re-ingreso)
CREATE UNIQUE INDEX ux_sujetos_rut_activo     ON sujetos (contrato_id, rut)     WHERE tipo = 'trabajador' AND estado <> 'baja';
CREATE UNIQUE INDEX ux_sujetos_patente_activa ON sujetos (contrato_id, patente) WHERE tipo = 'equipo' AND estado <> 'baja';
-- Búsqueda con similitud (search de la API: nombre, RUT, patente)
CREATE INDEX ix_sujetos_nombre_trgm  ON sujetos USING gin (nombre gin_trgm_ops);
CREATE INDEX ix_sujetos_rut_trgm     ON sujetos USING gin (rut gin_trgm_ops)     WHERE rut IS NOT NULL;
CREATE INDEX ix_sujetos_patente_trgm ON sujetos USING gin (patente gin_trgm_ops) WHERE patente IS NOT NULL;

-- --- Documentos: núcleo del cron de vencimientos y los checklists ---
CREATE INDEX ix_docs_sujeto          ON documentos (sujeto_id)   WHERE sujeto_id IS NOT NULL;
CREATE INDEX ix_docs_contrato        ON documentos (contrato_id) WHERE contrato_id IS NOT NULL;
CREATE INDEX ix_docs_company_estado  ON documentos (company_id, estado_calc);
-- El cron sólo recorre documentos con fecha de vencimiento
CREATE INDEX ix_docs_vencimiento     ON documentos (vence) WHERE vence IS NOT NULL;
CREATE INDEX ix_docs_emsipor         ON documentos (sujeto_id) WHERE es_emsipor;
CREATE INDEX ix_archivos_documento   ON documento_archivos (documento_id);
CREATE INDEX ix_lim_company          ON licencias_internas (company_id, estado);

-- --- IA ---
CREATE INDEX ix_iarev_company        ON ia_reviews (company_id, created_at DESC);
CREATE INDEX ix_iarev_archivo        ON ia_reviews (archivo_id) WHERE archivo_id IS NOT NULL;
CREATE INDEX ix_iarev_cola           ON ia_reviews (created_at) WHERE status IN ('queued','processing');
CREATE INDEX ix_iahall_review        ON ia_hallazgos (review_id);

-- --- Alertas: la bandeja consulta casi siempre no-leídas / activas ---
CREATE INDEX ix_alertas_bandeja      ON alertas (company_id, created_at DESC) WHERE resuelta_at IS NULL;
CREATE INDEX ix_alertas_noleidas     ON alertas (company_id) WHERE leida_at IS NULL;
CREATE INDEX ix_alertas_severidad    ON alertas (company_id, severidad);
CREATE INDEX ix_alertas_documento    ON alertas (documento_id) WHERE documento_id IS NOT NULL;
CREATE INDEX ix_alertas_sujeto       ON alertas (sujeto_id)    WHERE sujeto_id IS NOT NULL;
CREATE INDEX ix_alertas_contrato     ON alertas (contrato_id)  WHERE contrato_id IS NOT NULL;

-- --- Actividad: append-only con lecturas por empresa y rango temporal.
-- BRIN sobre created_at: minúsculo y suficiente porque la tabla crece en orden.
CREATE INDEX ix_actividad_company    ON actividad (company_id, id DESC);
CREATE INDEX ix_actividad_brin       ON actividad USING brin (created_at);
CREATE INDEX ix_actividad_entidad    ON actividad (entidad_tipo, entidad_id) WHERE entidad_id IS NOT NULL;

-- --- Calendario ---
CREATE INDEX ix_eventos_company_fecha ON eventos_calendario (company_id, fecha);

-- --- Reportes e integraciones ---
CREATE INDEX ix_reportes_company     ON reportes (company_id, created_at DESC);
CREATE INDEX ix_reportes_cola        ON reportes (created_at) WHERE status IN ('queued','processing');
CREATE INDEX ix_repprog_activos      ON reportes_programados (company_id) WHERE activo;
CREATE INDEX ix_synclogs_integracion ON sync_logs (integracion_id, started_at DESC);
CREATE INDEX ix_synclogs_company     ON sync_logs (company_id);

-- --- Facturación ---
CREATE INDEX ix_facturas_suscripcion ON facturas (suscripcion_id);
CREATE INDEX ix_facturas_company     ON facturas (company_id, estado);
CREATE INDEX ix_notifpref_company    ON notificacion_preferencias (company_id);

-- --- Bitácora técnica ---
CREATE INDEX ix_bitacora_registro    ON bitacora_cambios (tabla, registro_id);
CREATE INDEX ix_bitacora_brin        ON bitacora_cambios USING brin (created_at);
