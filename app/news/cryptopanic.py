"""CryptoPanic 뉴스 집계 API (무료 토큰 필요). 토큰이 없으면 비활성."""

from __future__ import annotations

from app.news.base import CollectContext, NewsCollector, NewsDraft, extract_coins, parse_dt

API_URL = "https://cryptopanic.com/api/developer/v2/posts/"


class CryptoPanicCollector(NewsCollector):
    name = "cryptopanic"
    interval_sec = 600

    def __init__(self, token: str):
        self.token = token

    async def fetch(self, ctx: CollectContext) -> list[NewsDraft]:
        params = {"auth_token": self.token, "public": "true", "kind": "news"}
        if ctx.watch_symbols:
            params["currencies"] = ",".join(ctx.watch_symbols)
        resp = await ctx.http.get(API_URL, params=params)
        resp.raise_for_status()
        out: list[NewsDraft] = []
        for p in resp.json().get("results") or []:
            title = p.get("title", "")
            url = p.get("original_url") or p.get("url")
            if not url and p.get("slug"):
                url = f"https://cryptopanic.com/news/{p.get('id')}/{p['slug']}"
            if not url:
                continue
            instruments = p.get("instruments") or p.get("currencies") or []
            coins = [c.get("code") for c in instruments if isinstance(c, dict) and c.get("code")]
            if not coins:
                coins = extract_coins(title, ctx.symbol_names)
            votes = p.get("votes") or {}
            pos, neg = float(votes.get("positive", 0) or 0), float(votes.get("negative", 0) or 0)
            sentiment = (pos - neg) / (pos + neg) if pos + neg > 0 else None
            out.append(
                NewsDraft(
                    source=self.name,
                    title=title,
                    url=url,
                    published_at=parse_dt(p.get("published_at")),
                    coins=coins,
                    summary=p.get("description", "") or "",
                    sentiment=sentiment,
                )
            )
        return out
