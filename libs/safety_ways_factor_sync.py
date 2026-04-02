"""
Batch-sync composite safety scores from the materialized view into ways.safety_factor.

Large graphs need many small committed UPDATEs (checkpoints) instead of one huge
transaction. Optional PostgreSQL CHECKPOINT between batches helps durability on
self-hosted clusters (skipped if the role lacks permission).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

SAFETY_WAYS_FACTOR_BATCH_SIZE = max(1, int(os.getenv("SAFETY_WAYS_FACTOR_BATCH_SIZE", "5000")))
SAFETY_PG_CHECKPOINT_EVERY_N_BATCHES = max(
    0, int(os.getenv("SAFETY_PG_CHECKPOINT_EVERY_N_BATCHES", "0"))
)

SAFETY_FACTOR_SYNC_STATE_TABLE = os.getenv(
    "SAFETY_FACTOR_SYNC_STATE_TABLE", "saferoute.safety_ways_factor_sync_state"
)


def _sync_state_ddl(table_qualified: str) -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS {table_qualified} (
        lock_id                 INTEGER PRIMARY KEY DEFAULT 1 CHECK (lock_id = 1),
        last_processed_gid      BIGINT NOT NULL DEFAULT 0,
        last_run_started_at     TIMESTAMPTZ,
        last_batch_at           TIMESTAMPTZ,
        batches_in_run          INTEGER NOT NULL DEFAULT 0,
        ways_updated_in_run     BIGINT NOT NULL DEFAULT 0
    )
    """


async def ensure_safety_factor_sync_state_table(conn: AsyncConnection, table_qualified: str) -> None:
    await conn.execute(text(_sync_state_ddl(table_qualified)))
    await conn.execute(
        text(f"""
        INSERT INTO {table_qualified} (lock_id) VALUES (1)
        ON CONFLICT (lock_id) DO NOTHING
        """)
    )


def _batch_update_sql(matview_qualified: str) -> str:
    return f"""
    WITH batch AS (
        SELECT gid FROM ways
        WHERE gid > (:cursor)::bigint
        ORDER BY gid
        LIMIT (:batch_size)::integer
    ),
    upd AS (
        UPDATE ways w
        SET safety_factor = CASE
            WHEN mv.composite_safety_score >= (:neutral)::double precision THEN
                GREATEST((:sf_safe)::double precision,
                    1.0::double precision
                    - (mv.composite_safety_score - (:neutral)::double precision)
                        / GREATEST(1.0::double precision - (:neutral)::double precision, 1e-9::double precision)
                        * (1.0::double precision - (:sf_safe)::double precision))
            ELSE
                LEAST((:sf_danger)::double precision,
                    1.0::double precision
                    + ((:neutral)::double precision - mv.composite_safety_score)
                        / GREATEST((:neutral)::double precision, 1e-9::double precision)
                        * ((:sf_danger)::double precision - 1.0::double precision))
        END
        FROM {matview_qualified} mv
        WHERE w.gid = mv.gid
          AND w.gid IN (SELECT gid FROM batch)
        RETURNING w.gid
    )
    SELECT COALESCE(MAX(gid), (:cursor)::bigint)::bigint AS last_gid,
           COUNT(*)::bigint AS n
    FROM upd
    """


async def reset_sync_state(conn: AsyncConnection, table_qualified: str) -> None:
    await conn.execute(
        text(f"""
        UPDATE {table_qualified}
        SET last_processed_gid = 0,
            last_run_started_at = NOW(),
            last_batch_at = NULL,
            batches_in_run = 0,
            ways_updated_in_run = 0
        WHERE lock_id = 1
        """)
    )


async def update_sync_state_progress(
    conn: AsyncConnection,
    table_qualified: str,
    *,
    last_gid: int,
    batch_index: int,
    ways_total: int,
) -> None:
    await conn.execute(
        text(f"""
        UPDATE {table_qualified}
        SET last_processed_gid = :last_gid,
            last_batch_at = NOW(),
            batches_in_run = :batch_index,
            ways_updated_in_run = ways_updated_in_run + :ways_total
        WHERE lock_id = 1
        """),
        {"last_gid": last_gid, "batch_index": batch_index, "ways_total": ways_total},
    )


async def maybe_run_pg_checkpoint(conn: AsyncConnection, batch_index: int) -> None:
    every = SAFETY_PG_CHECKPOINT_EVERY_N_BATCHES
    if every <= 0 or batch_index % every != 0:
        return
    try:
        await conn.execute(text("CHECKPOINT"))
        logger.info("PostgreSQL CHECKPOINT completed after batch %s", batch_index)
    except Exception as ex:
        logger.warning("CHECKPOINT skipped or failed (non-fatal): %s", ex)


async def sync_ways_safety_factors_batched(
    conn: AsyncConnection,
    *,
    matview_qualified: str,
    neutral: float,
    sf_safe: float,
    sf_danger: float,
    state_table_qualified: str = SAFETY_FACTOR_SYNC_STATE_TABLE,
    batch_size: Optional[int] = None,
) -> Tuple[int, int]:
    """
    Copy composite scores from the materialized view into ways.safety_factor in batches.

    Returns (total_rows_updated, batch_count).
    """
    bs = batch_size if batch_size is not None else SAFETY_WAYS_FACTOR_BATCH_SIZE
    await ensure_safety_factor_sync_state_table(conn, state_table_qualified)
    await reset_sync_state(conn, state_table_qualified)

    bounds = await conn.execute(text("SELECT COALESCE(MIN(gid), 0), COALESCE(MAX(gid), 0), COUNT(*) FROM ways"))
    row = bounds.first()
    if not row or row[2] == 0:
        logger.info("sync_ways_safety_factors_batched: ways table empty, nothing to do")
        return 0, 0

    min_gid, max_gid, ways_count = int(row[0]), int(row[1]), int(row[2])
    sql = text(_batch_update_sql(matview_qualified))

    total_updated = 0
    batch_count = 0
    cursor = min_gid - 1

    bind: dict[str, Any] = {
        "neutral": neutral,
        "sf_safe": sf_safe,
        "sf_danger": sf_danger,
        "batch_size": bs,
    }

    while True:
        bind["cursor"] = cursor
        result = await conn.execute(sql, bind)
        r = result.first()
        if not r:
            break
        last_gid, n = int(r[0]), int(r[1])
        if n == 0:
            break
        batch_count += 1
        total_updated += n
        cursor = last_gid
        await update_sync_state_progress(
            conn,
            state_table_qualified,
            last_gid=cursor,
            batch_index=batch_count,
            ways_total=n,
        )
        await maybe_run_pg_checkpoint(conn, batch_count)
        if n < bs:
            break

    if total_updated != ways_count:
        logger.warning(
            "ways safety_factor sync: updated %s rows but ways has %s rows — investigate gid/MV mismatch",
            total_updated,
            ways_count,
        )
    else:
        logger.info(
            "ways safety_factor sync: updated %s rows in %s batches (gid %s..%s)",
            total_updated,
            batch_count,
            min_gid,
            max_gid,
        )

    return total_updated, batch_count


async def verify_ways_matview_alignment(
    conn: AsyncConnection, matview_qualified: str
) -> Tuple[int, int, int]:
    """
    Return (ways_count, matview_count, ways_without_mv_row).

    For a view built as SELECT ... FROM ways w, ways_without_mv_row should be 0.
    """
    q = text(f"""
        SELECT
            (SELECT COUNT(*)::int FROM ways) AS wc,
            (SELECT COUNT(*)::int FROM {matview_qualified}) AS mc,
            (SELECT COUNT(*)::int FROM ways w
             WHERE NOT EXISTS (
                 SELECT 1 FROM {matview_qualified} mv WHERE mv.gid = w.gid
             )) AS missing
    """)
    r = (await conn.execute(q)).first()
    if not r:
        return 0, 0, 0
    return int(r[0]), int(r[1]), int(r[2])
