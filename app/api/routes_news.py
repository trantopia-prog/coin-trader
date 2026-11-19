from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends

from app.security import require_login
from app.services import Services, get_svc

router = APIRouter(prefix="/api/news", dependencies=[Depends(require_login)])


@router.get("")
async def list_news(
    coin: str | None = None,
    source: str | None = None,
    category: str | None = None,
    q: str | None = None,
    important: bool = False,
    limit: int = 50,
    before_id: int | None = None,
    svc: Services = Depends(get_svc),
):
    return await svc.news.query(coin, source, category, q, important, limit, before_id)


@router.get("/status")
async def status(svc: Services = Depends(get_svc)):
    return {"collectors": svc.news.status, "flagged": svc.risk.flagged}


@router.post("/refresh")
async def refresh(svc: Services = Depends(get_svc)):
    counts = await asyncio.gather(*(svc.news.run_once(c) for c in svc.news.collectors))
    return {c.name: n for c, n in zip(svc.news.collectors, counts)}
