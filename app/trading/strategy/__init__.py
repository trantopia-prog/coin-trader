from app.trading.strategy.base import Context, Param, Signal, Strategy
from app.trading.strategy.grid_dca import DCA, Grid
from app.trading.strategy.ma_cross import MACross
from app.trading.strategy.rsi_bollinger import RSIBollinger
from app.trading.strategy.volatility_breakout import VolatilityBreakout

STRATEGIES: dict[str, type[Strategy]] = {
    cls.type: cls for cls in (VolatilityBreakout, MACross, RSIBollinger, Grid, DCA)
}


def create_strategy(type_: str, params: dict | None = None) -> Strategy:
    if type_ not in STRATEGIES:
        raise ValueError(f"알 수 없는 전략: {type_}")
    return STRATEGIES[type_](params)


__all__ = ["STRATEGIES", "Context", "Param", "Signal", "Strategy", "create_strategy"]
