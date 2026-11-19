from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.backtest.runner import run_backtest
from app.config import Settings
from app.market import indicators
from app.news.base import classify_notice, extract_coins
from app.trading.broker import OrderRequest, PaperBroker
from app.trading.risk import RiskManager
from app.trading.strategy import Context, create_strategy
from app.trading.tick import round_price, tick_size

from tests.conftest import make_df

NOW = datetime(2024, 1, 1, tzinfo=timezone.utc)


def ctx(price, pos=0.0, avg=0.0, budget=1_000_000, state=None):
    return Context("KRW-BTC", price, NOW, pos, avg, budget, max(budget - pos * avg, 0), 0.0005, state if state is not None else {})


# ------------------------------------------------------------------ tick / indicators
@pytest.mark.parametrize("price,tick", [(2_500_000, "1000"), (1_500_000, "1000"), (700_000, "500"), (150_000, "100"),
                                        (60_000, "50"), (12_000, "10"), (7_000, "5"), (1_500, "1"), (150, "1"), (15, "0.1"), (5, "0.01")])
def test_tick_size(price, tick):
    assert str(tick_size(price)) == tick


def test_round_price_direction():
    assert round_price(1_234_567, "bid") == 1_234_000
    assert round_price(1_234_567, "ask") == 1_235_000
    assert round_price(15.07, "bid") == 15.0


def test_rsi_bounds_and_all_up():
    s = make_df(list(range(1, 40)))["close"]
    r = indicators.rsi(s, 14)
    assert r.iloc[-1] == 100.0
    s2 = make_df([10, 11, 10, 11, 10, 11] * 5)["close"]
    v = indicators.rsi(s2, 14).iloc[-1]
    assert 0 < v < 100


# ------------------------------------------------------------------ strategies
def test_volatility_breakout_buy_then_exit_next_bar():
    s = create_strategy("volatility_breakout", {"k": 0.5, "ma_filter": 0})
    df = make_df([100, 100, 100], spread=0.05)  # 전일 변동폭 = 105-95 = 10
    st: dict = {}
    assert s.on_bar(df, ctx(104, state=st)) == []  # 목표가 100 + 5 = 105 미만
    sigs = s.on_bar(df, ctx(106, state=st))
    assert sigs and sigs[0].action == "buy"
    c = ctx(106, state=st)
    s.on_fill(sigs[0], 100, 106, c)
    assert c.state["entry_bar"] == int(df["time"].iloc[-1])
    # 같은 봉: 보유 중이지만 청산하지 않음
    assert s.on_bar(df, ctx(106, pos=100, avg=106, state=c.state)) == []
    # 다음 봉: 청산
    df2 = make_df([100, 100, 100, 101], spread=0.05)
    out = s.on_bar(df2, ctx(101, pos=100, avg=106, state=c.state))
    assert out and out[0].action == "sell"


def test_ma_cross_golden_and_once_per_bar():
    s = create_strategy("ma_cross", {"short": 2, "long": 4, "ma_type": "sma"})
    closes = [10, 9, 8, 7, 6, 5, 5.5, 12, 12]  # 마지막 확정봉에서 골든크로스, 마지막 행은 진행 중 봉
    df = make_df(closes)
    st: dict = {}
    sigs = s.on_bar(df, ctx(12, state=st))
    assert [x.action for x in sigs] == ["buy"]
    assert s.on_bar(df, ctx(12, state=st)) == []  # 같은 봉에서 재평가해도 신호 없음


def test_ma_cross_rejects_bad_params():
    with pytest.raises(ValueError):
        create_strategy("ma_cross", {"short": 20, "long": 5})


def test_rsi_bollinger_buys_on_crash():
    s = create_strategy("rsi_bollinger", {"rsi_period": 14, "bb_period": 20})
    closes = [100.0] * 25 + [99, 97, 94, 90, 85, 80]
    df = make_df(closes, spread=0.001)
    sigs = s.on_bar(df, ctx(80))
    assert sigs and sigs[0].action == "buy"


def test_grid_buy_and_sell_lot():
    s = create_strategy("grid", {"lower": 90, "upper": 110, "grids": 4})  # 레벨: 90, 95, 100, 105, 110
    df = make_df([100, 100])
    st: dict = {}
    sigs = s.on_bar(df, ctx(99, state=st))
    assert [x.action for x in sigs] == ["buy"] and sigs[0].tag == "2"  # 가장 가까운 위 레벨 100
    c = ctx(99, state=st)
    s.on_fill(sigs[0], 2.0, 99, c)
    assert c.state["lots"] == {"2": [2.0, pytest.approx(99 * 1.0005)]}
    sell = [x for x in s.on_bar(df, ctx(106, pos=2.0, avg=99, state=c.state)) if x.action == "sell"]
    assert sell and sell[0].volume == 2.0 and sell[0].tag == "2"
    assert sell[0].cost_price == pytest.approx(99 * 1.0005)
    s.on_fill(sell[0], 2.0, 106, c)
    assert c.state["lots"] == {}


def test_grid_requires_valid_range():
    with pytest.raises(ValueError):
        create_strategy("grid", {"lower": 100, "upper": 90})


def test_dca_interval_and_drop():
    s = create_strategy("dca", {"amount": 10000, "interval_hours": 24, "drop_pct": 5, "take_profit_pct": 10})
    df = make_df([100, 100])
    st: dict = {}
    first = s.on_bar(df, ctx(100, state=st))
    assert first and first[0].action == "buy"
    c = ctx(100, state=st)
    s.on_fill(first[0], 100, 100, c)
    assert s.on_bar(df, ctx(99, pos=100, avg=100, state=c.state)) == []  # 주기 전, 하락폭 미달
    assert s.on_bar(df, ctx(94, pos=100, avg=100, state=c.state))[0].action == "buy"  # -5% 이하
    tp = s.on_bar(df, ctx(111, pos=100, avg=100, state=c.state))
    assert tp[0].action == "sell"


# ------------------------------------------------------------------ backtest
def test_backtest_runs_and_reports():
    closes = [100 + (i % 10) * 2 - 10 + i * 0.3 for i in range(200)]
    df = make_df(closes, spread=0.02)
    res = run_backtest(create_strategy("volatility_breakout", {"k": 0.5, "ma_filter": 3}), df, interval="day")
    m = res["metrics"]
    assert len(res["equity"]) == len(df)
    assert m["trades"] > 0 and m["mdd_pct"] <= 0
    assert m["fees"] > 0


def test_backtest_no_trades_keeps_capital():
    df = make_df([100.0] * 60, spread=0.0)
    res = run_backtest(create_strategy("ma_cross", {"short": 3, "long": 10}), df, interval="day", initial=500_000)
    assert res["metrics"]["final_equity"] == pytest.approx(500_000)


# ------------------------------------------------------------------ risk
async def test_risk_checks(db):
    r = RiskManager(Settings(risk_max_order_krw=100_000, risk_max_position_ratio=0.3, risk_daily_loss_limit_pct=5), db)
    buy = lambda amt: OrderRequest("KRW-BTC", "bid", "price", price=amt)  # noqa: E731
    assert "최소" in r.check(buy(4000), ref_price=1, equity=1e6, position_value=0)
    assert "최대 주문" in r.check(buy(200_000), ref_price=1, equity=1e6, position_value=0)
    assert "비중" in r.check(buy(90_000), ref_price=1, equity=1e6, position_value=250_000)
    assert r.check(buy(50_000), ref_price=1, equity=1e6, position_value=0) is None
    r.flag("BTC", "유의종목 지정")
    assert "유의" in r.check(buy(50_000), ref_price=1, equity=1e6, position_value=0)
    r.flag("BTC", "", on=False)
    # 일일 손실 한도
    await r.on_equity("paper", 1_000_000)
    await r.on_equity("paper", 940_000)
    assert r.halted
    assert "정지" in r.check(buy(50_000), ref_price=1, equity=1e6, position_value=0)
    sell = OrderRequest("KRW-BTC", "ask", "market", volume=1)
    assert r.check(sell, ref_price=10_000, equity=1e6, position_value=0) is None  # 정지 중에도 매도 허용


# ------------------------------------------------------------------ paper broker
async def test_paper_broker_buy_sell(db, bus):
    p = PaperBroker(db, bus, 1_000_000, slippage=0)
    await p.start()
    bus.publish("ticker", {"code": "KRW-BTC", "trade_price": 100_000})
    f = await p.place(OrderRequest("KRW-BTC", "bid", "price", price=100_000))
    assert f.state == "done" and f.executed_volume == pytest.approx(1.0)
    bal = await p.balances()
    assert bal["KRW"]["balance"] == pytest.approx(1_000_000 - 100_000 * 1.0005)
    assert bal["BTC"]["balance"] == pytest.approx(1.0)
    f2 = await p.place(OrderRequest("KRW-BTC", "ask", "market", volume=1.0))
    assert f2.state == "done"
    bal = await p.balances()
    assert "BTC" not in bal
    assert bal["KRW"]["balance"] == pytest.approx(1_000_000 - 100_000 * 0.0005 * 2)
    await p.stop()


async def test_paper_broker_rejects_overspend_and_fills_limit(db, bus):
    p = PaperBroker(db, bus, 100_000, slippage=0)
    await p.start()
    bus.publish("ticker", {"code": "KRW-ETH", "trade_price": 1000})
    assert not (await p.place(OrderRequest("KRW-ETH", "bid", "price", price=200_000))).ok
    f = await p.place(OrderRequest("KRW-ETH", "bid", "limit", price=900, volume=10))
    assert f.state == "wait"
    assert (await p.balances())["KRW"]["locked"] == pytest.approx(900 * 10 * 1.0005)
    await p._fill_limit(p._orders[f.uuid])
    got = await p.get_order(f.uuid)
    assert got.state == "done" and got.executed_volume == 10
    bal = await p.balances()
    assert bal["ETH"]["balance"] == 10 and bal["KRW"]["locked"] == pytest.approx(0)
    await p.stop()


async def test_paper_orderbook_walk(db, bus):
    p = PaperBroker(db, bus, 10_000_000, slippage=0)
    await p.start()
    bus.publish("orderbook", {"code": "KRW-BTC", "orderbook_units": [
        {"ask_price": 100, "ask_size": 1, "bid_price": 99, "bid_size": 1},
        {"ask_price": 110, "ask_size": 10, "bid_price": 98, "bid_size": 10},
    ]})
    f = await p.place(OrderRequest("KRW-BTC", "bid", "price", price=210))
    assert f.executed_volume == pytest.approx(2.0) and f.avg_price == pytest.approx(105)
    await p.stop()


# ------------------------------------------------------------------ news parsing
def test_extract_coins_and_classify():
    names = {"XRP": ["XRP", "엑스알피", "XRP"], "BTC": ["BTC", "비트코인", "Bitcoin"], "ONE": ["ONE", "하모니", "Harmony"]}
    assert extract_coins("엑스알피(XRP) 유의 종목 지정 안내", names) == ["XRP"]
    # 마켓 표기 "(BTC, USDT 마켓)" 를 코인으로 오인하지 않고, 원화마켓에 없는 신규 코인도 인식
    assert extract_coins("스타크넷(STRK), 리퀘스트(REQ) 신규 거래지원 안내 (BTC, USDT 마켓)", names) == ["STRK", "REQ"]
    assert "BTC" in extract_coins("Bitcoin hits new high", names)
    assert extract_coins("one more thing", names) == []
    assert classify_notice("엑스알피(XRP) 유의 종목 지정 안내") == "warning"
    assert classify_notice("엑스알피(XRP) 유의 종목 지정 해제 안내") == "release"
    assert classify_notice("OO(ABC) 거래지원 종료 안내") == "delisting"
    assert classify_notice("스타크넷(STRK) 신규 거래지원 안내 (BTC, USDT 마켓)") == "listing"


def test_backtest_breakout_fills_at_target_not_bar_high():
    """돌파 매수는 봉 고가가 아니라 목표가(+슬리피지)에 체결되어야 한다 (봉 내부 4지점 근사의 함정 방지)."""
    df = make_df([100.0, 100.0, 100.0, 120.0, 120.0], spread=0.05)
    res = run_backtest(create_strategy("volatility_breakout", {"k": 0.5, "ma_filter": 0}), df, interval="day", slippage=0.0)
    buy = next(t for t in res["trades"] if t["side"] == "buy")
    bar = df.iloc[3]
    prev = df.iloc[2]
    target = bar["open"] + 0.5 * (prev["high"] - prev["low"])
    assert buy["price"] == pytest.approx(target)
    assert buy["price"] < bar["high"]



def test_grid_backtest_lot_pnl_uses_lot_cost():
    """여러 칸을 보유한 상태에서 아래 칸을 팔아도, 평균단가가 아니라 그 칸의 원가로 손익이 계산되어야 한다."""
    closes = [100, 99, 97, 95, 93, 91, 93, 95]
    df = make_df(closes, spread=0.0, step=3600)
    res = run_backtest(create_strategy("grid", {"lower": 80, "upper": 100, "grids": 10}), df, interval="minute60", slippage=0.0)
    sells = [t for t in res["trades"] if t["side"] == "sell"]
    assert sells and all(t["pnl"] > 0 for t in sells)
