"""뉴스 수집 스케줄러와 조회.

수집기마다 독립 task 로 돌려서 하나가 실패하거나 느려도 나머지에 영향이 없게 한다.
새 항목은 DB 에 저장(url 해시로 중복 제거) 후 이벤트 버스로 브라우저에 push 하고,
유의종목/거래지원종료 공지는 리스크 매니저에 전달해 해당 코인 매수를 차단한다.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
from sqlalchemy import or_, select

from app.db import Database
from app.events import EventBus
from app.models import NewsItem, iso
from app.news.base import USER_AGENT, CollectContext, NewsCollector, NewsDraft

log = logging.getLogger(__name__)

BLOCKING = ("warning", "delisting")


class NewsService:
    def __init__(
        self,
        db: Database,
        bus: EventBus,
        collectors: list[NewsCollector],
        symbol_names: Callable,  # async () -> dict[str, list[str]]
        watch_symbols: Callable[[], list[str]],
        on_flag: Callable[[str, str, bool], None] | None = None,  # (symbol, reason, flagged)
    ):
        self.db = db
        self.bus = bus
        self.collectors = collectors
        self._symbol_names = symbol_names
        self._watch_symbols = watch_symbols
        self.on_flag = on_flag
        self.status: dict[str, dict] = {c.name: {"ok": None, "last_run": None, "error": "", "new": 0} for c in collectors}
        self._tasks: list[asyncio.Task] = []
        self._http = httpx.AsyncClient(timeout=15.0, headers={"User-Agent": USER_AGENT}, follow_redirects=True)

    async def start(self) -> None:
        await self._restore_flags()
        for c in self.collectors:
            self._tasks.append(asyncio.create_task(self._loop(c), name=f"news:{c.name}"))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._http.aclose()

    async def _loop(self, c: NewsCollector) -> None:
        await asyncio.sleep(2)
        while True:
            await self.run_once(c)
            await asyncio.sleep(c.interval_sec)

    async def run_once(self, c: NewsCollector) -> int:
        st = self.status[c.name]
        st["last_run"] = datetime.now(timezone.utc).isoformat()
        try:
            names = await self._symbol_names()
        except Exception:  # noqa: BLE001 - 종목 목록 조회 실패 시에도 수집은 진행
            names = {}
        ctx = CollectContext(self._http, names, self._watch_symbols())
        try:
            drafts = await c.fetch(ctx)
            new = await self.save(drafts)
            st.update(ok=True, error="", new=len(new))
            return len(new)
        except Exception as e:  # noqa: BLE001
            log.warning("뉴스 수집 실패 [%s]: %s", c.name, e)
            st.update(ok=False, error=str(e)[:300])
            return 0

    async def save(self, drafts: list[NewsDraft]) -> list[NewsItem]:
        if not drafts:
            return []
        uniq = {d.url_hash: d for d in drafts if d.url}
        async with self.db.session() as s:
            existing = set(
                (await s.execute(select(NewsItem.url_hash).where(NewsItem.url_hash.in_(list(uniq))))).scalars()
            )
            new_items = [
                NewsItem(
                    source=d.source,
                    title=d.title[:500],
                    url=d.url,
                    url_hash=h,
                    coins="," + ",".join(d.coins) + "," if d.coins else "",
                    published_at=d.published_at,
                    summary=d.summary[:1000],
                    category=d.category,
                    importance=d.importance,
                    sentiment=d.sentiment,
                )
                for h, d in uniq.items()
                if h not in existing
            ]
            s.add_all(new_items)
            await s.commit()
        for item in sorted(new_items, key=lambda i: i.published_at):
            self.bus.publish("news", serialize(item))
            self._apply_flag(item)
        return new_items

    def _apply_flag(self, item: NewsItem) -> None:
        if not self.on_flag or item.category != "notice":
            return
        coins = [c for c in item.coins.split(",") if c]
        for sym in coins:
            if item.importance in BLOCKING:
                self.on_flag(sym, item.title, True)
            elif item.importance == "release":
                self.on_flag(sym, item.title, False)

    async def _restore_flags(self) -> None:
        since = datetime.now(timezone.utc) - timedelta(days=30)
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(NewsItem)
                    .where(NewsItem.category == "notice", NewsItem.published_at >= since)
                    .order_by(NewsItem.published_at)
                )
            ).scalars()
            for item in rows:
                self._apply_flag(item)

    async def query(
        self,
        coin: str | None = None,
        source: str | None = None,
        category: str | None = None,
        q: str | None = None,
        important_only: bool = False,
        limit: int = 50,
        before_id: int | None = None,
    ) -> list[dict]:
        stmt = select(NewsItem).order_by(NewsItem.published_at.desc(), NewsItem.id.desc()).limit(min(limit, 200))
        if coin:
            stmt = stmt.where(NewsItem.coins.like(f"%,{coin.upper()},%"))
        if source:
            stmt = stmt.where(NewsItem.source == source)
        if category:
            stmt = stmt.where(NewsItem.category == category)
        if q:
            for word in q.split():
                stmt = stmt.where(or_(NewsItem.title.contains(word), NewsItem.summary.contains(word)))
        if important_only:
            stmt = stmt.where(NewsItem.importance != "normal")
        if before_id:
            stmt = stmt.where(NewsItem.id < before_id)
        async with self.db.session() as s:
            return [serialize(i) for i in (await s.execute(stmt)).scalars()]


def serialize(i: NewsItem) -> dict:
    return {
        "id": i.id,
        "source": i.source,
        "title": i.title,
        "url": i.url,
        "coins": [c for c in i.coins.split(",") if c],
        "published_at": iso(i.published_at),
        "summary": i.summary,
        "category": i.category,
        "importance": i.importance,
        "sentiment": i.sentiment,
    }
