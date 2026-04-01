from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def ensure_outbox_tables(session: AsyncSession) -> None:
    """
    Ensure outbox table exists.

    This repo does not use Alembic migrations; for local/dev we create the
    table idempotently.
    """

    # Keep schema creation separate to avoid failures if schema already exists.
    await session.execute(text("CREATE SCHEMA IF NOT EXISTS saferoute"))

    await session.execute(text("""
            CREATE TABLE IF NOT EXISTS saferoute.outbox (
                event_id UUID PRIMARY KEY,
                event_type TEXT NOT NULL,
                aggregate_id UUID NULL,
                payload JSONB NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                available_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                locked_at TIMESTAMPTZ NULL,
                processed_at TIMESTAMPTZ NULL,
                last_error TEXT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """))

    await session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS idx_outbox_status_available_at ON saferoute.outbox (status, available_at)"
        )
    )

    await session.commit()
