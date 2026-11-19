from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.backtest.runner import run_backtest
from app.security import require_login
from app.services import Services, get_svc
from app.trading.strategy import STRATEGIES, create_strategy
from app.upbit.rest import INTERVALS, UpbitAPIError

router = APIRouter(prefix="/api", dependencies=[Depends(require_login)])


class BacktestIn(BaseModel):
    type: str
    params: dict = {}
    market: str = "KRW-BTC"
    interval: str = "day"
    count: int = Field(365, ge=30, le=2000)
    initial: float = Field(1_000_000, ge=10_000)
    fee_rate: float = Field(0.0005, ge=0, le=0.01)
    slippage: float = Field(0.001, ge=0, le=0.05)


@router.post("/backtest")
async def backtest(body: BacktestIn, svc: Services = Depends(get_svc)):
    if body.type not in STRATEGIES:
        raise HTTPException(400, "알 수 없는 전략 유형")
    if body.interval not in INTERVALS:
        raise HTTPException(400, "잘못된 캔들 주기")
    try:
        strat = create_strategy(body.type, body.params)
        df = await svc.candles.fetch(body.market.upper(), body.interval, body.count)
        # CPU 작업이므로 스레드에서 실행해 이벤트 루프(실시간 매매)를 막지 않는다
        result = await asyncio.to_thread(
            run_backtest, strat, df, market=body.market.upper(), interval=body.interval,
            initial=body.initial, fee_rate=body.fee_rate, slippage=body.slippage,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except UpbitAPIError as e:
        raise HTTPException(502, str(e)) from e
    result["candles"] = df[["time", "open", "high", "low", "close"]].to_dict("records")
    result["params"] = strat.params
    return result
