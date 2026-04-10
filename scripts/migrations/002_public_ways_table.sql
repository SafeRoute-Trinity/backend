-- Migration 002: public.ways — routing graph edge table for PostGIS / pgRouting
--
-- Derived from:
--   - PostGIS Schema from WhatsApp.sql (columns + indexes)
--   - WhatsApp Ways.sql / pg_dump (COPY column order)
--   - \\d ways screenshot (gid sequence + PK + safety_factor NOT NULL)
--
-- Run AFTER PostGIS is enabled on the database, e.g.:
--   CREATE EXTENSION IF NOT EXISTS postgis;
--   CREATE EXTENSION IF NOT EXISTS pgrouting;
--
-- If you already have objects that reference public.ways (e.g. materialized views),
-- drop them first or omit the DROP below and adjust manually.
--
-- After loading rows from a dump, reset the sequence:
--   SELECT setval(pg_get_serial_sequence('public.ways', 'gid'),
--                 COALESCE((SELECT MAX(gid) FROM public.ways), 1));

CREATE EXTENSION IF NOT EXISTS postgis;
-- pgRouting is optional (many PostGIS images omit it). Install on the server, then:
--   CREATE EXTENSION IF NOT EXISTS pgrouting;

-- Optional: break dependents (matviews, FKs). Comment out if unsafe.
DROP TABLE IF EXISTS public.ways CASCADE;

DROP SEQUENCE IF EXISTS public.ways_gid_seq CASCADE;

CREATE SEQUENCE public.ways_gid_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;

CREATE TABLE public.ways (
    gid bigint NOT NULL DEFAULT nextval('public.ways_gid_seq'::regclass),
    u bigint,
    v bigint,
    key bigint,
    osmid text,
    length double precision,
    geometry public.geometry(LineString, 4326),
    name text,
    highway text,
    oneway boolean,
    source bigint,
    target bigint,
    safety_factor double precision NOT NULL DEFAULT 1.0
);

ALTER SEQUENCE public.ways_gid_seq OWNED BY public.ways.gid;

ALTER TABLE ONLY public.ways
    ADD CONSTRAINT ways_pkey PRIMARY KEY (gid);

-- Match PostGIS Schema from WhatsApp.sql (two GiST indexes on geometry — redundant;
-- you may drop one later for space.)
CREATE INDEX idx_ways_geometry ON public.ways USING gist (geometry);
CREATE INDEX ix_public_ways_gid ON public.ways USING btree (gid);
CREATE INDEX ways_geom_idx ON public.ways USING gist (geometry);
CREATE INDEX ways_source_idx ON public.ways USING btree (source);
CREATE INDEX ways_target_idx ON public.ways USING btree (target);
