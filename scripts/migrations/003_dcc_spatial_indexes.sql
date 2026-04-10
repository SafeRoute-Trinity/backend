-- Migration 003: GiST indexes for DCC feature layers (CCTV, lights, Garda).
-- Speeds up ST_DWithin / KNN when updating ways.safety_factor.
--
-- Requires: PostGIS, tables cctv_cameras(cctv_pt), street_lights(light_pt),
--           garda_stations(location).
--
--   psql "$POSTGIS_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/migrations/003_dcc_spatial_indexes.sql

CREATE EXTENSION IF NOT EXISTS postgis;

CREATE INDEX IF NOT EXISTS idx_cctv_pt ON public.cctv_cameras USING GIST (cctv_pt);
CREATE INDEX IF NOT EXISTS idx_light_pt ON public.street_lights USING GIST (light_pt);
CREATE INDEX IF NOT EXISTS idx_garda_location ON public.garda_stations USING GIST (location);
