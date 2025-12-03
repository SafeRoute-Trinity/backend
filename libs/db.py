# backend/libs/db.py
import os

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# Construct DATABASE_URL from individual environment variables if DATABASE_URL is not set
# This allows Kubernetes deployments to use separate env vars
if "DATABASE_URL" in os.environ:
    DATABASE_URL = os.getenv("DATABASE_URL")
else:
    # Build from individual components
    db_host = os.getenv("DATABASE_HOST", "127.0.0.1")
    db_port = os.getenv("DATABASE_PORT", "5432")
    db_user = os.getenv("DATABASE_USER", "saferoute")
    db_password = os.getenv("DATABASE_PASSWORD", "")
    db_name = os.getenv("DATABASE_NAME", "saferoute")

    # URL encode password if it contains special characters
    from urllib.parse import quote_plus

    db_password_encoded = quote_plus(db_password) if db_password else ""

    DATABASE_URL = f"postgresql+asyncpg://{db_user}:{db_password_encoded}@{db_host}:{db_port}/{db_name}"

engine = create_async_engine(
    DATABASE_URL,
    echo=False,  # 调试时可以改成 True 看 SQL
    future=True,
)

AsyncSessionLocal = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


async def get_db() -> AsyncSession:
    """FastAPI 依赖注入用的异步 Session。"""
    async with AsyncSessionLocal() as session:
        yield session
