"""업비트 Open API 인증 토큰 생성.

https://docs.upbit.com/kr/reference/auth
- 파라미터가 있는 요청은 쿼리스트링(또는 JSON body 를 같은 방식으로 직렬화한 문자열)의 SHA512 해시를 query_hash 로 넣는다.
- 쿼리스트링은 URL 디코딩된 상태(unquote)로 해시한다. 배열 파라미터는 key[]=a&key[]=b 형태.
- 실제 요청의 파라미터 순서와 해시 문자열의 순서가 같아야 한다 (dict 삽입 순서를 그대로 유지).
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Mapping
from urllib.parse import unquote, urlencode

import jwt


def build_query_string(params: Mapping[str, Any]) -> str:
    return unquote(urlencode(params, doseq=True))


def query_hash(params: Mapping[str, Any]) -> str:
    return hashlib.sha512(build_query_string(params).encode("utf-8")).hexdigest()


def make_token(access_key: str, secret_key: str, params: Mapping[str, Any] | None = None) -> str:
    payload: dict[str, Any] = {"access_key": access_key, "nonce": str(uuid.uuid4())}
    if params:
        payload["query_hash"] = query_hash(params)
        payload["query_hash_alg"] = "SHA512"
    return jwt.encode(payload, secret_key, algorithm="HS512")  # 업비트 문서 권장 알고리즘


def auth_header(access_key: str, secret_key: str, params: Mapping[str, Any] | None = None) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(access_key, secret_key, params)}"}
