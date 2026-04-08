#!/usr/bin/env python3
"""
Compute and update ways.safety_factor using a composite safety model.

Formula
-------
For each road edge (ways.gid) we compute a raw composite safety score S ∈ [0, 1]
(1 = perfectly safe) from four data sources, then convert to safety_factor:

    safety_factor = CLAMP(1.6 − 1.8 × S,  0.5,  2.0)

    S = 0.40·Sc + 0.30·Sv + 0.20·Sg + 0.10·Sl

Sub-scores (all 0–1, 1 = safest):
  Sc  Crime score        — inverse-normalised incident_count of the nearest Garda
                           station.  High crime area → Sc near 0.
  Sv  CCTV score         — exponential saturation over cameras within 150 m.
                           Sv = 1 − exp(−count / 2)
  Sg  Garda-station score — proximity × station quality.
                           proximity = max(0, 1 − dist_m / 800)
                           quality   = 1 − normalised_crime_at_nearest_station
                           Sg = proximity × quality
  Sl  Street-light score — exponential saturation over lights within 100 m.
                           Sl = 1 − exp(−count / 5)

Routing effect (routing_service)
---------------------------------
  cost per edge = length × POWER(CLAMP(safety_factor, 0.5, 50), 0.65)
  safety_factor < 1.0  → preferred (lower cost)
  safety_factor = 1.0  → neutral
  safety_factor > 1.0  → penalised (higher cost, router avoids)

Usage
-----
  python3 scripts/compute_safety_factors.py [options]

  --host       DB host   (default: 127.0.0.1)
  --port       DB port   (default: 5432)
  --db         DB name   (default: saferoute_geo)
  --user       DB user   (default: saferoute)
  --password   DB pass   (default: from env DATABASE_GEO_PASSWORD or DATABASE_URL)
  --batch-size Edges per batch (default: 5000)
  --dry-run    Print computed scores for a sample of 20 edges; do not UPDATE.
  --workers    Parallel DB connections for batch queries (default: 4)
"""

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Compute composite safety factors and update ways table."
    )
    p.add_argument("--host", default=None, help="DB host (overrides env)")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--db", default=None)
    p.add_argument("--user", default=None)
    p.add_argument("--password", default=None)
    p.add_argument("--batch-size", type=int, default=5000, dest="batch_size")
    p.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Compute scores for 20 edges and print; do not UPDATE.",
    )
    return p.parse_args()


def resolve_conn_params(args: argparse.Namespace) -> dict:
    """
    Priority: CLI args > DATABASE_GEO_URL env > DATABASE_URL env > defaults.
    The geo database is saferoute_geo; the app DATABASE_URL points to the OLTP DB.
    """
    backend_env = Path(__file__).resolve().parents[1] / ".env"
    if backend_env.exists():
        load_dotenv(backend_env)

    # Try to extract from a dedicated geo URL first
    geo_url = os.getenv("DATABASE_GEO_URL", "")
    app_url = os.getenv("DATABASE_URL", "").strip('"')

    defaults = {
        "host": "127.0.0.1",
        "port": 5432,
        "db": "saferoute_geo",
        "user": "saferoute",
        "password": "",
    }

    if geo_url:
        u = urlparse(geo_url)
        defaults.update(
            {
                "host": u.hostname or defaults["host"],
                "port": u.port or defaults["port"],
                "db": (u.path or "/saferoute_geo").lstrip("/"),
                "user": u.username or defaults["user"],
                "password": u.password or defaults["password"],
            }
        )
    elif app_url:
        u = urlparse(app_url)
        # Reuse host/port/creds from the app DB but override db name
        defaults.update(
            {
                "host": u.hostname or defaults["host"],
                "port": u.port or defaults["port"],
                "user": u.username or defaults["user"],
                "password": u.password or defaults["password"],
            }
        )
        # db stays saferoute_geo unless overridden

    # CLI overrides win
    return {
        "host": args.host or defaults["host"],
        "port": args.port or defaults["port"],
        "dbname": args.db or defaults["db"],
        "user": args.user or defaults["user"],
        "password": args.password or defaults["password"],
        "sslmode": "disable",
    }


# ---------------------------------------------------------------------------
# SQL fragments
# ---------------------------------------------------------------------------

# Fetch crime min/max for normalization across all stations with crime data
CRIME_BOUNDS_SQL = """
SELECT
    MIN(incident_count)::float AS min_crime,
    MAX(incident_count)::float AS max_crime
FROM crime_statistics
"""

# Total edge count for progress reporting
TOTAL_WAYS_SQL = "SELECT COUNT(*) FROM ways"

# Keyset-paginated composite safety score.
# %(after_gid)s = last gid processed (0 to start), %(limit)s = batch size.
# Uses the btree index on gid for O(log n) seek — no expensive OFFSET.
BATCH_SCORE_SQL = """
WITH
crime_bounds AS (
    SELECT
        MIN(incident_count)::float AS min_crime,
        MAX(incident_count)::float AS max_crime
    FROM crime_statistics
),
edge_midpoints AS (
    SELECT
        w.gid,
        ST_LineInterpolatePoint(w.geometry, 0.5) AS midpoint
    FROM ways w
    WHERE w.gid > %(after_gid)s
    ORDER BY w.gid
    LIMIT %(limit)s
),
nearest_garda AS (
    -- KNN order (geometry <->) uses GiST index; multiply by ~111320 for approx metres
    SELECT DISTINCT ON (em.gid)
        em.gid,
        cs.incident_count                                   AS gs_crime,
        ST_Distance(em.midpoint, gs.location) * 111320.0   AS dist_m
    FROM edge_midpoints em
    CROSS JOIN garda_stations gs
    JOIN  crime_statistics cs ON cs.station_name = gs.station_name
    ORDER BY em.gid, em.midpoint <-> gs.location
),
cctv_counts AS (
    -- 150 m ≈ 0.00135°; DWithin on geometry uses idx_cctv_pt GiST index
    SELECT
        em.gid,
        COUNT(c.camera_id)::float AS cnt
    FROM edge_midpoints em
    LEFT JOIN cctv_cameras c
           ON ST_DWithin(em.midpoint, c.cctv_pt, 0.00135)
    GROUP BY em.gid
),
light_counts AS (
    -- 100 m ≈ 0.0009°; DWithin on geometry uses idx_light_pt GiST index
    SELECT
        em.gid,
        COUNT(sl.light_id)::float AS cnt
    FROM edge_midpoints em
    LEFT JOIN street_lights sl
           ON ST_DWithin(em.midpoint, sl.light_pt, 0.0009)
    GROUP BY em.gid
),
scores AS (
    SELECT
        em.gid,
        -- Sc: crime score (0=most crime, 1=least crime)
        1.0 - LEAST(1.0,
            CASE WHEN (cb.max_crime - cb.min_crime) = 0 THEN 0.5
                 ELSE (ng.gs_crime - cb.min_crime) / (cb.max_crime - cb.min_crime)
            END
        ) AS sc,
        -- Sv: CCTV score (exponential saturation, half-point at 2 cameras)
        1.0 - EXP(-COALESCE(cc.cnt, 0) / 2.0) AS sv,
        -- Sg: garda proximity × station quality
        GREATEST(0.0,
            1.0 - LEAST(ng.dist_m, 800.0) / 800.0
        ) * (
            1.0 - LEAST(1.0,
                CASE WHEN (cb.max_crime - cb.min_crime) = 0 THEN 0.5
                     ELSE (ng.gs_crime - cb.min_crime) / (cb.max_crime - cb.min_crime)
                END
            )
        ) AS sg,
        -- Sl: street-light score (exponential saturation, half-point at 5 lights)
        1.0 - EXP(-COALESCE(lc.cnt, 0) / 5.0) AS sl
    FROM edge_midpoints  em
    CROSS JOIN crime_bounds cb
    JOIN  nearest_garda  ng ON ng.gid = em.gid
    LEFT JOIN cctv_counts   cc ON cc.gid = em.gid
    LEFT JOIN light_counts  lc ON lc.gid = em.gid
)
SELECT
    gid,
    sc, sv, sg, sl,
    -- Weighted composite S
    (0.40 * sc + 0.30 * sv + 0.20 * sg + 0.10 * sl) AS s,
    -- Final safety_factor: 1.0 is neutral, <1 = safer, >1 = more dangerous
    GREATEST(0.5, LEAST(2.0,
        1.6 - 1.8 * (0.40 * sc + 0.30 * sv + 0.20 * sg + 0.10 * sl)
    )) AS safety_factor
FROM scores
ORDER BY gid
"""

# Batch UPDATE using a VALUES list
BATCH_UPDATE_SQL = """
UPDATE ways AS w
SET safety_factor = v.sf
FROM (VALUES %s) AS v(gid, sf)
WHERE w.gid = v.gid::bigint
"""


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    params = resolve_conn_params(args)

    print(f"Connecting to {params['host']}:{params['port']}/{params['dbname']} …", flush=True)
    conn = psycopg2.connect(**params)
    conn.autocommit = False

    try:
        with conn.cursor() as cur:
            cur.execute(CRIME_BOUNDS_SQL)
            row = cur.fetchone()
            min_crime, max_crime = row[0], row[1]
            crime_range = max_crime - min_crime
            print(
                f"Crime bounds: min={min_crime:.0f}  max={max_crime:.0f}  "
                f"range={crime_range:.0f}  (normalised across {41} stations)",
                flush=True,
            )

            cur.execute(TOTAL_WAYS_SQL)
            total_ways = cur.fetchone()[0]
            print(f"Total edges to process: {total_ways:,}", flush=True)

        # ── DRY RUN ──────────────────────────────────────────────────────────
        if args.dry_run:
            return _dry_run(conn, total_ways)

        # ── FULL RUN ─────────────────────────────────────────────────────────
        return _full_run(conn, total_ways, args.batch_size)

    finally:
        conn.close()


def _dry_run(conn, total_ways: int) -> int:
    """Compute scores for a sample of 20 edges and print them without updating."""
    print("\n[DRY RUN] Sampling 20 edges from start of network …\n")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(BATCH_SCORE_SQL, {"after_gid": 0, "limit": 20})
        rows = cur.fetchall()

    print(f"{'gid':>10}  {'Sc':>6}  {'Sv':>6}  {'Sg':>6}  {'Sl':>6}  {'S':>6}  {'sf':>6}")
    print("-" * 62)
    for r in rows:
        print(
            f"{r['gid']:>10}  {r['sc']:.4f}  {r['sv']:.4f}  {r['sg']:.4f}  "
            f"{r['sl']:.4f}  {r['s']:.4f}  {r['safety_factor']:.4f}"
        )
    print("\n[DRY RUN] No changes written to DB.")
    return 0


def _full_run(conn, total_ways: int, batch_size: int) -> int:
    """Compute and write safety_factor for all edges in batches."""
    print(f"\nProcessing {total_ways:,} edges in batches of {batch_size:,} …\n", flush=True)

    total = total_ways
    processed = 0
    batches = 0
    after_gid = 0  # keyset cursor — last gid written
    t_start = time.time()
    t_last_log = t_start

    while True:
        # Step 1 — compute scores for this batch using keyset pagination
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(BATCH_SCORE_SQL, {"after_gid": after_gid, "limit": batch_size})
            rows = cur.fetchall()

        if not rows:
            break  # no more edges

        # Step 2 — write computed safety_factor values back to ways
        update_values = [(r["gid"], r["safety_factor"]) for r in rows]
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                BATCH_UPDATE_SQL,
                update_values,
                template="(%s, %s)",
                page_size=batch_size,
            )
        conn.commit()

        after_gid = rows[-1]["gid"]  # advance keyset cursor
        processed += len(rows)
        batches += 1
        now = time.time()
        elapsed = now - t_start
        rate = processed / elapsed if elapsed > 0 else 0
        eta = (total - processed) / rate if rate > 0 else 0

        if now - t_last_log >= 5 or processed >= total:
            pct = processed / total * 100
            print(
                f"  [{pct:5.1f}%] {processed:>7,}/{total:,} edges  "
                f"{batches} batches  {rate:.0f} edges/s  ETA {eta:.0f}s",
                flush=True,
            )
            t_last_log = now

    elapsed = time.time() - t_start
    print(
        f"\nDone. {processed:,} edges updated in {elapsed:.1f}s "
        f"({processed/elapsed:.0f} edges/s).",
        flush=True,
    )

    # Post-run statistics
    with conn.cursor() as cur:
        cur.execute("""
            SELECT
                MIN(safety_factor)::numeric(6,4)  AS min_sf,
                MAX(safety_factor)::numeric(6,4)  AS max_sf,
                AVG(safety_factor)::numeric(6,4)  AS avg_sf,
                PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY safety_factor)::numeric(6,4)
                                                  AS median_sf,
                COUNT(*) FILTER (WHERE safety_factor < 1.0)   AS safer_count,
                COUNT(*) FILTER (WHERE safety_factor = 1.0)   AS neutral_count,
                COUNT(*) FILTER (WHERE safety_factor > 1.0)   AS danger_count
            FROM ways
        """)
        stats = cur.fetchone()
        print(
            f"\nPost-update ways.safety_factor stats:\n"
            f"  min={stats[0]}  max={stats[1]}  avg={stats[2]}  median={stats[3]}\n"
            f"  safer (<1.0): {stats[4]:,}  neutral (=1.0): {stats[5]:,}  "
            f"danger (>1.0): {stats[6]:,}"
        )

    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    args = parse_args()
    sys.exit(run(args))
