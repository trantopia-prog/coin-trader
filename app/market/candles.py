from __future__ import annotations

import asyncio
import time

import pandas as pd

from app.upbit.rest import UpbitClient

COLUMNS = ["time", "open", "high", "low", "close", "volume", "value"]


def to_frame(raw: list[dict]) -> pd.DataFrame:
    """업비트 캔들 응답 → 시간 오름차순 DataFrame. time 은 UTC epoch 초."""
    if not raw:
        return pd.DataFrame(columns=COLUMNS)
    df = pd.DataFrame(
        {
            "time": pd.to_datetime([c["candle_date_time_utc"] for c in raw], utc=True),
            "open": [float(c["opening_price"]) for c in raw],
            "high": [float(c["high_price"]) for c in raw],
            "low": [float(c["low_price"]) for c in raw],
            "close": [float(c["trade_price"]) for c in raw],
            "volume": [float(c.get("candle_acc_trade_volume", 0)) for c in raw],
            "value": [float(c.get("candle_acc_trade_price", 0)) for c in raw],
        }
    )
    # pandas 버전마다 기본 해상도(ns/us)가 달라서 초 단위로 명시 변환
    df["time"] = df["time"].dt.as_unit("s").astype("int64")
    return df.drop_duplicates("time").sort_values("time").reset_index(drop=True)


def ttl_for(interval: str) -> float:
    return 3.0 if interval.startswith("minute") else 30.0


class CandleService:
    def __init__(self, client: UpbitClient, max_count: int = 2000):
        self.client = client
        self.max_count = max_count
        self._cache: dict[tuple[str, str, int], tuple[float, pd.DataFrame]] = {}
        self._locks: dict[tuple[str, str, int], asyncio.Lock] = {}

    async def fetch(self, market: str, interval: str, count: int = 200, use_cache: bool = True) -> pd.DataFrame:
        count = max(1, min(count, self.max_count))
        key = (market, interval, count)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            hit = self._cache.get(key)
            if use_cache and hit and hit[0] > time.monotonic():
                return hit[1].copy()
            df = await self._fetch_paged(market, interval, count)
            self._cache[key] = (time.monotonic() + ttl_for(interval), df)
            return df.copy()

    async def _fetch_paged(self, market: str, interval: str, count: int) -> pd.DataFrame:
        raw: list[dict] = []
        to: str | None = None
        while len(raw) < count:
            batch = await self.client.get_candles(market, interval, count=min(200, count - len(raw)), to=to)
            if not batch:
                break
            raw.extend(batch)
            to = batch[-1]["candle_date_time_utc"] + "Z"  # 가장 오래된 캔들 이전으로 (to 는 exclusive)
            if len(batch) < 200:
                break
        return to_frame(raw)
