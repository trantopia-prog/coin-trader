from __future__ import annotations

import hashlib

import httpx
import jwt
import pytest
import respx

from app.market.candles import to_frame
from app.upbit.auth import build_query_string, make_token
from app.upbit.ratelimit import parse_remaining_req
from app.upbit.rest import BASE_URL, UpbitAPIError, UpbitClient, _fmt, candle_path


def test_query_string_array_params_not_url_encoded():
    qs = build_query_string({"market": "KRW-BTC", "states[]": ["wait", "watch"], "limit": 10})
    assert qs == "market=KRW-BTC&states[]=wait&states[]=watch&limit=10"


def test_token_contains_query_hash():
    params = {"market": "KRW-BTC", "side": "bid", "volume": "0.001", "price": "50000000", "ord_type": "limit"}
    token = make_token("access", "secret", params)
    payload = jwt.decode(token, "secret", algorithms=["HS512"])
    expected = hashlib.sha512(b"market=KRW-BTC&side=bid&volume=0.001&price=50000000&ord_type=limit").hexdigest()
    assert payload["access_key"] == "access"
    assert payload["query_hash"] == expected
    assert payload["query_hash_alg"] == "SHA512"
    assert jwt.get_unverified_header(token)["alg"] == "HS512"


def test_token_without_params_has_no_hash():
    payload = jwt.decode(make_token("a", "s"), "s", algorithms=["HS512"])
    assert "query_hash" not in payload and payload["nonce"]


def test_remaining_req_parse():
    assert parse_remaining_req("group=default; min=1800; sec=29") == ("default", 29)
    assert parse_remaining_req(None) is None


def test_number_format_no_exponent():
    assert _fmt(0.00000123) == "0.00000123"
    assert _fmt(5000.0) == "5000"


def test_candle_path():
    assert candle_path("minute15") == "/candles/minutes/15"
    assert candle_path("day") == "/candles/days"
    with pytest.raises(ValueError):
        candle_path("minute7")


def test_to_frame_sorts_ascending():
    raw = [
        {"candle_date_time_utc": "2024-01-02T00:00:00", "opening_price": 2, "high_price": 3, "low_price": 1, "trade_price": 2.5, "candle_acc_trade_volume": 10, "candle_acc_trade_price": 25},
        {"candle_date_time_utc": "2024-01-01T00:00:00", "opening_price": 1, "high_price": 2, "low_price": 0.5, "trade_price": 2, "candle_acc_trade_volume": 5, "candle_acc_trade_price": 10},
    ]
    df = to_frame(raw)
    assert list(df["close"]) == [2.0, 2.5]
    assert df["time"].iloc[0] == 1704067200


@respx.mock
async def test_private_request_sends_bearer_and_parses_error():
    route = respx.get(f"{BASE_URL}/accounts").mock(
        return_value=httpx.Response(401, json={"error": {"name": "invalid_access_key", "message": "잘못된 키"}})
    )
    c = UpbitClient("a", "s")
    with pytest.raises(UpbitAPIError) as e:
        await c.get_accounts()
    assert e.value.name == "invalid_access_key"
    assert route.calls[0].request.headers["Authorization"].startswith("Bearer ")
    await c.aclose()


async def test_private_request_without_keys_fails_fast():
    c = UpbitClient()
    with pytest.raises(UpbitAPIError) as e:
        await c.get_accounts()
    assert e.value.name == "no_api_key"
    await c.aclose()


@respx.mock
async def test_place_order_hash_matches_json_body():
    route = respx.post(f"{BASE_URL}/orders").mock(return_value=httpx.Response(201, json={"uuid": "u1", "state": "wait"}))
    c = UpbitClient("a", "s")
    await c.place_order("KRW-BTC", "bid", "price", price=10000)
    req = route.calls[0].request
    token = req.headers["Authorization"].removeprefix("Bearer ")
    payload = jwt.decode(token, "s", algorithms=["HS512"])
    assert payload["query_hash"] == hashlib.sha512(b"market=KRW-BTC&side=bid&ord_type=price&price=10000").hexdigest()
    await c.aclose()


@respx.mock
async def test_retries_on_429():
    route = respx.get(f"{BASE_URL}/ticker").mock(
        side_effect=[httpx.Response(429, json={}), httpx.Response(200, json=[{"market": "KRW-BTC", "trade_price": 1}])]
    )
    c = UpbitClient()
    rows = await c.get_tickers(["KRW-BTC"])
    assert rows[0]["trade_price"] == 1 and route.call_count == 2
    await c.aclose()


@pytest.mark.live_quote
async def test_live_quote_smoke():
    """실제 업비트 시세 API 호출: pytest -m live_quote"""
    c = UpbitClient()
    markets = await c.get_markets()
    assert any(m["market"] == "KRW-BTC" for m in markets)
    candles = await c.get_candles("KRW-BTC", "minute60", 5)
    assert len(candles) == 5
    await c.aclose()
