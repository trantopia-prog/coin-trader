"""업비트 공지사항 (코인 시장의 사실상 '공시').

업비트 웹이 사용하는 비공식 엔드포인트라 예고 없이 바뀔 수 있다. 실패해도 다른 수집기에는 영향이 없다.
"""

from __future__ import annotations

from app.news.base import CollectContext, NewsCollector, NewsDraft, classify_notice, extract_coins, parse_dt

NOTICE_API = "https://api-manager.upbit.com/api/v1/announcements"
NOTICE_PAGE = "https://upbit.com/service_center/notice?id={id}"


class UpbitNoticeCollector(NewsCollector):
    name = "upbit_notice"
    interval_sec = 60  # 유의종목/거래지원종료는 빨리 반영해야 하므로 짧게

    async def fetch(self, ctx: CollectContext) -> list[NewsDraft]:
        resp = await ctx.http.get(NOTICE_API, params={"os": "web", "page": 1, "per_page": 30, "category": "all"})
        resp.raise_for_status()
        data = resp.json().get("data") or {}
        out: list[NewsDraft] = []
        for n in data.get("notices") or []:
            title = n.get("title", "")
            out.append(
                NewsDraft(
                    source=self.name,
                    title=title,
                    url=NOTICE_PAGE.format(id=n.get("id")),
                    published_at=parse_dt(n.get("first_listed_at") or n.get("listed_at")),
                    coins=extract_coins(title, ctx.symbol_names),
                    summary=n.get("category", ""),
                    category="notice",
                    importance=classify_notice(title),
                )
            )
        return out
