"""브라우저용 WebSocket. 실시간 시세/호가/체결/주문/로그/뉴스를 push 한다.

클라이언트 → 서버: {"market": "KRW-BTC"}  (호가/체결은 선택한 종목만 전송)
서버 → 클라이언트: {"topic": "...", "data": {...}}
"""

from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()

TOPICS = ("ticker", "orderbook", "trade", "order", "log", "news", "engine", "system")


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not ws.session.get("auth"):
        await ws.close(code=4401)
        return
    await ws.accept()
    svc = ws.app.state.svc
    q = svc.bus.subscribe(*TOPICS, maxsize=2000)
    selected = {"market": None}
    last_ticker_sent: dict[str, float] = {}

    async def reader():
        while True:
            msg = json.loads(await ws.receive_text())
            if isinstance(msg, dict) and msg.get("market"):
                selected["market"] = str(msg["market"]).upper()
                svc.ws.add_codes(selected["market"])

    async def writer():
        while True:
            topic, data = await q.get()
            if topic in ("orderbook", "trade") and data.get("code") != selected["market"]:
                continue
            if topic == "ticker" and data.get("code") != selected["market"]:
                # 목록용 시세는 종목당 초당 1회로 제한
                now = time.monotonic()
                if now - last_ticker_sent.get(data["code"], 0) < 1.0:
                    continue
                last_ticker_sent[data["code"]] = now
            await ws.send_text(json.dumps({"topic": topic, "data": data}, ensure_ascii=False, default=str))

    tasks = [asyncio.create_task(reader()), asyncio.create_task(writer())]
    try:
        await ws.send_text(json.dumps({"topic": "engine", "data": await svc.engine.status()}, default=str))
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for t in done:
            t.result()
    except (WebSocketDisconnect, RuntimeError, ValueError):
        pass
    finally:
        for t in tasks:
            t.cancel()
        svc.bus.unsubscribe(q)
