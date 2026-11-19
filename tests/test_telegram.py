from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
import respx

from app.config import Settings
from app.models import AppState
from app.notify.telegram import TelegramClient, TelegramError, TelegramNotifier
from app.security import KeyVault

TOKEN = "123456:ABCDEF"
SEND = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


def fake_engine(mode="paper"):
    async def equity():
        return 1_000_000.0

    return SimpleNamespace(mode=mode, last_equity=1_234_567.0, daily_pnl_pct=-1.5,
                           risk=SimpleNamespace(halted=False, halt_reason=""), equity=equity)


@pytest.fixture
async def notifier(db, bus):
    n = TelegramNotifier(db, bus, KeyVault("test-secret"), fake_engine(), Settings())
    await n.load()
    yield n
    await n.client.aclose()


async def enable(n: TelegramNotifier):
    await n.save(enabled=True, chat_id="42", events={}, daily_hour=9, token=TOKEN)


# ------------------------------------------------------------------ 메시지 형식
def test_format_trade_and_escape(notifier):
    kind, text = notifier.format_event("log", {"level": "trade", "message": "KRW-BTC 매수 체결 <b>&"})
    assert kind == "trade" and "🟢" in text and "[모의]" in text
    assert "&lt;b&gt;&amp;" in text  # HTML 주입 방지
    assert notifier.format_event("log", {"level": "info", "message": "x"}) is None
    kind, text = notifier.format_event("news", {"importance": "delisting", "title": "카바(KAVA) 거래지원 종료", "coins": ["KAVA"], "url": "https://u/x"})
    assert kind == "notice" and "거래지원 종료" in text and "KAVA" in text
    assert notifier.format_event("news", {"importance": "normal", "title": "t"}) is None


def test_live_prefix(notifier):
    notifier.engine.mode = "live"
    assert "[실거래]" in notifier.format_event("log", {"level": "risk", "message": "정지"})[1]


# ------------------------------------------------------------------ 필터/중복
async def test_enqueue_respects_switches_and_dedupe(notifier):
    assert not notifier.enqueue("error", "x")  # 꺼진 상태
    await enable(notifier)
    assert notifier.enqueue("error", "같은 오류")
    assert not notifier.enqueue("error", "같은 오류")  # 5분 내 중복
    assert notifier.enqueue("trade", "체결1") and notifier.enqueue("trade", "체결1")  # 체결은 항상
    notifier.config.events["notice"] = False
    assert not notifier.enqueue("notice", "공지")


# ------------------------------------------------------------------ 설정 저장
async def test_token_encrypted_at_rest_and_reloaded(notifier, db, bus):
    await enable(notifier)
    async with db.session() as s:
        stored = (await s.get(AppState, "telegram")).value
    assert TOKEN not in json.dumps(stored) and stored["chat_id"] == "42"
    other = TelegramNotifier(db, bus, KeyVault("test-secret"), fake_engine(), Settings())
    await other.load()
    assert other.config.token == TOKEN and other.config.ready
    pub = other.public_config()
    assert TOKEN not in json.dumps(pub) and pub["token_masked"] == "123456:••••"
    await other.client.aclose()


async def test_env_token_takes_precedence(db, bus):
    n = TelegramNotifier(db, bus, KeyVault("s"), fake_engine(), Settings(telegram_bot_token=TOKEN, telegram_chat_id="7"))
    await n.load()
    assert n.config.ready and n.config.from_env and n.config.chat_id == "7"
    await n.client.aclose()


# ------------------------------------------------------------------ API 클라이언트
@respx.mock
async def test_client_retries_429_then_succeeds(monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    route = respx.post(SEND).mock(side_effect=[
        httpx.Response(429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 3}}),
        httpx.Response(200, json={"ok": True, "result": {}}),
    ])
    c = TelegramClient()
    await c.send(TOKEN, "42", "hi")
    assert route.call_count == 2
    body = json.loads(route.calls[1].request.content)
    assert body["chat_id"] == "42" and body["parse_mode"] == "HTML"
    await c.aclose()


@respx.mock
async def test_client_bad_token_message():
    respx.post(f"https://api.telegram.org/bot{TOKEN}/getMe").mock(
        return_value=httpx.Response(401, json={"ok": False, "description": "Unauthorized"}))
    c = TelegramClient()
    with pytest.raises(TelegramError, match="봇 토큰"):
        await c.bot_name(TOKEN)
    await c.aclose()


@respx.mock
async def test_find_chats():
    respx.post(f"https://api.telegram.org/bot{TOKEN}/getUpdates").mock(return_value=httpx.Response(200, json={"ok": True, "result": [
        {"update_id": 1, "message": {"chat": {"id": 42, "first_name": "홍", "last_name": "길동", "type": "private"}, "text": "/start"}},
        {"update_id": 2, "message": {"chat": {"id": 42, "first_name": "홍", "type": "private"}, "text": "again"}},
        {"update_id": 3, "channel_post": {"chat": {"id": -100, "title": "알림채널", "type": "channel"}}},
    ]}))
    c = TelegramClient()
    chats = await c.find_chats(TOKEN)
    assert {x["chat_id"] for x in chats} == {"42", "-100"}
    assert any(x["name"] == "알림채널" for x in chats)
    await c.aclose()


# ------------------------------------------------------------------ 전체 흐름: 버스 이벤트 → 전송
@respx.mock
async def test_bus_event_is_delivered(notifier, bus):
    route = respx.post(SEND).mock(return_value=httpx.Response(200, json={"ok": True, "result": {}}))
    await enable(notifier)
    tasks = [asyncio.create_task(notifier._sender()), asyncio.create_task(notifier._listener())]
    await asyncio.sleep(0.05)
    bus.publish("log", {"level": "trade", "message": "KRW-ETH 매수 체결 0.01 @ 3,000,000"})
    for _ in range(50):
        if route.called:
            break
        await asyncio.sleep(0.05)
    for t in tasks:
        t.cancel()
    assert route.called
    assert "KRW-ETH 매수 체결" in json.loads(route.calls[0].request.content)["text"]
    assert notifier.sent_count == 1


async def test_daily_summary_contents(notifier):
    text = await notifier.daily_summary()
    assert "1,234,567원" in text and "-1.50%" in text and "실행 중 전략: 0개" in text


async def _no_sleep(_s=0):
    return None
