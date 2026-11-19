from __future__ import annotations

import os
import secrets
import sys
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

FROZEN = getattr(sys, "frozen", False)  # PyInstaller 로 묶인 실행파일 여부


def _user_data_dir() -> Path:
    """설치판의 사용자 데이터 위치. 설치 폴더(Program Files)는 쓰기 불가이므로 분리한다."""
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "CoinTrader"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "coin-trader"


if FROZEN:
    BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))  # 번들된 리소스(web/) 위치
    DATA_DIR = Path(os.environ.get("COIN_TRADER_HOME") or _user_data_dir())
    ENV_FILE = DATA_DIR / ".env"
else:
    BASE_DIR = Path(__file__).resolve().parent.parent
    # COIN_TRADER_HOME 을 지정하면 데이터와 .env 를 모두 그 폴더에서 사용 (여러 인스턴스/테스트 격리)
    DATA_DIR = Path(os.environ.get("COIN_TRADER_HOME") or BASE_DIR / "data")
    ENV_FILE = DATA_DIR / ".env" if os.environ.get("COIN_TRADER_HOME") else BASE_DIR / ".env"
WEB_DIR = BASE_DIR / "web"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore")

    admin_password: str = "change-me"
    secret_key: str = ""

    upbit_access_key: str = ""
    upbit_secret_key: str = ""

    trading_mode: Literal["paper", "live"] = "paper"
    paper_initial_krw: float = 10_000_000

    risk_max_order_krw: float = 1_000_000
    risk_max_position_ratio: float = 0.3
    risk_daily_loss_limit_pct: float = 5.0

    watch_markets: str = "KRW-BTC,KRW-ETH,KRW-XRP,KRW-SOL"

    cryptopanic_token: str = ""
    news_interval_sec: int = 300
    disclosure_scrape_urls: str = ""

    engine_eval_sec: float = 5.0
    database_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8000

    @property
    def watch_market_list(self) -> list[str]:
        return [m.strip().upper() for m in self.watch_markets.split(",") if m.strip()]

    @property
    def db_url(self) -> str:
        return self.database_url or f"sqlite+aiosqlite:///{DATA_DIR / 'coin.db'}"

    def resolved_secret(self) -> str:
        """SECRET_KEY가 없으면 data/.secret 에 생성해 재시작 후에도 세션/암호화 키가 유지되게 한다."""
        if self.secret_key:
            return self.secret_key
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / ".secret"
        if not path.exists():
            path.write_text(secrets.token_urlsafe(48))
            path.chmod(0o600)
        return path.read_text().strip()


@lru_cache
def get_settings() -> Settings:
    return Settings()
