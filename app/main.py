"""앱 진입점. `uvicorn app.main:app` 또는 `python -m app`.

lifespan 에서 서비스들을 조립하고 백그라운드 task(업비트 WebSocket, 매매 엔진, 뉴스 수집기)를 시작/종료한다.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.api import routes_auth, routes_backtest, routes_market, routes_news, routes_trading, ws
from app.config import DATA_DIR, WEB_DIR, get_settings
from app.db import Database
from app.events import EventBus
from app.market.candles import CandleService
from app.market.screener import Screener
from app.models import AppState
from app.news.cryptopanic import CryptoPanicCollector
from app.news.disclosure import DisclosureCollector
from app.news.google_rss import GoogleNewsCollector
from app.news.service import NewsService
from app.news.upbit_notice import UpbitNoticeCollector
from app.security import KeyVault
from app.services import Services
from app.trading.broker import PaperBroker
from app.trading.engine import TradingEngine
from app.trading.risk import RiskManager
from app.upbit.rest import UpbitClient
from app.upbit.websocket import UpbitWebSocket

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


async def build_services() -> Services:
    settings = get_settings()
    db = Database(settings.db_url)
    await db.create_all()
    bus = EventBus()
    vault = KeyVault(settings.resolved_secret())

    access, secret = settings.upbit_access_key, settings.upbit_secret_key
    if not (access and secret):
        async with db.session() as s:
            st = await s.get(AppState, "upbit_keys")
        if st and isinstance(st.value, dict):
            access = vault.decrypt(st.value.get("access", "")) or ""
            secret = vault.decrypt(st.value.get("secret", "")) or ""

    client = UpbitClient(access, secret)
    candles = CandleService(client)
    screener = Screener(client, candles)
    upbit_ws = UpbitWebSocket(bus, settings.watch_market_list)
    risk = RiskManager(settings, db)
    await risk.load()
    paper = PaperBroker(db, bus, settings.paper_initial_krw)
    engine = TradingEngine(settings, db, bus, client, candles, risk, paper, upbit_ws)

    collectors = [UpbitNoticeCollector(), GoogleNewsCollector(),
                  DisclosureCollector([u.strip() for u in settings.disclosure_scrape_urls.split(",") if u.strip()])]
    if settings.cryptopanic_token:
        collectors.append(CryptoPanicCollector(settings.cryptopanic_token))
    for c in collectors:
        if c.name != "upbit_notice":
            c.interval_sec = max(c.interval_sec, settings.news_interval_sec)

    news = NewsService(
        db, bus, collectors,
        symbol_names=screener.symbol_names,
        watch_symbols=lambda: [m.split("-", 1)[1] for m in settings.watch_market_list],
        on_flag=risk.flag,
    )
    return Services(settings, db, bus, client, candles, screener, upbit_ws, risk, engine, news, vault)


@asynccontextmanager
async def lifespan(app: FastAPI):
    svc = await build_services()
    app.state.svc = svc
    if svc.settings.admin_password == "change-me":
        log.warning("ADMIN_PASSWORD 가 기본값입니다. .env 에서 반드시 변경하세요.")
    ws_task = asyncio.create_task(svc.ws.run(), name="upbit-ws")
    await svc.engine.start()
    await svc.news.start()
    log.info("시작 완료: 매매 모드=%s, 데이터 폴더=%s", svc.engine.mode, DATA_DIR)
    try:
        yield
    finally:
        await svc.news.stop()
        await svc.engine.stop()
        svc.ws.stop()
        ws_task.cancel()
        await asyncio.gather(ws_task, return_exceptions=True)
        await svc.client.aclose()
        await svc.db.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Coin Trader", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.add_middleware(
        SessionMiddleware, secret_key=settings.resolved_secret(), session_cookie="coin_session",
        max_age=60 * 60 * 24 * 7, same_site="strict", https_only=False,
    )
    for r in (routes_auth, routes_market, routes_news, routes_trading, routes_backtest, ws):
        app.include_router(r.router)

    @app.get("/", include_in_schema=False)
    async def index():
        return RedirectResponse("/index.html")

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


app = create_app()
