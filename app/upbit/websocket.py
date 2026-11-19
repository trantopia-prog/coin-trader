"""업비트 실시간 시세 WebSocket.

wss://api.upbit.com/websocket/v1 에 [{"ticket"}, {"type", "codes"}, {"format"}] 형식으로 구독한다.
응답은 바이너리(JSON bytes)로 온다. 연결이 끊기면 지수 백오프로 재연결하고, 구독 종목이 바뀌면 재연결해서 다시 구독한다.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

try:  # websockets >= 13
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # pragma: no cover
    from websockets import connect as ws_connect  # type: ignore

from app.events import EventBus

log = logging.getLogger(__name__)

WS_URL = "wss://api.upbit.com/websocket/v1"


class UpbitWebSocket:
    def __init__(self, bus: EventBus, codes: list[str], types: tuple[str, ...] = ("ticker", "orderbook", "trade")):
        self.bus = bus
        self.codes: set[str] = set(codes)
        self.types = types
        self.connected = False
        self._reconnect = asyncio.Event()
        self._stop = False

    def set_codes(self, codes: set[str] | list[str]) -> None:
        new = {c.upper() for c in codes}
        if new != self.codes:
            self.codes = new
            self._reconnect.set()

    def add_codes(self, *codes: str) -> None:
        self.set_codes(self.codes | {c.upper() for c in codes})

    def _subscription(self) -> str:
        msg: list[dict] = [{"ticket": str(uuid.uuid4())}]
        codes = sorted(self.codes)
        for t in self.types:
            msg.append({"type": t, "codes": codes})
        msg.append({"format": "DEFAULT"})
        return json.dumps(msg)

    async def run(self) -> None:
        backoff = 1.0
        while not self._stop:
            if not self.codes:
                await asyncio.sleep(1)
                continue
            try:
                async with ws_connect(WS_URL, ping_interval=30, ping_timeout=20, max_size=2**22) as ws:
                    await ws.send(self._subscription())
                    self.connected = True
                    self._reconnect.clear()
                    backoff = 1.0
                    log.info("업비트 WebSocket 연결: %d개 종목", len(self.codes))
                    self.bus.publish("system", {"event": "ws_connected", "codes": sorted(self.codes)})
                    while not self._reconnect.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 어떤 오류든 재연결
                log.warning("업비트 WebSocket 오류: %s (%.0f초 후 재연결)", e, backoff)
                self.bus.publish("system", {"event": "ws_disconnected", "error": str(e)})
                self.connected = False
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                self.connected = False

    def _dispatch(self, raw: bytes | str) -> None:
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return
        typ = data.get("type")
        if typ in ("ticker", "orderbook", "trade"):
            self.bus.publish(typ, data)

    def stop(self) -> None:
        self._stop = True
        self._reconnect.set()
