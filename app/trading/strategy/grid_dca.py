from __future__ import annotations

import pandas as pd

from app.trading.strategy.base import Context, Param, Signal, Strategy


class Grid(Strategy):
    """그리드 매매: [하단, 상단] 구간을 N칸으로 나눠 각 칸 하단에서 사고 한 칸 위에서 판다.

    칸별 [보유수량, 단위원가] 를 state["lots"] 에 기록해, 산 수량만큼만 팔고 손익도 그 칸의 원가로 계산한다.
    """

    type = "grid"
    label = "그리드 매매"
    description = "가격 구간을 N칸으로 나누어 칸 하단에서 분할 매수, 한 칸 위에서 분할 매도합니다. 횡보장에 적합합니다."
    default_interval = "minute1"
    params_spec = [
        Param("lower", "하단 가격", "float", 0, 0, help="구간 하단 (원)"),
        Param("upper", "상단 가격", "float", 0, 0, help="구간 상단 (원)"),
        Param("grids", "칸 수", "int", 10, 2, 100),
    ]

    def validate(self) -> None:
        if self.p("lower") <= 0 or self.p("upper") <= self.p("lower"):
            raise ValueError("하단 < 상단 인 양수 가격 구간을 지정하세요")

    def levels(self) -> list[float]:
        lo, hi, n = self.p("lower"), self.p("upper"), self.p("grids")
        step = (hi - lo) / n
        return [lo + step * i for i in range(n + 1)]

    def on_bar(self, df: pd.DataFrame, ctx: Context) -> list[Signal]:
        lv = self.levels()
        lots: dict[str, list[float]] = ctx.state.setdefault("lots", {})
        price = ctx.price
        signals: list[Signal] = []

        for key, (vol, cost) in list(lots.items()):
            i = int(key)
            if vol > 0 and price >= lv[i + 1]:
                signals.append(Signal("sell", volume=vol, reason=f"그리드 {i}칸 매도 ({lv[i + 1]:,.0f})",
                                      tag=key, price=lv[i + 1], cost_price=cost))

        if price < lv[0] or price > lv[-1]:
            return signals
        # 현재가 바로 위의 매수선(가장 가까운 칸)만 매수 → 급락 시 한 번에 여러 칸을 사지 않는다
        candidates = [i for i in range(len(lv) - 1) if lv[i] >= price and str(i) not in lots]
        if candidates:
            i = min(candidates)
            if lv[i] - price <= (lv[1] - lv[0]):
                per_grid = ctx.budget / self.p("grids") / (1 + ctx.fee_rate)
                amount = min(per_grid, self.buyable(ctx))
                if amount >= 5000:
                    signals.append(Signal("buy", amount_krw=amount, reason=f"그리드 {i}칸 매수 ({lv[i]:,.0f})", tag=str(i), price=lv[i]))
        return signals

    def on_fill(self, signal: Signal, volume: float, price: float, ctx: Context) -> None:
        lots = ctx.state.setdefault("lots", {})
        if signal.tag is None:  # 전략 외부 청산(긴급 청산 등)
            lots.clear()
            return
        if signal.action == "buy":
            old_vol, old_cost = lots.get(signal.tag, [0.0, 0.0])
            unit_cost = price * (1 + ctx.fee_rate)
            new_vol = old_vol + volume
            lots[signal.tag] = [new_vol, (old_vol * old_cost + volume * unit_cost) / new_vol]
        else:
            lots.pop(signal.tag, None)


class DCA(Strategy):
    """분할매수(적립식): 일정 주기 또는 일정 하락폭마다 정액 매수, 평균단가 대비 목표 수익에서 전량 매도."""

    type = "dca"
    label = "분할매수(DCA)"
    description = "정해진 주기마다(또는 직전 매수가 대비 하락 시) 정액 매수하고, 평균단가 대비 목표 수익률에서 전량 매도합니다."
    default_interval = "minute60"
    params_spec = [
        Param("amount", "회당 매수금액", "float", 50000, 5000),
        Param("interval_hours", "매수 주기(시간)", "float", 24, 0.1, 24 * 30),
        Param("drop_pct", "추가매수 하락폭 %", "float", 3, 0, 50, help="직전 매수가 대비 이만큼 떨어지면 주기와 무관하게 매수. 0=끔"),
        Param("max_buys", "최대 매수 횟수", "int", 10, 1, 1000),
        Param("take_profit_pct", "익절 %", "float", 10, 0, 500),
        Param("stop_loss_pct", "손절 %", "float", 0, 0, 90, help="0이면 사용 안 함"),
    ]

    def on_bar(self, df: pd.DataFrame, ctx: Context) -> list[Signal]:
        stop = self.exit_by_stop(ctx)
        if stop:
            return [stop]
        st = ctx.state
        if st.get("buys", 0) >= self.p("max_buys"):
            return []
        amount = min(self.p("amount"), self.buyable(ctx))
        if amount < 5000:
            return []
        now = ctx.now.timestamp()
        last_ts, last_px = st.get("last_buy_ts"), st.get("last_buy_price")
        due = last_ts is None or now - last_ts >= self.p("interval_hours") * 3600
        dropped = bool(last_px) and self.p("drop_pct") > 0 and ctx.price <= last_px * (1 - self.p("drop_pct") / 100)
        if due or dropped:
            reason = "정기 매수" if due else f"직전 매수가 대비 -{self.p('drop_pct')}% 추가매수"
            trigger = None if due else last_px * (1 - self.p("drop_pct") / 100)
            return [Signal("buy", amount_krw=amount, reason=f"{reason} ({st.get('buys', 0) + 1}회차)", tag=str(now), price=trigger)]
        return []

    def on_fill(self, signal: Signal, volume: float, price: float, ctx: Context) -> None:
        st = ctx.state
        if signal.action == "buy":
            st["buys"] = st.get("buys", 0) + 1
            st["last_buy_ts"] = float(signal.tag) if signal.tag else ctx.now.timestamp()
            st["last_buy_price"] = price
        else:  # 전량 매도 → 사이클 리셋
            st.update(buys=0, last_buy_ts=None, last_buy_price=None)
