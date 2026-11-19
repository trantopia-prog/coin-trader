"""DAXA(디지털자산거래소 공동협의체) / 쟁글(Xangle) 공시.

두 곳 모두 공식 공개 API 가 없다. 그래서 두 가지 경로를 쓴다.
1) DISCLOSURE_SCRAPE_URLS 에 지정한 페이지를 BeautifulSoup 로 긁어 링크/제목을 추출 (사이트 구조가 바뀌면 깨질 수 있음)
2) Google News 에서 DAXA/쟁글 관련 기사 검색 (항상 동작하는 대체 경로)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.news.base import CollectContext, NewsCollector, NewsDraft, classify_notice, extract_coins
from app.news.google_rss import fetch_rss

FALLBACK_QUERIES = ["DAXA 유의종목 OR 거래지원종료", "쟁글 공시 코인"]
KEYWORDS = ("공시", "유의", "거래지원", "상장", "disclosure", "notice")


class DisclosureCollector(NewsCollector):
    name = "disclosure"
    interval_sec = 900

    def __init__(self, scrape_urls: list[str]):
        self.scrape_urls = scrape_urls

    async def fetch(self, ctx: CollectContext) -> list[NewsDraft]:
        out: list[NewsDraft] = []
        for url in self.scrape_urls:
            try:
                out.extend(await self._scrape(ctx, url))
            except Exception:  # noqa: BLE001
                continue
        for q in FALLBACK_QUERIES:
            try:
                drafts = await fetch_rss(ctx, q, self.name, category="disclosure")
            except Exception:  # noqa: BLE001
                continue
            for d in drafts:
                d.importance = classify_notice(d.title)
            out.extend(drafts)
            await asyncio.sleep(1.0)
        return out

    async def _scrape(self, ctx: CollectContext, url: str) -> list[NewsDraft]:
        resp = await ctx.http.get(url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        now = datetime.now(timezone.utc)
        out: list[NewsDraft] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            title = " ".join(a.get_text(" ", strip=True).split())
            if len(title) < 8 or not any(k in title.lower() for k in KEYWORDS):
                continue
            link = urljoin(url, a["href"])
            if link in seen:
                continue
            seen.add(link)
            out.append(
                NewsDraft(
                    source=self.name,
                    title=title,
                    url=link,
                    published_at=now,
                    coins=extract_coins(title, ctx.symbol_names),
                    category="disclosure",
                    importance=classify_notice(title),
                )
            )
        return out[:50]
