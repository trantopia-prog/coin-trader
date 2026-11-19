"""브로커: 모의투자(PaperBroker)와 실거래(UpbitBroker)가 같은 인터페이스를 구현한다.

엔진은 어떤 브로커가 주입되었는지 모른 채 동일한 경로로 주문한다.
"""

from __future__ import annotations

import asyncio
import logging
import uuid as uuidlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from sqlalchemy import select

from app.db import Database
from app.events import EventBus
from app.models import Holding
from app.trading.tick import FEE_RATE
from app.upbit.rest import UpbitAPIError, UpbitClient

log = logging.getLogger(__name__)


@dataclass
class OrderRequest:
    market: str
    side: str  # bid | ask
    ord_type: str  # price(시장가 매수, price=총액) | market(시장가 매도, volume) | limit
    price: float | None = None
    volume: float | None = None


@dataclass
class Fill:
    ok: bool
    state: str  # wait | done | cancel | rejected
    uuid: str | None = None
    executed_volume: float = 0.0
    avg_price: float = 0.0
    paid_fee: float = 0.0
    error: str = ""

    @property
    def funds(self) -> float:
        return self.executed_volume * self.avg_price

    @property
    def finished(self) -> bool:
        return self.state in ("done", "cancel", "rejected")


class Broker(ABC):
    mode: str

    @abstractmethod
    async def place(self, req: OrderRequest) -> Fill: ...

    @abstractmethod
    async def get_order(self, uuid: str) -> Fill: ...

    @abstractmethod
    async def cancel(self, uuid: str) -> Fill: ...

    @abstractmethod
    async def balances(self) -> dict[str, dict]:
        """currency -> {balance, locked, avg_buy_price}"""

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


# ======================================================================
# 모의투자
# ======================================================================
@dataclass
class _PaperOrder:
    uuid: str
    req: OrderRequest
    state: str = "wait"
    fill: Fill | None = None
    locked: float = field(default=0.0)


class PaperBroker(Broker):
    """실시간 호가/현재가로 체결을 흉내 낸다.

    - 시장가: 수신 중인 호가창을 위에서부터 소진하며 평균 체결가 계산 (호가가 없으면 현재가 + 슬리피지)
    - 지정가: 잔고를 잠그고, 현재가가 지정가에 닿으면 지정가로 체결
    - 수수료 0.05%, 매수 시 주문총액에 수수료가 추가로 빠져나가는 업비트 방식과 동일
    """

    mode = "paper"

    def __init__(self, db: Database, bus: EventBus, initial_krw: float, fee_rate: float = FEE_RATE, slippage: float = 0.0005):
        self.db = db
        self.bus = bus
        self.initial_krw = initial_krw
        self.fee_rate = fee_rate
        self.slippage = slippage
        self._orders: dict[str, _PaperOrder] = {}
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        async with self.db.session() as s:
            krw = await s.scalar(select(Holding).where(Holding.mode == "paper", Holding.currency == "KRW"))
            if krw is None:
                s.add(Holding(mode="paper", currency="KRW", balance=self.initial_krw))
            # 모의 지정가 주문은 메모리에만 있으므로, 재시작 시 잠긴 잔고를 풀어준다 (엔진이 해당 주문을 취소 처리)
            for h in (await s.execute(select(Holding).where(Holding.mode == "paper", Holding.locked > 0))).scalars():
                h.balance += h.locked
                h.locked = 0.0
            await s.commit()
        self._task = asyncio.create_task(self._match_limits(), name="paper-limit-matcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def reset(self, krw: float | None = None) -> None:
        async with self._lock, self.db.session() as s:
            for h in (await s.execute(select(Holding).where(Holding.mode == "paper"))).scalars():
                await s.delete(h)
            s.add(Holding(mode="paper", currency="KRW", balance=krw or self.initial_krw))
            await s.commit()
            self._orders.clear()

    async def balances(self) -> dict[str, dict]:
        async with self.db.session() as s:
            rows = (await s.execute(select(Holding).where(Holding.mode == "paper"))).scalars()
            return {
                h.currency: {"balance": h.balance, "locked": h.locked, "avg_buy_price": h.avg_buy_price}
                for h in rows
                if h.balance > 0 or h.locked > 0 or h.currency == "KRW"
            }

    def _market_fill(self, market: str, side: str, krw: float | None, volume: float | None) -> tuple[float, float] | None:
        """(체결수량, 평균가) 계산. 매수는 krw 총액 기준, 매도는 volume 기준."""
        ob = self.bus.last_orderbook.get(market)
        units = (ob or {}).get("orderbook_units") or []
        if units:
            levels = [(u["ask_price"], u["ask_size"]) for u in units] if side == "bid" else [(u["bid_price"], u["bid_size"]) for u in units]
            got_vol = got_krw = 0.0
            for px, size in levels:
                if side == "bid":
                    take = min(size, (krw - got_krw) / px)
                else:
                    take = min(size, volume - got_vol)
                got_vol += take
                got_krw += take * px
                if (side == "bid" and got_krw >= krw - 1e-9) or (side == "ask" and got_vol >= volume - 1e-12):
                    return got_vol, got_krw / got_vol
            # 호가창 깊이 부족 → 마지막 호가에서 나머지 체결로 간주
            last_px = levels[-1][0]
            if side == "bid":
                rest = (krw - got_krw) / last_px
                return got_vol + rest, krw / (got_vol + rest)
            rest = volume - got_vol
            return volume, (got_krw + rest * last_px) / volume
        price = self.bus.price(market)
        if not price:
            return None
        px = price * (1 + self.slippage) if side == "bid" else price * (1 - self.slippage)
        return ((krw / px) if side == "bid" else volume), px

    async def _holding(self, s, currency: str) -> Holding:
        h = await s.scalar(select(Holding).where(Holding.mode == "paper", Holding.currency == currency))
        if h is None:
            h = Holding(mode="paper", currency=currency, balance=0.0, locked=0.0, avg_buy_price=0.0)
            s.add(h)
        return h

    async def place(self, req: OrderRequest) -> Fill:
        oid = "paper-" + uuidlib.uuid4().hex
        coin = req.market.split("-", 1)[1]
        async with self._lock, self.db.session() as s:
            krw = await self._holding(s, "KRW")
            hold = await self._holding(s, coin)

            if req.ord_type == "price" and req.side == "bid":
                amount = req.price or 0
                cost = amount * (1 + self.fee_rate)
                if cost > krw.balance + 1e-6:
                    return Fill(False, "rejected", oid, error=f"원화 잔고 부족 (필요 {cost:,.0f} / 보유 {krw.balance:,.0f})")
                res = self._market_fill(req.market, "bid", amount, None)
                if not res:
                    return Fill(False, "rejected", oid, error="시세 정보 없음")
                vol, px = res
                krw.balance -= cost
                new_bal = hold.balance + vol
                hold.avg_buy_price = (hold.balance * hold.avg_buy_price + amount) / new_bal
                hold.balance = new_bal
                fill = Fill(True, "done", oid, vol, px, amount * self.fee_rate)

            elif req.ord_type == "market" and req.side == "ask":
                vol = min(req.volume or 0, hold.balance)
                if vol <= 0:
                    return Fill(False, "rejected", oid, error="매도 가능 수량 없음")
                res = self._market_fill(req.market, "ask", None, vol)
                if not res:
                    return Fill(False, "rejected", oid, error="시세 정보 없음")
                vol, px = res
                proceeds = vol * px
                fee = proceeds * self.fee_rate
                hold.balance -= vol
                if hold.balance <= 1e-12:
                    hold.balance, hold.avg_buy_price = 0.0, 0.0
                krw.balance += proceeds - fee
                fill = Fill(True, "done", oid, vol, px, fee)

            elif req.ord_type == "limit":
                px, vol = req.price or 0, req.volume or 0
                if req.side == "bid":
                    need = px * vol * (1 + self.fee_rate)
                    if need > krw.balance + 1e-6:
                        return Fill(False, "rejected", oid, error="원화 잔고 부족")
                    krw.balance -= need
                    krw.locked += need
                    locked = need
                else:
                    if vol > hold.balance + 1e-12:
                        return Fill(False, "rejected", oid, error="매도 가능 수량 부족")
                    hold.balance -= vol
                    hold.locked += vol
                    locked = vol
                self._orders[oid] = _PaperOrder(oid, req, locked=locked)
                fill = Fill(True, "wait", oid)
            else:
                return Fill(False, "rejected", oid, error=f"지원하지 않는 주문 유형: {req.side}/{req.ord_type}")
            await s.commit()
        if fill.state == "done":
            self._orders[oid] = _PaperOrder(oid, req, state="done", fill=fill)
        return fill

    async def get_order(self, uuid: str) -> Fill:
        o = self._orders.get(uuid)
        if not o:
            return Fill(False, "cancel", uuid, error="알 수 없는 주문 (재시작으로 모의 미체결 주문이 사라졌을 수 있음)")
        return o.fill or Fill(True, o.state, uuid)

    async def cancel(self, uuid: str) -> Fill:
        async with self._lock:
            o = self._orders.get(uuid)
            if not o or o.state != "wait":
                return Fill(False, o.state if o else "cancel", uuid, error="취소할 수 없는 주문")
            coin = o.req.market.split("-", 1)[1]
            async with self.db.session() as s:
                h = await self._holding(s, "KRW" if o.req.side == "bid" else coin)
                h.locked -= o.locked
                h.balance += o.locked
                await s.commit()
            o.state = "cancel"
            o.fill = Fill(True, "cancel", uuid)
            return o.fill

    async def _match_limits(self) -> None:
        q = self.bus.subscribe("ticker")
        try:
            while True:
                _, t = await q.get()
                waiting = [o for o in self._orders.values() if o.state == "wait" and o.req.market == t["code"]]
                for o in waiting:
                    price = float(t["trade_price"])
                    if (o.req.side == "bid" and price <= (o.req.price or 0)) or (o.req.side == "ask" and price >= (o.req.price or 0)):
                        await self._fill_limit(o)
        except asyncio.CancelledError:
            self.bus.unsubscribe(q)
            raise

    async def _fill_limit(self, o: _PaperOrder) -> None:
        async with self._lock:
            if o.state != "wait":
                return
            req = o.req
            coin = req.market.split("-", 1)[1]
            px, vol = req.price or 0, req.volume or 0
            async with self.db.session() as s:
                krw = await self._holding(s, "KRW")
                hold = await self._holding(s, coin)
                if req.side == "bid":
                    krw.locked -= o.locked
                    new_bal = hold.balance + vol
                    hold.avg_buy_price = (hold.balance * hold.avg_buy_price + px * vol) / new_bal
                    hold.balance = new_bal
                    fee = px * vol * self.fee_rate
                else:
                    hold.locked -= o.locked
                    fee = px * vol * self.fee_rate
                    krw.balance += px * vol - fee
                await s.commit()
            o.state = "done"
            o.fill = Fill(True, "done", o.uuid, vol, px, fee)
        self.bus.publish("order_update", {"uuid": o.uuid})


# ======================================================================
# 실거래
# ======================================================================
def parse_order(resp: dict) -> Fill:
    trades = resp.get("trades") or []
    vol = sum(float(t.get("volume", 0)) for t in trades)
    funds = sum(float(t.get("funds", 0)) for t in trades)
    executed = float(resp.get("executed_volume") or vol or 0)
    avg = funds / vol if vol > 0 else float(resp.get("price") or 0) if executed else 0.0
    return Fill(
        ok=True,
        state=resp.get("state", "wait"),
        uuid=resp.get("uuid"),
        executed_volume=executed,
        avg_price=avg,
        paid_fee=float(resp.get("paid_fee") or 0),
    )


class UpbitBroker(Broker):
    mode = "live"

    def __init__(self, client: UpbitClient):
        self.client = client

    async def place(self, req: OrderRequest) -> Fill:
        try:
            resp = await self.client.place_order(req.market, req.side, req.ord_type, volume=req.volume, price=req.price)
        except UpbitAPIError as e:
            return Fill(False, "rejected", error=str(e))
        fill = parse_order(resp)
        if req.ord_type in ("price", "market"):
            # 시장가 주문은 곧바로 체결되므로 잠깐 폴링해 체결 내역(평균가)을 가져온다
            for _ in range(10):
                await asyncio.sleep(0.3)
                try:
                    fill = parse_order(await self.client.get_order(fill.uuid))
                except UpbitAPIError as e:
                    log.warning("주문 조회 실패 %s: %s", fill.uuid, e)
                    continue
                if fill.finished:
                    break
        return fill

    async def get_order(self, uuid: str) -> Fill:
        return parse_order(await self.client.get_order(uuid))

    async def cancel(self, uuid: str) -> Fill:
        try:
            await self.client.cancel_order(uuid)
        except UpbitAPIError as e:
            return Fill(False, "wait", uuid, error=str(e))
        return await self.get_order(uuid)

    async def balances(self) -> dict[str, dict]:
        return {
            a["currency"]: {
                "balance": float(a["balance"]),
                "locked": float(a["locked"]),
                "avg_buy_price": float(a.get("avg_buy_price") or 0),
            }
            for a in await self.client.get_accounts()
        }
