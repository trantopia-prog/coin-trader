"""텔레그램 알림.

이벤트 버스(log/news)를 구독해 체결·오류·거래 정지·거래소 공지를 텔레그램으로 보내고,
매일 지정 시각(KST)에 계좌 요약을 보낸다.

- 설정은 AppState["telegram"] 에 저장 (봇 토큰은 KeyVault 로 암호화). .env 의 TELEGRAM_BOT_TOKEN/CHAT_ID 가 있으면 우선.
- 전송은 큐 + 단일 워커: 텔레그램 제한(채팅당 초당 1건)을 지키고 429 의 retry_after 만큼 기다린다.
- 같은 내용은 5분 안에 한 번만 보낸다 (체결/거래정지는 항상 보냄).
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import func, select

from app.db import Database
from app.events import EventBus
from app.models import AppState, Order, StrategyConfig
from app.security import KeyVault

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
KST = timezone(timedelta(hours=9))
EVENTS = {
    "trade": "체결",
    "error": "오류",
    "risk": "거래 정지/재개",
    "notice": "유의·거래지원종료 공지",
    "daily": "매일 계좌 요약",
}
ALWAYS_SEND = ("trade", "risk")


class TelegramError(Exception):
    pass


@dataclass
class TelegramConfig:
    enabled: bool = False
    token: str = ""
    chat_id: str = ""
    events: dict[str, bool] = field(default_factory=lambda: {k: True for k in EVENTS})
    daily_hour: int = 9
    from_env: bool = False

    @property
    def ready(self) -> bool:
        return self.enabled and bool(self.token and self.chat_id)


class TelegramClient:
    def __init__(self, http: httpx.AsyncClient | None = None):
        self._http = http or httpx.AsyncClient(timeout=15.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def call(self, token: str, method: str, payload: dict | None = None, retries: int = 3) -> dict | list:
        for attempt in range(retries + 1):
            try:
                r = await self._http.post(API.format(token=token, method=method), json=payload or {})
            except httpx.TransportError as e:
                if attempt >= retries:
                    raise TelegramError(f"네트워크 오류: {e}") from e
                await asyncio.sleep(2**attempt)
                continue
            try:
                data = r.json()
            except ValueError as e:
                raise TelegramError(f"잘못된 응답 ({r.status_code})") from e
            if data.get("ok"):
                return data["result"]
            if r.status_code == 429 and attempt < retries:
                await asyncio.sleep(float((data.get("parameters") or {}).get("retry_after", 1)) + 0.5)
                continue
            desc = data.get("description", f"HTTP {r.status_code}")
            if r.status_code == 401:
                desc = "봇 토큰이 올바르지 않습니다"
            elif r.status_code in (400, 403) and "chat" in desc.lower():
                desc = f"채팅 ID 를 확인하세요 ({desc}). 봇에게 먼저 /start 를 보내야 합니다."
            raise TelegramError(desc)
        raise TelegramError("전송 제한 초과")

    async def send(self, token: str, chat_id: str, text: str) -> None:
        await self.call(token, "sendMessage", {
            "chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True,
        })

    async def bot_name(self, token: str) -> str:
        me = await self.call(token, "getMe", retries=0)
        return me.get("username", "")

    async def find_chats(self, token: str) -> list[dict]:
        """봇에게 최근 메시지를 보낸 채팅 목록. 사용자가 봇에게 /start 를 보낸 뒤 호출."""
        updates = await self.call(token, "getUpdates", {"limit": 50, "timeout": 0}, retries=0)
        chats: dict[str, dict] = {}
        for u in updates:
            msg = u.get("message") or u.get("channel_post") or u.get("my_chat_member") or {}
            chat = msg.get("chat")
            if chat:
                name = chat.get("title") or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")])) or chat.get("username", "")
                chats[str(chat["id"])] = {"chat_id": str(chat["id"]), "name": name, "type": chat.get("type", "")}
        return list(chats.values())


class TelegramNotifier:
    def __init__(self, db: Database, bus: EventBus, vault: KeyVault, engine, settings, client: TelegramClient | None = None):
        self.db = db
        self.bus = bus
        self.vault = vault
        self.engine = engine
        self.settings = settings
        self.client = client or TelegramClient()
        self.config = TelegramConfig()
        self.last_error = ""
        self.sent_count = 0
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        self._recent: dict[str, float] = {}
        self._tasks: list[asyncio.Task] = []
        self._last_daily: str = ""

    # ------------------------------------------------------------------ 설정
    async def load(self) -> None:
        cfg = TelegramConfig()
        async with self.db.session() as s:
            st = await s.get(AppState, "telegram")
        if st and isinstance(st.value, dict):
            v = st.value
            cfg.enabled = bool(v.get("enabled"))
            cfg.token = (self.vault.decrypt(v["token"]) or "") if v.get("token") else ""
            cfg.chat_id = str(v.get("chat_id", ""))
            cfg.events.update({k: bool(x) for k, x in (v.get("events") or {}).items() if k in EVENTS})
            cfg.daily_hour = int(v.get("daily_hour", 9))
            self._last_daily = v.get("last_daily", "")
        if self.settings.telegram_bot_token:
            cfg.token, cfg.from_env = self.settings.telegram_bot_token, True
            cfg.chat_id = self.settings.telegram_chat_id or cfg.chat_id
            if not (st and isinstance(st.value, dict) and "enabled" in st.value):
                cfg.enabled = True
        self.config = cfg

    async def save(self, *, enabled: bool, chat_id: str, events: dict[str, bool], daily_hour: int, token: str | None) -> None:
        if not 0 <= daily_hour <= 23:
            raise ValueError("요약 시각은 0~23 시")
        if token is not None and not self.config.from_env:
            self.config.token = token.strip()
        self.config.enabled = enabled
        self.config.chat_id = chat_id.strip()
        self.config.events.update({k: bool(v) for k, v in events.items() if k in EVENTS})
        self.config.daily_hour = daily_hour
        await self._persist()

    async def _persist(self) -> None:
        c = self.config
        value = {
            "enabled": c.enabled,
            "token": self.vault.encrypt(c.token) if c.token and not c.from_env else "",
            "chat_id": c.chat_id,
            "events": c.events,
            "daily_hour": c.daily_hour,
            "last_daily": self._last_daily,
        }
        async with self.db.session() as s:
            await s.merge(AppState(key="telegram", value=value))
            await s.commit()

    def public_config(self) -> dict:
        c = self.config
        return {
            "enabled": c.enabled,
            "configured": bool(c.token),
            "token_masked": (c.token.split(":")[0] + ":••••") if c.token else "",
            "chat_id": c.chat_id,
            "events": c.events,
            "event_labels": EVENTS,
            "daily_hour": c.daily_hour,
            "from_env": c.from_env,
            "last_error": self.last_error,
            "sent_count": self.sent_count,
        }

    # ------------------------------------------------------------------ 수명
    async def start(self) -> None:
        await self.load()
        self._tasks = [
            asyncio.create_task(self._sender(), name="telegram:sender"),
            asyncio.create_task(self._listener(), name="telegram:listener"),
            asyncio.create_task(self._daily_loop(), name="telegram:daily"),
        ]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.client.aclose()

    # ------------------------------------------------------------------ 메시지 생성
    def _prefix(self) -> str:
        return "🔴 <b>[실거래]</b>" if self.engine.mode == "live" else "🧪 <b>[모의]</b>"

    def format_event(self, topic: str, d: dict) -> tuple[str, str] | None:
        """(이벤트 종류, 메시지) 또는 보낼 필요 없으면 None."""
        if topic == "log":
            level = d.get("level")
            msg = html.escape(d.get("message", ""))
            if level == "trade":
                icon = "🟢" if "매수" in msg else "🔵"
                return "trade", f"{self._prefix()} {icon} <b>체결</b>\n{msg}"
            if level == "error":
                return "error", f"{self._prefix()} ⚠️ <b>오류</b>\n{msg}"
            if level == "risk":
                return "risk", f"{self._prefix()} ⛔ <b>리스크</b>\n{msg}"
            return None
        if topic == "news" and d.get("importance") in ("warning", "delisting"):
            coins = ", ".join(d.get("coins") or [])
            kind = "거래지원 종료" if d["importance"] == "delisting" else "유의 종목"
            return "notice", (
                f"📢 <b>업비트 공지: {kind}</b>\n{html.escape(d.get('title', ''))}\n"
                f"{('해당 코인(' + html.escape(coins) + ') 신규 매수가 자동 차단됩니다.') if coins else ''}\n"
                f"<a href=\"{html.escape(d.get('url', ''), quote=True)}\">공지 보기</a>"
            )
        return None

    async def daily_summary(self) -> str:
        eng = self.engine
        since = datetime.now(timezone.utc) - timedelta(days=1)
        async with self.db.session() as s:
            trades = await s.scalar(
                select(func.count()).select_from(Order).where(
                    Order.mode == eng.mode, Order.created_at >= since, Order.executed_volume > 0
                )
            )
            running = await s.scalar(select(func.count()).select_from(StrategyConfig).where(StrategyConfig.enabled.is_(True)))
        equity = eng.last_equity or await eng.equity()
        pnl = eng.daily_pnl_pct
        lines = [
            f"{self._prefix()} 📊 <b>일일 계좌 요약</b> ({datetime.now(KST):%m/%d %H:%M})",
            f"총 자산: <b>{equity:,.0f}원</b>",
            f"오늘 손익률: {pnl:+.2f}%" if pnl is not None else "오늘 손익률: -",
            f"최근 24시간 체결: {trades or 0}건",
            f"실행 중 전략: {running or 0}개",
        ]
        if eng.risk.halted:
            lines.append(f"⛔ 거래 정지 중: {html.escape(eng.risk.halt_reason)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 전송
    def enqueue(self, kind: str, text: str) -> bool:
        c = self.config
        if not c.ready or not c.events.get(kind, False):
            return False
        now = time.monotonic()
        if kind not in ALWAYS_SEND:
            key = kind + text[:120]
            if now - self._recent.get(key, -1e9) < 300:
                return False
            self._recent[key] = now
            if len(self._recent) > 500:
                self._recent = {k: t for k, t in self._recent.items() if now - t < 300}
        try:
            self._queue.put_nowait(text)
            return True
        except asyncio.QueueFull:
            log.warning("텔레그램 전송 대기열이 가득 차 알림을 버립니다")
            return False

    async def send_now(self, text: str) -> None:
        """설정 화면의 '테스트 메시지' 처럼 즉시 보내고 오류를 그대로 올린다."""
        c = self.config
        if not (c.token and c.chat_id):
            raise TelegramError("봇 토큰과 채팅 ID 를 먼저 입력하세요")
        await self.client.send(c.token, c.chat_id, text)
        self.sent_count += 1
        self.last_error = ""

    async def _sender(self) -> None:
        while True:
            text = await self._queue.get()
            c = self.config
            if not c.ready:
                continue
            try:
                await self.client.send(c.token, c.chat_id, text)
                self.sent_count += 1
                self.last_error = ""
            except TelegramError as e:
                self.last_error = str(e)
                log.warning("텔레그램 전송 실패: %s", e)
            await asyncio.sleep(1.05)  # 채팅당 초당 1건 제한

    async def _listener(self) -> None:
        q = self.bus.subscribe("log", "news")
        try:
            while True:
                topic, d = await q.get()
                ev = self.format_event(topic, d)
                if ev:
                    self.enqueue(*ev)
        finally:
            self.bus.unsubscribe(q)

    async def _daily_loop(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                now = datetime.now(KST)
                today = now.date().isoformat()
                c = self.config
                if c.ready and c.events.get("daily") and now.hour >= c.daily_hour and self._last_daily != today:
                    self._last_daily = today
                    await self._persist()
                    self.enqueue("daily", await self.daily_summary())
            except Exception as e:  # noqa: BLE001
                log.warning("일일 요약 실패: %s", e)
            await asyncio.sleep(60)
