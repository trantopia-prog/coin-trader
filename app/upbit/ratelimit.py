"""업비트 요청 수 제한 대응.

업비트는 그룹별 초당 요청 수를 제한하고 응답 헤더 `Remaining-Req: group=default; min=1800; sec=29` 로 잔여량을 알려준다.
- 시세(Quotation) API: 초당 10회 (IP 단위)
- 거래(Exchange) 주문 API: 초당 8회, 그 외 거래 API: 초당 30회 (계정 단위)
여기서는 그룹별 슬라이딩 윈도우로 선제 제한하고, 헤더의 sec 잔여량이 바닥나면 다음 초까지 대기한다.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque

GROUP_LIMITS = {"quotation": 10, "order": 8, "exchange": 30}
_REMAINING_RE = re.compile(r"group=(?P<group>[\w-]+);\s*min=(?P<min>\d+);\s*sec=(?P<sec>\d+)")


def parse_remaining_req(header: str | None) -> tuple[str, int] | None:
    if not header:
        return None
    m = _REMAINING_RE.search(header)
    if not m:
        return None
    return m.group("group"), int(m.group("sec"))


class RateLimiter:
    def __init__(self, limits: dict[str, int] | None = None, window: float = 1.0):
        self.limits = limits or GROUP_LIMITS
        self.window = window
        self._calls: dict[str, deque[float]] = {g: deque() for g in self.limits}
        self._locks: dict[str, asyncio.Lock] = {}
        self._blocked_until: dict[str, float] = {}

    def _lock(self, group: str) -> asyncio.Lock:
        if group not in self._locks:
            self._locks[group] = asyncio.Lock()
        return self._locks[group]

    async def acquire(self, group: str) -> None:
        limit = self.limits.get(group, 10)
        calls = self._calls.setdefault(group, deque())
        async with self._lock(group):
            while True:
                now = time.monotonic()
                blocked = self._blocked_until.get(group, 0.0)
                if blocked > now:
                    await asyncio.sleep(blocked - now)
                    continue
                while calls and now - calls[0] >= self.window:
                    calls.popleft()
                if len(calls) < limit:
                    calls.append(now)
                    return
                await asyncio.sleep(self.window - (now - calls[0]) + 0.005)

    def feedback(self, group: str, remaining_header: str | None) -> None:
        parsed = parse_remaining_req(remaining_header)
        if parsed and parsed[1] <= 0:
            self._blocked_until[group] = time.monotonic() + self.window

    def backoff(self, group: str, seconds: float) -> None:
        self._blocked_until[group] = max(self._blocked_until.get(group, 0.0), time.monotonic() + seconds)
