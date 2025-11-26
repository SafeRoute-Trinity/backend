# backend/libs/postgis_db.py
import os

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


async def get_postgis_db() -> AsyncSession:
    """给 FastAPI 依赖注入用的 PostGIS Session。"""
    async with AsyncPostgisSessionLocal() as session:
        yield session
