"""엔진 통합 테스트: 전략 평가 → 리스크 → 모의 브로커 → 주문 기록 → 전략 장부 갱신."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import Order, StrategyConfig
from app.trading.broker import PaperBroker
from app.trading.engine import TradingEngine
from app.trading.risk import RiskManager
from app.upbit.rest import UpbitClient
from app.upbit.websocket import UpbitWebSocket

from tests.conftest import make_df


class FakeCandles:
    def __init__(self, df):
        self.df = df

    async def fetch(self, market, interval, count=200, use_cache=True):
        return self.df.copy()


@pytest.fixture
async def engine(db, bus):
    settings = Settings(risk_max_order_krw=0, risk_max_position_ratio=0, risk_daily_loss_limit_pct=0)
    risk = RiskManager(settings, db)
    paper = PaperBroker(db, bus, 1_000_000, slippage=0)
    eng = TradingEngine(settings, db, bus, UpbitClient(), FakeCandles(make_df([100_000.0] * 5)), risk, paper, UpbitWebSocket(bus, []))
    await paper.start()
    bus.publish("ticker", {"code": "KRW-BTC", "trade_price": 100_000.0})
    yield eng
    await paper.stop()
    await eng.client.aclose()


async def _add(db, **kw) -> StrategyConfig:
    cfg = StrategyConfig(name="t", type="dca", market="KRW-BTC", interval="day",
                         params={"amount": 50_000, "interval_hours": 24, "drop_pct": 0, "max_buys": 3, "take_profit_pct": 0},
                         budget_krw=200_000, enabled=True, **kw)
    async with db.session() as s:
        s.add(cfg)
        await s.commit()
    return cfg


async def test_evaluate_buys_and_books_position(engine, db):
    cfg = await _add(db)
    sigs = await engine.evaluate(cfg)
    assert [s.action for s in sigs] == ["buy"]
    rt = await engine.load_runtime(cfg.id)
    assert rt.pos_volume == pytest.approx(0.5)
    assert rt.avg_price == pytest.approx(100_000 * 1.0005)  # 수수료 포함 단가
    assert rt.state["buys"] == 1
    # 주기 전이라 재평가해도 추가 매수 없음
    assert await engine.evaluate(cfg) == []
    async with db.session() as s:
        orders = list((await s.execute(select(Order))).scalars())
    assert len(orders) == 1 and orders[0].state == "done" and orders[0].strategy_id == cfg.id


async def test_liquidate_and_realized_pnl(engine, db):
    cfg = await _add(db)
    await engine.evaluate(cfg)
    assert await engine.liquidate_strategies(cfg.id) == 1
    rt = await engine.load_runtime(cfg.id)
    assert rt.pos_volume == 0
    assert rt.realized_pnl == pytest.approx(-50_000 * 0.0005 * 2)  # 왕복 수수료만큼 손실
    assert rt.state["buys"] == 0  # DCA 사이클 리셋


async def test_halted_blocks_buy_but_records_once(engine, db):
    cfg = await _add(db)
    engine.risk.flag("BTC", "유의종목 지정")
    await engine.evaluate(cfg)
    await engine.evaluate(cfg)
    async with db.session() as s:
        orders = list((await s.execute(select(Order))).scalars())
    assert len(orders) == 1 and orders[0].state == "rejected" and "유의" in orders[0].error
    assert (await engine.load_runtime(cfg.id)).pos_volume == 0


async def test_manual_limit_order_and_cancel(engine, db):
    r = await engine.manual_order("KRW-BTC", "bid", "limit", 90_123.0, 0.1)
    assert r["ok"] and r["state"] == "wait"
    async with db.session() as s:
        o = (await s.execute(select(Order))).scalars().one()
    assert o.price == 90_100  # 5만~10만원 구간 호가 단위 50원으로 내림
    res = await engine.cancel_order(o.id)
    assert res["state"] == "cancel"
    bal = await engine.paper.balances()
    assert bal["KRW"]["balance"] == pytest.approx(1_000_000) and bal["KRW"]["locked"] == pytest.approx(0)
