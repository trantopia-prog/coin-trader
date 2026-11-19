from __future__ import annotations

import pandas as pd

from app.market import indicators
from app.trading.strategy.base import Context, Param, Signal, Strategy, bar_id


class MACross(Strategy):
    """이동평균 교차. 확정된 봉(현재 진행 봉 제외)으로만 판단해 신호가 흔들리지 않게 한다."""

    type = "ma_cross"
    label = "이동평균 교차"
    description = "단기 이평이 장기 이평을 상향 돌파(골든크로스)하면 매수, 하향 돌파(데드크로스)하면 매도합니다."
    default_interval = "minute60"
    params_spec = [
        Param("short", "단기 이평", "int", 5, 2, 200),
        Param("long", "장기 이평", "int", 20, 3, 400),
        Param("ma_type", "이평 종류", "select", "ema", options=["sma", "ema"]),
        Param("alloc_pct", "예산 사용 비율 %", "float", 100, 1, 100),
        Param("stop_loss_pct", "손절 %", "float", 0, 0, 50, help="0이면 사용 안 함"),
        Param("take_profit_pct", "익절 %", "float", 0, 0, 500, help="0이면 사용 안 함"),
    ]

    def validate(self) -> None:
        if self.p("short") >= self.p("long"):
            raise ValueError("단기 이평은 장기 이평보다 작아야 합니다")

    @property
    def min_bars(self) -> int:
        return self.p("long") + 3

    def on_bar(self, df: pd.DataFrame, ctx: Context) -> list[Signal]:
        if len(df) < self.min_bars:
            return []
        stop = self.exit_by_stop(ctx)
        if stop:
            return [stop]

        bar = bar_id(df)
        if ctx.state.get("last_eval_bar") == bar:
            return []
        ctx.state["last_eval_bar"] = bar

        closed = df["close"].iloc[:-1]
        fn = indicators.ema if self.p("ma_type") == "ema" else indicators.sma
        s, l = fn(closed, self.p("short")), fn(closed, self.p("long"))
        if pd.isna(s.iloc[-2]) or pd.isna(l.iloc[-2]):
            return []
        golden = s.iloc[-2] <= l.iloc[-2] and s.iloc[-1] > l.iloc[-1]
        dead = s.iloc[-2] >= l.iloc[-2] and s.iloc[-1] < l.iloc[-1]

        if golden and not ctx.has_position:
            return [Signal("buy", amount_krw=self.buyable(ctx, self.p("alloc_pct") / 100), reason="골든크로스")]
        if dead and ctx.has_position:
            return [Signal("sell", reason="데드크로스")]
        return []
