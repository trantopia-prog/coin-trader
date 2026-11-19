from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from app.models import AppState, EngineLog, EquitySnapshot, Order, StrategyConfig, StrategyRuntime, iso
from app.security import check_password, mask, record_failure, require_login, throttle_login
from app.services import Services, get_svc
from app.trading.engine import order_dict
from app.trading.strategy import STRATEGIES, create_strategy
from app.upbit.rest import INTERVALS, UpbitAPIError

router = APIRouter(prefix="/api", dependencies=[Depends(require_login)])


# ---------------------------------------------------------------- 상태 / 잔고
@router.get("/status")
async def status(svc: Services = Depends(get_svc)):
    return await svc.engine.status()


@router.get("/balances")
async def balances(svc: Services = Depends(get_svc)):
    try:
        bal = await svc.engine.broker.balances()
    except UpbitAPIError as e:
        raise HTTPException(502, str(e)) from e
    rows = []
    for cur, b in bal.items():
        qty = b["balance"] + b["locked"]
        price = 1.0 if cur == "KRW" else await svc.engine.price_of(f"KRW-{cur}")
        value = qty * price if price else None
        cost = qty * b["avg_buy_price"] if cur != "KRW" else qty
        rows.append({
            "currency": cur, "balance": b["balance"], "locked": b["locked"], "avg_buy_price": b["avg_buy_price"],
            "price": price, "value": value,
            "pnl": (value - cost) if value is not None and cur != "KRW" else None,
            "pnl_pct": ((value / cost - 1) * 100) if value and cost and cur != "KRW" else None,
        })
    rows.sort(key=lambda r: -(r["value"] or 0))
    return {"mode": svc.engine.mode, "rows": rows, "equity": sum(r["value"] or 0 for r in rows)}


@router.get("/equity")
async def equity(days: int = 30, svc: Services = Depends(get_svc)):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    async with svc.db.session() as s:
        rows = (await s.execute(
            select(EquitySnapshot).where(EquitySnapshot.mode == svc.engine.mode, EquitySnapshot.ts >= since).order_by(EquitySnapshot.ts)
        )).scalars()
        return [{"ts": iso(r.ts), "equity": r.equity} for r in rows]


# ---------------------------------------------------------------- 전략
class StrategyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    type: str
    market: str
    interval: str
    params: dict = {}
    budget_krw: float = Field(gt=0)


def _validate(body: StrategyIn) -> dict:
    if body.type not in STRATEGIES:
        raise HTTPException(400, "알 수 없는 전략 유형")
    if body.interval not in INTERVALS:
        raise HTTPException(400, "잘못된 캔들 주기")
    if body.budget_krw < 5000:
        raise HTTPException(400, "예산은 최소 5,000원 이상")
    try:
        return create_strategy(body.type, body.params).params
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/strategies/types")
async def strategy_types():
    return [cls.describe() for cls in STRATEGIES.values()]


@router.get("/strategies")
async def list_strategies(svc: Services = Depends(get_svc)):
    mode = svc.engine.mode
    async with svc.db.session() as s:
        cfgs = list((await s.execute(select(StrategyConfig).order_by(StrategyConfig.id))).scalars())
        rts = {r.strategy_id: r for r in (await s.execute(select(StrategyRuntime).where(StrategyRuntime.mode == mode))).scalars()}
    out = []
    for c in cfgs:
        rt = rts.get(c.id)
        price = svc.bus.price(c.market)
        pos = rt.pos_volume if rt else 0.0
        avg = rt.avg_price if rt else 0.0
        out.append({
            "id": c.id, "name": c.name, "type": c.type, "label": STRATEGIES[c.type].label if c.type in STRATEGIES else c.type,
            "market": c.market, "interval": c.interval, "params": c.params, "budget_krw": c.budget_krw, "enabled": c.enabled,
            "pos_volume": pos, "avg_price": avg, "price": price,
            "unrealized_pnl": (price - avg) * pos if price and pos else 0.0,
            "realized_pnl": rt.realized_pnl if rt else 0.0,
            "state": rt.state if rt else {},
            "error": svc.engine.last_error.get(c.id),
        })
    return out


@router.post("/strategies")
async def create(body: StrategyIn, svc: Services = Depends(get_svc)):
    params = _validate(body)
    cfg = StrategyConfig(name=body.name, type=body.type, market=body.market.upper(), interval=body.interval,
                         params=params, budget_krw=body.budget_krw, enabled=False)
    async with svc.db.session() as s:
        s.add(cfg)
        await s.commit()
    await svc.engine.refresh_subscriptions()
    await svc.engine.log("info", f"전략 생성: {cfg.name} ({cfg.market}, {cfg.type})", cfg.id)
    return {"id": cfg.id}


@router.put("/strategies/{sid}")
async def update(sid: int, body: StrategyIn, svc: Services = Depends(get_svc)):
    params = _validate(body)
    async with svc.db.session() as s:
        cfg = await s.get(StrategyConfig, sid)
        if not cfg:
            raise HTTPException(404, "전략 없음")
        rt = await s.scalar(select(StrategyRuntime).where(StrategyRuntime.strategy_id == sid, StrategyRuntime.mode == svc.engine.mode))
        if rt and rt.pos_volume > 0 and body.market.upper() != cfg.market:
            raise HTTPException(400, "포지션 보유 중에는 종목을 바꿀 수 없습니다. 먼저 청산하세요.")
        cfg.name, cfg.type, cfg.market, cfg.interval = body.name, body.type, body.market.upper(), body.interval
        cfg.params, cfg.budget_krw = params, body.budget_krw
        await s.commit()
    await svc.engine.refresh_subscriptions()
    return {"ok": True}


@router.delete("/strategies/{sid}")
async def remove(sid: int, svc: Services = Depends(get_svc)):
    async with svc.db.session() as s:
        cfg = await s.get(StrategyConfig, sid)
        if not cfg:
            raise HTTPException(404, "전략 없음")
        held = await s.scalar(select(StrategyRuntime).where(StrategyRuntime.strategy_id == sid, StrategyRuntime.pos_volume > 0))
        if held:
            raise HTTPException(400, f"{held.mode} 모드에 보유 포지션이 있습니다. 청산 후 삭제하세요.")
        await s.execute(delete(StrategyRuntime).where(StrategyRuntime.strategy_id == sid))
        await s.delete(cfg)
        await s.commit()
    return {"ok": True}


@router.post("/strategies/{sid}/start")
async def start(sid: int, svc: Services = Depends(get_svc)):
    return await _set_enabled(svc, sid, True)


@router.post("/strategies/{sid}/stop")
async def stop(sid: int, liquidate: bool = False, svc: Services = Depends(get_svc)):
    res = await _set_enabled(svc, sid, False)
    if liquidate:
        res["liquidated"] = await svc.engine.liquidate_strategies(sid)
    return res


async def _set_enabled(svc: Services, sid: int, enabled: bool) -> dict:
    async with svc.db.session() as s:
        cfg = await s.get(StrategyConfig, sid)
        if not cfg:
            raise HTTPException(404, "전략 없음")
        cfg.enabled = enabled
        await s.commit()
    await svc.engine.log("info", f"전략 {'시작' if enabled else '정지'}: {cfg.name}", sid)
    return {"ok": True}


# ---------------------------------------------------------------- 주문
class OrderIn(BaseModel):
    market: str
    side: str
    ord_type: str
    price: float | None = None
    volume: float | None = None


@router.get("/orders")
async def orders(state: str | None = None, market: str | None = None, limit: int = 100, svc: Services = Depends(get_svc)):
    stmt = select(Order).where(Order.mode == svc.engine.mode).order_by(Order.id.desc()).limit(min(limit, 500))
    if state:
        stmt = stmt.where(Order.state == state)
    if market:
        stmt = stmt.where(Order.market == market.upper())
    async with svc.db.session() as s:
        return [order_dict(o) for o in (await s.execute(stmt)).scalars()]


@router.post("/orders")
async def place(body: OrderIn, svc: Services = Depends(get_svc)):
    try:
        return await svc.engine.manual_order(body.market.upper(), body.side, body.ord_type, body.price, body.volume)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/orders/{oid}")
async def cancel(oid: int, svc: Services = Depends(get_svc)):
    try:
        return await svc.engine.cancel_order(oid)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e


@router.get("/logs")
async def logs(limit: int = 200, strategy_id: int | None = None, svc: Services = Depends(get_svc)):
    stmt = select(EngineLog).order_by(EngineLog.id.desc()).limit(min(limit, 1000))
    if strategy_id:
        stmt = stmt.where(EngineLog.strategy_id == strategy_id)
    async with svc.db.session() as s:
        return [{"id": l.id, "ts": iso(l.ts), "level": l.level, "message": l.message, "strategy_id": l.strategy_id}
                for l in (await s.execute(stmt)).scalars()]


# ---------------------------------------------------------------- 리스크 / 모드
class PasswordIn(BaseModel):
    password: str


def _confirm_password(request: Request, svc: Services, password: str) -> None:
    ip = request.client.host if request.client else "?"
    throttle_login(ip)
    if not check_password(svc.settings, password):
        record_failure(ip)
        raise HTTPException(403, "비밀번호가 올바르지 않습니다")


class EmergencyIn(BaseModel):
    liquidate: bool = False


@router.post("/emergency-stop")
async def emergency_stop(body: EmergencyIn, svc: Services = Depends(get_svc)):
    return await svc.engine.emergency_stop(body.liquidate)


@router.post("/resume")
async def resume(svc: Services = Depends(get_svc)):
    await svc.risk.resume()
    await svc.engine.log("warning", "거래 재개")
    return await svc.engine.status()


class RiskIn(BaseModel):
    max_order_krw: float | None = None
    max_position_ratio: float | None = Field(None, ge=0, le=1)
    daily_loss_limit_pct: float | None = Field(None, ge=0, le=100)


@router.put("/risk")
async def update_risk(body: RiskIn, svc: Services = Depends(get_svc)):
    try:
        await svc.risk.update_limits(**body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return svc.risk.snapshot()


class ModeIn(BaseModel):
    mode: str
    password: str = ""


@router.post("/mode")
async def set_mode(body: ModeIn, request: Request, svc: Services = Depends(get_svc)):
    if body.mode == "live":
        _confirm_password(request, svc, body.password)
    try:
        await svc.engine.set_mode(body.mode, reason="(대시보드)")
    except (RuntimeError, ValueError, UpbitAPIError) as e:
        raise HTTPException(400, str(e)) from e
    return await svc.engine.status()


class PaperResetIn(BaseModel):
    krw: float | None = Field(None, gt=0)


@router.post("/paper/reset")
async def paper_reset(body: PaperResetIn, svc: Services = Depends(get_svc)):
    try:
        await svc.engine.reset_paper(body.krw)
    except RuntimeError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


# ---------------------------------------------------------------- API 키
class KeysIn(BaseModel):
    access_key: str
    secret_key: str
    password: str


@router.get("/settings/keys")
async def get_keys(svc: Services = Depends(get_svc)):
    from_env = bool(svc.settings.upbit_access_key and svc.settings.upbit_secret_key)
    return {"access_key": mask(svc.client.access_key), "configured": svc.client.has_keys, "from_env": from_env,
            "trading_mode_env": svc.settings.trading_mode}


@router.put("/settings/keys")
async def put_keys(body: KeysIn, request: Request, svc: Services = Depends(get_svc)):
    _confirm_password(request, svc, body.password)
    if svc.settings.upbit_access_key:
        raise HTTPException(400, ".env 에 키가 설정되어 있어 대시보드에서 변경할 수 없습니다")
    if svc.engine.mode == "live":
        raise HTTPException(400, "실거래 모드에서는 키를 바꿀 수 없습니다. 모의투자로 전환 후 변경하세요.")
    access, secret = body.access_key.strip(), body.secret_key.strip()
    old = (svc.client.access_key, svc.client.secret_key)
    svc.client.set_keys(access, secret)
    try:
        await svc.client.get_accounts()
    except UpbitAPIError as e:
        svc.client.set_keys(*old)
        raise HTTPException(400, f"키 검증 실패: {e}") from e
    async with svc.db.session() as s:
        await s.merge(AppState(key="upbit_keys", value={"access": svc.vault.encrypt(access), "secret": svc.vault.encrypt(secret)}))
        await s.commit()
    await svc.engine.log("warning", "업비트 API 키 변경")
    return {"ok": True, "access_key": mask(access)}
