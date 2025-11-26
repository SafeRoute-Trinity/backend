# backend/libs/db.py
import os

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

# 本地开发时可以用 127.0.0.1（配合 kubectl port-forward）
# 部署到 K8s 时，把 host 改成 postgresql 服务名即可
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://saferoute:YOUR_PASSWORD_HERE@127.0.0.1:5432/saferoute",
)

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
