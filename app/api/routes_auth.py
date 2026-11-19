from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.security import check_password, record_failure, throttle_login
from app.services import Services, get_svc

router = APIRouter(prefix="/api")


class LoginIn(BaseModel):
    password: str


@router.post("/login")
async def login(body: LoginIn, request: Request, svc: Services = Depends(get_svc)):
    ip = request.client.host if request.client else "?"
    throttle_login(ip)
    if not check_password(svc.settings, body.password):
        record_failure(ip)
        raise HTTPException(401, "비밀번호가 올바르지 않습니다")
    request.session["auth"] = True
    return {"ok": True, "default_password": svc.settings.admin_password == "change-me"}


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/me")
async def me(request: Request, svc: Services = Depends(get_svc)):
    return {"auth": bool(request.session.get("auth")), "default_password": svc.settings.admin_password == "change-me"}
