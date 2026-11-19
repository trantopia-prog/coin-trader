"""자동매매 엔진.

흐름: (주기 실행) 전략 평가 → Signal → 수량/예산 보정 → RiskManager 검사 → Broker 주문 → Order 기록
      → 전략 포지션(StrategyRuntime) 갱신 → strategy.on_fill → 이벤트 버스로 UI push

- 모의/실거래 전환은 broker 교체로만 이루어진다. 실거래는 TRADING_MODE=live 이고 UI 에서 비밀번호로 '무장(arm)' 했을 때만 가능.
- 같은 종목에 대한 주문은 종목별 lock 으로 직렬화한다.
- DB 세션은 짧게: 세션을 연 채로 브로커(네트워크/다른 세션 쓰기)를 호출하지 않는다 (SQLite 쓰기 잠금 방지).
"""

from __future__ import annotations

import asyncio
import copy
import logging
import math
from collections import defaultdict
from datetime import datetime, timezone

from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.events import EventBus
from app.market.candles import CandleService
from app.models import AppState, EngineLog, EquitySnapshot, Order, StrategyConfig, StrategyRuntime, iso
from app.trading.broker import Broker, Fill, OrderRequest, PaperBroker, UpbitBroker
from app.trading.risk import RiskManager
from app.trading.strategy import Context, Signal, Strategy, create_strategy
from app.trading.tick import FEE_RATE, MIN_ORDER_KRW, round_price
from app.upbit.rest import UpbitAPIError, UpbitClient
from app.upbit.websocket import UpbitWebSocket

log = logging.getLogger(__name__)


class TradingEngine:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        bus: EventBus,
        client: UpbitClient,
        candles: CandleService,
        risk: RiskManager,
        paper: PaperBroker,
        ws: UpbitWebSocket,
    ):
        self.settings = settings
        self.db = db
        self.bus = bus
        self.client = client
        self.candles = candles
        self.risk = risk
        self.paper = paper
        self.ws = ws
        self.live: UpbitBroker | None = None
        self.broker: Broker = paper
        self._instances: dict[int, tuple[tuple, Strategy]] = {}
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tasks: list[asyncio.Task] = []
        self.last_equity: float = 0.0
        self.daily_pnl_pct: float | None = None
        self.last_error: dict[int, str] = {}
        self._rejections: dict[tuple, float] = {}

    @property
    def mode(self) -> str:
        return self.broker.mode

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.paper.start()
        await self._cancel_stale_paper_orders()
        async with self.db.session() as s:
            armed = await s.get(AppState, "live_armed")
        if armed and armed.value is True:
            try:
                await self.set_mode("live", reason="(재시작 후 실거래 모드 복구)")
            except (RuntimeError, UpbitAPIError) as e:
                await self.log("error", f"실거래 모드 복구 실패, 모의투자로 시작: {e}")
        await self.refresh_subscriptions()
        self._tasks = [
            asyncio.create_task(self._strategy_loop(), name="engine:strategies"),
            asyncio.create_task(self._order_sync_loop(), name="engine:orders"),
            asyncio.create_task(self._equity_loop(), name="engine:equity"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.paper.stop()

    async def set_mode(self, mode: str, reason: str = "") -> None:
        if mode == self.mode:
            return
        if mode == "live":
            if self.settings.trading_mode != "live":
                raise RuntimeError(".env 의 TRADING_MODE=live 설정이 필요합니다")
            if not self.client.has_keys:
                raise RuntimeError("업비트 API 키가 설정되지 않았습니다")
            live = self.live or UpbitBroker(self.client)
            await live.balances()  # 키 유효성 확인 (실패 시 UpbitAPIError)
            self.live = live
            self.broker = live
            await self._set_state("live_armed", True)
            await self.reconcile()
        elif mode == "paper":
            self.broker = self.paper
            await self._set_state("live_armed", False)
        else:
            raise ValueError(mode)
        self._instances.clear()
        await self.log("warning", f"매매 모드 전환 → {'실거래' if mode == 'live' else '모의투자'} {reason}".strip())
        await self.refresh_subscriptions()
        self.bus.publish("engine", await self.status())

    # ------------------------------------------------------------------ helpers
    async def log(self, level: str, message: str, strategy_id: int | None = None) -> None:
        log.log(logging.WARNING if level in ("warning", "error") else logging.INFO, message)
        entry = EngineLog(level=level, message=message, strategy_id=strategy_id)
        async with self.db.session() as s:
            s.add(entry)
            await s.commit()
        self.bus.publish(
            "log", {"id": entry.id, "ts": iso(entry.ts), "level": level, "message": message, "strategy_id": strategy_id}
        )

    async def _set_state(self, key: str, value) -> None:
        async with self.db.session() as s:
            await s.merge(AppState(key=key, value=value))
            await s.commit()

    def instance(self, cfg: StrategyConfig) -> Strategy:
        key = (cfg.type, repr(sorted((cfg.params or {}).items())))
        cached = self._instances.get(cfg.id)
        if not cached or cached[0] != key:
            self._instances[cfg.id] = (key, create_strategy(cfg.type, cfg.params))
        return self._instances[cfg.id][1]

    async def price_of(self, market: str) -> float | None:
        p = self.bus.price(market)
        if p:
            return p
        try:
            t = await self.client.get_tickers([market])
            return float(t[0]["trade_price"]) if t else None
        except UpbitAPIError:
            return None

    async def load_runtime(self, strategy_id: int, mode: str | None = None) -> StrategyRuntime:
        mode = mode or self.mode
        async with self.db.session() as s:
            rt = await s.scalar(
                select(StrategyRuntime).where(StrategyRuntime.strategy_id == strategy_id, StrategyRuntime.mode == mode)
            )
            if rt is None:
                rt = StrategyRuntime(strategy_id=strategy_id, mode=mode, pos_volume=0.0, avg_price=0.0, realized_pnl=0.0, state={})
                s.add(rt)
                await s.commit()
        return rt

    async def save_runtime(self, rt: StrategyRuntime) -> None:
        async with self.db.session() as s:
            await s.merge(rt)
            await s.commit()

    async def refresh_subscriptions(self) -> None:
        codes = set(self.settings.watch_market_list)
        async with self.db.session() as s:
            codes |= set((await s.execute(select(StrategyConfig.market))).scalars())
        try:
            for cur, b in (await self.broker.balances()).items():
                if cur != "KRW" and b.get("balance", 0) + b.get("locked", 0) > 0:
                    codes.add(f"KRW-{cur}")
        except Exception:  # noqa: BLE001
            pass
        self.ws.set_codes(codes)

    # ------------------------------------------------------------------ strategy loop
    async def _strategy_loop(self) -> None:
        await asyncio.sleep(3)  # WebSocket 첫 시세 수신 대기
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            if not self.risk.halted:
                async with self.db.session() as s:
                    cfgs = list((await s.execute(select(StrategyConfig).where(StrategyConfig.enabled.is_(True)))).scalars())
                for cfg in cfgs:
                    try:
                        await self.evaluate(cfg)
                        self.last_error.pop(cfg.id, None)
                    except Exception as e:  # noqa: BLE001 - 전략 하나의 오류가 엔진 전체를 멈추지 않게
                        msg = f"[{cfg.name}] 평가 오류: {e}"
                        if self.last_error.get(cfg.id) != msg:  # 같은 오류 반복 로그 방지
                            await self.log("error", msg, cfg.id)
                        self.last_error[cfg.id] = msg
            await asyncio.sleep(max(0.5, self.settings.engine_eval_sec - (loop.time() - started)))

    async def evaluate(self, cfg: StrategyConfig) -> list[Signal]:
        strat = self.instance(cfg)
        price = await self.price_of(cfg.market)
        if not price:
            return []
        df = await self.candles.fetch(cfg.market, cfg.interval, max(strat.min_bars + 5, 120))
        if df.empty:
            return []
        last = df.index[-1]
        df.loc[last, "close"] = price
        df.loc[last, "high"] = max(df.loc[last, "high"], price)
        df.loc[last, "low"] = min(df.loc[last, "low"], price)

        async with self._locks[cfg.market]:
            rt = await self.load_runtime(cfg.id)
            ctx = self._context(cfg, rt, price)
            signals = strat.on_bar(df, ctx)
            for sig in signals:
                await self._execute(cfg, strat, sig, ctx, rt)
            rt.state = ctx.state
            await self.save_runtime(rt)
        return signals

    @staticmethod
    def _context(cfg: StrategyConfig, rt: StrategyRuntime, price: float) -> Context:
        return Context(
            market=cfg.market,
            price=price,
            now=datetime.now(timezone.utc),
            pos_volume=rt.pos_volume,
            avg_price=rt.avg_price,
            budget=cfg.budget_krw,
            cash=max(cfg.budget_krw - rt.pos_volume * rt.avg_price, 0.0),
            fee_rate=FEE_RATE,
            state=copy.deepcopy(rt.state or {}),
        )

    async def _execute(self, cfg: StrategyConfig, strat: Strategy, sig: Signal, ctx: Context, rt: StrategyRuntime) -> None:
        if sig.action == "buy":
            amount = math.floor(min(sig.amount_krw or 0, ctx.cash / (1 + FEE_RATE)))
            if amount < MIN_ORDER_KRW:
                return
            req = OrderRequest(cfg.market, "bid", "price", price=amount)
        else:
            coin = cfg.market.split("-", 1)[1]
            available = (await self.broker.balances()).get(coin, {}).get("balance", 0.0)
            vol = min(sig.volume if sig.volume is not None else rt.pos_volume, rt.pos_volume, available)
            if vol * ctx.price < MIN_ORDER_KRW:
                if rt.pos_volume * ctx.price < MIN_ORDER_KRW or available * ctx.price < MIN_ORDER_KRW:
                    # 최소주문금액 미만 잔량(또는 외부에서 이미 매도됨)은 팔 수 없으므로 전략 장부에서 정리
                    await self.log("warning", f"[{cfg.name}] 매도 불가 잔량 정리 ({rt.pos_volume:.8g} {coin})", cfg.id)
                    rt.pos_volume, rt.avg_price = 0.0, 0.0
                    ctx.pos_volume, ctx.avg_price = 0.0, 0.0
                    strat.on_fill(sig, 0.0, ctx.price, ctx)
                return
            req = OrderRequest(cfg.market, "ask", "market", volume=vol)

        fill = await self._place(req, ref_price=ctx.price, strategy_id=cfg.id, reason=f"[{cfg.name}] {sig.reason}")
        if fill.executed_volume > 0:
            self._apply_fill(rt, req.side, fill, sig.cost_price)
            ctx.pos_volume, ctx.avg_price = rt.pos_volume, rt.avg_price
            ctx.cash = max(cfg.budget_krw - rt.pos_volume * rt.avg_price, 0.0)
            strat.on_fill(sig, fill.executed_volume, fill.avg_price, ctx)

    @staticmethod
    def _apply_fill(rt: StrategyRuntime, side: str, fill: Fill, cost_price: float | None = None) -> None:
        vol, px, fee = fill.executed_volume, fill.avg_price, fill.paid_fee
        if side == "bid":
            new_vol = rt.pos_volume + vol
            rt.avg_price = (rt.pos_volume * rt.avg_price + vol * px + fee) / new_vol  # 수수료 포함 손익분기 단가
            rt.pos_volume = new_vol
        else:
            vol = min(vol, rt.pos_volume)
            cost = cost_price if cost_price is not None else rt.avg_price  # 칸별 원가(그리드) 또는 평균단가
            rt.realized_pnl += vol * px - fee - vol * cost
            remaining_cost = max(rt.pos_volume * rt.avg_price - vol * cost, 0.0)
            rt.pos_volume -= vol
            if rt.pos_volume * px < 1:
                rt.pos_volume, rt.avg_price = 0.0, 0.0
            else:
                rt.avg_price = remaining_cost / rt.pos_volume

    async def _place(self, req: OrderRequest, *, ref_price: float, strategy_id: int | None, reason: str) -> Fill:
        equity, position_value = await self._exposure(req.market, ref_price)
        rejected = self.risk.check(req, ref_price=ref_price, equity=equity, position_value=position_value)
        if rejected and strategy_id is not None:
            # 전략은 수 초마다 같은 신호를 다시 낼 수 있으므로 같은 사유의 거부는 10분에 한 번만 기록
            key = (strategy_id, req.side, rejected.split("(")[0])
            now = asyncio.get_running_loop().time()
            if now - self._rejections.get(key, -1e9) < 600:
                return Fill(False, "rejected", error=rejected)
            self._rejections[key] = now
        order = Order(
            mode=self.mode, strategy_id=strategy_id, market=req.market, side=req.side, ord_type=req.ord_type,
            price=req.price, volume=req.volume, reason=reason,
        )
        if rejected:
            fill = Fill(False, "rejected", error=rejected)
        else:
            fill = await self.broker.place(req)
        order.state = fill.state if fill.ok else "rejected"
        order.exchange_uuid = fill.uuid
        order.executed_volume, order.avg_price, order.paid_fee = fill.executed_volume, fill.avg_price, fill.paid_fee
        order.error = fill.error
        async with self.db.session() as s:
            s.add(order)
            await s.commit()

        side_kr = "매수" if req.side == "bid" else "매도"
        if fill.executed_volume > 0:
            await self.log(
                "trade",
                f"{req.market} {side_kr} 체결 {fill.executed_volume:.8g} @ {fill.avg_price:,.8g} ({fill.funds:,.0f}원) {reason}",
                strategy_id,
            )
        elif not fill.ok:
            await self.log("warning", f"{req.market} {side_kr} 거부: {fill.error} {reason}", strategy_id)
        self.bus.publish("order", order_dict(order))
        if req.side == "bid" and fill.executed_volume > 0:
            self.ws.add_codes(req.market)
        return fill

    async def _exposure(self, market: str, ref_price: float) -> tuple[float, float]:
        """(총자산, 해당 종목 평가금액)"""
        balances = await self.broker.balances()
        pos = balances.get(market.split("-", 1)[1], {})
        position_value = (pos.get("balance", 0) + pos.get("locked", 0)) * ref_price
        return await self.equity(balances), position_value

    async def equity(self, balances: dict[str, dict] | None = None) -> float:
        balances = balances if balances is not None else await self.broker.balances()
        total = 0.0
        missing: list[str] = []
        for cur, b in balances.items():
            qty = b.get("balance", 0) + b.get("locked", 0)
            if cur == "KRW":
                total += qty
            elif qty > 0:
                p = self.bus.price(f"KRW-{cur}")
                if p:
                    total += qty * p
                else:
                    missing.append(cur)
        for cur in missing:  # 원화마켓이 없는 코인(에어드랍 등)은 평가에서 제외
            try:
                t = await self.client.get_tickers([f"KRW-{cur}"])
                b = balances[cur]
                total += (b.get("balance", 0) + b.get("locked", 0)) * float(t[0]["trade_price"])
            except (UpbitAPIError, IndexError, KeyError):
                continue
        return total

    # ------------------------------------------------------------------ manual orders
    async def manual_order(self, market: str, side: str, ord_type: str, price: float | None, volume: float | None) -> dict:
        ref = await self.price_of(market)
        if not ref:
            raise ValueError("현재가를 가져올 수 없습니다")
        if ord_type == "limit":
            if not price or not volume or side not in ("bid", "ask"):
                raise ValueError("지정가 주문은 매수/매도 구분, 가격, 수량이 필요합니다")
            price = round_price(price, side)
        elif ord_type == "price":
            if side != "bid" or not price:
                raise ValueError("시장가 매수는 주문 총액이 필요합니다")
            volume = None
        elif ord_type == "market":
            if side != "ask" or not volume:
                raise ValueError("시장가 매도는 수량이 필요합니다")
            price = None
        else:
            raise ValueError("ord_type 은 limit/price/market 중 하나")
        req = OrderRequest(market, side, ord_type, price=price, volume=volume)
        async with self._locks[market]:
            fill = await self._place(req, ref_price=ref, strategy_id=None, reason="수동 주문")
        self.bus.publish("order_update", {"uuid": fill.uuid})
        return {"ok": fill.ok, "state": fill.state, "error": fill.error, "uuid": fill.uuid}

    async def cancel_order(self, order_id: int) -> dict:
        async with self.db.session() as s:
            o = await s.get(Order, order_id)
        if not o or o.state != "wait" or not o.exchange_uuid:
            raise ValueError("취소할 수 있는 미체결 주문이 아닙니다")
        if o.mode != self.mode:
            raise ValueError("현재 매매 모드의 주문이 아닙니다")
        fill = await self.broker.cancel(o.exchange_uuid)
        if fill.finished:
            await self._update_order(o, fill)
        return {"ok": fill.ok, "state": fill.state, "error": fill.error}

    async def _update_order(self, o: Order, fill: Fill) -> None:
        o.state = fill.state
        o.executed_volume, o.avg_price, o.paid_fee = fill.executed_volume, fill.avg_price, fill.paid_fee
        async with self.db.session() as s:
            await s.merge(o)
            await s.commit()
        self.bus.publish("order", order_dict(o))

    # ------------------------------------------------------------------ order sync / reconcile
    async def _order_sync_loop(self) -> None:
        q = self.bus.subscribe("order_update")
        while True:
            try:
                await asyncio.wait_for(q.get(), timeout=3.0)
            except asyncio.TimeoutError:
                pass
            try:
                await self.sync_open_orders()
            except Exception as e:  # noqa: BLE001
                log.warning("미체결 주문 동기화 실패: %s", e)

    async def sync_open_orders(self) -> None:
        async with self.db.session() as s:
            waiting = list(
                (await s.execute(select(Order).where(Order.state == "wait", Order.mode == self.mode))).scalars()
            )
        for o in waiting:
            if not o.exchange_uuid:
                continue
            fill = await self.broker.get_order(o.exchange_uuid)
            if not fill.finished and fill.executed_volume == o.executed_volume:
                continue
            if not fill.finished:
                fill.state = "wait"
            await self._update_order(o, fill)
            if fill.finished and fill.executed_volume > 0:
                side_kr = "매수" if o.side == "bid" else "매도"
                await self.log("trade", f"{o.market} 지정가 {side_kr} 체결 {fill.executed_volume:.8g} @ {fill.avg_price:,.8g}")

    async def _cancel_stale_paper_orders(self) -> None:
        async with self.db.session() as s:
            for o in (await s.execute(select(Order).where(Order.state == "wait", Order.mode == "paper"))).scalars():
                o.state, o.error = "cancel", "재시작으로 모의 미체결 주문 취소"
            await s.commit()

    async def reconcile(self) -> None:
        """실거래 잔고와 전략 포지션 대사. 실제 잔고보다 전략 장부가 많으면 비율대로 줄인다."""
        balances = await self.broker.balances()
        messages: list[str] = []
        async with self.db.session() as s:
            rows = (
                await s.execute(
                    select(StrategyRuntime, StrategyConfig.market)
                    .join(StrategyConfig, StrategyConfig.id == StrategyRuntime.strategy_id)
                    .where(StrategyRuntime.mode == self.mode, StrategyRuntime.pos_volume > 0)
                )
            ).all()
            by_coin: dict[str, list[StrategyRuntime]] = defaultdict(list)
            for rt, market in rows:
                by_coin[market.split("-", 1)[1]].append(rt)
            for coin, rts in by_coin.items():
                book = sum(r.pos_volume for r in rts)
                b = balances.get(coin, {})
                actual = b.get("balance", 0.0) + b.get("locked", 0.0)
                if book > actual * 1.0001:
                    ratio = actual / book if book else 0.0
                    for r in rts:
                        r.pos_volume *= ratio
                        if r.pos_volume == 0:
                            r.avg_price = 0.0
                    messages.append(f"대사: {coin} 전략 장부 {book:.8g} > 실제 잔고 {actual:.8g} → {ratio:.4f} 비율로 축소")
            await s.commit()
        for m in messages:
            await self.log("warning", m)

    # ------------------------------------------------------------------ equity / risk
    async def _equity_loop(self) -> None:
        await asyncio.sleep(5)
        while True:
            try:
                eq = await self.equity()
                self.last_equity = eq
                self.daily_pnl_pct = await self.risk.on_equity(self.mode, eq)
                async with self.db.session() as s:
                    s.add(EquitySnapshot(mode=self.mode, equity=eq))
                    await s.commit()
                self.bus.publish("engine", await self.status())
            except Exception as e:  # noqa: BLE001
                log.warning("자산 평가 실패: %s", e)
            await asyncio.sleep(60)

    async def emergency_stop(self, liquidate: bool = False) -> dict:
        await self.risk.halt("사용자 긴급정지")
        async with self.db.session() as s:
            waiting = list((await s.execute(select(Order).where(Order.state == "wait", Order.mode == self.mode))).scalars())
        cancelled = 0
        for o in waiting:
            if o.exchange_uuid:
                f = await self.broker.cancel(o.exchange_uuid)
                if f.finished:
                    await self._update_order(o, f)
                    cancelled += 1
        sold = await self.liquidate_strategies() if liquidate else 0
        await self.log("warning", f"긴급정지 실행: 미체결 {cancelled}건 취소, 전략 포지션 {sold}건 청산")
        self.bus.publish("engine", await self.status())
        return {"cancelled": cancelled, "liquidated": sold}

    async def liquidate_strategies(self, strategy_id: int | None = None) -> int:
        stmt = select(StrategyConfig)
        if strategy_id:
            stmt = stmt.where(StrategyConfig.id == strategy_id)
        async with self.db.session() as s:
            cfgs = list((await s.execute(stmt)).scalars())
        sold = 0
        for cfg in cfgs:
            async with self._locks[cfg.market]:
                rt = await self.load_runtime(cfg.id)
                price = await self.price_of(cfg.market)
                if rt.pos_volume <= 0 or not price:
                    continue
                ctx = self._context(cfg, rt, price)
                await self._execute(cfg, self.instance(cfg), Signal("sell", reason="강제 청산"), ctx, rt)
                rt.state = ctx.state
                await self.save_runtime(rt)
                sold += 1
        return sold

    async def reset_paper(self, krw: float | None = None) -> None:
        if self.mode != "paper":
            raise RuntimeError("모의투자 모드에서만 초기화할 수 있습니다")
        await self.paper.reset(krw)
        async with self.db.session() as s:
            for rt in (await s.execute(select(StrategyRuntime).where(StrategyRuntime.mode == "paper"))).scalars():
                rt.pos_volume, rt.avg_price, rt.realized_pnl, rt.state = 0.0, 0.0, 0.0, {}
            await s.commit()
        self.risk.day_start.pop("paper", None)
        await self.log("warning", "모의투자 계좌 초기화")

    async def status(self) -> dict:
        return {
            "mode": self.mode,
            "live_allowed": self.settings.trading_mode == "live",
            "has_keys": self.client.has_keys,
            "ws_connected": self.ws.connected,
            "equity": self.last_equity,
            "daily_pnl_pct": self.daily_pnl_pct,
            "risk": self.risk.snapshot(),
        }


def order_dict(o: Order) -> dict:
    return {
        "id": o.id,
        "created_at": iso(o.created_at),
        "mode": o.mode,
        "strategy_id": o.strategy_id,
        "market": o.market,
        "side": o.side,
        "ord_type": o.ord_type,
        "price": o.price,
        "volume": o.volume,
        "state": o.state,
        "executed_volume": o.executed_volume,
        "avg_price": o.avg_price,
        "paid_fee": o.paid_fee,
        "reason": o.reason,
        "error": o.error,
    }
