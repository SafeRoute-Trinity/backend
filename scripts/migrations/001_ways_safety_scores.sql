-- Migration 001: Safety scoring infrastructure
--
-- Run this against your PostGIS database ONCE before starting the service.
-- The ways_safety_scores materialized view itself is created dynamically by
-- the safety_scoring service (so feature table names can be configured via
-- env vars). This file sets up the supporting schema objects.
--
-- Usage:
--   psql "$POSTGIS_DATABASE_URL" -f scripts/migrations/001_ways_safety_scores.sql

-- ─── Schema ───────────────────────────────────────────────────────────────────
CREATE SCHEMA IF NOT EXISTS saferoute;

-- ─── Route cache ──────────────────────────────────────────────────────────────
-- Keyed by (origin, destination, weights_hash) so repeated route requests with
-- the same parameters are served without running pgRouting again.
-- TTL is controlled by expires_at; expired rows are cleaned up by the service.
CREATE TABLE IF NOT EXISTS saferoute.route_cache (
    cache_key         TEXT PRIMARY KEY,
    origin_lat        DOUBLE PRECISION NOT NULL,
    origin_lon        DOUBLE PRECISION NOT NULL,
    destination_lat   DOUBLE PRECISION NOT NULL,
    destination_lon   DOUBLE PRECISION NOT NULL,
    weights_hash      TEXT NOT NULL DEFAULT 'default',
    response_payload  JSONB NOT NULL,
    safety_score      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at        TIMESTAMPTZ NOT NULL,
    hit_count         INTEGER NOT NULL DEFAULT 0,
    last_hit_at       TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_route_cache_expires_at
    ON saferoute.route_cache (expires_at);

-- Progress / checkpoint row for batched ways.safety_factor updates (optional;
-- the safety_scoring service also creates this if missing).
CREATE TABLE IF NOT EXISTS saferoute.safety_ways_factor_sync_state (
    lock_id                 INTEGER PRIMARY KEY DEFAULT 1 CHECK (lock_id = 1),
    last_processed_gid      BIGINT NOT NULL DEFAULT 0,
    last_run_started_at     TIMESTAMPTZ,
    last_batch_at           TIMESTAMPTZ,
    batches_in_run          INTEGER NOT NULL DEFAULT 0,
    ways_updated_in_run     BIGINT NOT NULL DEFAULT 0
);
INSERT INTO saferoute.safety_ways_factor_sync_state (lock_id) VALUES (1)
    ON CONFLICT (lock_id) DO NOTHING;

-- ─── User safety weights persistence ─────────────────────────────────────────
-- Stores each user's factor preferences so the weights endpoint has a backing
-- store. The routing service reads these at route-calculation time.
CREATE TABLE IF NOT EXISTS saferoute.user_safety_weights (
    user_id            TEXT PRIMARY KEY,
    cctv_coverage      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    street_lighting    DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    business_activity  DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    crime_rate         DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    pedestrian_traffic DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ─── Spatial indexes on external feature tables ───────────────────────────────
-- Uncomment and run once your feature tables are populated.
-- Adjust table names to match your actual schema if different.
--
-- CREATE INDEX IF NOT EXISTS idx_street_lights_geom
--     ON street_lights USING GIST (geometry);
--
-- CREATE INDEX IF NOT EXISTS idx_cctv_cameras_geom
--     ON cctv_cameras USING GIST (geometry);
--
-- CREATE INDEX IF NOT EXISTS idx_garda_stations_geom
--     ON garda_stations USING GIST (geometry);
