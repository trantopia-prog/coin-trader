from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) CoinTrader/0.1"


@dataclass
class NewsDraft:
    source: str
    title: str
    url: str
    published_at: datetime
    coins: list[str] = field(default_factory=list)
    summary: str = ""
    category: str = "news"  # news | notice | disclosure
    importance: str = "normal"  # normal | listing | warning | delisting
    sentiment: float | None = None

    @property
    def url_hash(self) -> str:
        return hashlib.sha256(self.url.encode()).hexdigest()


@dataclass
class CollectContext:
    http: httpx.AsyncClient
    symbol_names: dict[str, list[str]]  # BTC -> [BTC, 비트코인, Bitcoin]
    watch_symbols: list[str]


class NewsCollector(ABC):
    name: str = "base"
    interval_sec: int = 300

    @abstractmethod
    async def fetch(self, ctx: CollectContext) -> list[NewsDraft]: ...


_PAREN_SYMBOL = re.compile(r"\(([A-Z0-9]{2,12})\)")


def extract_coins(text: str, symbol_names: dict[str, list[str]]) -> list[str]:
    """제목/본문에서 언급된 코인 심볼을 추출.

    - "엑스알피(XRP)" 같은 '이름(심볼)' 괄호 표기가 있으면 그것만 사용한다. 거래소 공지는 항상 이 형식이고,
      본문의 "(BTC, USDT 마켓)" 같은 마켓 표기를 코인으로 오인하면 엉뚱한 코인이 매수 차단될 수 있다.
      원화마켓에 없는 신규 상장 코인도 괄호 표기면 그대로 인정한다.
    - 괄호 표기가 없으면 대문자 심볼(단어 경계), 한글명(2글자 이상), 영문명(4글자 이상)으로 매칭
    """
    found: list[str] = []
    for sym in _PAREN_SYMBOL.findall(text):
        if sym not in found:
            found.append(sym)
    if found:
        return found
    lower = text.lower()
    for sym, names in symbol_names.items():
        if sym in found:
            continue
        if len(sym) >= 3 and re.search(rf"(?<![A-Za-z0-9]){re.escape(sym)}(?![A-Za-z0-9])", text):
            found.append(sym)
            continue
        kr = names[1] if len(names) > 1 else ""
        en = names[2] if len(names) > 2 else ""
        if len(kr) >= 2 and kr in text:
            found.append(sym)
        elif len(en) >= 4 and re.search(rf"\b{re.escape(en.lower())}\b", lower):
            found.append(sym)
    return found


def classify_notice(title: str) -> str:
    t = title.replace(" ", "")
    if "거래지원종료" in t or "상장폐지" in t:
        return "delisting"
    if ("유의종목" in t or "투자유의" in t) and "해제" in t:
        return "release"
    if "유의종목" in t or "투자유의" in t or "유의촉구" in t or "주의종목" in t:
        return "warning"
    if "신규거래지원" in t or "디지털자산추가" in t or "마켓추가" in t:
        return "listing"
    if "입출금" in t and "중단" in t:
        return "suspend"
    return "normal"


def parse_dt(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
