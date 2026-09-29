-- ============================================================================
-- ACREDITTIA — 05_verificacion.sql
-- Verificación de integridad estructural y lógica.
-- Uso: ejecutar completo (psql -f). Cada bloque devuelve filas SOLO si hay
-- problemas; una ejecución sana no devuelve resultados (salvo el resumen final).
-- Recomendado: correrlo tras cada migración y semanalmente (job programado).
-- ============================================================================

\echo '=== 1. Tablas multi-tenant sin RLS habilitado (esperado: 0 filas) ==='
SELECT c.relname AS tabla_sin_rls
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN information_schema.columns col
  ON col.table_name = c.relname AND col.table_schema = 'public' AND col.column_name = 'company_id'
WHERE n.nspname = 'public' AND c.relkind = 'r' AND NOT c.relrowsecurity;

\echo '=== 2. Foreign keys sin índice de soporte (esperado: 0 filas) ==='
SELECT conrelid::regclass AS tabla, conname AS fk,
       pg_get_constraintdef(oid) AS definicion
FROM pg_constraint pc
WHERE contype = 'f'
  AND connamespace = 'public'::regnamespace
  AND NOT EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indrelid = pc.conrelid
      AND (i.indkey::int2[])[0:cardinality(pc.conkey)-1] @> pc.conkey::int2[]
  );

\echo '=== 3. Constraints NOT VALID pendientes de validar (esperado: 0 filas) ==='
SELECT conrelid::regclass AS tabla, conname
FROM pg_constraint
WHERE connamespace = 'public'::regnamespace AND NOT convalidated;

\echo '=== 4. Documentos: estado_calc inconsistente con vence/estado (esperado: 0) ==='
SELECT id, estado, estado_calc, vence
FROM documentos
WHERE estado_calc <> CASE
        WHEN estado = 'falta' THEN 'falta'::doc_estado_calc
        WHEN vence IS NULL THEN 'ok'::doc_estado_calc
        WHEN vence < CURRENT_DATE THEN 'venc'::doc_estado_calc
        WHEN vence <= CURRENT_DATE + 30 THEN 'porvenc'::doc_estado_calc
        ELSE 'ok'::doc_estado_calc
      END;

\echo '=== 5. Cruce de tenants: documento con empresa distinta a su dueño (esperado: 0) ==='
SELECT d.id, d.company_id AS doc_company, s.company_id AS sujeto_company
FROM documentos d JOIN sujetos s ON s.id = d.sujeto_id
WHERE d.company_id <> s.company_id
UNION ALL
SELECT d.id, d.company_id, c.company_id
FROM documentos d JOIN contratos c ON c.id = d.contrato_id
WHERE d.company_id <> c.company_id;

\echo '=== 6. Sujetos: empresa distinta a la del contrato (esperado: 0) ==='
SELECT s.id, s.company_id, c.company_id AS contrato_company
FROM sujetos s JOIN contratos c ON c.id = s.contrato_id
WHERE s.company_id <> c.company_id;

\echo '=== 7. Conductores sin licencia interna / LIM huérfana (esperado: 0) ==='
SELECT s.id AS conductor_sin_lim, s.nombre
FROM sujetos s
LEFT JOIN licencias_internas l ON l.sujeto_id = s.id
WHERE s.tipo = 'trabajador' AND s.es_conductor AND s.estado <> 'baja' AND l.id IS NULL
UNION ALL
SELECT l.id, 'LIM de sujeto no conductor'
FROM licencias_internas l JOIN sujetos s ON s.id = l.sujeto_id
WHERE NOT s.es_conductor;

\echo '=== 8. EMSIPOR: estado derivado desincronizado (esperado: 0) ==='
WITH avance AS (
  SELECT sujeto_id,
         count(*) FILTER (WHERE obligatorio) AS oblig,
         count(*) FILTER (WHERE obligatorio AND estado = 'ok') AS ok
  FROM documentos WHERE es_emsipor GROUP BY sujeto_id
)
SELECT l.sujeto_id, l.emsipor_estado, a.ok, a.oblig
FROM licencias_internas l JOIN avance a ON a.sujeto_id = l.sujeto_id
WHERE l.emsipor_estado <> CASE
        WHEN a.ok = 0 THEN 'pendiente'::emsipor_estado
        WHEN a.ok < a.oblig THEN 'parcial'::emsipor_estado
        ELSE 'aprobado'::emsipor_estado END;

\echo '=== 9. Duplicados de identidad activos (esperado: 0; refuerza índices únicos parciales) ==='
SELECT contrato_id, rut, count(*)
FROM sujetos WHERE tipo = 'trabajador' AND estado <> 'baja'
GROUP BY contrato_id, rut HAVING count(*) > 1
UNION ALL
SELECT contrato_id, patente, count(*)
FROM sujetos WHERE tipo = 'equipo' AND estado <> 'baja'
GROUP BY contrato_id, patente HAVING count(*) > 1;

\echo '=== 10. Archivos sin blob_path o reviews colgadas > 1 hora (esperado: 0) ==='
SELECT id::text AS problema, 'archivo sin blob_path' AS detalle
FROM documento_archivos WHERE blob_path IS NULL OR blob_path = ''
UNION ALL
SELECT id::text, 'ia_review en processing hace más de 1 hora'
FROM ia_reviews
WHERE status = 'processing' AND started_at < now() - interval '1 hour';

\echo '=== 11. Alertas resueltas sin fecha / leídas en el futuro (esperado: 0) ==='
SELECT id, estado, leida_at, resuelta_at
FROM alertas
WHERE (estado = 'resuelta' AND resuelta_at IS NULL)
   OR (leida_at IS NOT NULL AND leida_at > now())
   OR (resuelta_at IS NOT NULL AND resuelta_at > now());

\echo '=== 12. Suscripciones: más de una activa/trial por empresa (esperado: 0) ==='
SELECT company_id, count(*)
FROM suscripciones WHERE estado IN ('trial','activa')
GROUP BY company_id HAVING count(*) > 1;

\echo '=== 13. Índices inválidos o duplicados (esperado: 0) ==='
SELECT indexrelid::regclass AS indice_invalido
FROM pg_index WHERE NOT indisvalid;

\echo '=== 14. Salud física: bloat aproximado y última limpieza (informativo) ==='
SELECT relname,
       n_live_tup, n_dead_tup,
       CASE WHEN n_live_tup > 0
            THEN round(100.0 * n_dead_tup / n_live_tup, 1) END AS pct_muertas,
       last_autovacuum, last_autoanalyze
FROM pg_stat_user_tables
WHERE n_dead_tup > 1000
ORDER BY n_dead_tup DESC;

\echo '=== 15. Resumen de volúmenes (informativo) ==='
SELECT relname AS tabla,
       to_char(n_live_tup, 'FM999G999G999') AS filas_aprox,
       pg_size_pretty(pg_total_relation_size(relid)) AS tamano_total
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC
LIMIT 20;

-- ============================================================================
-- Verificación física profunda (opcional, requiere extensión amcheck):
--   CREATE EXTENSION IF NOT EXISTS amcheck;
--   SELECT bt_index_check(indexrelid) FROM pg_index i
--     JOIN pg_class c ON c.oid = i.indexrelid
--     JOIN pg_namespace n ON n.oid = c.relnamespace
--   WHERE n.nspname = 'public' AND i.indisvalid;
-- En Azure Flexible Server amcheck está disponible vía azure.extensions.
-- ============================================================================
