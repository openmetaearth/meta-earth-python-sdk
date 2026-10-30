"""Owned, bounded HTTP transport for the read-only node endpoints."""

from __future__ import annotations

import asyncio
import math
from typing import Any, NoReturn
from urllib.parse import urlsplit

import httpx

from ..exceptions import (
    ClosedError,
    NetworkError,
    ProtocolError,
    ValidationError,
)
from ..exceptions import (
    TimeoutError as SDKTimeout,
)

_ALLOWED_PATHS = frozenset(
    {"/status", "/abci_info", "/cosmos/base/tendermint/v1beta1/node_info"}
)


def _request_url(base_url: str, path: str) -> httpx.URL:
    """Validate again at the transport boundary, without echoing unsafe inputs."""
    if not isinstance(path, str) or path not in _ALLOWED_PATHS:
        raise ValidationError("Unsupported node HTTP path.", layer="http")
    if (
        not isinstance(base_url, str)
        or not base_url
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in base_url)
        or any(character in base_url for character in ("?", "#", "\\"))
    ):
        raise ValidationError("Invalid node HTTP base URL.", operation=path, layer="http")
    try:
        parts = urlsplit(base_url)
        if (
            parts.scheme.lower() not in {"http", "https"}
            or not parts.hostname
            or "@" in parts.netloc
            or parts.port is not None and not 1 <= parts.port <= 65535
        ):
            raise ValueError("Invalid base URL.")
        # Do not use urljoin: a leading endpoint slash would discard the prefix.
        url = httpx.URL(base_url.rstrip("/") + path)
        if not url.host or url.username or url.password:
            raise ValueError("Invalid base URL.")
        return url
    except (ValueError, httpx.InvalidURL):
        raise ValidationError("Invalid node HTTP base URL.", operation=path, layer="http") from None


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("Invalid JSON numeric constant.")


class NodeHTTP:
    """A client owning all its resources, including an injected transport.

    Construction performs no network I/O. There are no retries, redirects, or
    fallback endpoints. Responses are raw JSON: JSON-RPC error envelopes and
    JSON ``null`` are returned unchanged; semantic validation belongs to callers.
    Calling ``aclose`` also closes the injected transport, which must not be shared
    with another owner. Calls after closure raise ``ClosedError``.
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

    async def get(self, base_url: str, path: str, *, timeout: float) -> Any:  # noqa: ASYNC109
        """GET one allowlisted endpoint within a total and per-I/O time budget."""
        if self._closed:
            raise ClosedError("Node HTTP transport is closed.", layer="http")
        url = _request_url(base_url, path)
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValidationError("HTTP timeout must be positive and finite.", operation=path, layer="http")
        try:
            async with asyncio.timeout(timeout):
                response = await self._client.get(url, timeout=timeout)
                if not 200 <= response.status_code < 300:
                    raise NetworkError(
                        "Node HTTP request returned an unsuccessful status.",
                        operation=path,
                        layer="http",
                        status_code=response.status_code,
                    )
                try:
                    return response.json(parse_constant=_reject_json_constant)
                except (ValueError, RecursionError):
                    raise ProtocolError(
                        "Node HTTP response is not valid JSON.", operation=path, layer="http"
                    ) from None
        except (TimeoutError, httpx.TimeoutException):
            raise SDKTimeout("Node HTTP request timed out.", operation=path, layer="http") from None
        except httpx.RequestError:
            raise NetworkError("Node HTTP request failed.", operation=path, layer="http") from None

    async def aclose(self) -> None:
        """Close once; cancelling a waiter does not abandon owned resources."""
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._client.aclose())
        await asyncio.shield(self._close_task)
