"""백테스트: 라이브와 같은 Strategy 클래스를 과거 캔들 위에서 실행한다.

봉 내부 경로 가정: 양봉은 시가→저가→고가→종가, 음봉은 시가→고가→저가→종가.
각 지점에서 '진행 중인 봉'(시가, 지금까지의 고가/저가, 현재가)을 마지막 행으로 넣어 on_bar 를 호출하므로
라이브 엔진이 수 초마다 평가하는 것과 같은 의미가 된다.

체결 기준가: 신호에 price(목표가 등)가 있고 직전 지점→현재 지점 구간이 그 가격을 지나가면 그 가격,
아니면(갭으로 건너뛴 경우 등) 현재 지점 가격. 4개 지점만 보므로 이렇게 하지 않으면 돌파 매수가 항상
봉의 고가에 체결되는 비현실적인 결과가 나온다. 여기에 슬리피지와 수수료를 더한다.
"""

from __future__ import annotations

import copy
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from app.trading.strategy import Context, Strategy
from app.trading.tick import MIN_ORDER_KRW

BARS_PER_YEAR = {
    "minute1": 525_600, "minute3": 175_200, "minute5": 105_120, "minute10": 52_560, "minute15": 35_040,
    "minute30": 17_520, "minute60": 8_760, "minute240": 2_190, "day": 365, "week": 52, "month": 12,
}


def run_backtest(
    strategy: Strategy,
    df: pd.DataFrame,
    *,
    market: str = "KRW-BTC",
    interval: str = "day",
    initial: float = 1_000_000,
    fee_rate: float = 0.0005,
    slippage: float = 0.001,
    window: int | None = None,
) -> dict:
    if len(df) < strategy.min_bars + 1:
        raise ValueError(f"캔들이 부족합니다 (필요 {strategy.min_bars + 1}개, 보유 {len(df)}개)")
    df = df.reset_index(drop=True)
    window = window or max(strategy.min_bars + 50, 300)

    cash = initial
    pos = avg = realized = fees = 0.0
    state: dict = {}
    trades: list[dict] = []
    curve: list[dict] = []

    for i in range(len(df)):
        bar = df.iloc[i]
        o, h, l, c = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        path = [o, l, h, c] if c >= o else [o, h, l, c]
        hist = df.iloc[max(0, i - window + 1) : i + 1].copy()
        last = hist.index[-1]
        now = datetime.fromtimestamp(int(bar["time"]), timezone.utc)
        hi = lo = prev_p = o
        for p in path:
            hi, lo = max(hi, p), min(lo, p)
            hist.loc[last, ["high", "low", "close"]] = [hi, lo, p]
            ctx = Context(market, p, now, pos, avg, initial, max(initial - pos * avg, 0.0), fee_rate, state)
            seg_lo, seg_hi = min(prev_p, p), max(prev_p, p)
            for sig in strategy.on_bar(hist, ctx):
                ref = sig.price if sig.price is not None and seg_lo <= sig.price <= seg_hi else p
                if sig.action == "buy":
                    amount = math.floor(min(sig.amount_krw or 0, ctx.cash / (1 + fee_rate), cash / (1 + fee_rate)))
                    if amount < MIN_ORDER_KRW:
                        continue
                    px = ref * (1 + slippage)
                    vol = amount / px
                    fee = amount * fee_rate
                    cash -= amount + fee
                    fees += fee
                    avg = (pos * avg + amount + fee) / (pos + vol)
                    pos += vol
                    trades.append({"time": int(bar["time"]), "side": "buy", "price": px, "volume": vol, "amount": amount, "reason": sig.reason})
                else:
                    vol = min(sig.volume if sig.volume is not None else pos, pos)
                    if vol <= 0 or vol * ref < MIN_ORDER_KRW:
                        continue
                    px = ref * (1 - slippage)
                    proceeds = vol * px
                    fee = proceeds * fee_rate
                    cost = sig.cost_price if sig.cost_price is not None else avg
                    pnl = proceeds - fee - vol * cost
                    cash += proceeds - fee
                    fees += fee
                    realized += pnl
                    remaining_cost = max(pos * avg - vol * cost, 0.0)
                    pos -= vol
                    if pos * px < 1:
                        pos, avg = 0.0, 0.0
                    else:
                        avg = remaining_cost / pos
                    trades.append({"time": int(bar["time"]), "side": "sell", "price": px, "volume": vol, "amount": proceeds, "pnl": pnl, "reason": sig.reason})
                ctx.pos_volume, ctx.avg_price = pos, avg
                ctx.cash = max(initial - pos * avg, 0.0)
                strategy.on_fill(sig, vol, px, ctx)
            state = ctx.state
            prev_p = p
        curve.append({"time": int(bar["time"]), "equity": cash + pos * c})

    return {"metrics": metrics(curve, trades, df, initial, interval, fees, realized), "equity": curve, "trades": trades, "final_state": copy.deepcopy(state)}


def metrics(curve: list[dict], trades: list[dict], df: pd.DataFrame, initial: float, interval: str, fees: float, realized: float) -> dict:
    eq = pd.Series([c["equity"] for c in curve], dtype=float)
    peak = eq.cummax()
    mdd = float(((eq / peak) - 1).min() * 100) if len(eq) else 0.0
    rets = eq.pct_change().dropna()
    std = rets.std()
    sharpe = float(rets.mean() / std * np.sqrt(BARS_PER_YEAR.get(interval, 365))) if std and std > 0 else 0.0
    sells = [t for t in trades if t["side"] == "sell"]
    wins = [t for t in sells if t.get("pnl", 0) > 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = -sum(t["pnl"] for t in sells if t.get("pnl", 0) <= 0)
    first, last = float(df["close"].iloc[0]), float(df["close"].iloc[-1])
    return {
        "initial": initial,
        "final_equity": float(eq.iloc[-1]) if len(eq) else initial,
        "total_return_pct": float((eq.iloc[-1] / initial - 1) * 100) if len(eq) else 0.0,
        "buy_hold_return_pct": (last / first - 1) * 100 if first else 0.0,
        "mdd_pct": mdd,
        "sharpe": sharpe,
        "trades": len(trades),
        "round_trips": len(sells),
        "win_rate_pct": len(wins) / len(sells) * 100 if sells else 0.0,
        "profit_factor": gross_win / gross_loss if gross_loss > 0 else None,
        "realized_pnl": realized,
        "fees": fees,
        "start": int(df["time"].iloc[0]),
        "end": int(df["time"].iloc[-1]),
        "bars": len(df),
    }
