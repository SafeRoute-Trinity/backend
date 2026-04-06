"""
CAS Enforcer — database-backed Compare-and-Swap for distributed consistency.

While ``cas_logger`` emits observability logs, this module **enforces**
state transitions atomically in PostgreSQL so that concurrent replicas
cannot push the same operation through conflicting paths.

Flow:
    1. ``begin()`` — INSERT a new ``cas_state`` row (version = 1).
    2. ``transition()`` — atomic UPDATE … WHERE current_state = :expected.
       If zero rows are affected the expected state was already changed by
       another replica → ``CASConflictError`` is raised.
    3. Every successful write is published to Redis ``cas:state_changes``
       so peer replicas can react immediately (cache invalidation, UI push,
       etc.) without polling the database.

The enforcer owns a **dedicated** async engine so it works regardless of
whether the calling service talks to Postgres or PostGIS.

**URL resolution** — ``resolve_cas_database_url`` (used by ``initialize``):
``CAS_DATABASE_URL`` → ``POSTGIS_DATABASE_URL`` → PostGIS DSN if
``POSTGIS_HOST`` is set → ``DATABASE_URL`` → else ``POSTGRES_*``.
PostGIS is preferred before ``DATABASE_URL`` so services that read/write
``ways`` on PostGIS (e.g. safety-scoring) persist ``cas_state`` on the same
DB even when ``DATABASE_URL`` points at a separate application Postgres.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from libs.cas_logger import Op, _payload_hash
from libs.trace_context import trace_id_var
from models.cas_state import CASState

logger = logging.getLogger("cas.enforcer")

CAS_CHANNEL = "cas:state_changes"
CAS_STATE_TTL_HOURS = int(os.getenv("CAS_STATE_TTL_HOURS", "24"))


def _normalize_async_database_url(url: str) -> str:
    u = url.strip()
    if u.startswith("postgresql://"):
        return u.replace("postgresql://", "postgresql+asyncpg://", 1)
    if u.startswith("postgres://"):
        return u.replace("postgres://", "postgresql+asyncpg://", 1)
    return u


def _build_postgis_async_url() -> str:
    from urllib.parse import quote_plus

    host = os.getenv("POSTGIS_HOST", "127.0.0.1")
    port = os.getenv("POSTGIS_PORT", "5433")
    user = os.getenv("POSTGIS_USER", "saferoute")
    pw = quote_plus(os.getenv("POSTGIS_PASSWORD", ""))
    db = os.getenv("POSTGIS_DATABASE", "saferoute_geo")
    return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}"


def _build_postgres_async_url() -> str:
    from urllib.parse import quote_plus

    host = os.getenv("POSTGRES_HOST", "127.0.0.1")
    port = os.getenv("POSTGRES_PORT", "5432")
    user = os.getenv("POSTGRES_USER", "saferoute")
    pw = quote_plus(os.getenv("POSTGRES_PASSWORD", ""))
    db = os.getenv("POSTGRES_DATABASE", "saferoute")
    return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}"


def resolve_cas_database_url(explicit: Optional[str]) -> str:
    """
    Pick the DB URL for ``cas_state``.

    Order: explicit arg → ``CAS_DATABASE_URL`` → ``POSTGIS_DATABASE_URL`` →
    if ``POSTGIS_HOST`` is set, PostGIS DSN from ``POSTGIS_*`` →
    ``DATABASE_URL`` → else PostgreSQL DSN from ``POSTGRES_*``.

    PostGIS is checked before ``DATABASE_URL`` so CAS rows land with routing
    data; set ``CAS_DATABASE_URL`` to override.
    """
    if explicit and explicit.strip():
        return _normalize_async_database_url(explicit)
    raw = os.getenv("CAS_DATABASE_URL", "").strip()
    if raw:
        return _normalize_async_database_url(raw)
    raw = os.getenv("POSTGIS_DATABASE_URL", "").strip()
    if raw:
        return _normalize_async_database_url(raw)
    if os.getenv("POSTGIS_HOST", "").strip():
        return _build_postgis_async_url()
    raw = os.getenv("DATABASE_URL", "").strip()
    if raw:
        return _normalize_async_database_url(raw)
    return _build_postgres_async_url()


class CASConflictError(Exception):
    """Raised when a CAS transition fails because the expected state no longer matches."""

    def __init__(self, operation: str, expected: str, new: str, trace_id: str):
        self.operation = operation
        self.expected = expected
        self.new = new
        self.trace_id = trace_id
        super().__init__(
            f"CAS conflict on {operation}: expected '{expected}' → '{new}' "
            f"but current state differs (trace_id={trace_id})"
        )


class CASEnforcer:
    """
    Database-backed CAS enforcement with Redis pub/sub notification.

    Call ``await initialize()`` once at startup (the ``FastAPIServiceFactory``
    does this automatically).  After that every ``begin`` / ``transition``
    is persisted atomically and broadcast to all replicas.
    """

    def __init__(self) -> None:
        self._engine = None
        self._session_maker = None
        self._redis: Optional[aioredis.Redis] = None
        self._service: str = "unknown"
        self._initialized = False

    async def initialize(
        self,
        service_name: str,
        database_url: Optional[str] = None,
    ) -> None:
        """
        Create the dedicated async engine and Redis pub/sub client.

        Parameters
        ----------
        service_name:
            Identifies the pod / service writing the state row.
        database_url:
            Explicit ``postgresql+asyncpg://`` URL.  If omitted, see
            ``resolve_cas_database_url`` (PostGIS when ``POSTGIS_HOST`` is set).
        """
        self._service = service_name

        database_url = resolve_cas_database_url(database_url or "")

        self._engine = create_async_engine(database_url, pool_size=5, max_overflow=2)
        self._session_maker = sessionmaker(
            bind=self._engine, class_=AsyncSession, expire_on_commit=False
        )

        redis_host = os.getenv("REDIS_HOST", "localhost")
        redis_port = int(os.getenv("REDIS_PORT", "6379"))
        redis_password = os.getenv("REDIS_PASSWORD") or None
        try:
            self._redis = aioredis.Redis(
                host=redis_host,
                port=redis_port,
                password=redis_password,
                decode_responses=True,
                socket_connect_timeout=3,
            )
            await self._redis.ping()
            logger.info("CAS enforcer Redis connected (%s:%s)", redis_host, redis_port)
        except Exception as exc:
            logger.warning("CAS enforcer Redis unavailable (%s) — pub/sub disabled", exc)
            self._redis = None

        self._initialized = True
        logger.info("CAS enforcer initialized for service=%s", service_name)

    @property
    def ready(self) -> bool:
        return self._initialized and self._engine is not None

    async def begin(
        self,
        operation: Op,
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        """INSERT the INIT state row. Returns version (always 1)."""
        if not self.ready:
            return 0

        trace_id = trace_id_var.get("")
        now = datetime.now(timezone.utc)
        row = CASState(
            trace_id=trace_id,
            operation=operation.value,
            service=self._service,
            current_state="INIT",
            version=1,
            payload_hash=_payload_hash(detail),
            detail=detail,
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(hours=CAS_STATE_TTL_HOURS),
        )

        async with self._session_maker() as session:
            session.add(row)
            await session.commit()

        await self._publish(trace_id, operation, "INIT", 1)
        return 1

    async def transition(
        self,
        operation: Op,
        expected: str,
        new: str,
        detail: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Atomic compare-and-swap: update the row only if ``current_state``
        matches ``expected``.  Returns the new version number.

        Raises ``CASConflictError`` when the row's current state has diverged
        (another replica won the race).
        """
        if not self.ready:
            return 0

        trace_id = trace_id_var.get("")

        async with self._session_maker() as session:
            stmt = (
                update(CASState)
                .where(
                    CASState.trace_id == trace_id,
                    CASState.operation == operation.value,
                    CASState.current_state == expected,
                )
                .values(
                    current_state=new,
                    version=CASState.version + 1,
                    payload_hash=_payload_hash(detail),
                    detail=detail,
                    updated_at=datetime.now(timezone.utc),
                )
                .returning(CASState.version)
            )
            result = await session.execute(stmt)
            row = result.fetchone()

            if row is None:
                await session.rollback()
                logger.warning(
                    "CAS CONFLICT %s: %s → %s (trace=%s)",
                    operation.value,
                    expected,
                    new,
                    trace_id,
                )
                raise CASConflictError(operation.value, expected, new, trace_id)

            await session.commit()
            new_version = row[0]

        await self._publish(trace_id, operation, new, new_version)
        return new_version

    async def get_state(self, trace_id: str, operation: Op) -> Optional[Dict[str, Any]]:
        """Read the current CAS state for a given operation (any replica)."""
        if not self.ready:
            return None

        from sqlalchemy import select

        async with self._session_maker() as session:
            stmt = select(CASState).where(
                CASState.trace_id == trace_id,
                CASState.operation == operation.value,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if not row:
                return None
            return {
                "trace_id": row.trace_id,
                "operation": row.operation,
                "current_state": row.current_state,
                "version": row.version,
                "service": row.service,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }

    async def _publish(
        self,
        trace_id: str,
        operation: Op,
        state: str,
        version: int,
    ) -> None:
        """Broadcast a state-change event to all replicas via Redis pub/sub."""
        if not self._redis:
            return
        try:
            payload = json.dumps(
                {
                    "trace_id": trace_id,
                    "operation": operation.value,
                    "state": state,
                    "version": version,
                    "service": self._service,
                    "ts": datetime.now(timezone.utc).isoformat(),
                }
            )
            await self._redis.publish(CAS_CHANNEL, payload)
        except Exception as exc:
            logger.debug("CAS pub/sub publish failed: %s", exc)

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None
        if self._engine:
            await self._engine.dispose()
            self._engine = None
        self._initialized = False


cas_enforcer = CASEnforcer()
