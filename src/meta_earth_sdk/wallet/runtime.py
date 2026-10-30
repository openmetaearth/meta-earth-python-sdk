"""Bounded offline work; only the owning event loop publishes wallet state."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import TypeVar

from ..exceptions import ClosedError, SDKError, SigningError, ValidationError, WalletNotFoundError
from ..transport.executor import BoundedExecutor
from ..wallet_models import WalletAccount

T = TypeVar("T")


class WalletRuntime:
    def __init__(self, max_pending: int = 16) -> None:
        self._executor = BoundedExecutor(max_pending, thread_name_prefix="me-sdk-wallet")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: int | None = None
        self._closed = False
        self._accounts: dict[str, WalletAccount] = {}
        self._capabilities: dict[int, WalletAccount] = {}
        self._next_capability = 0
        self._close_task: asyncio.Task[None] | None = None

    def bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise ClosedError("Wallet belongs to a different event loop")
        self._loop = loop
        self._thread = threading.get_ident()

    def check(self) -> None:
        self.bind()
        if self._closed:
            raise ClosedError("Wallet runtime is closing or closed")

    def _check_snapshot(self) -> None:
        if self._closed:
            raise ClosedError("Wallet runtime is closing or closed")
        if self._thread is not None and self._thread != threading.get_ident():
            raise ClosedError("Wallet belongs to a different thread")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._loop is not None and self._loop is not loop:
            raise ClosedError("Wallet belongs to a different event loop")

    async def run(self, operation: Callable[[], T]) -> T:
        self.check()
        future = asyncio.wrap_future(self._executor.submit(operation))
        try:
            result = await future
        except SDKError:
            raise
        except asyncio.CancelledError:
            raise
        except Exception:
            raise SigningError("Offline wallet operation failed") from None
        self.check()
        return result

    def publish(self, accounts: list[WalletAccount]) -> None:
        self.check()
        self._accounts.update((account.address, account) for account in accounts)

    def addresses(self) -> list[str]:
        self._check_snapshot()
        return list(self._accounts)

    def find(self, address: str) -> WalletAccount | None:
        self.check()
        if not isinstance(address, str) or not address:
            raise ValidationError("Expected a wallet address")
        return self._accounts.get(address)

    def require(self, address: str) -> WalletAccount:
        account = self.find(address)
        if account is None:
            raise WalletNotFoundError("Wallet not found")
        return account

    def register(self, account: WalletAccount) -> int:
        self.check()
        self._next_capability += 1
        self._capabilities[self._next_capability] = account
        return self._next_capability

    def resolve(self, capability: int) -> WalletAccount:
        self.check()
        return self.resolve_snapshot(capability)

    def resolve_snapshot(self, capability: int) -> WalletAccount:
        self._check_snapshot()
        account = self._capabilities.get(capability)
        if account is None:
            raise ClosedError("Signer capability is no longer available")
        return account

    def stop(self) -> None:
        self.bind()
        self._closed = True
        self._accounts.clear()
        self._capabilities.clear()
        self._executor.stop()

    async def aclose(self) -> None:
        self.bind()
        if self._close_task is None:
            self.stop()
            self._close_task = asyncio.create_task(self._executor.aclose())
            self._close_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        await asyncio.shield(self._close_task)
