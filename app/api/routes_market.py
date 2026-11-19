from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.market import indicators
from app.security import require_login
from app.services import Services, get_svc
from app.upbit.rest import INTERVALS, UpbitAPIError

router = APIRouter(prefix="/api", dependencies=[Depends(require_login)])


def _clean(series) -> list:
    return [None if v != v else round(float(v), 8) for v in series]  # NaN → None


@router.get("/intervals")
async def intervals():
    return INTERVALS


@router.get("/markets")
async def markets(q: str = "", limit: int = 30, svc: Services = Depends(get_svc)):
    res = await svc.screener.search(q, limit=limit)
    return [
        {"market": m.market, "symbol": m.symbol, "korean_name": m.korean_name, "english_name": m.english_name,
         "warning": m.warning, "caution": m.caution, "flagged": svc.risk.flagged.get(m.symbol)}
        for m in res
    ]


@router.get("/tickers")
async def tickers(svc: Services = Depends(get_svc)):
    rows = await svc.screener.tickers("KRW")
    for r in rows:
        r["flagged"] = svc.risk.flagged.get(r["symbol"])
    return rows


@router.get("/candles")
async def candles(
    market: str,
    interval: str = "minute60",
    count: int = Query(300, ge=10, le=2000),
    svc: Services = Depends(get_svc),
):
    if interval not in INTERVALS:
        raise HTTPException(400, f"interval 은 {INTERVALS} 중 하나")
    try:
        df = await svc.candles.fetch(market.upper(), interval, count)
    except UpbitAPIError as e:
        raise HTTPException(502, str(e)) from e
    close = df["close"]
    mid, up, lo = indicators.bollinger(close, 20, 2)
    macd_line, macd_sig, macd_hist = indicators.macd(close)
    return {
        "market": market.upper(),
        "interval": interval,
        "candles": df[["time", "open", "high", "low", "close", "volume"]].to_dict("records"),
        "indicators": {
            "ma5": _clean(indicators.sma(close, 5)),
            "ma20": _clean(indicators.sma(close, 20)),
            "ma60": _clean(indicators.sma(close, 60)),
            "bb_mid": _clean(mid),
            "bb_upper": _clean(up),
            "bb_lower": _clean(lo),
            "rsi14": _clean(indicators.rsi(close, 14)),
            "macd": _clean(macd_line),
            "macd_signal": _clean(macd_sig),
            "macd_hist": _clean(macd_hist),
        },
    }


@router.get("/orderbook")
async def orderbook(market: str, svc: Services = Depends(get_svc)):
    cached = svc.bus.last_orderbook.get(market.upper())
    if cached:
        return cached
    rows = await svc.client.get_orderbook([market.upper()])
    return rows[0] if rows else {}


@router.get("/trades")
async def trades(market: str, count: int = 50, svc: Services = Depends(get_svc)):
    return await svc.client.get_trades(market.upper(), min(count, 200))


@router.get("/screener")
async def screener(
    min_value_24h: float = 0,
    min_change: float | None = None,
    max_change: float | None = None,
    rsi_below: float | None = None,
    rsi_above: float | None = None,
    rsi_interval: str = "minute60",
    exclude_warning: bool = True,
    sort: str = "value_24h",
    limit: int = 50,
    svc: Services = Depends(get_svc),
):
    if sort not in ("value_24h", "change_rate", "price"):
        raise HTTPException(400, "sort 는 value_24h/change_rate/price")
    if rsi_interval not in INTERVALS:
        raise HTTPException(400, "잘못된 rsi_interval")
    rows = await svc.screener.scan(
        min_value_24h=min_value_24h, min_change=min_change, max_change=max_change, rsi_below=rsi_below,
        rsi_above=rsi_above, rsi_interval=rsi_interval, exclude_warning=exclude_warning, sort=sort, limit=limit,
    )
    for r in rows:
        r["flagged"] = svc.risk.flagged.get(r["symbol"])
    return rows
