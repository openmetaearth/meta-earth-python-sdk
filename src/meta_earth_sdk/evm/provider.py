"""Injectable async JSON-RPC provider boundary and OPTIONAL Web3 adapter source.

The AsyncWeb3 adapter is disabled. Use an explicitly injected async provider.
Injected providers are trusted capabilities and must disable all send retries,
redirects, middleware resend and endpoint fallback, including their transport.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any, Protocol

from ..exceptions import ClosedError, NetworkError, ProtocolError, RPCError


class AsyncEvmProvider(Protocol):
    send_retries_disabled: bool

    async def request(self, method: str, params: list[Any]) -> Any: ...

    async def aclose(self) -> None: ...


class OptionalDependencyError(ImportError):
    """Optional Web3 adapter cannot be loaded; no installation is attempted."""


class AsyncWeb3Provider:
    """UNVERIFIED real adapter source; fake-provider tests do not validate it.

    Calls the single AsyncHTTPProvider directly, bypassing Web3 middleware.
    ``exception_retry_configuration=None`` disables Web3's HTTP retries. The
    SDK never invokes Web3 signing, gas filling, nonce filling or send helpers.
    External provider use is outside the SDK's single-send/coordinator boundary.
    """

    integration_verified = False
    send_retries_disabled = True

    def __init__(self, url: str) -> None:
        try:
            web3 = importlib.import_module("web3")
        except ImportError:
            raise OptionalDependencyError(
                "EVM RPC requires optional Web3, which is not installed. "
                "Use an explicitly injected async provider for offline tests; "
                "the real AsyncWeb3 adapter is unverified."
            ) from None
        # An ambient installation must not silently enable a different transport.
        del web3
        raise OptionalDependencyError(
            "The real AsyncWeb3 adapter is disabled. Inject a trusted "
            "async provider explicitly; no automatic alternative is used."
        )

    def _initialize_unverified_source(self, web3: Any, url: str) -> None:
        """Initialize adapter internals; the public constructor remains disabled."""
        self._provider = web3.AsyncHTTPProvider(
            url, request_kwargs={"allow_redirects": False},
            exception_retry_configuration=None,
        )
        self._web3 = web3.AsyncWeb3(self._provider)
        self._web3.middleware_onion.clear()
        self._closed = False

    async def request(self, method: str, params: list[Any]) -> Any:
        if self._closed:
            raise ClosedError("EVM provider is closed")
        try:
            response = await self._provider.make_request(method, params)
        except Exception:
            raise NetworkError("EVM RPC request failed") from None
        if not isinstance(response, dict) or ("error" in response) == ("result" in response):
            raise ProtocolError("Invalid EVM RPC envelope")
        if "error" in response:
            raise RPCError("EVM RPC rejected the request") from None
        return response["result"]

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                await self._provider.disconnect()
            except Exception:
                raise ClosedError("EVM provider close failed") from None


class ProviderResource:
    """Generation-owned adapter; a caller-owned provider is only borrowed."""

    def __init__(self, provider: AsyncEvmProvider, *, owned: bool, close_timeout: float = 60.0) -> None:
        self.provider = provider
        self.owned = owned
        self._close_timeout = close_timeout
        self.closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def aclose(self) -> None:
        if self._close_task is None:
            self.closed = True
            self._close_task = asyncio.create_task(self._close())
            self._close_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self.owned:
            try:
                async with asyncio.timeout(self._close_timeout):
                    await self.provider.aclose()
            except Exception:
                raise ClosedError("EVM provider close failed") from None
