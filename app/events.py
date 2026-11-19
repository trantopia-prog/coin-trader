"""프로세스 내부 pub/sub. 업비트 WebSocket → 엔진/모의브로커/브라우저 WebSocket 으로 이벤트를 퍼뜨린다."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from typing import Any

log = logging.getLogger(__name__)


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self.last_ticker: dict[str, dict[str, Any]] = {}
        self.last_orderbook: dict[str, dict[str, Any]] = {}

    def subscribe(self, *topics: str, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        for t in topics:
            self._subs[t].add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        for subs in self._subs.values():
            subs.discard(q)

    def publish(self, topic: str, data: dict[str, Any]) -> None:
        if topic == "ticker":
            self.last_ticker[data["code"]] = data
        elif topic == "orderbook":
            self.last_orderbook[data["code"]] = data
        for q in list(self._subs.get(topic, ())):
            try:
                q.put_nowait((topic, data))
            except asyncio.QueueFull:
                # 느린 구독자(예: 백그라운드 탭의 브라우저)는 오래된 이벤트를 버린다
                try:
                    q.get_nowait()
                    q.put_nowait((topic, data))
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def price(self, market: str) -> float | None:
        t = self.last_ticker.get(market)
        return float(t["trade_price"]) if t else None
