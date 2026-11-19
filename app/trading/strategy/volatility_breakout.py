from __future__ import annotations

import pandas as pd

from app.trading.strategy.base import Context, Param, Signal, Strategy, bar_id


class VolatilityBreakout(Strategy):
    """래리 윌리엄스 변동성 돌파.

    매수: 현재가 ≥ 당일 시가 + k × (전일 고가 − 전일 저가)  (선택: 시가가 N일 이동평균 위일 때만)
    매도: 다음 봉 시작 시 전량 청산 (+ 선택적 손절)
    """

    type = "volatility_breakout"
    label = "변동성 돌파"
    description = "당일 시가 + k×전일 변동폭을 돌파하면 매수하고 다음 봉 시가에 청산합니다."
    default_interval = "day"
    params_spec = [
        Param("k", "k 값", "float", 0.5, 0.1, 1.0, help="변동폭에 곱하는 계수. 클수록 보수적"),
        Param("ma_filter", "이평 필터 (0=끔)", "int", 5, 0, 120, help="시가가 N봉 이동평균보다 높을 때만 매수"),
        Param("alloc_pct", "예산 사용 비율 %", "float", 100, 1, 100),
        Param("stop_loss_pct", "손절 %", "float", 0, 0, 50, help="0이면 사용 안 함"),
    ]

    @property
    def min_bars(self) -> int:
        return max(self.p("ma_filter"), 1) + 2

    def on_bar(self, df: pd.DataFrame, ctx: Context) -> list[Signal]:
        if len(df) < self.min_bars:
            return []
        bar = bar_id(df)
        cur, prev = df.iloc[-1], df.iloc[-2]

        if ctx.has_position:
            entry = ctx.state.get("entry_bar")
            if entry is None:  # 상태 유실(재시작 등) 시 다음 봉에 청산되도록 표시
                ctx.state["entry_bar"] = bar
                return []
            if bar != entry:
                return [Signal("sell", reason="다음 봉 시가 청산")]
            stop = self.exit_by_stop(ctx)
            return [stop] if stop else []

        if ctx.state.get("traded_bar") == bar:
            return []  # 같은 봉에서 재진입 금지
        target = cur["open"] + self.p("k") * (prev["high"] - prev["low"])
        if ctx.price < target:
            return []
        n = self.p("ma_filter")
        if n > 0:
            ma = df["close"].iloc[-n - 1 : -1].mean()
            if cur["open"] <= ma:
                return []
        amount = self.buyable(ctx, self.p("alloc_pct") / 100)
        return [Signal("buy", amount_krw=amount, reason=f"목표가 {target:,.0f} 돌파", tag=str(bar), price=target)]

    def on_fill(self, signal: Signal, volume: float, price: float, ctx: Context) -> None:
        if signal.action == "buy":
            ctx.state["entry_bar"] = int(signal.tag) if signal.tag else None
            ctx.state["traded_bar"] = ctx.state["entry_bar"]
        else:
            ctx.state["entry_bar"] = None
