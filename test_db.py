import os, asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.getenv("DATABASE_URL")

async def main():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not set")

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.connect() as conn:
        rows = (await conn.execute(text("""
            SELECT feedback_id, status, type, severity, created_at
            FROM saferoute.feedback
            ORDER BY created_at DESC
            LIMIT 5;
        """))).all()

        print("Latest 5 feedback rows:")
        for r in rows:
            print(r)

    await engine.dispose()

asyncio.run(main())
