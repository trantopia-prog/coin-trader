"""주문 전 리스크 검사와 거래 정지 상태 관리.

- 매수만 막는다. 매도(청산)는 정지 상태에서도 허용해 언제든 포지션을 정리할 수 있게 한다.
- 정지 사유: 사용자 긴급정지, 일일 손실 한도 도달
- 업비트 공지로 유의종목/거래지원종료가 감지된 코인은 매수 차단
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.db import Database
from app.models import AppState
from app.trading.broker import OrderRequest
from app.trading.tick import MIN_ORDER_KRW

log = logging.getLogger(__name__)
KST = timezone(timedelta(hours=9))


class RiskManager:
    def __init__(self, settings: Settings, db: Database):
        self.db = db
        self.limits = {
            "max_order_krw": settings.risk_max_order_krw,
            "max_position_ratio": settings.risk_max_position_ratio,
            "daily_loss_limit_pct": settings.risk_daily_loss_limit_pct,
        }
        self.halted = False
        self.halt_reason = ""
        self.flagged: dict[str, str] = {}
        self.day_start: dict[str, tuple[str, float]] = {}  # mode -> (KST 날짜, 시작 자산)

    async def load(self) -> None:
        async with self.db.session() as s:
            st = await s.get(AppState, "risk")
            if st and isinstance(st.value, dict):
                self.limits.update(st.value.get("limits", {}))
                self.halted = bool(st.value.get("halted"))
                self.halt_reason = st.value.get("halt_reason", "")
                self.day_start = {k: tuple(v) for k, v in st.value.get("day_start", {}).items()}

    async def save(self) -> None:
        value = {
            "limits": self.limits,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "day_start": {k: list(v) for k, v in self.day_start.items()},
        }
        async with self.db.session() as s:
            await s.merge(AppState(key="risk", value=value))
            await s.commit()

    async def update_limits(self, **kw: float) -> None:
        for k, v in kw.items():
            if k in self.limits and v is not None:
                if float(v) < 0:
                    raise ValueError(f"{k} 는 0 이상이어야 합니다")
                self.limits[k] = float(v)
        await self.save()

    async def halt(self, reason: str) -> None:
        self.halted, self.halt_reason = True, reason
        log.warning("거래 정지: %s", reason)
        await self.save()

    async def resume(self) -> None:
        self.halted, self.halt_reason = False, ""
        await self.save()

    def flag(self, symbol: str, reason: str, on: bool = True) -> None:
        if on:
            self.flagged[symbol.upper()] = reason
        else:
            self.flagged.pop(symbol.upper(), None)

    def check(self, req: OrderRequest, *, ref_price: float, equity: float, position_value: float) -> str | None:
        """거부 사유를 반환. 통과하면 None."""
        amount = req.price if req.ord_type == "price" else (req.price or ref_price) * (req.volume or 0)
        amount = amount or 0.0
        if amount < MIN_ORDER_KRW:
            return f"최소 주문금액 {MIN_ORDER_KRW:,.0f}원 미만 ({amount:,.0f}원)"
        if req.side == "ask":
            return None
        if self.halted:
            return f"거래 정지 상태: {self.halt_reason}"
        symbol = req.market.split("-", 1)[1]
        if symbol in self.flagged:
            return f"유의/거래지원종료 공지 종목: {self.flagged[symbol]}"
        if self.limits["max_order_krw"] > 0 and amount > self.limits["max_order_krw"]:
            return f"1회 최대 주문금액 초과 ({amount:,.0f} > {self.limits['max_order_krw']:,.0f})"
        ratio = self.limits["max_position_ratio"]
        if ratio > 0 and equity > 0 and (position_value + amount) / equity > ratio + 1e-9:
            return f"종목당 최대 비중 {ratio * 100:.0f}% 초과"
        return None

    async def on_equity(self, mode: str, equity: float, now: datetime | None = None) -> float | None:
        """자산 갱신. KST 자정마다 기준 자산을 다시 잡고, 손실 한도에 닿으면 정지. 당일 손익률(%) 반환."""
        if equity <= 0:
            return None
        today = (now or datetime.now(timezone.utc)).astimezone(KST).date().isoformat()
        start = self.day_start.get(mode)
        if not start or start[0] != today:
            self.day_start[mode] = (today, equity)
            await self.save()
            return 0.0
        pnl_pct = (equity / start[1] - 1) * 100
        limit = self.limits["daily_loss_limit_pct"]
        if limit > 0 and pnl_pct <= -limit and not self.halted:
            await self.halt(f"일일 손실 한도 도달 ({pnl_pct:.2f}%)")
        return pnl_pct

    def snapshot(self) -> dict:
        return {
            "limits": self.limits,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "flagged": self.flagged,
            "day_start": self.day_start,
        }
