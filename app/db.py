from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import DATA_DIR


class Base(DeclarativeBase):
    pass


class Database:
    """SQLite 는 쓰기 트랜잭션이 하나만 가능하다.
    세션은 짧게 열고 닫을 것: 세션을 연 채로 네트워크 호출이나 다른 세션의 쓰기를 기다리면 잠금이 걸린다."""

    def __init__(self, url: str):
        is_sqlite = url.startswith("sqlite")
        if is_sqlite:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(
            url, future=True, connect_args={"timeout": 30} if is_sqlite else {}
        )
        if is_sqlite:

            @event.listens_for(self.engine.sync_engine, "connect")
            def _pragma(dbapi_conn, _):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()

        self.session: async_sessionmaker[AsyncSession] = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        from app import models  # noqa: F401  (모델 등록)

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()
