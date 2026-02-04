# backend/libs/postgis_db.py
import asyncio
import os
from typing import Optional, Tuple

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

POSTGIS_DATABASE_URL = os.getenv(
    "POSTGIS_DATABASE_URL",
    # 本地开发默认：通过 5433 连接上面建的 saferoute_geo
    "postgresql+asyncpg://POSTGIS_USER:POSTGIS_PASSWORD@127.0.0.1:5433/saferoute_geo",
)

engine_postgis = create_async_engine(
    POSTGIS_DATABASE_URL,
    echo=False,
    future=True,
)

AsyncPostgisSessionLocal = sessionmaker(
    bind=engine_postgis,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def check_postgis_health(timeout: float = 1.0) -> Tuple[bool, Optional[str]]:
    """
    PostGIS health check (SELECT PostGIS_Version()). Reuses engine pool.
    Returns (success, error_message). Safe for use in /health/ready.
    """
    try:
        async def _run() -> None:
            async with engine_postgis.connect() as conn:
                await conn.execute(text("SELECT PostGIS_Version()"))
        await asyncio.wait_for(_run(), timeout=timeout)
        return True, None
    except asyncio.TimeoutError:
        return False, "postgis check timeout"
    except Exception as e:
        return False, str(e).split("\n")[0][:200]


async def get_postgis_db() -> AsyncSession:
    """给 FastAPI 依赖注入用的 PostGIS Session。"""
    async with AsyncPostgisSessionLocal() as session:
        yield session
