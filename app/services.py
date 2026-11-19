"""앱 전역 서비스 컨테이너. main.py 의 lifespan 에서 만들어 app.state.svc 에 둔다."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.config import Settings
from app.db import Database
from app.events import EventBus
from app.market.candles import CandleService
from app.market.screener import Screener
from app.news.service import NewsService
from app.notify.telegram import TelegramNotifier
from app.security import KeyVault
from app.trading.engine import TradingEngine
from app.trading.risk import RiskManager
from app.upbit.rest import UpbitClient
from app.upbit.websocket import UpbitWebSocket


@dataclass
class Services:
    settings: Settings
    db: Database
    bus: EventBus
    client: UpbitClient
    candles: CandleService
    screener: Screener
    ws: UpbitWebSocket
    risk: RiskManager
    engine: TradingEngine
    news: NewsService
    vault: KeyVault
    telegram: TelegramNotifier


def get_svc(request: Request) -> Services:
    return request.app.state.svc
