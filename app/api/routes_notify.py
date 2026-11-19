from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.notify.telegram import TelegramError
from app.security import require_login
from app.services import Services, get_svc

router = APIRouter(prefix="/api/telegram", dependencies=[Depends(require_login)])


class TelegramIn(BaseModel):
    enabled: bool
    chat_id: str = ""
    token: str | None = None  # None 이면 기존 토큰 유지
    events: dict[str, bool] = {}
    daily_hour: int = Field(9, ge=0, le=23)


class TokenIn(BaseModel):
    token: str | None = None


@router.get("")
async def get_config(svc: Services = Depends(get_svc)):
    return svc.telegram.public_config()


@router.put("")
async def put_config(body: TelegramIn, svc: Services = Depends(get_svc)):
    token = body.token.strip() if body.token else None
    if token:
        try:
            await svc.telegram.client.bot_name(token)
        except TelegramError as e:
            raise HTTPException(400, f"봇 토큰 확인 실패: {e}") from e
    if body.enabled and not ((token or svc.telegram.config.token) and body.chat_id.strip()):
        raise HTTPException(400, "알림을 켜려면 봇 토큰과 채팅 ID 가 필요합니다")
    try:
        await svc.telegram.save(enabled=body.enabled, chat_id=body.chat_id, events=body.events,
                                daily_hour=body.daily_hour, token=token)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    await svc.engine.log("info", f"텔레그램 알림 설정 변경 ({'켜짐' if body.enabled else '꺼짐'})")
    return svc.telegram.public_config()


@router.post("/detect")
async def detect_chat(body: TokenIn, svc: Services = Depends(get_svc)):
    """봇에게 /start 를 보낸 채팅을 찾아 채팅 ID 후보를 돌려준다."""
    token = (body.token or "").strip() or svc.telegram.config.token
    if not token:
        raise HTTPException(400, "봇 토큰을 먼저 입력하세요")
    try:
        name = await svc.telegram.client.bot_name(token)
        chats = await svc.telegram.client.find_chats(token)
    except TelegramError as e:
        raise HTTPException(400, str(e)) from e
    return {"bot": name, "chats": chats}


@router.post("/test")
async def test_message(svc: Services = Depends(get_svc)):
    try:
        await svc.telegram.send_now(
            f"{svc.telegram._prefix()} ✅ <b>CoinTrader 테스트 메시지</b>\n알림이 정상적으로 연결되었습니다."
        )
    except TelegramError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@router.post("/daily-preview")
async def daily_preview(svc: Services = Depends(get_svc)):
    """일일 요약을 지금 한 번 보내 본다."""
    try:
        await svc.telegram.send_now(await svc.telegram.daily_summary())
    except TelegramError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}
