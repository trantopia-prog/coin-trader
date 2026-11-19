"""대시보드 로그인(단일 관리자 비밀번호 + 세션 쿠키)과 API 키 암호화 저장."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Request, status

_FAILED: dict[str, list[float]] = {}


def check_password(settings, password: str) -> bool:
    return hmac.compare_digest(password.encode(), settings.admin_password.encode())


def throttle_login(ip: str) -> None:
    """IP 당 5분에 10회까지 로그인 시도 허용 (무차별 대입 방지)."""
    now = time.time()
    attempts = [t for t in _FAILED.get(ip, []) if now - t < 300]
    _FAILED[ip] = attempts
    if len(attempts) >= 10:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "로그인 시도가 너무 많습니다. 잠시 후 다시 시도하세요.")


def record_failure(ip: str) -> None:
    _FAILED.setdefault(ip, []).append(time.time())


def require_login(request: Request) -> None:
    if not request.session.get("auth"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "로그인이 필요합니다")


class KeyVault:
    def __init__(self, secret: str):
        key = base64.urlsafe_b64encode(hashlib.sha256(("upbit-keys:" + secret).encode()).digest())
        self._f = Fernet(key)

    def encrypt(self, plain: str) -> str:
        return self._f.encrypt(plain.encode()).decode()

    def decrypt(self, token: str) -> str | None:
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken:
            return None


def mask(key: str) -> str:
    return key[:4] + "…" + key[-4:] if len(key) > 10 else ("설정됨" if key else "")
