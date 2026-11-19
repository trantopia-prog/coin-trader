from __future__ import annotations

import pandas as pd

from app.market import indicators
from app.trading.strategy.base import Context, Param, Signal, Strategy, bar_id


class RSIBollinger(Strategy):
    """RSI + 볼린저밴드 역추세(평균회귀)."""

    type = "rsi_bollinger"
    label = "RSI/볼린저 역추세"
    description = "가격이 볼린저 하단 이하이고 RSI 과매도일 때 매수, 상단(또는 중심선) 도달 또는 RSI 과매수 시 매도합니다."
    default_interval = "minute15"
    params_spec = [
        Param("rsi_period", "RSI 기간", "int", 14, 2, 100),
        Param("oversold", "과매도 RSI", "float", 30, 1, 50),
        Param("overbought", "과매수 RSI", "float", 70, 50, 99),
        Param("bb_period", "볼린저 기간", "int", 20, 5, 200),
        Param("bb_k", "볼린저 표준편차 배수", "float", 2.0, 0.5, 4.0),
        Param("exit_at", "청산 기준", "select", "upper", options=["upper", "mid"]),
        Param("alloc_pct", "예산 사용 비율 %", "float", 100, 1, 100),
        Param("stop_loss_pct", "손절 %", "float", 3, 0, 50, help="0이면 사용 안 함"),
    ]

    @property
    def min_bars(self) -> int:
        return max(self.p("rsi_period"), self.p("bb_period")) + 2

    def on_bar(self, df: pd.DataFrame, ctx: Context) -> list[Signal]:
        if len(df) < self.min_bars:
            return []
        close = df["close"]
        r = indicators.rsi(close, self.p("rsi_period")).iloc[-1]
        mid, upper, lower = (x.iloc[-1] for x in indicators.bollinger(close, self.p("bb_period"), self.p("bb_k")))
        if pd.isna(r) or pd.isna(lower):
            return []
        bar = bar_id(df)

        if ctx.has_position:
            stop = self.exit_by_stop(ctx)
            if stop:
                stop.tag = str(bar)
                return [stop]
            target = upper if self.p("exit_at") == "upper" else mid
            if ctx.price >= target:
                return [Signal("sell", reason=f"밴드 {self.p('exit_at')} 도달 ({target:,.0f})", tag=str(bar), price=target)]
            if r >= self.p("overbought"):
                return [Signal("sell", reason=f"RSI 과매수 {r:.1f}", tag=str(bar))]
            return []

        if ctx.state.get("exit_bar") == bar:
            return []  # 청산한 봉에서 바로 재진입하지 않음
        if ctx.price <= lower and r <= self.p("oversold"):
            amount = self.buyable(ctx, self.p("alloc_pct") / 100)
            return [Signal("buy", amount_krw=amount, reason=f"하단밴드 이탈 + RSI {r:.1f}", tag=str(bar), price=lower)]
        return []

    def on_fill(self, signal: Signal, volume: float, price: float, ctx: Context) -> None:
        if signal.action == "sell" and signal.tag:
            ctx.state["exit_bar"] = int(signal.tag)
