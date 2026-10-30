"""Instance-local cross-protocol write gate, deliberately independent of endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from ..exceptions import BusyError, ClosedError, ValidationError
from ..transaction_models import PendingTransaction


@dataclass
class _Gate:
    lock: asyncio.Lock
    users: int = 0
    pending: PendingTransaction | None = None


class ExecutionToken:
    def __init__(self, gate: _Gate, logical_chain: str, identity: bytes) -> None:
        self._gate = gate
        self._chain = logical_chain
        self._identity = identity.hex()
        self._active = True

    def mark_pending(self, handle: PendingTransaction) -> None:
        if not self._active:
            raise ClosedError("Execution coordination token is no longer active")
        if handle.logical_chain != self._chain or handle.identity != self._identity:
            raise ValidationError("Recovery handle does not match execution identity")
        if self._gate.pending is not None and self._gate.pending != handle:
            raise BusyError("An unresolved transaction already protects this identity")
        self._gate.pending = handle

    def resolve(self, handle: PendingTransaction) -> None:
        if not self._active:
            raise ClosedError("Execution coordination token is no longer active")
        if self._gate.pending == handle:
            self._gate.pending = None


class ExecutionCoordinator:
    """Share this exact instance between Direct and EVM engines.

    Callers supply a *verified* logical chain and canonical 20-byte payload. In
    absence of a mutually verified deployment fingerprint, use the verified
    Cosmos chain ID alone: conservatively sharing a gate is safer than splitting
    same-ID deployments or DNS aliases into independent write channels.
    """

    def __init__(self, max_pending: int = 16) -> None:
        if type(max_pending) is not int or max_pending < 1:
            raise ValidationError("Execution capacity must be a positive integer")
        self._capacity = max_pending
        self._waiting = 0
        self._gates: dict[tuple[str, bytes], _Gate] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _bind(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise ClosedError("Execution coordinator belongs to another event loop")

    @property
    def pending(self) -> tuple[PendingTransaction, ...]:
        return tuple(gate.pending for gate in self._gates.values() if gate.pending is not None)

    def resolve(self, handle: PendingTransaction) -> None:
        """Internal receipt-proven resolution; never call on mere not-found/nonce."""
        self._bind()
        key = (handle.logical_chain, bytes.fromhex(handle.identity))
        gate = self._gates.get(key)
        if gate is not None and gate.pending == handle:
            gate.pending = None
            if gate.users == 0:
                self._gates.pop(key, None)

    @asynccontextmanager
    async def acquire(self, logical_chain: str, identity: bytes) -> AsyncIterator[ExecutionToken]:
        self._bind()
        if (not isinstance(logical_chain, str) or not logical_chain
                or any(c.isspace() or ord(c) < 32 for c in logical_chain)
                or not isinstance(identity, bytes) or len(identity) != 20):
            raise ValidationError("Invalid canonical execution identity")
        if self._waiting >= self._capacity:
            raise BusyError("Execution coordination capacity is full")
        key = (logical_chain, identity)
        if key not in self._gates and len(self._gates) >= self._capacity:
            raise BusyError("Unresolved execution identity capacity is full")
        gate = self._gates.setdefault(key, _Gate(asyncio.Lock()))
        gate.users += 1
        self._waiting += 1
        token = ExecutionToken(gate, logical_chain, identity)
        try:
            async with gate.lock:
                if gate.pending is not None:
                    raise BusyError("Unresolved transaction blocks this chain identity")
                yield token
        finally:
            token._active = False
            self._waiting -= 1
            gate.users -= 1
            if gate.users == 0 and gate.pending is None:
                self._gates.pop(key, None)
