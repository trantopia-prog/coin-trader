"""업비트 원화(KRW) 마켓 호가 단위 / 최소 주문금액.

출처: https://docs.upbit.com/kr/docs/krw-market-info (2025-07-31 개편 기준, 2026-11 확인)
업비트가 정책을 바꾸면 이 표도 갱신해야 한다. 일부 종목(USDT/USDC 등)은 예외 단위가 있다.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_UP, Decimal

MIN_ORDER_KRW = 5000.0
FEE_RATE = 0.0005  # 원화마켓 일반 수수료 0.05%

_KRW_TICKS: list[tuple[float, str]] = [
    (1_000_000, "1000"),
    (500_000, "500"),
    (100_000, "100"),
    (50_000, "50"),
    (10_000, "10"),
    (5_000, "5"),
    (100, "1"),
    (10, "0.1"),
    (1, "0.01"),
    (0.1, "0.001"),
    (0.01, "0.0001"),
    (0.001, "0.00001"),
    (0.0001, "0.000001"),
    (0.00001, "0.0000001"),
    (0, "0.00000001"),
]


def tick_size(price: float) -> Decimal:
    for threshold, tick in _KRW_TICKS:
        if price >= threshold:
            return Decimal(tick)
    return Decimal(_KRW_TICKS[-1][1])


def round_price(price: float, side: str = "bid") -> float:
    """호가 단위로 맞춘다. 매수는 내림(더 싸게), 매도는 올림(더 비싸게)."""
    tick = tick_size(price)
    q = (Decimal(str(price)) / tick).quantize(Decimal(1), rounding=ROUND_DOWN if side == "bid" else ROUND_UP)
    return float(q * tick)
