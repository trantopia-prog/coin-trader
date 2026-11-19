from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.db import Database
from app.events import EventBus


def make_df(closes: list[float], spread: float = 0.01, start: int = 1_700_000_000, step: int = 86400) -> pd.DataFrame:
    """종가 목록으로 OHLCV DataFrame 생성 (시가=직전 종가, 고/저=±spread)."""
    closes = [float(c) for c in closes]
    opens = [closes[0]] + closes[:-1]
    highs = [max(o, c) * (1 + spread) for o, c in zip(opens, closes)]
    lows = [min(o, c) * (1 - spread) for o, c in zip(opens, closes)]
    n = len(closes)
    return pd.DataFrame({
        "time": [start + i * step for i in range(n)],
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.ones(n), "value": closes,
    })


@pytest.fixture
async def db(tmp_path):
    d = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await d.create_all()
    yield d
    await d.dispose()


@pytest.fixture
def bus():
    return EventBus()
