"""Bounded synchronous work whose lifetime is independent of its awaiter."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TypeVar

from ..exceptions import BusyError, ClosedError

T = TypeVar("T")


class BoundedExecutor:
    def __init__(self, max_pending: int = 16, *, thread_name_prefix: str = "me-sdk-query") -> None:
        if isinstance(max_pending, bool) or not isinstance(max_pending, int) or max_pending < 1:
            raise ValueError("max_pending must be a positive integer")
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=thread_name_prefix)
        self._limit = max_pending
        # Only real pool completion releases admission capacity. Public cancellation
        # leaves its wrapper queued, so never expose or cancel the pool future.
        self._pending: dict[Future[None], Callable[[], bool]] = {}
        self._accepting = True
        self._loop: asyncio.AbstractEventLoop | None = None
        self._idle: asyncio.Event | None = None
        self._close_task: asyncio.Task[None] | None = None

    def _bind(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._idle = asyncio.Event()
            self._idle.set()
        elif self._loop is not loop:
            raise ClosedError("Executor belongs to a different event loop")
        return loop

    @property
    def pending(self) -> int:
        return len(self._pending)

    def submit(self, function: Callable[[], T]) -> Future[T]:
        loop = self._bind()
        if not self._accepting:
            raise ClosedError("Executor is closing")
        if len(self._pending) >= self._limit:
            raise BusyError("Synchronous work capacity is full")
        future: Future[T] = Future()

        def work() -> None:
            # Cancellation may already have released the runtime's generation.
            # Atomically claim the public future before touching any resources;
            # once running, it cannot be cancelled while the operation uses them.
            if not future.set_running_or_notify_cancel():
                return
            try:
                result = function()
            except BaseException as exc:
                future.set_exception(exc)
            else:
                future.set_result(result)

        internal = self._pool.submit(work)
        self._pending[internal] = future.cancel
        assert self._idle is not None
        self._idle.clear()
        internal.add_done_callback(lambda done: loop.call_soon_threadsafe(self._finished, done))
        return future

    def _finished(self, future: Future[None]) -> None:
        self._pending.pop(future, None)
        if not self._pending:
            assert self._idle is not None
            self._idle.set()

    def stop(self) -> None:
        self._bind()
        self._accepting = False
        for cancel in tuple(self._pending.values()):
            cancel()

    async def drain(self) -> None:
        self._bind()
        assert self._idle is not None
        await self._idle.wait()

    async def control(self, function: Callable[[], T]) -> T:
        """Lifecycle work bypasses query admission, including after stop()."""
        self._bind()
        future = self._pool.submit(function)
        return await asyncio.shield(asyncio.wrap_future(future))

    async def aclose(self) -> None:
        self._bind()
        if self._close_task is None:
            self.stop()
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        await self.drain()
        await asyncio.to_thread(self._pool.shutdown, wait=True, cancel_futures=True)
