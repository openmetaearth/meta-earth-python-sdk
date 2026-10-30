"""Per-instance configuration generations and ownership of in-flight reads."""

from __future__ import annotations

import asyncio
import builtins
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar, cast

if TYPE_CHECKING:
    from .business import BusinessHTTP

from ..adapters.cosmpy.bank import BankClient
from ..config import SDKConfig
from ..exceptions import BusyError, ClosedError, TimeoutError
from .executor import BoundedExecutor
from .http import NodeHTTP

T = TypeVar("T")


class _AsyncResource(Protocol):
    async def aclose(self) -> None: ...


R = TypeVar("R", bound=_AsyncResource)


@dataclass(eq=False)
class _Generation:
    config: SDKConfig
    banks: dict[str, BankClient] = field(default_factory=dict)
    http: NodeHTTP | None = None
    active: int = 0
    retired: bool = False
    cleanup: asyncio.Task[None] | None = None
    resources: dict[str, _AsyncResource] = field(default_factory=dict)


class OperationLease:
    """One immutable generation for an entire operation, including recovery reads."""

    def __init__(self, runtime: QueryRuntime, generation: _Generation) -> None:
        self._runtime = runtime
        self._generation = generation
        self._released = False
        self.deadline = time.monotonic() + generation.config.timeout_ms / 1000

    @property
    def config(self) -> SDKConfig:
        return self._generation.config

    def _check_lease(self) -> None:
        self._runtime._bind()
        if self._released:
            raise ClosedError("Operation lease has ended")

    def check_active(self) -> None:
        """Reject a new signing/sending transition after shutdown or expiry."""
        self._check_lease()
        if self._runtime._closing:
            raise ClosedError("Client is closing or closed")
        if time.monotonic() >= self.deadline:
            raise TimeoutError("Operation budget expired")

    def resource(self, name: str, factory: Callable[[], R]) -> R:
        self._check_lease()
        if name not in self._generation.resources:
            self.check_active()
            self._generation.resources[name] = factory()
        return cast(R, self._generation.resources[name])

    @property
    def transport(self) -> BusinessHTTP:
        if self._runtime._business_factory is None:
            from .business import BusinessHTTP

            return self.resource("business_http", BusinessHTTP)
        return self.resource("business_http", self._runtime._business_factory)

    async def run_sync(self, function: Callable[[], T]) -> T:
        """Keep a generation reference until the real bounded worker finishes."""
        self.check_active()
        generation = self._generation
        generation.active += 1
        try:
            future = self._runtime._executor.submit(function)
        except BaseException:
            self._runtime._release(generation)
            raise
        loop = asyncio.get_running_loop()
        future.add_done_callback(
            lambda _: loop.call_soon_threadsafe(self._runtime._release, generation)
        )
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        try:
            async with asyncio.timeout(max(0.0, self.deadline - time.monotonic())):
                result = await asyncio.shield(wrapped)
            self.check_active()
            return result
        except asyncio.CancelledError:
            future.cancel()
            raise
        except builtins.TimeoutError:
            future.cancel()
            raise TimeoutError("Operation budget expired") from None


class QueryRuntime:
    def __init__(
        self,
        config: SDKConfig,
        *,
        bank_factory: Callable[..., BankClient] = BankClient,
        http_factory: Callable[[], NodeHTTP] = NodeHTTP,
        business_factory: Callable[[], BusinessHTTP] | None = None,
    ) -> None:
        self._current = _Generation(config)
        self._retired: set[_Generation] = set()
        self._executor = BoundedExecutor(config.max_pending)
        self._bank_factory = bank_factory
        self._http_factory = http_factory
        self._business_factory = business_factory
        self._operations_active = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._idle: asyncio.Event | None = None
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        self._http_active = 0
        self._cancellations: set[threading.Event] = set()

    @property
    def config(self) -> SDKConfig:
        return self._current.config

    def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._idle = asyncio.Event()
            self._idle.set()
        elif self._loop is not loop:
            raise ClosedError("Client belongs to a different event loop")

    def _check(self) -> None:
        self._bind()
        if self._closing:
            raise ClosedError("Client is closing or closed")
        if any(item.cleanup is not None and item.cleanup.done() and not item.cleanup.cancelled()
               and item.cleanup.exception() is not None for item in self._retired):
            raise ClosedError("Resource retirement failed; close this client and create a new one")

    def _acquire(self) -> _Generation:
        self._check()
        generation = self._current
        generation.active += 1
        assert self._idle is not None
        self._idle.clear()
        return generation

    def _release(self, generation: _Generation) -> None:
        generation.active -= 1
        if generation.retired and generation.active == 0 and not self._closing:
            self._schedule_cleanup(generation)
        if self._current.active == 0 and all(item.active == 0 for item in self._retired):
            assert self._idle is not None
            self._idle.set()

    def _schedule_cleanup(self, generation: _Generation) -> asyncio.Task[None]:
        if generation.cleanup is None:
            generation.cleanup = asyncio.create_task(self._cleanup(generation))
            # Retrieve failures without suppressing them for close()/configuration callers.
            generation.cleanup.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        return generation.cleanup

    async def _cleanup(self, generation: _Generation) -> None:
        try:
            if generation.banks:
                def close_banks() -> None:
                    first: Exception | None = None
                    for client in generation.banks.values():
                        try:
                            client.close()
                        except Exception as exc:
                            first = first or exc
                    if first is not None:
                        raise first

                await self._executor.control(close_banks)
        finally:
            resources: list[_AsyncResource] = list(generation.resources.values())
            if generation.http is not None:
                resources.append(generation.http)
            # Attempt every owned close even if another resource fails.
            results = await asyncio.gather(
                *(resource.aclose() for resource in resources), return_exceptions=True
            )
            if any(isinstance(result, BaseException) for result in results):
                raise ClosedError("Generation resource cleanup failed") from None
        self._retired.discard(generation)

    @asynccontextmanager
    async def operation(self) -> AsyncIterator[OperationLease]:
        self._check()
        if self._operations_active >= self.config.max_pending:
            raise BusyError("Operation capacity is full")
        generation = self._acquire()
        self._operations_active += 1
        lease = OperationLease(self, generation)
        try:
            yield lease
        finally:
            lease._released = True
            self._operations_active -= 1
            self._release(generation)

    async def replace_config(self, config: SDKConfig) -> None:
        self._check()
        if config.max_pending != self.config.max_pending:
            from ..exceptions import ValidationError

            raise ValidationError("max_pending cannot change after client construction")
        if config == self.config:
            return
        if self._retired:
            raise BusyError("Previous network generation is still retiring")
        candidate = _Generation(config)
        previous = self._current
        self._current = candidate
        previous.retired = True
        self._retired.add(previous)
        if previous.active == 0:
            # Publication has committed. Retirement failure is reported by the next
            # operation/close, not as an apparent rollback of this configuration.
            cleanup = self._schedule_cleanup(previous)
            await asyncio.shield(asyncio.gather(cleanup, return_exceptions=True))

    async def bank(
        self,
        layer: str,
        operation: Callable[[BankClient, float, threading.Event], T],
    ) -> T:
        self._check()
        if layer not in ("hub", "rollup"):
            from ..exceptions import ValidationError

            raise ValidationError("layer must be hub or rollup")
        endpoint = getattr(self.config.resolved_network(), layer)
        generation = self._acquire()
        deadline = time.monotonic() + generation.config.timeout_ms / 1000
        cancelled = threading.Event()

        def work() -> T:
            if cancelled.is_set() or time.monotonic() >= deadline:
                raise TimeoutError("Balance query budget expired", operation="balance")
            if layer not in generation.banks:
                generation.banks[layer] = self._bank_factory(endpoint)
            return operation(generation.banks[layer], deadline, cancelled)

        try:
            future = self._executor.submit(work)
        except BaseException:
            self._release(generation)
            raise
        self._cancellations.add(cancelled)
        assert self._loop is not None
        loop = self._loop

        def completed() -> None:
            self._cancellations.discard(cancelled)
            self._release(generation)

        future.add_done_callback(lambda _: loop.call_soon_threadsafe(completed))
        wrapped = asyncio.wrap_future(future)
        # A cancelled waiter must not leave a late worker exception unobserved.
        wrapped.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
        try:
            remaining = max(0.0, deadline - time.monotonic())
            async with asyncio.timeout(remaining):
                return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            cancelled.set()
            future.cancel()
            raise
        except TimeoutError:
            cancelled.set()
            future.cancel()
            raise
        except builtins.TimeoutError:
            cancelled.set()
            future.cancel()
            raise TimeoutError("Balance query budget expired", operation="balance") from None

    async def node(self, layer: str, path: str, *, rest: bool = False) -> Any:
        self._check()
        if self._http_active >= self.config.max_pending:
            raise BusyError("HTTP query capacity is full")
        generation = self._acquire()
        self._http_active += 1
        try:
            if generation.http is None:
                generation.http = self._http_factory()
            endpoint = getattr(generation.config.resolved_network(), layer)
            base = endpoint.restful_url if rest else endpoint.rpc_url
            return await generation.http.get(
                base, path, timeout=generation.config.timeout_ms / 1000
            )
        finally:
            self._http_active -= 1
            self._release(generation)

    def stop(self) -> None:
        self._bind()
        self._closing = True
        for cancelled in self._cancellations:
            cancelled.set()
        self._executor.stop()

    async def aclose(self) -> None:
        self._bind()
        if self._close_task is None:
            self.stop()
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        await self._executor.drain()
        assert self._idle is not None
        await self._idle.wait()
        generations = tuple(self._retired) + (self._current,)
        results = await asyncio.gather(
            *(self._schedule_cleanup(item) for item in generations), return_exceptions=True
        )
        await self._executor.aclose()
        if any(isinstance(result, BaseException) for result in results):
            raise ClosedError("One or more client resources failed to close") from None

