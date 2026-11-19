"""Google News RSS 검색. (피드 약관상 개인적·비상업적 용도로만 사용 가능)"""

from __future__ import annotations

import asyncio
import calendar
from datetime import datetime, timezone
from urllib.parse import quote

import feedparser

from app.news.base import CollectContext, NewsCollector, NewsDraft, extract_coins

RSS_URL = "https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"


async def fetch_rss(ctx: CollectContext, query: str, source: str, category: str = "news") -> list[NewsDraft]:
    resp = await ctx.http.get(RSS_URL.format(q=quote(query)))
    resp.raise_for_status()
    feed = await asyncio.to_thread(feedparser.parse, resp.content)
    out: list[NewsDraft] = []
    for e in feed.entries[:30]:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime.fromtimestamp(calendar.timegm(ts), timezone.utc) if ts else datetime.now(timezone.utc)
        title = e.get("title", "")
        out.append(
            NewsDraft(
                source=source,
                title=title,
                url=e.get("link", ""),
                published_at=published,
                coins=extract_coins(title, ctx.symbol_names),
                summary=(e.get("source") or {}).get("title", "") if isinstance(e.get("source"), dict) else "",
                category=category,
            )
        )
    return [d for d in out if d.url]


class GoogleNewsCollector(NewsCollector):
    name = "google_news"
    interval_sec = 600

    async def fetch(self, ctx: CollectContext) -> list[NewsDraft]:
        queries = ["가상자산 OR 암호화폐"]
        for sym in ctx.watch_symbols:
            names = ctx.symbol_names.get(sym)
            queries.append(f"{names[1]} 코인" if names and names[1] else f"{sym} 코인")
        out: list[NewsDraft] = []
        for q in queries:
            try:
                out.extend(await fetch_rss(ctx, q, self.name))
            except Exception:  # noqa: BLE001 - 쿼리 하나 실패해도 나머지는 진행
                continue
            await asyncio.sleep(1.0)  # 과도한 요청 방지
        return out
