"""
Database connection module for SafeRoute backend.

Provides async database engine and session management using SQLAlchemy.
"""

import asyncio
import os
from typing import Optional, Tuple
from urllib.parse import quote_plus

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# Construct DATABASE_URL from individual environment variables if DATABASE_URL is not set
# This allows Kubernetes deployments to use separate env vars
if "DATABASE_URL" in os.environ:
    DATABASE_URL = os.getenv("DATABASE_URL", "")
else:
    # Build from individual components
    db_host = os.getenv("DATABASE_HOST", "127.0.0.1")
    db_port = os.getenv("DATABASE_PORT", "5432")
    db_user = os.getenv("DATABASE_USER", "saferoute")
    db_password = os.getenv("DATABASE_PASSWORD", "")
    db_name = os.getenv("DATABASE_NAME", "saferoute")

    # URL encode password if it contains special characters
    db_password_encoded = quote_plus(db_password) if db_password else ""

    DATABASE_URL = (
        f"postgresql+asyncpg://{db_user}:{db_password_encoded}@" f"{db_host}:{db_port}/{db_name}"
    )

engine = create_async_engine(
    DATABASE_URL,
    echo=False,  # Set to True for SQL debugging
    future=True,
)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def check_health(timeout: float = 1.0) -> Tuple[bool, Optional[str]]:
    """
    Lightweight DB health check (SELECT 1). Reuses engine pool.
    Returns (success, error_message). Safe for use in /health/ready.
    """
    try:
        async def _run() -> None:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        await asyncio.wait_for(_run(), timeout=timeout)
        return True, None
    except asyncio.TimeoutError:
        return False, "database connection timeout"
    except Exception as e:
        return False, str(e).split("\n")[0][:200]


async def get_db():
    """
    FastAPI dependency for async database session.

    Yields an async database session that is automatically closed after use.

    Yields:
        AsyncSession: SQLAlchemy async session instance

    Example:
        ```python
        @app.get("/users")
        async def get_users(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(User))
            return result.scalars().all()
        ```
    """
    async with AsyncSessionLocal() as session:
        yield session
