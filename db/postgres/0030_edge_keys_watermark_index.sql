-- 0030_edge_keys_watermark_index.sql
-- Edge-key delta feed supporting index (Initiative 2 §6.2 review).
--
-- The /v1/edge/keys feed (gateway.edge_keys.EdgeKeyStore) pages api_keys by a keyset watermark over
-- (GREATEST(created_at, COALESCE(revoked_at, created_at)), id), filtering
--   WHERE (watermark, id) > (cursor_wm, cursor_id)
--     AND watermark <= now() - safe_lag
--   ORDER BY watermark, id
-- Without an index on that exact expression tuple every poll is a full sequential scan of api_keys
-- plus an in-memory sort, which grows with total key count and runs every proxy refresh interval.
-- This expression index makes each poll an index range scan in watermark/id order, so the feed reads
-- only the changed tail. The index expression MUST match the query's watermark expression byte for
-- byte (including COALESCE(revoked_at, created_at)) or the planner will not use it.
--
-- IF NOT EXISTS so replaying the whole migration set stays idempotent, matching the rest of db/postgres.
CREATE INDEX IF NOT EXISTS idx_api_keys_edge_watermark
    ON api_keys (GREATEST(created_at, COALESCE(revoked_at, created_at)), id);
