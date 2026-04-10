-- Recompute ways.safety_factor for edges inside a Dublin bounding box using:
--   - cctv_cameras (camera_id, cctv_pt), ~75 m
--   - street_lights (light_id, light_pt), ~50 m
--   - crime via nearest garda_stations.location within ~5 km + crime_statistics totals
--
-- Caps: CCTV count / 10, lights / 5 (documented project defaults).
-- Formula: safe_score = clamp(0.35*cctv_norm + 0.35*light_norm - 0.30*crime_norm, 0, 1)
--          safety_factor = clamp(1.7 - safe_score, 0.7, 1.7)  (lower = safer for routing)
--
-- Prerequisites: migrations 003 (GiST) recommended; feature tables populated.
--
--   psql "$POSTGIS_DATABASE_URL" -v ON_ERROR_STOP=1 -f scripts/sql/update_ways_safety_factor_dublin.sql

BEGIN;

CREATE TEMP TABLE way_scores ON COMMIT DROP AS
WITH dublin_ways AS (
    SELECT gid, ST_Centroid(geometry) AS pt
    FROM ways
    WHERE geometry && ST_MakeEnvelope(-6.5, 53.2, -6.0, 53.6, 4326)
),
raw_counts AS (
    SELECT
        w.gid,
        COUNT(DISTINCT c.camera_id) AS cctv_cnt,
        COUNT(DISTINCT l.light_id) AS light_cnt
    FROM dublin_ways w
    LEFT JOIN cctv_cameras c
        ON ST_DWithin(w.pt, c.cctv_pt, 0.00067)
    LEFT JOIN street_lights l
        ON ST_DWithin(w.pt, l.light_pt, 0.00045)
    GROUP BY w.gid
),
station_crime AS (
    SELECT station_name, SUM(incident_count)::double precision AS total_crime
    FROM crime_statistics
    GROUP BY station_name
),
crime_cap AS (
    SELECT GREATEST(1.0::double precision, COALESCE(MAX(total_crime), 1.0)) AS max_crime
    FROM station_crime
),
way_crime AS (
    SELECT DISTINCT ON (w.gid)
        w.gid,
        COALESCE(sc.total_crime, 0.0)::double precision AS crime_val
    FROM dublin_ways w
    INNER JOIN garda_stations gs ON ST_DWithin(w.pt, gs.location, 0.05)
    LEFT JOIN station_crime sc USING (station_name)
    ORDER BY w.gid, w.pt <-> gs.location
)
SELECT
    rc.gid,
    LEAST(rc.cctv_cnt::double precision, 10.0) / 10.0 AS cctv_norm,
    LEAST(rc.light_cnt::double precision, 5.0) / 5.0 AS light_norm,
    LEAST(
        COALESCE(wc.crime_val, 0.0)::double precision / NULLIF(cc.max_crime, 0),
        1.0
    ) AS crime_norm
FROM raw_counts rc
CROSS JOIN crime_cap cc
LEFT JOIN way_crime wc ON wc.gid = rc.gid;

UPDATE ways w
SET safety_factor = GREATEST(0.7, LEAST(1.7,
    1.7 - 1.0 * GREATEST(0.0, LEAST(1.0,
        0.35 * s.cctv_norm
        + 0.35 * s.light_norm
        - 0.30 * s.crime_norm
    ))
))
FROM way_scores s
WHERE w.gid = s.gid;

COMMIT;
