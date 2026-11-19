from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    """SQLite 는 tz 정보를 버리므로 naive 값은 UTC 로 간주해 직렬화한다."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class AppState(Base):
    """키/값 런타임 상태 (긴급정지 여부, 실거래 활성화 여부, 암호화된 API 키 등)."""

    __tablename__ = "app_state"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict | list | str | float | bool | None] = mapped_column(JSON, nullable=True)


class Holding(Base):
    """모의투자 잔고. 실거래 잔고는 업비트 /accounts 에서 직접 조회한다."""

    __tablename__ = "holdings"
    __table_args__ = (UniqueConstraint("mode", "currency"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), default="paper")
    currency: Mapped[str] = mapped_column(String(16))
    balance: Mapped[float] = mapped_column(Float, default=0.0)
    locked: Mapped[float] = mapped_column(Float, default=0.0)
    avg_buy_price: Mapped[float] = mapped_column(Float, default=0.0)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    mode: Mapped[str] = mapped_column(String(8), index=True)
    strategy_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    market: Mapped[str] = mapped_column(String(32), index=True)
    side: Mapped[str] = mapped_column(String(4))  # bid | ask
    ord_type: Mapped[str] = mapped_column(String(8))  # limit | price | market
    price: Mapped[float | None] = mapped_column(Float, nullable=True)  # limit 가격 또는 price 타입의 총 주문금액
    volume: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String(12), default="wait")  # wait | done | cancel | rejected
    exchange_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    executed_volume: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    paid_fee: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(Text, default="")
    error: Mapped[str] = mapped_column(Text, default="")


class StrategyConfig(Base):
    __tablename__ = "strategies"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    name: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(32))
    market: Mapped[str] = mapped_column(String(32))
    interval: Mapped[str] = mapped_column(String(16))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    budget_krw: Mapped[float] = mapped_column(Float)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)


class StrategyRuntime(Base):
    """전략별 포지션/내부상태. 모의/실거래가 섞이지 않도록 mode 별로 분리 저장."""

    __tablename__ = "strategy_runtime"
    __table_args__ = (UniqueConstraint("strategy_id", "mode"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int] = mapped_column(Integer, index=True)
    mode: Mapped[str] = mapped_column(String(8))
    pos_volume: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    state: Mapped[dict] = mapped_column(JSON, default=dict)


class NewsItem(Base):
    __tablename__ = "news"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(Text)
    url: Mapped[str] = mapped_column(Text)
    url_hash: Mapped[str] = mapped_column(String(64), unique=True)
    coins: Mapped[str] = mapped_column(String(256), default="", index=True)  # ",BTC,ETH," 형태 (LIKE 검색용)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(16), default="news")  # news | notice | disclosure
    importance: Mapped[str] = mapped_column(String(16), default="normal")  # normal | listing | warning | delisting
    sentiment: Mapped[float | None] = mapped_column(Float, nullable=True)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    mode: Mapped[str] = mapped_column(String(8))
    equity: Mapped[float] = mapped_column(Float)


class EngineLog(Base):
    __tablename__ = "engine_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(8), default="info")
    message: Mapped[str] = mapped_column(Text)
    strategy_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
