# Run:
# uvicorn services.safety_scoring.main:app --host 0.0.0.0 --port 20003 --reload
# Docs: http://127.0.0.1:20003/docs

import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from hashlib import md5
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import httpx
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# Add parent directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

# Load backend .env for local development convenience.
backend_env_path = Path(__file__).resolve().parents[2] / ".env"
if backend_env_path.exists():
    load_dotenv(backend_env_path)

from libs.cas_enforcer import cas_enforcer
from libs.cas_logger import Op, cas_log
from libs.cas_sync import cas_subscriber
from libs.db import DatabaseType, get_database_factory, initialize_databases
from libs.fastapi_service import ServiceAppConfig
from libs.rate_limiter import RateLimiter, default_rate_limit_config
from libs.safety_ways_factor_sync import (
    SAFETY_FACTOR_SYNC_STATE_TABLE,
    sync_ways_safety_factors_batched,
    verify_ways_matview_alignment,
)
from libs.structured_logging import setup_structured_logging
from libs.trace_context import TRACE_HEADER, get_or_create_trace_id, trace_id_var

# Initialize database factory
initialize_databases([DatabaseType.POSTGIS])

app = FastAPI()
setup_structured_logging("safety_scoring")


@app.on_event("startup")
async def _startup_cas():
    await cas_enforcer.initialize("safety_scoring")
    cas_log.attach_enforcer(cas_enforcer)
    await cas_subscriber.start()


@app.on_event("startup")
async def _startup_safety():
    global _safety_refresh_task
    await _ensure_safety_infrastructure()
    _safety_refresh_task = asyncio.create_task(_safety_refresh_loop())


@app.on_event("shutdown")
async def _shutdown_cas():
    await cas_subscriber.stop()
    await cas_enforcer.close()


@app.on_event("shutdown")
async def _shutdown_safety():
    global _safety_refresh_task
    if _safety_refresh_task and not _safety_refresh_task.done():
        _safety_refresh_task.cancel()
        try:
            await _safety_refresh_task
        except asyncio.CancelledError:
            pass


_rate_limiter = RateLimiter(default_rate_limit_config(), "safety_scoring")


@app.middleware("http")
async def _trace_middleware(request: Request, call_next):
    incoming = request.headers.get(TRACE_HEADER)
    tid = get_or_create_trace_id(incoming)
    trace_id_var.set(tid)
    response = await call_next(request)
    response.headers[TRACE_HEADER] = tid
    return response


@app.middleware("http")
async def _rate_limit_middleware(request: Request, call_next):
    rejection = await _rate_limiter.check(request)
    if rejection is not None:
        return rejection
    response = await call_next(request)
    for k, v in getattr(request.state, "rate_limit_headers", {}).items():
        response.headers[k] = v
    return response


@app.on_event("shutdown")
async def _close_rate_limiter():
    await _rate_limiter.close()


# Get database session dependency
db_factory = get_database_factory()
get_db = db_factory.get_session_dependency(DatabaseType.POSTGIS)
get_postgis_db = db_factory.get_session_dependency(DatabaseType.POSTGIS)
# Routing tuning knobs (can be overridden by env vars)
ROUTE_SUBGRAPH_EXPAND_DEGREES = float(os.getenv("ROUTE_SUBGRAPH_EXPAND_DEGREES", "0.01"))
ROUTE_SUBGRAPH_EXPAND_MAX_DEGREES = float(os.getenv("ROUTE_SUBGRAPH_EXPAND_MAX_DEGREES", "0.08"))
ROUTE_DEBUG_LOG = os.getenv("ROUTE_DEBUG_LOG", "false").lower() == "true"
GRAPHHOPPER_PROXY_SERVICE_URL = os.getenv(
    "GRAPHHOPPER_PROXY_SERVICE_URL",
    os.getenv("CH_ROUTING_SERVICE_URL", "http://127.0.0.1:20007"),
)
GRAPHHOPPER_PROXY_TIMEOUT_SECONDS = float(
    os.getenv("GRAPHHOPPER_PROXY_TIMEOUT_SECONDS", os.getenv("CH_ROUTING_TIMEOUT_SECONDS", "8"))
)
CH_FALLBACK_TO_DIJKSTRA = os.getenv("CH_FALLBACK_TO_DIJKSTRA", "true").lower() == "true"

# Safety scoring dedicated database URL
# Priority: SAFETY_SCORING_DATABASE_URL > POSTGIS_DATABASE_URL > shared get_db fallback
SAFETY_SCORING_DATABASE_URL = os.getenv("SAFETY_SCORING_DATABASE_URL") or os.getenv(
    "POSTGIS_DATABASE_URL"
)

# ── Safety score refresh ───────────────────────────────────────────────────────
SAFETY_REFRESH_INTERVAL_HOURS = int(os.getenv("SAFETY_REFRESH_INTERVAL_HOURS", "1"))
SAFETY_REFRESH_ON_STARTUP = os.getenv("SAFETY_REFRESH_ON_STARTUP", "true").lower() == "true"

# ── External feature table names (override to match your PostGIS schema) ───────
SAFETY_FEATURE_TABLE_LIGHTS = os.getenv("SAFETY_FEATURE_TABLE_LIGHTS", "street_lights")
SAFETY_FEATURE_TABLE_CCTV = os.getenv("SAFETY_FEATURE_TABLE_CCTV", "cctv_cameras")
SAFETY_FEATURE_TABLE_GARDA = os.getenv("SAFETY_FEATURE_TABLE_GARDA", "garda_stations")
# crime_statistics has no geometry — it joins to garda_stations via station_name
SAFETY_FEATURE_TABLE_CRIME = os.getenv("SAFETY_FEATURE_TABLE_CRIME", "crime_statistics")
# Geometry column names (differ per table; override if your schema uses a different name)
SAFETY_GEOM_COL_LIGHTS = os.getenv("SAFETY_GEOM_COL_LIGHTS", "light_pt")
SAFETY_GEOM_COL_CCTV = os.getenv("SAFETY_GEOM_COL_CCTV", "cctv_pt")
SAFETY_GEOM_COL_GARDA = os.getenv("SAFETY_GEOM_COL_GARDA", "location")
# Qualified name, e.g. saferoute.ways_safety_view. Override if a broken OID/type
# blocks recreating the default name (set e.g. saferoute.ways_safety_view_v2).
_raw_scores_view = os.getenv("SAFETY_SCORES_VIEW", "saferoute.ways_safety_view").strip()
if "." in _raw_scores_view:
    SAFETY_SCORES_VIEW_SCHEMA, SAFETY_SCORES_VIEW_NAME = _raw_scores_view.split(".", 1)
else:
    SAFETY_SCORES_VIEW_SCHEMA, SAFETY_SCORES_VIEW_NAME = "public", _raw_scores_view
SAFETY_SCORES_VIEW = f"{SAFETY_SCORES_VIEW_SCHEMA}.{SAFETY_SCORES_VIEW_NAME}"

# ── Spatial influence radii (metres) ─────────────────────────────────────────
SAFETY_BUFFER_LIGHTS_M = float(os.getenv("SAFETY_BUFFER_LIGHTS_M", "60"))
SAFETY_BUFFER_CCTV_M = float(os.getenv("SAFETY_BUFFER_CCTV_M", "100"))
SAFETY_BUFFER_GARDA_M = float(os.getenv("SAFETY_BUFFER_GARDA_M", "400"))
# Crime uses the same Garda station buffer (incidents are anchored to stations)
SAFETY_BUFFER_CRIME_M = float(os.getenv("SAFETY_BUFFER_CRIME_M", "400"))
# Conversion factor: metres → degrees (approximate for Ireland lat ~53°).
# Used to pass a planar ST_DWithin radius so GiST indices are used directly
# without the expensive ::geography geodesic cast on every row.
_M_TO_DEG = 1.0 / 111_000.0

# ── Composite weight distribution (should sum to 1.0) ────────────────────────
# Crime is an INVERTED factor: its contribution = w_crime * (1 - crime_density_norm)
SAFETY_WEIGHT_LIGHTS = float(os.getenv("SAFETY_WEIGHT_LIGHTS", "0.25"))
SAFETY_WEIGHT_CCTV = float(os.getenv("SAFETY_WEIGHT_CCTV", "0.25"))
SAFETY_WEIGHT_GARDA = float(os.getenv("SAFETY_WEIGHT_GARDA", "0.25"))
SAFETY_WEIGHT_CRIME = float(os.getenv("SAFETY_WEIGHT_CRIME", "0.25"))

# ── safety_factor semantics ───────────────────────────────────────────────────
# sf = 1.0  → neutral / grey   (default for all ways; no feature data)
# sf < 1.0  → safe   / green   (well-covered by lights, CCTV, Garda)
# sf > 1.0  → danger / red     (high crime, poor coverage)
#
# SAFETY_FACTOR_MIN:          floor for safe segments  (default 0.5)
# SAFETY_FACTOR_DANGEROUS_MAX: ceiling for dangerous segments visible in scoring (default 10.0)
# ROUTE_SAFETY_FACTOR_MAX:    pgRouting CLAMP — hard ceiling for cost expression (default 50.0)
SAFETY_FACTOR_MIN = float(os.getenv("ROUTE_SAFETY_FACTOR_MIN", "0.5"))
SAFETY_FACTOR_DANGEROUS_MAX = float(os.getenv("SAFETY_FACTOR_DANGEROUS_MAX", "10.0"))
SAFETY_FACTOR_MAX = float(os.getenv("ROUTE_SAFETY_FACTOR_MAX", "50.0"))
# The composite score at which safety_factor = 1.0 (neutral/grey).
# Lower this below 0.5 if your city's infrastructure is sparse so the
# population median lands in the neutral band instead of the danger zone.
# Calibrate by running: SELECT AVG(composite_safety_score) FROM saferoute.ways_safety_scores;
# and picking a value ~10–15 pts below that average (so most roads are neutral/slightly-safe).
SAFETY_NEUTRAL_COMPOSITE = float(os.getenv("SAFETY_NEUTRAL_COMPOSITE", "0.35"))
# Tiny stable spread on composite [0,1] per ways.gid so edges with identical
# feature-derived scores (common in uniform suburbs) do not all map to the same
# safety_factor. Set 0 to disable. Typical 0.012–0.025; routing cost uses sf^exp so
# keep this modest.
SAFETY_COMPOSITE_GID_JITTER = float(os.getenv("SAFETY_COMPOSITE_GID_JITTER", "0.018"))
# Set to "true" to DROP + recreate the materialized view on each startup.
# Flip this after any formula change, then set back to "false" once deployed.
SAFETY_SCORES_RECREATE_VIEW = os.getenv("SAFETY_SCORES_RECREATE_VIEW", "false").lower() == "true"

# ── Route cache ───────────────────────────────────────────────────────────────
SAFETY_ROUTE_CACHE_ENABLED = os.getenv("SAFETY_ROUTE_CACHE_ENABLED", "true").lower() == "true"
SAFETY_ROUTE_CACHE_TTL_SECONDS = int(os.getenv("SAFETY_ROUTE_CACHE_TTL_SECONDS", "3600"))
SAFETY_ROUTE_CACHE_TABLE = "saferoute.route_cache"

# ── Mutable module-level state ────────────────────────────────────────────────
_SAFETY_INFRA_INITIALIZED: bool = False
_SAFETY_ROUTE_CACHE_TABLE_INITIALIZED: bool = False
_safety_refresh_task: Optional[asyncio.Task] = None

_SafetyScoringSessionLocal = None
_SafetyScoringRefreshSession = None  # long-timeout session used only for mat-view refresh
_safety_scoring_engine = None
_safety_scoring_refresh_engine = None
if SAFETY_SCORING_DATABASE_URL:
    _safety_scoring_engine = create_async_engine(
        SAFETY_SCORING_DATABASE_URL,
        echo=False,
        future=True,
    )
    # Separate engine for long-running REFRESH MATERIALIZED VIEW queries.
    # asyncpg command_timeout=None disables the client-side per-command deadline;
    # we also SET statement_timeout=0 at the session level before each refresh.
    _safety_scoring_refresh_engine = create_async_engine(
        SAFETY_SCORING_DATABASE_URL,
        echo=False,
        future=True,
        connect_args={"command_timeout": None},
    )
    _SafetyScoringSessionLocal = sessionmaker(
        bind=_safety_scoring_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    _SafetyScoringRefreshSession = sessionmaker(
        bind=_safety_scoring_refresh_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )


async def get_safety_scoring_db():
    if _SafetyScoringSessionLocal is not None:
        async with _SafetyScoringSessionLocal() as session:
            yield session
        return

    async for shared_session in get_db():
        yield shared_session


# ─────────────────────────────────────────────────────────────────────────────
# Safety factor conversion helpers
# ─────────────────────────────────────────────────────────────────────────────


def _composite_to_safety_factor(composite: float) -> float:
    """
    Map composite_safety_score [0, 1] → ways.safety_factor.

    The neutral point N = SAFETY_NEUTRAL_COMPOSITE (default 0.35) maps to sf = 1.0.
    Segments above N trend green (sf < 1); segments below N trend red (sf > 1).

    Safe   zone [N, 1.0] → sf [1.0, SAFETY_FACTOR_MIN]
    Danger zone [0.0, N] → sf [1.0, SAFETY_FACTOR_DANGEROUS_MAX]

    Using a configurable N lets you calibrate per city so the population
    median lands in the neutral band rather than all roads going orange.
    Run `SELECT AVG(composite_safety_score) FROM saferoute.ways_safety_scores`
    and set SAFETY_NEUTRAL_COMPOSITE ~10–15 pts below that average.
    """
    sf_safe = SAFETY_FACTOR_MIN
    sf_danger = SAFETY_FACTOR_DANGEROUS_MAX
    N = SAFETY_NEUTRAL_COMPOSITE
    c = max(0.0, min(1.0, composite))
    if c >= N:
        # Safe zone: [N → 1.0] → sf [1.0 → sf_safe]
        span = max(1.0 - N, 1e-9)
        return round(1.0 - (c - N) / span * (1.0 - sf_safe), 4)
    else:
        # Danger zone: [0.0 → N] → sf [sf_danger → 1.0]
        return round(1.0 + (N - c) / max(N, 1e-9) * (sf_danger - 1.0), 4)


def _sf_to_score_0_100(avg_sf: float) -> float:
    """
    Convert a ways.safety_factor value to a 0–100 user-facing score.

    sf = SAFETY_FACTOR_MIN (0.5) →  100  (fully safe  / green)
    sf = 1.0                      →   50  (neutral     / grey)
    sf = SAFETY_FACTOR_DANGEROUS_MAX (10) →  0  (max danger / red)

    Piecewise-linear, sf=1.0 always maps to score 50 regardless of neutral composite.
    """
    sf_safe = SAFETY_FACTOR_MIN
    sf_danger = SAFETY_FACTOR_DANGEROUS_MAX
    if avg_sf <= 1.0:
        return round(
            max(0.0, min(100.0, 50.0 + (1.0 - avg_sf) / max(1.0 - sf_safe, 1e-9) * 50.0)), 1
        )
    else:
        return round(
            max(0.0, min(100.0, 50.0 - (avg_sf - 1.0) / max(sf_danger - 1.0, 1e-9) * 50.0)), 1
        )


# ─────────────────────────────────────────────────────────────────────────────
# Safety infrastructure helpers
# ─────────────────────────────────────────────────────────────────────────────


def _build_mat_view_sql() -> str:
    """Return CREATE MATERIALIZED VIEW SQL using the configured feature table/column names."""
    tl = SAFETY_FEATURE_TABLE_LIGHTS
    tc = SAFETY_FEATURE_TABLE_CCTV
    tg = SAFETY_FEATURE_TABLE_GARDA
    tcr = SAFETY_FEATURE_TABLE_CRIME
    gl = SAFETY_GEOM_COL_LIGHTS  # e.g. "light_pt"
    gc = SAFETY_GEOM_COL_CCTV  # e.g. "cctv_pt"
    gg = SAFETY_GEOM_COL_GARDA  # e.g. "location"
    # Convert metre buffers to degrees so ST_DWithin operates on the native
    # geometry SRID (EPSG:4326, unit = degrees). This lets the GiST index kick
    # in without any ::geography cast, making the query orders of magnitude faster.
    bl = round(SAFETY_BUFFER_LIGHTS_M * _M_TO_DEG, 7)
    bc = round(SAFETY_BUFFER_CCTV_M * _M_TO_DEG, 7)
    bg = round(SAFETY_BUFFER_GARDA_M * _M_TO_DEG, 7)
    bcr = round(SAFETY_BUFFER_CRIME_M * _M_TO_DEG, 7)
    wl, wc, wg, wcr = (
        SAFETY_WEIGHT_LIGHTS,
        SAFETY_WEIGHT_CCTV,
        SAFETY_WEIGHT_GARDA,
        SAFETY_WEIGHT_CRIME,
    )
    neutral_c = SAFETY_NEUTRAL_COMPOSITE
    comp_jit = SAFETY_COMPOSITE_GID_JITTER

    # Distance floor in degrees: ~1m in EPSG:4326 at lat 53°.
    # Prevents division-by-zero for coincident points while keeping
    # relative weighting sensible (planar degrees, not metres).
    dist_floor = round(1.0 * _M_TO_DEG, 7)

    return f"""
    CREATE MATERIALIZED VIEW {SAFETY_SCORES_VIEW} AS
    WITH
      -- AS MATERIALIZED: ensure PostgreSQL computes each CTE once and stores the
      -- result set. Without this hint (PG12+), the planner may inline a CTE that
      -- is referenced more than once (norms + final SELECT), recomputing it twice.
      light_scores AS MATERIALIZED (
        SELECT w.gid,
          COALESCE(SUM(1.0 / GREATEST(
            ST_Distance(w.geometry, sl.{gl}), {dist_floor}
          )), 0.0) AS raw_score
        FROM ways w
        LEFT JOIN {tl} sl ON ST_DWithin(w.geometry, sl.{gl}, {bl})
        GROUP BY w.gid
      ),
      cctv_scores AS MATERIALIZED (
        SELECT w.gid,
          COALESCE(SUM(1.0 / GREATEST(
            ST_Distance(w.geometry, cc.{gc}), {dist_floor}
          )), 0.0) AS raw_score
        FROM ways w
        LEFT JOIN {tc} cc ON ST_DWithin(w.geometry, cc.{gc}, {bc})
        GROUP BY w.gid
      ),
      -- Pre-aggregate crime data per station BEFORE joining to ways.
      -- Reduces the join from ways × stations × crime_rows → ways × stations.
      crime_per_station AS MATERIALIZED (
        SELECT gs.{gg} AS geom,
          COALESCE(SUM(cr.incident_count), 0)::float AS total_incidents
        FROM {tg} gs
        LEFT JOIN {tcr} cr ON cr.station_name = gs.station_name
        GROUP BY gs.{gg}
      ),
      -- Nearest Garda station per way via a correlated subquery + GiST index.
      -- Avoids the DISTINCT ON (w.gid) ORDER BY sort on 588K × N_stations rows
      -- that was generating hundreds of MB of temp files per minute.
      garda_scores AS MATERIALIZED (
        SELECT w.gid,
          COALESCE((
            SELECT 1.0 / GREATEST(ST_Distance(w.geometry, gs.{gg}), {dist_floor})
            FROM {tg} gs
            WHERE ST_DWithin(w.geometry, gs.{gg}, {bg})
            ORDER BY ST_Distance(w.geometry, gs.{gg})
            LIMIT 1
          ), 0.0) AS raw_score
        FROM ways w
      ),
      -- Crime: incidents per station joined to ways via pre-aggregated table.
      -- Avoids the three-way join explosion (ways × stations × crime_rows).
      crime_scores AS MATERIALIZED (
        SELECT w.gid,
          COALESCE(SUM(
            cps.total_incidents
            / GREATEST(ST_Distance(w.geometry, cps.geom), {dist_floor})
          ), 0.0) AS raw_score
        FROM ways w
        LEFT JOIN crime_per_station cps ON ST_DWithin(w.geometry, cps.geom, {bcr})
        GROUP BY w.gid
      ),
      -- Scalar subqueries aggregate each CTE independently, avoiding the implicit
      -- Cartesian product that `FROM ls, cs, gs, cr` would create (even for a
      -- pure MAX query, PostgreSQL does not always eliminate the cross join).
      norms AS (
        SELECT
          (SELECT NULLIF(MAX(raw_score), 0) FROM light_scores) AS light_max,
          (SELECT NULLIF(MAX(raw_score), 0) FROM cctv_scores)  AS cctv_max,
          (SELECT NULLIF(MAX(raw_score), 0) FROM garda_scores) AS garda_max,
          (SELECT NULLIF(MAX(raw_score), 0) FROM crime_scores) AS crime_max
      )
    SELECT
      w.gid,
      -- Composite safety score in [0, 1]; higher = safer.
      -- Crime is inverted: (1 - crime_norm) so high crime lowers the score.
      --
      -- If a global norm (light_max, etc.) is NULL (no feature data / all zeros),
      -- that factor is excluded and remaining weights are renormalized. Previously
      -- crime with NULL crime_max became w_crime * 1.0 for every row → identical
      -- composites and flat ways.safety_factor. When no factor has signal, use the
      -- configured neutral composite so routes stay grey (sf → 1.0 at neutral N).
      -- Per-gid jitter breaks ties: many segments share the same raw composite after
      -- normalization; routing_service still consumes the single ways.safety_factor column.
      LEAST(1.0::double precision, GREATEST(0.0::double precision,
        (
          CASE
            WHEN norms.light_max IS NULL AND norms.cctv_max IS NULL
             AND norms.garda_max IS NULL AND norms.crime_max IS NULL
            THEN {neutral_c}::double precision
            ELSE LEAST(1.0::double precision, GREATEST(0.0::double precision,
              (
                  CASE WHEN norms.light_max IS NOT NULL THEN {wl}::double precision
                       * LEAST(COALESCE(ls.raw_score / norms.light_max, 0.0::double precision), 1.0::double precision)
                       ELSE 0.0::double precision END
                + CASE WHEN norms.cctv_max IS NOT NULL THEN {wc}::double precision
                       * LEAST(COALESCE(cs.raw_score / norms.cctv_max, 0.0::double precision), 1.0::double precision)
                       ELSE 0.0::double precision END
                + CASE WHEN norms.garda_max IS NOT NULL THEN {wg}::double precision
                       * LEAST(COALESCE(gs.raw_score / norms.garda_max, 0.0::double precision), 1.0::double precision)
                       ELSE 0.0::double precision END
                + CASE WHEN norms.crime_max IS NOT NULL THEN {wcr}::double precision
                       * (1.0::double precision - LEAST(
                           COALESCE(cr.raw_score / norms.crime_max, 0.0::double precision), 1.0::double precision))
                       ELSE 0.0::double precision END
              ) / NULLIF(
                  (CASE WHEN norms.light_max IS NOT NULL THEN {wl}::double precision ELSE 0.0::double precision END)
                + (CASE WHEN norms.cctv_max  IS NOT NULL THEN {wc}::double precision ELSE 0.0::double precision END)
                + (CASE WHEN norms.garda_max IS NOT NULL THEN {wg}::double precision ELSE 0.0::double precision END)
                + (CASE WHEN norms.crime_max IS NOT NULL THEN {wcr}::double precision ELSE 0.0::double precision END),
                  0.0::double precision)
            ))
          END
        )
        + ({comp_jit}::double precision * (
            2.0::double precision
            * (MOD(ABS(hashtext(w.gid::text)), 1000001))::double precision
            / 1000000.0::double precision
            - 1.0::double precision
          ))
      )) AS composite_safety_score,
      -- Per-factor normalised scores (crime stored non-inverted; callers invert as needed)
      COALESCE(ls.raw_score / NULLIF(norms.light_max, 0),  0.0) AS light_score_norm,
      COALESCE(cs.raw_score / NULLIF(norms.cctv_max,  0),  0.0) AS cctv_score_norm,
      COALESCE(gs.raw_score / NULLIF(norms.garda_max, 0),  0.0) AS garda_score_norm,
      COALESCE(cr.raw_score / NULLIF(norms.crime_max, 0),  0.0) AS crime_score_norm,
      NOW() AS computed_at
    FROM ways w
    LEFT JOIN light_scores ls ON ls.gid = w.gid
    LEFT JOIN cctv_scores cs  ON cs.gid  = w.gid
    LEFT JOIN garda_scores gs ON gs.gid  = w.gid
    LEFT JOIN crime_scores cr ON cr.gid  = w.gid
    CROSS JOIN norms
    """


async def _ensure_safety_infrastructure() -> None:
    """Create supporting tables and the materialized view if they don't exist."""
    global _SAFETY_INFRA_INITIALIZED, _SAFETY_ROUTE_CACHE_TABLE_INITIALIZED
    if _SAFETY_INFRA_INITIALIZED:
        return
    if _SafetyScoringSessionLocal is None:
        print("Safety infrastructure: no dedicated DB session, skipping init.")
        _SAFETY_INFRA_INITIALIZED = True
        return

    try:
        async with _SafetyScoringSessionLocal() as session:
            await session.execute(text("CREATE SCHEMA IF NOT EXISTS saferoute"))

            await session.execute(text(f"""
                CREATE TABLE IF NOT EXISTS {SAFETY_ROUTE_CACHE_TABLE} (
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
                )
            """))
            await session.execute(text(f"""
                CREATE INDEX IF NOT EXISTS idx_route_cache_expires_at
                ON {SAFETY_ROUTE_CACHE_TABLE} (expires_at)
            """))

            await session.execute(text("""
                CREATE TABLE IF NOT EXISTS saferoute.user_safety_weights (
                    user_id            TEXT PRIMARY KEY,
                    cctv_coverage      DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                    street_lighting    DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                    business_activity  DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                    crime_rate         DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                    pedestrian_traffic DOUBLE PRECISION NOT NULL DEFAULT 1.0,
                    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """))

            # Mat view creation (and any forced recreation) is handled by the
            # background _refresh_safety_scores task in AUTOCOMMIT mode so it
            # doesn't block startup or run inside a transaction block.
            if SAFETY_SCORES_RECREATE_VIEW:
                print(
                    f"SAFETY_SCORES_RECREATE_VIEW=true — dropping {SAFETY_SCORES_VIEW}; "
                    "will be recreated by background refresh loop."
                )
                try:
                    await session.execute(
                        text(f"DROP MATERIALIZED VIEW IF EXISTS {SAFETY_SCORES_VIEW} CASCADE")
                    )
                except Exception:
                    await session.rollback()

            await session.commit()
            _SAFETY_INFRA_INITIALIZED = True
            _SAFETY_ROUTE_CACHE_TABLE_INITIALIZED = True
            print("Safety infrastructure initialized.")
    except Exception as e:
        print(f"Safety infrastructure init failed (non-fatal): {e}")


async def _refresh_safety_scores() -> None:
    """
    Refresh the materialized view then write the computed safety_factor back to
    ways.safety_factor so pgRouting picks it up immediately.

    Uses AUTOCOMMIT mode so REFRESH MATERIALIZED VIEW CONCURRENTLY is allowed.

    The heavy UPDATE uses batched commits (see saferoute.safety_ways_factor_sync_state)
    plus optional PostgreSQL CHECKPOINT between batches — not a single long transaction.
    statement_timeout is disabled only for REFRESH MATERIALIZED VIEW, which must run as
    one statement; batched updates use the server default per batch.
    """
    engine = _safety_scoring_refresh_engine or _safety_scoring_engine
    if engine is None:
        return
    try:
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            # Disable parallel gather so PostgreSQL uses only one worker per CTE
            # (prevents RAM multiplication on the 384Mi-limited container).
            # work_mem intentionally left at default (4MB) — temp-file spills
            # go to the 10Gi PVC which has plenty of room.
            await conn.execute(text("SET max_parallel_workers_per_gather = 0"))

            existing = await conn.execute(
                text("""
                SELECT ispopulated FROM pg_matviews
                WHERE schemaname = :sch AND matviewname = :mv
                """),
                {"sch": SAFETY_SCORES_VIEW_SCHEMA, "mv": SAFETY_SCORES_VIEW_NAME},
            )
            row = existing.first()

            if row is None:
                # View doesn't exist (or is in an orphaned/partial state from a
                # prior crash). Drop any leftover pg_class entry before creating.
                print(f"Creating and populating {SAFETY_SCORES_VIEW}…")
                await conn.execute(text("SET statement_timeout = 0"))
                try:
                    await conn.execute(
                        text(f"DROP MATERIALIZED VIEW IF EXISTS {SAFETY_SCORES_VIEW} CASCADE")
                    )
                    await conn.execute(text(_build_mat_view_sql()))
                    await conn.execute(text(f"CREATE UNIQUE INDEX ON {SAFETY_SCORES_VIEW} (gid)"))
                    print(f"Created and populated {SAFETY_SCORES_VIEW}")
                except Exception as create_err:
                    print(f"Could not create {SAFETY_SCORES_VIEW}: {create_err}")
                    return
                finally:
                    await conn.execute(text("SET statement_timeout TO DEFAULT"))
            elif not row[0]:
                # View exists but is empty (was created WITH NO DATA in a prior
                # attempt) — do a plain (non-concurrent) initial population.
                print(f"Initial population of {SAFETY_SCORES_VIEW} (non-concurrent)…")
                await conn.execute(text("SET statement_timeout = 0"))
                try:
                    await conn.execute(text(f"REFRESH MATERIALIZED VIEW {SAFETY_SCORES_VIEW}"))
                finally:
                    await conn.execute(text("SET statement_timeout TO DEFAULT"))
            else:
                # View already has data — prefer CONCURRENTLY; fall back if catalogs/indexes break.
                await conn.execute(text("SET statement_timeout = 0"))
                try:
                    try:
                        await conn.execute(
                            text(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {SAFETY_SCORES_VIEW}")
                        )
                    except Exception as conc_err:
                        print(
                            f"CONCURRENTLY refresh failed ({conc_err}); "
                            f"retrying non-concurrent REFRESH {SAFETY_SCORES_VIEW}…"
                        )
                        await conn.execute(text(f"REFRESH MATERIALIZED VIEW {SAFETY_SCORES_VIEW}"))
                finally:
                    await conn.execute(text("SET statement_timeout TO DEFAULT"))

            N = SAFETY_NEUTRAL_COMPOSITE
            updated, batches = await sync_ways_safety_factors_batched(
                conn,
                matview_qualified=SAFETY_SCORES_VIEW,
                neutral=N,
                sf_safe=SAFETY_FACTOR_MIN,
                sf_danger=SAFETY_FACTOR_DANGEROUS_MAX,
                state_table_qualified=SAFETY_FACTOR_SYNC_STATE_TABLE,
            )
            wc, mc, missing = await verify_ways_matview_alignment(conn, SAFETY_SCORES_VIEW)
            if missing > 0 or wc != mc:
                print(
                    f"Safety sync verification: ways={wc} matview={mc} ways_missing_mv={missing} "
                    f"(updated_rows={updated} batches={batches})"
                )
            else:
                print(
                    f"Safety sync verification OK: ways=matview={wc} rows, "
                    f"updated={updated} in {batches} batches"
                )

            print(f"Safety scores refreshed at {datetime.utcnow().isoformat()}Z")
    except Exception as e:
        print(f"Safety score refresh failed: {e}")


async def _safety_refresh_loop() -> None:
    """Periodic background task: refresh safety scores every N hours."""
    await asyncio.sleep(15)  # short initial delay so DB is fully ready
    if SAFETY_REFRESH_ON_STARTUP:
        await _refresh_safety_scores()
    while True:
        try:
            await asyncio.sleep(SAFETY_REFRESH_INTERVAL_HOURS * 3600)
            await _refresh_safety_scores()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Safety refresh loop error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Route cache helpers
# ─────────────────────────────────────────────────────────────────────────────


def _route_cache_key(
    start: "Coordinate", end: "Coordinate", weights: Optional["RouteSafetyWeightsInput"]
) -> str:
    weights_sig = "default"
    if weights:
        weights_sig = md5(json.dumps(weights.model_dump(), sort_keys=True).encode()).hexdigest()[:8]
    return f"{start.lat:.5f},{start.lng:.5f}:{end.lat:.5f},{end.lng:.5f}:{weights_sig}"


async def _get_cached_route(db: "AsyncSession", cache_key: str) -> Optional[Dict[str, Any]]:
    if not SAFETY_ROUTE_CACHE_ENABLED:
        return None
    try:
        result = await db.execute(
            text(f"""
                SELECT response_payload
                FROM {SAFETY_ROUTE_CACHE_TABLE}
                WHERE cache_key = :k AND expires_at > NOW()
                LIMIT 1
            """),
            {"k": cache_key},
        )
        row = result.first()
        if not row:
            return None
        await db.execute(
            text(f"""
                UPDATE {SAFETY_ROUTE_CACHE_TABLE}
                SET hit_count = hit_count + 1, last_hit_at = NOW()
                WHERE cache_key = :k
            """),
            {"k": cache_key},
        )
        await db.commit()
        payload = row.response_payload
        if isinstance(payload, str):
            payload = json.loads(payload)
        return payload if isinstance(payload, dict) else None
    except Exception as e:
        print(f"Route cache read failed: {e}")
        try:
            await db.rollback()
        except Exception:
            pass
        return None


async def _store_route_cache(
    db: "AsyncSession",
    cache_key: str,
    start: "Coordinate",
    end: "Coordinate",
    weights: Optional["RouteSafetyWeightsInput"],
    payload: Dict[str, Any],
    safety_score: float,
) -> None:
    if not SAFETY_ROUTE_CACHE_ENABLED:
        return
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=SAFETY_ROUTE_CACHE_TTL_SECONDS)
    weights_sig = (
        "default"
        if not weights
        else md5(json.dumps(weights.model_dump(), sort_keys=True).encode()).hexdigest()[:8]
    )
    try:
        await db.execute(
            text(f"""
                INSERT INTO {SAFETY_ROUTE_CACHE_TABLE} (
                    cache_key, origin_lat, origin_lon,
                    destination_lat, destination_lon,
                    weights_hash, response_payload, safety_score, expires_at
                ) VALUES (
                    :k, :olat, :olon, :dlat, :dlon,
                    :whash, CAST(:payload AS JSONB), :score, :exp
                )
                ON CONFLICT (cache_key) DO UPDATE SET
                    response_payload = EXCLUDED.response_payload,
                    safety_score     = EXCLUDED.safety_score,
                    expires_at       = EXCLUDED.expires_at,
                    hit_count        = {SAFETY_ROUTE_CACHE_TABLE}.hit_count + 1,
                    last_hit_at      = NOW()
            """),
            {
                "k": cache_key,
                "olat": start.lat,
                "olon": start.lng,
                "dlat": end.lat,
                "dlon": end.lng,
                "whash": weights_sig,
                "payload": json.dumps(payload),
                "score": safety_score,
                "exp": expires_at,
            },
        )
        await db.commit()
    except Exception as e:
        print(f"Route cache write failed: {e}")
        try:
            await db.rollback()
        except Exception:
            pass


async def get_ch_route_geojson(route_request: "RouteRequest") -> dict:
    """
    Fetch route from GraphHopper proxy and return GeoJSON-compatible payload.
    """
    try:
        _headers = {}
        tid = trace_id_var.get("")
        if tid:
            _headers[TRACE_HEADER] = tid
        async with httpx.AsyncClient(timeout=GRAPHHOPPER_PROXY_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{GRAPHHOPPER_PROXY_SERVICE_URL}/api/route",
                params={"algorithm": "ch"},
                json={
                    "start": {"lat": route_request.start.lat, "lng": route_request.start.lng},
                    "end": {"lat": route_request.end.lat, "lng": route_request.end.lng},
                },
                headers=_headers,
            )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"GraphHopper proxy request failed: {e}") from e

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"GraphHopper proxy error: status={response.status_code}, body={response.text[:200]}",
        )

    body = response.json()
    if not isinstance(body, dict) or body.get("type") != "FeatureCollection":
        raise HTTPException(status_code=502, detail="Invalid CH response format.")
    return body


# Create service configuration
service_config = ServiceAppConfig(
    service_name="safety_scoring",
    title="Safety Scoring Service",
    description="Safety scoring, factors, and weights APIs.",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files (if needed, though usually served by frontend or nginx)
# app.mount("/static", StaticFiles(directory="../frontend/static"), name="static")

# ========= Metrics =========

SERVICE_NAME = "safety_scoring"
registry = CollectorRegistry()

# Generic per-request counter (shared schema across services)
REQUEST_COUNT = Counter(
    "service_requests_total",
    "Total HTTP requests handled by the service",
    ["service", "method", "path", "http_status"],
    registry=registry,
)

# Latency histogram per path
REQUEST_LATENCY = Histogram(
    "service_request_duration_seconds",
    "Request latency in seconds",
    ["service", "path"],
    registry=registry,
)

# Business metrics for this service
SAFETY_SCORE_ROUTE_REQUESTS_TOTAL = Counter(
    "safety_score_route_requests_total",
    "Total number of safety route scoring requests",
    registry=registry,
)

SAFETY_FACTORS_QUERIES_TOTAL = Counter(
    "safety_factors_queries_total",
    "Total number of safety factors queries",
    registry=registry,
)

SAFETY_WEIGHTS_UPDATES_TOTAL = Counter(
    "safety_weights_updates_total",
    "Total number of safety weights update requests",
    registry=registry,
)


@app.middleware("http")
async def prometheus_middleware(request: Request, call_next):
    """
    Track:
    - request count
    - latency per path
    for every HTTP request handled by this service.
    """
    start = time.time()
    response = await call_next(request)

    path = request.url.path

    REQUEST_COUNT.labels(
        service=SERVICE_NAME,
        method=request.method,
        path=path,
        http_status=response.status_code,
    ).inc()

    REQUEST_LATENCY.labels(
        service=SERVICE_NAME,
        path=path,
    ).observe(time.time() - start)

    return response


# ========= Models =========


class PointModel(BaseModel):
    lat: float
    lon: float


class Coordinate(BaseModel):
    lat: float
    lng: float


class WeightUpdateRequest(BaseModel):
    edge_id: int  # gid in ways table
    safety_factor: float


class RouteSafetyWeightsInput(BaseModel):
    """Per-user factor importance weights for route cost scaling (all default to 1.0)."""

    cctv_coverage: float = 1.0
    street_lighting: float = 1.0
    business_activity: float = 1.0
    crime_rate: float = 1.0
    pedestrian_traffic: float = 1.0


class RouteRequest(BaseModel):
    start: Coordinate
    end: Coordinate
    safety_weights: Optional[RouteSafetyWeightsInput] = None


class SafetySegmentInput(BaseModel):
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float


class ScoreRouteRequest(BaseModel):
    route_geometry: str
    segments: List[SafetySegmentInput]
    time_of_day: datetime
    weather_conditions: Optional[Literal["clear", "rain", "fog"]] = None


class RiskFactor(BaseModel):
    type: str
    severity: str


class SafetySegmentScore(BaseModel):
    segment_id: str
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    score: float
    risk_factors: List[RiskFactor] = []


class SafetyAlert(BaseModel):
    type: str
    location: PointModel
    severity: str
    message: str


class ScoreRouteResponse(BaseModel):
    overall_score: float
    scoring_breakdown: Dict[str, float]
    segments: List[SafetySegmentScore]
    alerts: List[SafetyAlert]
    calculated_at: datetime


class SafetyFactorsRequest(BaseModel):
    lat: float
    lon: float
    radius_m: int = 50


class SafetyFactorsResponse(BaseModel):
    location: PointModel
    radius_m: int
    factors: Dict[str, object]
    composite_score: float
    queried_at: datetime


class SafetyWeights(BaseModel):
    cctv_coverage: float
    street_lighting: float
    business_activity: float
    crime_rate: float
    pedestrian_traffic: float


class SafetyWeightsRequest(BaseModel):
    user_id: str
    weights: SafetyWeights


class SafetyWeightsResponse(BaseModel):
    status: Literal["updated"]
    user_id: str
    weights: SafetyWeights
    weights_sum: float
    updated_at: datetime


# ---------- Pagination & filters (list response convention) ----------


class PaginationMeta(BaseModel):
    """Metadata for paginated list responses."""

    page: int = Field(..., ge=1, description="Current page (1-based)")
    page_size: int = Field(..., ge=1, le=500, description="Items per page")
    total: int = Field(..., ge=0, description="Total number of items")
    total_pages: int = Field(..., ge=0, description="Total number of pages")


def _total_pages(total: int, page_size: int) -> int:
    return max(0, (total + page_size - 1) // page_size) if page_size > 0 else 0


# ========= Endpoints =========


@app.get("/")
async def root():
    return {"service": "safety_scoring", "status": "running"}


@app.get("/health")
async def health():
    return {"status": "ok", "service": "safety_scoring"}


@app.get("/metrics")
async def metrics():
    """
    Expose Prometheus metrics for this Safety Scoring service.
    """
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


# --- Merged from main copy.py (Converted to Async) ---


@app.get("/api/danger_zones")
async def get_danger_zones(
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(50, ge=1, le=200, description="Items per page"),
    min_safety_factor: Optional[float] = Query(
        None, description="Filter: safety_factor >= value (e.g. 0.5)"
    ),
    max_safety_factor: Optional[float] = Query(
        None, description="Filter: safety_factor <= value (e.g. 2.0)"
    ),
    db: AsyncSession = Depends(get_safety_scoring_db),
):
    """
    Return edges that have custom weights. Paginated with optional filters.
    Response follows convention: data, filters, pagination.
    """
    try:
        # Filters for response (empty string when not set)
        filters_resp: Dict[str, Any] = {
            "min_safety_factor": min_safety_factor if min_safety_factor is not None else "",
            "max_safety_factor": max_safety_factor if max_safety_factor is not None else "",
        }
        # SQL: always exclude default weight 1.0; optional range
        where_clause = "safety_factor != 1.0"
        params: Dict[str, Any] = {}
        if min_safety_factor is not None:
            where_clause += " AND safety_factor >= :min_sf"
            params["min_sf"] = min_safety_factor
        if max_safety_factor is not None:
            where_clause += " AND safety_factor <= :max_sf"
            params["max_sf"] = max_safety_factor

        count_query = text(f"SELECT COUNT(*) FROM ways WHERE {where_clause}")
        count_result = await db.execute(count_query, params)
        total = count_result.scalar() or 0

        offset = (page - 1) * page_size
        params["limit"] = page_size
        params["offset"] = offset
        query = text(f"""
            SELECT gid, safety_factor, ST_AsGeoJSON(geometry) as geojson
            FROM ways
            WHERE {where_clause}
            ORDER BY gid
            LIMIT :limit OFFSET :offset
            """)
        result = await db.execute(query, params)
        rows = result.fetchall()

        features = []
        for r in rows:
            features.append(
                {
                    "type": "Feature",
                    "properties": {"id": r.gid, "weight": r.safety_factor, "type": "edge"},
                    "geometry": json.loads(r.geojson),
                }
            )

        pagination = PaginationMeta(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=_total_pages(total, page_size),
        )
        return {
            "type": "FeatureCollection",
            "data": features,
            "features": features,  # backward compat
            "filters": filters_resp,
            "pagination": pagination.model_dump(),
        }
    except Exception as e:
        print(f"Error fetching zones: {e}")
        return {
            "type": "FeatureCollection",
            "data": [],
            "features": [],
            "filters": {"min_safety_factor": "", "max_safety_factor": ""},
            "pagination": PaginationMeta(page=1, page_size=50, total=0, total_pages=0).model_dump(),
        }


@app.post("/api/danger_zones")
async def update_danger_zone(
    update: WeightUpdateRequest,
    db: AsyncSession = Depends(get_safety_scoring_db),
):
    """
    Update safety weight for a specific edge and its bidirectional counterpart.
    """
    try:
        await cas_log.begin(Op.SAFETY_WEIGHT_UPDATE, {"edge_id": update.edge_id})
        # Find the geometry of the selected edge
        geom_query = text("SELECT geometry FROM ways WHERE gid = :id")
        result = await db.execute(geom_query, {"id": update.edge_id})
        geom_res = result.fetchone()

        if not geom_res:
            await cas_log.transition(Op.SAFETY_WEIGHT_UPDATE, "INIT", "EDGE_NOT_FOUND")
            await cas_log.transition(Op.SAFETY_WEIGHT_UPDATE, "EDGE_NOT_FOUND", "FAILED")
            raise HTTPException(status_code=404, detail="Edge not found")

        await cas_log.transition(Op.SAFETY_WEIGHT_UPDATE, "INIT", "EDGE_FOUND")

        # Update ALL edges that share exactly the same geometry (spatial equality)
        update_query = text("""
            UPDATE ways 
            SET safety_factor = :w 
            WHERE ST_Equals(geometry, :geom)
        """)

        await db.execute(update_query, {"w": update.safety_factor, "geom": geom_res[0]})
        await cas_log.transition(Op.SAFETY_WEIGHT_UPDATE, "EDGE_FOUND", "UPDATED")
        await db.commit()
        await cas_log.transition(Op.SAFETY_WEIGHT_UPDATE, "UPDATED", "COMMITTED")
        await cas_log.transition(Op.SAFETY_WEIGHT_UPDATE, "COMMITTED", "COMPLETED")

        # TODO: add audit when auth is ready

        # audit = Audit(
        #     log_id=uuid.uuid4(),
        #     user_id=user_id,
        #     event_type="authentication",
        #     event_id=user_id,
        #     message="Register",
        #     created_at=now,
        #     updated_at=now,
        # )

        # db.add(audit)

        # TODO: add audit when auth is ready

        # audit = Audit(
        #     log_id=uuid.uuid4(),
        #     user_id=user_id,
        #     event_type="authentication",
        #     event_id=user_id,
        #     message="Register",
        #     created_at=now,
        #     updated_at=now,
        # )

        # db.add(audit)

        # Metric
        SAFETY_WEIGHTS_UPDATES_TOTAL.inc()

        return {
            "status": "updated",
            "id": update.edge_id,
            "safety_factor": update.safety_factor,
            "note": "Updated bidirectional edges",
        }
    except HTTPException:
        raise
    except Exception as e:
        await db.rollback()
        print(f"Error updating safety_factor: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/danger_zones/{zone_id}")
async def reset_danger_zone(zone_id: int, db: AsyncSession = Depends(get_safety_scoring_db)):
    """
    Reset safety_factor(weight) for a zone to default (1.0).
    """
    try:
        query = text("UPDATE ways SET safety_factor = 1.0 WHERE gid = :id")
        await db.execute(query, {"id": zone_id})
        await db.commit()

        # TODO: add audit when auth is ready

        # audit = Audit(
        #     log_id=uuid.uuid4(),
        #     user_id=user_id,
        #     event_type="authentication",
        #     event_id=user_id,
        #     message="Register",
        #     created_at=now,
        #     updated_at=now,
        # )

        # db.add(audit)

        return {"status": "reset"}
    except Exception as e:
        await db.rollback()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/route")
async def get_route(
    request: RouteRequest,
    algorithm: Literal["ch", "astar", "dijkstra", "bd_dijkstra"] = Query("dijkstra"),
    db: AsyncSession = Depends(get_safety_scoring_db),
):
    """
    Calculate route using pgRouting with safety weights.
    """
    try:
        await cas_log.begin(Op.SAFETY_ROUTE, {"algorithm": algorithm})

        # Metric
        SAFETY_SCORE_ROUTE_REQUESTS_TOTAL.inc()

        ch_fallback_to_dijkstra = False
        if algorithm == "ch":
            await cas_log.transition(Op.SAFETY_ROUTE, "INIT", "CH_REQUESTED")
            try:
                ch_result = await get_ch_route_geojson(request)
                await cas_log.transition(Op.SAFETY_ROUTE, "CH_REQUESTED", "ROUTE_COMPUTED")
                await cas_log.transition(Op.SAFETY_ROUTE, "ROUTE_COMPUTED", "COMPLETED")
                return ch_result
            except Exception:
                await cas_log.transition(Op.SAFETY_ROUTE, "CH_REQUESTED", "CH_FAILED")
                if not CH_FALLBACK_TO_DIJKSTRA:
                    raise
                await cas_log.transition(Op.SAFETY_ROUTE, "CH_FAILED", "DIJKSTRA_REQUESTED")
                algorithm = "dijkstra"
                ch_fallback_to_dijkstra = True

        if not ch_fallback_to_dijkstra:
            await cas_log.transition(Op.SAFETY_ROUTE, "INIT", "DIJKSTRA_REQUESTED")

        # 1. Find nearest graph node by snapping to nearest edge endpoint.
        # This avoids scanning huge start/end-point candidate sets.
        node_query = text("""
        WITH p AS (
            SELECT ST_SetSRID(ST_MakePoint(:lng, :lat), 4326) AS pt
        ),
        nearest_edge AS (
            SELECT w.source, w.target, w.geometry, p.pt
            FROM ways w
            CROSS JOIN p
            ORDER BY w.geometry <-> p.pt
            LIMIT 1
        )
        SELECT
            CASE
                WHEN ST_Distance(ST_StartPoint(geometry)::geography, pt::geography)
                   <= ST_Distance(ST_EndPoint(geometry)::geography, pt::geography)
                THEN source
                ELSE target
            END AS id
        FROM nearest_edge;
        """)

        start_res = await db.execute(
            node_query, {"lng": request.start.lng, "lat": request.start.lat}
        )
        start_node_res = start_res.fetchone()

        end_res = await db.execute(node_query, {"lng": request.end.lng, "lat": request.end.lat})
        end_node_res = end_res.fetchone()

        if not start_node_res or not end_node_res:
            raise HTTPException(status_code=404, detail="Could not find nearest road nodes.")

        start_node = start_node_res[0]
        end_node = end_node_res[0]

        # Compute user safety scalar from optional per-factor weights.
        # A scalar > 1 amplifies the penalty for unsafe edges (more cautious routing).
        user_safety_scalar = 1.0
        if request.safety_weights:
            sw = request.safety_weights
            total = (
                sw.cctv_coverage
                + sw.street_lighting
                + sw.business_activity
                + sw.crime_rate
                + sw.pedestrian_traffic
            )
            user_safety_scalar = max(0.3, min(3.0, total / 5.0))

        # Check route cache before running pgRouting.
        cache_key = _route_cache_key(request.start, request.end, request.safety_weights)
        cached = await _get_cached_route(db, cache_key)
        if cached is not None:
            await cas_log.transition(Op.SAFETY_ROUTE, "DIJKSTRA_REQUESTED", "ROUTE_COMPUTED")
            await cas_log.transition(Op.SAFETY_ROUTE, "ROUTE_COMPUTED", "COMPLETED")
            return cached

        # 2. Build query against progressively larger local subgraphs, then full graph fallback.
        routing_fn = (
            "pgr_aStar"
            if algorithm == "astar"
            else "pgr_bdDijkstra" if algorithm == "bd_dijkstra" else "pgr_dijkstra"
        )
        routing_query = text(
            """
            SELECT
                d.seq,
                d.path_seq,
                d.node,
                d.edge,
                d.cost,
                d.agg_cost,
                ST_AsGeoJSON(w.geometry) as geojson,
                w.length,
                w.safety_factor,
                w.source,
                w.target
            FROM """
            + routing_fn
            + """(
                CAST(:sql AS TEXT),
                CAST(:start_node AS BIGINT),
                CAST(:end_node AS BIGINT),
                false
            ) as d
            LEFT JOIN ways w ON d.edge = w.gid
            ORDER BY d.seq;
        """
        )
        expanded = max(0.001, ROUTE_SUBGRAPH_EXPAND_DEGREES)
        max_expand = max(expanded, ROUTE_SUBGRAPH_EXPAND_MAX_DEGREES)
        expansions: List[float] = []
        while expanded <= max_expand + 1e-9:
            expansions.append(round(expanded, 6))
            expanded *= 2

        scalar_sql = f"* {user_safety_scalar:.4f}" if user_safety_scalar != 1.0 else ""
        routes = []
        for expand in expansions:
            cost_sql = f"""
                WITH route_window AS (
                    SELECT ST_Expand(
                        ST_Envelope(
                            ST_Collect(
                                ST_SetSRID(ST_MakePoint({request.start.lng}, {request.start.lat}), 4326),
                                ST_SetSRID(ST_MakePoint({request.end.lng}, {request.end.lat}), 4326)
                            )
                        ),
                        {expand}
                    ) AS bbox
                )
                SELECT
                    w.gid AS id,
                    w.source,
                    w.target,
                    w.length * w.safety_factor {scalar_sql} AS cost,
                    w.length * w.safety_factor {scalar_sql} AS reverse_cost,
                    ST_X(ST_StartPoint(w.geometry)) AS x1,
                    ST_Y(ST_StartPoint(w.geometry)) AS y1,
                    ST_X(ST_EndPoint(w.geometry)) AS x2,
                    ST_Y(ST_EndPoint(w.geometry)) AS y2
                FROM ways w
                CROSS JOIN route_window rw
                WHERE w.geometry && rw.bbox
            """
            try:
                route_res = await db.execute(
                    routing_query, {"sql": cost_sql, "start_node": start_node, "end_node": end_node}
                )
                routes = route_res.fetchall()
                if routes:
                    break
            except Exception:
                continue

        if not routes:
            full_cost_sql = f"""
                SELECT
                    gid AS id,
                    source,
                    target,
                    length * safety_factor {scalar_sql} AS cost,
                    length * safety_factor {scalar_sql} AS reverse_cost,
                    ST_X(ST_StartPoint(geometry)) AS x1,
                    ST_Y(ST_StartPoint(geometry)) AS y1,
                    ST_X(ST_EndPoint(geometry)) AS x2,
                    ST_Y(ST_EndPoint(geometry)) AS y2
                FROM ways
            """
            route_res = await db.execute(
                routing_query,
                {"sql": full_cost_sql, "start_node": start_node, "end_node": end_node},
            )
            routes = route_res.fetchall()

        if not routes:
            await cas_log.transition(Op.SAFETY_ROUTE, "DIJKSTRA_REQUESTED", "NO_PATH")
            await cas_log.transition(Op.SAFETY_ROUTE, "NO_PATH", "FAILED")
            raise HTTPException(status_code=404, detail="No path found.")

        await cas_log.transition(Op.SAFETY_ROUTE, "DIJKSTRA_REQUESTED", "ROUTE_COMPUTED")

        # 4. Construct GeoJSON Response
        features = []
        total_distance = 0.0
        path_sf_weighted: List[tuple] = []  # (safety_factor, length)

        # Path Segments
        road_coords = []
        if ROUTE_DEBUG_LOG:
            print(f"--- Routing from {start_node} to {end_node} ---")
        for r in routes:
            if r.geojson:
                geom = json.loads(r.geojson)
                coords = geom["coordinates"]

                if r.node == r.target:
                    coords = coords[::-1]

                if not road_coords:
                    road_coords.extend(coords)
                else:
                    if road_coords[-1] == coords[0]:
                        road_coords.extend(coords[1:])
                    else:
                        road_coords.extend(coords)

                if r.length:
                    total_distance += r.length
                    sf = getattr(r, "safety_factor", None)
                    if sf is not None:
                        path_sf_weighted.append((float(sf), float(r.length)))

        trimmed_coords = road_coords

        if trimmed_coords:
            # Start Connector
            features.append(
                {
                    "type": "Feature",
                    "properties": {"type": "connector"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[request.start.lng, request.start.lat], trimmed_coords[0]],
                    },
                }
            )

            features.append(
                {
                    "type": "Feature",
                    "properties": {"type": "road"},
                    "geometry": {"type": "LineString", "coordinates": trimmed_coords},
                }
            )

            # End Connector
            features.append(
                {
                    "type": "Feature",
                    "properties": {"type": "connector"},
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [trimmed_coords[-1], [request.end.lng, request.end.lat]],
                    },
                }
            )

        walking_speed_mps = 1.39
        duration_seconds = total_distance / walking_speed_mps

        # Compute real path safety score from length-weighted avg safety_factor.
        # sf=1.0 → 50 (neutral/grey), sf<1 → >50 (green/safe), sf>1 → <50 (red/danger).
        if path_sf_weighted and total_distance > 0:
            total_sf_len = sum(length for _, length in path_sf_weighted)
            avg_sf = sum(sf * length for sf, length in path_sf_weighted) / max(total_sf_len, 1e-9)
            computed_safety_score = _sf_to_score_0_100(avg_sf)
        else:
            computed_safety_score = 50.0

        await cas_log.transition(Op.SAFETY_ROUTE, "ROUTE_COMPUTED", "COMPLETED")
        result = {
            "type": "FeatureCollection",
            "features": features,
            "properties": {
                "summary": {
                    "distance_meters": total_distance,
                    "distance_km": round(total_distance / 1000, 2),
                    "duration": duration_seconds,
                },
                "safety_score": computed_safety_score,
            },
        }
        await _store_route_cache(
            db,
            cache_key,
            request.start,
            request.end,
            request.safety_weights,
            result,
            computed_safety_score,
        )
        return result

    except HTTPException:
        raise
    except Exception as e:
        print(f"Error: {e}")
        import traceback

        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/graph")
async def get_graph_geojson(
    min_lng: float = Query(..., description="Minimum Longitude"),
    min_lat: float = Query(..., description="Minimum Latitude"),
    max_lng: float = Query(..., description="Maximum Longitude"),
    max_lat: float = Query(..., description="Maximum Latitude"),
    db: AsyncSession = Depends(get_safety_scoring_db),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(100, ge=1, le=2000, description="Items per page"),
):
    """
    Return graph edges in bbox. Paginated. Response follows convention: data, filters, pagination.
    """
    try:
        filters_resp: Dict[str, Any] = {
            "min_lng": min_lng,
            "min_lat": min_lat,
            "max_lng": max_lng,
            "max_lat": max_lat,
        }
        params = {
            "min_lng": min_lng,
            "min_lat": min_lat,
            "max_lng": max_lng,
            "max_lat": max_lat,
        }

        # Total count in bbox
        count_query = text("""
            SELECT COUNT(*) FROM ways
            WHERE geometry && ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326)
            """)
        count_result = await db.execute(count_query, params)
        total = count_result.scalar() or 0

        offset = (page - 1) * page_size
        params["limit"] = page_size
        params["offset"] = offset
        query = text("""
            SELECT gid, source, target, ST_AsGeoJSON(geometry) as geojson, safety_factor
            FROM ways
            WHERE geometry && ST_MakeEnvelope(:min_lng, :min_lat, :max_lng, :max_lat, 4326)
            ORDER BY gid
            LIMIT :limit OFFSET :offset
        """)
        result = await db.execute(query, params)
        rows = result.fetchall()

        features = []
        for r in rows:
            features.append(
                {
                    "type": "Feature",
                    "properties": {"type": "edge", "id": r.gid, "weight": r.safety_factor},
                    "geometry": json.loads(r.geojson),
                }
            )

        pagination = PaginationMeta(
            page=page,
            page_size=page_size,
            total=total,
            total_pages=_total_pages(total, page_size),
        )
        return {
            "type": "FeatureCollection",
            "data": features,
            "features": features,  # backward compat
            "filters": filters_resp,
            "pagination": pagination.model_dump(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# --- Existing Validated Endpoints (Keep for backward compatibility) ---


@app.get("/v1/safety/factors", response_model=SafetyFactorsResponse)
async def get_factors(
    body: SafetyFactorsRequest,
    db: AsyncSession = Depends(get_safety_scoring_db),
):
    SAFETY_FACTORS_QUERIES_TOTAL.inc()

    composite_score = 50.0
    factors: Dict[str, object] = {}

    try:
        # Nearest way segment to the queried point, then look up its scores.
        result = await db.execute(
            text(f"""
                SELECT
                    mv.composite_safety_score,
                    mv.light_score_norm,
                    mv.cctv_score_norm,
                    mv.garda_score_norm,
                    mv.crime_score_norm
                FROM {SAFETY_SCORES_VIEW} mv
                JOIN ways w ON w.gid = mv.gid
                ORDER BY w.geometry <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                LIMIT 1
            """),
            {"lon": body.lon, "lat": body.lat},
        )
        row = result.fetchone()
        if row:
            composite_score = round(float(row.composite_safety_score) * 100, 1)
            factors = {
                "street_lighting": round(float(row.light_score_norm) * 100, 1),
                "cctv_coverage": round(float(row.cctv_score_norm) * 100, 1),
                "garda_proximity": round(float(row.garda_score_norm) * 100, 1),
                # Exposed as raw density (0 = no crime, 100 = highest relative density)
                "crime_density": round(float(row.crime_score_norm) * 100, 1),
            }
    except Exception as e:
        print(f"get_factors DB query failed (returning defaults): {e}")
        factors = {"street_lighting": 0, "cctv_coverage": 0, "garda_proximity": 0}

    return SafetyFactorsResponse(
        location=PointModel(lat=body.lat, lon=body.lon),
        radius_m=body.radius_m,
        factors=factors,
        composite_score=composite_score,
        queried_at=datetime.utcnow(),
    )


@app.post("/v1/safety/score-route", response_model=ScoreRouteResponse)
async def score_route(
    body: ScoreRouteRequest,
    db: AsyncSession = Depends(get_safety_scoring_db),
):
    SAFETY_SCORE_ROUTE_REQUESTS_TOTAL.inc()

    segs: List[SafetySegmentScore] = []
    all_scores: List[float] = []
    breakdown_lights: List[float] = []
    breakdown_cctv: List[float] = []
    breakdown_garda: List[float] = []
    breakdown_crime: List[float] = []

    for i, s in enumerate(body.segments):
        seg_score = 50.0
        risk_factors: List[RiskFactor] = []
        try:
            mid_lon = (s.start_lon + s.end_lon) / 2
            mid_lat = (s.start_lat + s.end_lat) / 2
            result = await db.execute(
                text(f"""
                    SELECT
                        mv.composite_safety_score,
                        mv.light_score_norm,
                        mv.cctv_score_norm,
                        mv.garda_score_norm,
                        mv.crime_score_norm
                    FROM {SAFETY_SCORES_VIEW} mv
                    JOIN ways w ON w.gid = mv.gid
                    ORDER BY w.geometry <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)
                    LIMIT 1
                """),
                {"lon": mid_lon, "lat": mid_lat},
            )
            row = result.fetchone()
            if row:
                seg_score = round(float(row.composite_safety_score) * 100, 1)
                breakdown_lights.append(float(row.light_score_norm) * 100)
                breakdown_cctv.append(float(row.cctv_score_norm) * 100)
                breakdown_garda.append(float(row.garda_score_norm) * 100)
                breakdown_crime.append(float(row.crime_score_norm) * 100)
                # Flag high crime density as a risk factor
                if float(row.crime_score_norm) > 0.7:
                    risk_factors.append(RiskFactor(type="high_crime_area", severity="high"))
                elif float(row.crime_score_norm) > 0.4:
                    risk_factors.append(RiskFactor(type="elevated_crime", severity="medium"))
                if seg_score < 40:
                    risk_factors.append(RiskFactor(type="low_safety_score", severity="high"))
                elif seg_score < 60:
                    risk_factors.append(RiskFactor(type="moderate_risk", severity="medium"))
        except Exception as e:
            print(f"score_route segment {i} query failed: {e}")

        all_scores.append(seg_score)
        segs.append(
            SafetySegmentScore(
                segment_id=f"seg_{i + 1:03d}",
                start_lat=s.start_lat,
                start_lon=s.start_lon,
                end_lat=s.end_lat,
                end_lon=s.end_lon,
                score=seg_score,
                risk_factors=risk_factors,
            )
        )

    overall = round(sum(all_scores) / max(len(all_scores), 1), 1)
    scoring_breakdown: Dict[str, float] = {
        "street_lighting": round(sum(breakdown_lights) / max(len(breakdown_lights), 1), 1),
        "cctv_coverage": round(sum(breakdown_cctv) / max(len(breakdown_cctv), 1), 1),
        "garda_proximity": round(sum(breakdown_garda) / max(len(breakdown_garda), 1), 1),
        # Exposed as density (higher = more crime in this corridor)
        "crime_density": round(sum(breakdown_crime) / max(len(breakdown_crime), 1), 1),
    }

    return ScoreRouteResponse(
        overall_score=overall,
        scoring_breakdown=scoring_breakdown,
        segments=segs,
        alerts=[],
        calculated_at=datetime.utcnow(),
    )


@app.put("/v1/safety/weights", response_model=SafetyWeightsResponse)
async def update_weights(
    body: SafetyWeightsRequest,
    db: AsyncSession = Depends(get_safety_scoring_db),
):
    w = body.weights
    total = (
        w.cctv_coverage
        + w.street_lighting
        + w.business_activity
        + w.crime_rate
        + w.pedestrian_traffic
    )
    SAFETY_WEIGHTS_UPDATES_TOTAL.inc()
    now = datetime.utcnow()

    try:
        await db.execute(
            text("""
                INSERT INTO saferoute.user_safety_weights
                    (user_id, cctv_coverage, street_lighting, business_activity,
                     crime_rate, pedestrian_traffic, updated_at)
                VALUES
                    (:uid, :cctv, :lights, :biz, :crime, :ped, :now)
                ON CONFLICT (user_id) DO UPDATE SET
                    cctv_coverage      = EXCLUDED.cctv_coverage,
                    street_lighting    = EXCLUDED.street_lighting,
                    business_activity  = EXCLUDED.business_activity,
                    crime_rate         = EXCLUDED.crime_rate,
                    pedestrian_traffic = EXCLUDED.pedestrian_traffic,
                    updated_at         = EXCLUDED.updated_at
            """),
            {
                "uid": body.user_id,
                "cctv": w.cctv_coverage,
                "lights": w.street_lighting,
                "biz": w.business_activity,
                "crime": w.crime_rate,
                "ped": w.pedestrian_traffic,
                "now": now,
            },
        )
        await db.commit()
    except Exception as e:
        print(f"Failed to persist safety weights for {body.user_id}: {e}")
        try:
            await db.rollback()
        except Exception:
            pass

    return SafetyWeightsResponse(
        status="updated",
        user_id=body.user_id,
        weights=w,
        weights_sum=total,
        updated_at=now,
    )


@app.post("/internal/refresh-safety-scores", include_in_schema=False)
async def manual_refresh_safety_scores():
    """
    Manually trigger a safety score refresh. Useful after bulk updates to
    feature tables (e.g. new CCTV cameras imported).
    """
    await _refresh_safety_scores()
    return {"status": "refreshed", "refreshed_at": datetime.utcnow().isoformat() + "Z"}
