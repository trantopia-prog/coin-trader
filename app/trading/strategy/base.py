"""전략 인터페이스.

같은 전략 클래스를 라이브 엔진과 백테스트가 공유한다.
- on_bar(df, ctx): df 의 마지막 행은 '진행 중인 현재 봉'이며 close 는 현재가(ctx.price)로 갱신되어 있다.
  라이브에서는 수 초마다, 백테스트에서는 봉 하나당 시가→저가/고가→종가 경로의 4개 지점에서 호출된다.
- 전략은 Signal 리스트를 돌려주기만 하고, 실제 주문/리스크 검사/포지션 갱신은 엔진(또는 백테스트 러너)이 한다.
- ctx.state 는 DB 에 저장되는 전략별 상태 dict 로, on_bar/on_fill 에서 자유롭게 수정할 수 있다.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Literal

import pandas as pd


@dataclass
class Param:
    key: str
    label: str
    type: Literal["int", "float", "bool", "select"]
    default: Any
    min: float | None = None
    max: float | None = None
    options: list[str] | None = None
    help: str = ""

    def coerce(self, v: Any) -> Any:
        if v is None:
            return self.default
        if self.type == "int":
            v = int(float(v))
        elif self.type == "float":
            v = float(v)
        elif self.type == "bool":
            v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "on", "yes")
        elif self.type == "select":
            v = str(v)
            if self.options and v not in self.options:
                raise ValueError(f"{self.label}: {self.options} 중 하나여야 합니다")
            return v
        if self.min is not None and v < self.min:
            raise ValueError(f"{self.label}: {self.min} 이상이어야 합니다")
        if self.max is not None and v > self.max:
            raise ValueError(f"{self.label}: {self.max} 이하여야 합니다")
        return v


@dataclass
class Signal:
    action: Literal["buy", "sell"]
    amount_krw: float | None = None  # 매수 금액 (수수료 제외)
    volume: float | None = None  # 매도 수량. None 이면 전략 보유분 전량
    reason: str = ""
    tag: str | None = None  # on_fill 로 되돌려 받을 식별자 (예: 그리드 칸 번호)
    price: float | None = None  # 신호를 만든 기준가(목표가, 그리드 레벨, 손절가 등). 라이브는 시장가로 주문하므로 무시하고,
    #                             백테스트는 봉 내부 경로가 이 가격을 지나가면 이 가격으로 체결한다.
    cost_price: float | None = None  # 매도 시 이 수량의 단위 매입원가(수수료 포함). 그리드처럼 칸별 원가를 따로 관리할 때
    #                                  지정하면 실현손익을 평균단가 대신 이 값으로 계산한다.


@dataclass
class Context:
    market: str
    price: float
    now: datetime
    pos_volume: float
    avg_price: float
    budget: float  # 전략에 할당된 총 예산
    cash: float  # 남은 예산 = budget - 보유분 매입원가
    fee_rate: float = 0.0005
    state: dict = field(default_factory=dict)

    @property
    def has_position(self) -> bool:
        return self.pos_volume * self.price >= 1000  # 1,000원 미만 잔량은 '먼지'로 간주

    @property
    def pnl_pct(self) -> float:
        return (self.price / self.avg_price - 1) * 100 if self.avg_price > 0 else 0.0


class Strategy(ABC):
    type: ClassVar[str]
    label: ClassVar[str]
    description: ClassVar[str]
    default_interval: ClassVar[str] = "minute60"
    params_spec: ClassVar[list[Param]] = []

    def __init__(self, params: dict | None = None):
        params = params or {}
        self.params = {p.key: p.coerce(params.get(p.key)) for p in self.params_spec}
        self.validate()

    def validate(self) -> None:
        """파라미터 간 관계 검증 (예: 단기 < 장기). 문제가 있으면 ValueError."""

    @property
    def min_bars(self) -> int:
        return 2

    @abstractmethod
    def on_bar(self, df: pd.DataFrame, ctx: Context) -> list[Signal]: ...

    def on_fill(self, signal: Signal, volume: float, price: float, ctx: Context) -> None:
        """주문 체결 후 호출. 기본 동작 없음."""

    # ---- 공용 헬퍼 ----
    def p(self, key: str) -> Any:
        return self.params[key]

    @staticmethod
    def buyable(ctx: Context, ratio: float = 1.0) -> float:
        return math.floor(max(ctx.cash, 0) * ratio / (1 + ctx.fee_rate))

    def exit_by_stop(self, ctx: Context) -> Signal | None:
        if not ctx.has_position:
            return None
        sl = self.params.get("stop_loss_pct") or 0
        tp = self.params.get("take_profit_pct") or 0
        if sl > 0 and ctx.pnl_pct <= -sl:
            return Signal("sell", reason=f"손절 {ctx.pnl_pct:.2f}%", price=ctx.avg_price * (1 - sl / 100))
        if tp > 0 and ctx.pnl_pct >= tp:
            return Signal("sell", reason=f"익절 {ctx.pnl_pct:.2f}%", price=ctx.avg_price * (1 + tp / 100))
        return None

    @classmethod
    def describe(cls) -> dict:
        return {
            "type": cls.type,
            "label": cls.label,
            "description": cls.description,
            "default_interval": cls.default_interval,
            "params": [asdict(p) for p in cls.params_spec],
        }


def bar_id(df: pd.DataFrame) -> int:
    return int(df["time"].iloc[-1])
