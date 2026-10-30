"""Immutable transaction inputs and payload-safe public execution records."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, field_validator

from .models import Coin, _SafeModel

_UINT64_MAX = (1 << 64) - 1
TransactionState = Literal[
    "prepared", "signed", "submitted", "check_tx_rejected",
    "committed_success", "committed_failed", "unknown",
]


class TransactionOptions(_SafeModel):
    """Exact umec fees and real execution gas; simulation is never the gas limit.

    ``timeout_ms`` can shorten, but cannot extend, the runtime's total budget.
    An explicit fee bypasses automatic pricing, not simulation or safety checks.
    """

    fee_amount: int | None = Field(default=None, ge=0, le=_UINT64_MAX)
    gas_limit: int | None = Field(default=None, gt=0, le=_UINT64_MAX)
    max_fee_amount: int | None = Field(default=None, ge=0, le=_UINT64_MAX)
    max_gas_limit: int | None = Field(default=None, gt=0, le=_UINT64_MAX)
    timeout_ms: int | None = Field(default=None, gt=0)
    wait_for_commit: bool = True
    poll_interval_ms: int = Field(default=250, gt=0, le=60_000)


class GasEstimate(_SafeModel):
    gas_used: int = Field(ge=0, le=_UINT64_MAX)
    gas_wanted: int = Field(ge=0, le=_UINT64_MAX)
    pricing_gas: int = Field(ge=0)
    fee_amount: int = Field(ge=0)
    gas_limit: int = Field(gt=0, le=_UINT64_MAX)


class PendingTransaction(_SafeModel):
    """Query-only recovery handle; never contains a signed payload or secret.

    Coordination is per SDK instance, not persistent or distributed. A not-found
    response or an advanced sequence is NOT evidence that this handle is resolved.
    """

    handle: str
    transaction_hash: str
    logical_chain: str
    identity: str
    protocol: Literal["cosmos", "evm"] = "cosmos"

    @field_validator("transaction_hash")
    @classmethod
    def _hash(cls, value: str) -> str:
        text = value.removeprefix("0x")
        if re.fullmatch(r"[0-9a-fA-F]{64}", text) is None:
            raise ValueError("Invalid transaction hash")
        return text.upper() if not value.startswith("0x") else "0x" + text.lower()

    @field_validator("identity")
    @classmethod
    def _identity(cls, value: str) -> str:
        if re.fullmatch(r"[0-9a-f]{40}", value) is None:
            raise ValueError("Invalid canonical identity")
        return value


class TransactionAttribute(_SafeModel):
    key: str
    value: str


class TransactionEvent(_SafeModel):
    type: str
    attributes: tuple[TransactionAttribute, ...] = ()


class TransactionResult(_SafeModel):
    """A SYNC acceptance is ``submitted``, never committed success.

    Raw logs, remote error strings and transaction bytes are intentionally not
    retained. Use the explicitly opt-in byte export only on a locally signed
    result; recovery/query results have no local signed bytes.
    """

    transaction_hash: str
    state: TransactionState
    code: int | None = Field(default=None, ge=0, le=(1 << 32) - 1)
    codespace: str = ""
    height: int = Field(default=0, ge=0)
    gas_used: int | None = Field(default=None, ge=0)
    gas_wanted: int | None = Field(default=None, ge=0)
    fee: Coin | None = None
    pending_handle: PendingTransaction | None = None
    events: tuple[TransactionEvent, ...] = ()
    signed_bytes: bytes = Field(default=b"", repr=False, exclude=True)

    @property
    def txhash(self) -> str:
        return self.transaction_hash

    @property
    def hash(self) -> str:
        return self.transaction_hash

    @property
    def committed(self) -> bool:
        return self.state in ("committed_success", "committed_failed")

    def export_signed_bytes(self) -> bytes:
        return self.signed_bytes


class TransactionHistoryService(_SafeModel):
    """Explicit external history/fees service; never inferred from node URLs."""

    base_url: str

    @field_validator("base_url")
    @classmethod
    def _url(cls, value: str) -> str:
        from .config import _http_url

        return _http_url(value)
