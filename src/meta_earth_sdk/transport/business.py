"""Generation-owned, non-retrying HTTP for admitted Hub business operations."""

from __future__ import annotations

import asyncio
import json as jsonlib
import math
import re
from collections.abc import Mapping
from typing import Any, NoReturn

import httpx

from ..config import _http_url
from ..exceptions import ClosedError, NetworkError, ProtocolError, ValidationError
from ..exceptions import TimeoutError as SDKTimeout

_MAX_REQUEST = 8 << 20
_MAX_RESPONSE = 4 << 20
_ADDRESS = r"me1[023456789acdefghjklmnpqrstuvwxyz]{38}"
_GET = tuple(re.compile(pattern + r"\Z") for pattern in (
    r"/status", r"/genesis", r"/cosmos/auth/v1beta1/accounts/" + _ADDRESS,
    r"/cosmos/tx/v1beta1/txs/[0-9A-Fa-f]{64}",
    r"/api/v1/transactions/" + _ADDRESS, r"/api/v1/network/fees",
    r"/metaearth/did/(?:did|didInfo)",
    r"/metaearth/kyc/QuerySubAccountDidResponse",
    r"/metaearth/wstaking/fixed_deposit_cfg",
    r"/metaearth/wstaking/fixed_deposit_by_acct/" + _ADDRESS + r"/(?:ALL_STATE|NOT_EXPIRED|EXPIRED|[012])",
    r"/metaearth/wstaking/(?:delegation|delegation-rewards)/" + _ADDRESS,
    r"/cosmos/gov/v1/proposals(?:/[0-9]+(?:/(?:votes|deposits|tally))?)?",
    r"/cosmwasm/wasm/v1/code(?:/[0-9]+(?:/contracts)?)?",
    r"/cosmwasm/wasm/v1/contracts/creator/" + _ADDRESS,
    r"/cosmwasm/wasm/v1/contract/" + _ADDRESS + r"(?:/smart/(?:[A-Za-z0-9_+=-]|%2[fFbB]|%3[dD]){1,32768})?",
))
_POST = frozenset({"/cosmos/tx/v1beta1/simulate", "/cosmos/tx/v1beta1/txs"})


def _constant(_value: str) -> NoReturn:
    raise ValueError("Invalid JSON constant")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _url(base: str, path: str, method: str) -> httpx.URL:
    if not isinstance(path, str) or not (
        method == "GET" and any(pattern.fullmatch(path) for pattern in _GET)
        or method == "POST" and path in _POST
    ):
        raise ValidationError("Unsupported business HTTP operation", layer="http")
    try:
        if not isinstance(base, str):
            raise ValueError
        _http_url(base)
        result = httpx.URL(base.rstrip("/") + path)
        if result.username or result.password:
            raise ValueError
        return result
    except (ValueError, TypeError, httpx.InvalidURL):
        raise ValidationError("Invalid business HTTP endpoint", layer="http") from None


class BusinessHTTP:
    """Owns its client (including injected transport); never retries a send.

    No environment proxies, redirects, alternate endpoint failover, automatic
    auth or middleware. Both streaming response size and the complete request
    duration are bounded, independently of the HTTP per-I/O timeout.
    """

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def request(  # noqa: ASYNC109
        self, base: str, path: str, *, method: str = "GET", json: Any = None,
        params: Mapping[str, Any] | None = None, timeout: float,  # noqa: ASYNC109
    ) -> Any:
        if self._closed:
            raise ClosedError("Business HTTP transport is closed", layer="http")
        url = _url(base, path, method)
        if (isinstance(timeout, bool) or not isinstance(timeout, int | float)
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValidationError("HTTP timeout must be positive and finite", layer="http")
        if params is not None and (
            not isinstance(params, Mapping) or len(params) > 32
            or any(not isinstance(k, str) or len(k) > 128
                   or type(v) not in (str, int, bool) or len(str(v)) > 32768
                   for k, v in params.items())
        ):
            raise ValidationError("Invalid business HTTP query parameters", layer="http")
        if method == "GET" and json is not None:
            raise ValidationError("GET operations cannot have a request body", layer="http")
        content: bytes | None = None
        if json is not None:
            try:
                content = jsonlib.dumps(json, allow_nan=False, separators=(",", ":")).encode()
            except (ValueError, TypeError, RecursionError):
                raise ValidationError("Invalid business HTTP JSON body", layer="http") from None
            if len(content) > _MAX_REQUEST:
                raise ValidationError("Business HTTP request exceeds size limit", layer="http")
        try:
            async with asyncio.timeout(timeout):
                async with self._client.stream(
                    method, url, content=content, params=params,
                    headers={"content-type": "application/json"} if content is not None else None,
                    timeout=timeout,
                ) as response:
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(data) + len(chunk) > _MAX_RESPONSE:
                            raise ProtocolError("Business HTTP response exceeds size limit", layer="http")
                        data.extend(chunk)
                    if not 200 <= response.status_code < 300:
                        missing_identity = False
                        if response.status_code in (404, 500) and path in {
                            "/metaearth/did/did", "/metaearth/did/didInfo",
                            "/metaearth/kyc/QuerySubAccountDidResponse",
                        }:
                            try:
                                error = jsonlib.loads(data, parse_constant=_constant, object_pairs_hook=_object)
                                allowed_codes = (5, "5") if response.status_code == 404 else (2, "2", 13, "13")
                                if isinstance(error, dict) and error.get("code") in allowed_codes:
                                    message = error.get("message")
                                    expected_message = (
                                        "failed to get DID by sub-account"
                                        if path == "/metaearth/kyc/QuerySubAccountDidResponse"
                                        else "DID not found"
                                    )
                                    missing_identity = message == expected_message
                            except (ValueError, RecursionError):
                                pass
                        raise NetworkError(
                            "ME ID is not registered" if missing_identity else "Business HTTP request returned an unsuccessful status",
                            operation="me_id_not_found" if missing_identity else None,
                            layer="http", status_code=response.status_code,
                        )
                    try:
                        return jsonlib.loads(data, parse_constant=_constant, object_pairs_hook=_object)
                    except (ValueError, RecursionError):
                        raise ProtocolError("Business HTTP response is not valid JSON", layer="http") from None
        except (TimeoutError, httpx.TimeoutException):
            raise SDKTimeout("Business HTTP request timed out", layer="http") from None
        except httpx.RequestError:
            raise NetworkError("Business HTTP request failed", layer="http") from None

    async def aclose(self) -> None:
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._client.aclose())
        await asyncio.shield(self._close_task)
