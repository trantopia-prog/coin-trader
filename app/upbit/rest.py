"""업비트 REST 클라이언트 (async).

시세 API 는 키 없이 사용 가능하고, 거래 API 는 access/secret 키가 필요하다.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.upbit.auth import auth_header
from app.upbit.ratelimit import RateLimiter

log = logging.getLogger(__name__)

BASE_URL = "https://api.upbit.com/v1"

MINUTE_UNITS = {1, 3, 5, 10, 15, 30, 60, 240}
INTERVALS = [f"minute{u}" for u in sorted(MINUTE_UNITS)] + ["day", "week", "month"]


class UpbitAPIError(Exception):
    def __init__(self, status: int, name: str, message: str):
        super().__init__(f"[{status}] {name}: {message}")
        self.status = status
        self.name = name
        self.message = message


def candle_path(interval: str) -> str:
    if interval.startswith("minute"):
        unit = int(interval.removeprefix("minute"))
        if unit not in MINUTE_UNITS:
            raise ValueError(f"지원하지 않는 분봉 단위: {unit}")
        return f"/candles/minutes/{unit}"
    if interval in ("day", "week", "month"):
        return f"/candles/{interval}s"
    raise ValueError(f"지원하지 않는 캔들 주기: {interval}")


class UpbitClient:
    def __init__(
        self,
        access_key: str = "",
        secret_key: str = "",
        http: httpx.AsyncClient | None = None,
        limiter: RateLimiter | None = None,
    ):
        self.access_key = access_key
        self.secret_key = secret_key
        self._http = http or httpx.AsyncClient(base_url=BASE_URL, timeout=10.0)
        self.limiter = limiter or RateLimiter()

    @property
    def has_keys(self) -> bool:
        return bool(self.access_key and self.secret_key)

    def set_keys(self, access_key: str, secret_key: str) -> None:
        self.access_key, self.secret_key = access_key, secret_key

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        private: bool = False,
        group: str = "quotation",
        retries: int = 3,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        body = {k: v for k, v in (body or {}).items() if v is not None} if body is not None else None
        for attempt in range(retries + 1):
            await self.limiter.acquire(group)
            headers: dict[str, str] = {}
            if private:
                if not self.has_keys:
                    raise UpbitAPIError(401, "no_api_key", "업비트 API 키가 설정되지 않았습니다.")
                headers.update(auth_header(self.access_key, self.secret_key, body if body is not None else params))
            try:
                resp = await self._http.request(method, path, params=params or None, json=body, headers=headers)
            except httpx.TransportError as e:
                if attempt >= retries:
                    raise UpbitAPIError(0, "network_error", str(e)) from e
                await asyncio.sleep(0.5 * 2**attempt)
                continue

            self.limiter.feedback(group, resp.headers.get("Remaining-Req"))
            if resp.status_code == 429 and attempt < retries:
                self.limiter.backoff(group, 0.5 * 2**attempt)
                continue
            if resp.status_code >= 500 and attempt < retries and method == "GET":
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if resp.status_code >= 400:
                name, message = "http_error", resp.text[:300]
                try:
                    err = resp.json().get("error", {})
                    name, message = err.get("name", name), err.get("message", message)
                except ValueError:
                    pass
                raise UpbitAPIError(resp.status_code, str(name), str(message))
            return resp.json()
        raise UpbitAPIError(429, "too_many_requests", "요청 제한 초과")

    # ---------------- 시세 ----------------
    async def get_markets(self, details: bool = True) -> list[dict]:
        return await self._request("GET", "/market/all", params={"isDetails": str(details).lower()})

    async def get_candles(self, market: str, interval: str, count: int = 200, to: str | None = None) -> list[dict]:
        """최신 → 과거 순으로 반환 (업비트 원본 순서). count 최대 200."""
        return await self._request(
            "GET", candle_path(interval), params={"market": market, "count": min(count, 200), "to": to}
        )

    async def get_tickers(self, markets: list[str]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(markets), 100):
            chunk = markets[i : i + 100]
            out.extend(await self._request("GET", "/ticker", params={"markets": ",".join(chunk)}))
        return out

    async def get_orderbook(self, markets: list[str]) -> list[dict]:
        return await self._request("GET", "/orderbook", params={"markets": ",".join(markets)})

    async def get_trades(self, market: str, count: int = 50) -> list[dict]:
        return await self._request("GET", "/trades/ticks", params={"market": market, "count": count})

    # ---------------- 거래 ----------------
    async def get_accounts(self) -> list[dict]:
        return await self._request("GET", "/accounts", private=True, group="exchange")

    async def get_order_chance(self, market: str) -> dict:
        return await self._request("GET", "/orders/chance", params={"market": market}, private=True, group="exchange")

    async def place_order(
        self,
        market: str,
        side: str,
        ord_type: str,
        volume: float | None = None,
        price: float | None = None,
        identifier: str | None = None,
    ) -> dict:
        """side: bid(매수)/ask(매도), ord_type: limit(지정가) / price(시장가 매수, price=총액) / market(시장가 매도)."""
        body = {
            "market": market,
            "side": side,
            "ord_type": ord_type,
            "volume": _fmt(volume) if volume is not None else None,
            "price": _fmt(price) if price is not None else None,
            "identifier": identifier,
        }
        return await self._request("POST", "/orders", body=body, private=True, group="order", retries=0)

    async def get_order(self, uuid: str) -> dict:
        return await self._request("GET", "/order", params={"uuid": uuid}, private=True, group="exchange")

    async def cancel_order(self, uuid: str) -> dict:
        return await self._request("DELETE", "/order", params={"uuid": uuid}, private=True, group="exchange", retries=0)

    async def get_open_orders(self, market: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"market": market, "states[]": ["wait", "watch"]}
        return await self._request("GET", "/orders/open", params=params, private=True, group="exchange")


def _fmt(x: float) -> str:
    """업비트는 숫자를 문자열로 받는다. 지수표기를 피하고 불필요한 0 을 제거."""
    s = f"{x:.8f}".rstrip("0").rstrip(".")
    return s or "0"
