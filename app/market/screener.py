"""종목 검색 / 조건 검색."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from app.market import indicators
from app.market.candles import CandleService
from app.upbit.rest import UpbitClient


@dataclass
class MarketInfo:
    market: str
    korean_name: str
    english_name: str
    warning: bool  # 업비트 '유의' 지정 여부
    caution: list[str]  # 주의 사유 (가격 급등락, 거래량 급등 등)

    @property
    def symbol(self) -> str:
        return self.market.split("-", 1)[1]

    @property
    def quote(self) -> str:
        return self.market.split("-", 1)[0]


def parse_market(m: dict) -> MarketInfo:
    event = m.get("market_event") or {}
    warning = bool(event.get("warning")) or m.get("market_warning") == "CAUTION"
    caution = [k for k, v in (event.get("caution") or {}).items() if v]
    return MarketInfo(m["market"], m.get("korean_name", ""), m.get("english_name", ""), warning, caution)


class Screener:
    def __init__(self, client: UpbitClient, candles: CandleService):
        self.client = client
        self.candles = candles
        self._markets: list[MarketInfo] = []
        self._markets_at = 0.0
        self._lock = asyncio.Lock()

    async def markets(self, quote: str | None = "KRW", refresh: bool = False) -> list[MarketInfo]:
        async with self._lock:
            if refresh or not self._markets or time.monotonic() - self._markets_at > 3600:
                self._markets = [parse_market(m) for m in await self.client.get_markets(details=True)]
                self._markets_at = time.monotonic()
        return [m for m in self._markets if quote is None or m.quote == quote]

    async def info(self, market: str) -> MarketInfo | None:
        for m in await self.markets(quote=None):
            if m.market == market:
                return m
        return None

    async def symbol_names(self) -> dict[str, list[str]]:
        """심볼 → [심볼, 한글명, 영문명]. 뉴스에서 코인 언급 추출에 사용."""
        out: dict[str, list[str]] = {}
        for m in await self.markets("KRW"):
            out[m.symbol] = [m.symbol, m.korean_name, m.english_name]
        return out

    async def search(self, q: str, quote: str = "KRW", limit: int = 30) -> list[MarketInfo]:
        q = q.strip().lower()
        ms = await self.markets(quote)
        if not q:
            return ms[:limit]

        def score(m: MarketInfo) -> int:
            fields = [m.symbol.lower(), m.korean_name.lower(), m.english_name.lower()]
            if q in (fields[0], fields[1]):
                return 0
            if any(f.startswith(q) for f in fields):
                return 1
            if any(q in f for f in fields):
                return 2
            return 99

        ranked = sorted((score(m), m.market, m) for m in ms)
        return [m for s, _, m in ranked if s < 99][:limit]

    async def tickers(self, quote: str = "KRW") -> list[dict]:
        ms = await self.markets(quote)
        info = {m.market: m for m in ms}
        rows = await self.client.get_tickers([m.market for m in ms])
        out = []
        for t in rows:
            m = info.get(t["market"])
            out.append(
                {
                    "market": t["market"],
                    "symbol": m.symbol if m else t["market"],
                    "korean_name": m.korean_name if m else "",
                    "english_name": m.english_name if m else "",
                    "warning": m.warning if m else False,
                    "caution": m.caution if m else [],
                    "price": t["trade_price"],
                    "change_rate": t.get("signed_change_rate", 0.0),
                    "value_24h": t.get("acc_trade_price_24h", 0.0),
                    "high": t.get("high_price"),
                    "low": t.get("low_price"),
                }
            )
        return out

    async def scan(
        self,
        min_value_24h: float = 0,
        min_change: float | None = None,
        max_change: float | None = None,
        rsi_below: float | None = None,
        rsi_above: float | None = None,
        rsi_interval: str = "minute60",
        exclude_warning: bool = True,
        sort: str = "value_24h",
        limit: int = 50,
        rsi_top_n: int = 30,
    ) -> list[dict]:
        rows = await self.tickers("KRW")
        rows = [r for r in rows if r["value_24h"] >= min_value_24h]
        if exclude_warning:
            rows = [r for r in rows if not r["warning"]]
        if min_change is not None:
            rows = [r for r in rows if r["change_rate"] * 100 >= min_change]
        if max_change is not None:
            rows = [r for r in rows if r["change_rate"] * 100 <= max_change]
        rows.sort(key=lambda r: r.get(sort) or 0, reverse=True)

        if rsi_below is not None or rsi_above is not None:
            # RSI 는 종목마다 캔들 조회가 필요하므로 거래대금 상위 N개로 제한 (시세 API 초당 10회 제한)
            candidates = rows[:rsi_top_n]
            for r in candidates:
                df = await self.candles.fetch(r["market"], rsi_interval, 100)
                val = indicators.rsi(df["close"], 14).iloc[-1] if len(df) > 15 else None
                r["rsi"] = None if val is None or val != val else round(float(val), 2)
            rows = [
                r
                for r in candidates
                if r.get("rsi") is not None
                and (rsi_below is None or r["rsi"] <= rsi_below)
                and (rsi_above is None or r["rsi"] >= rsi_above)
            ]
        return rows[:limit]
