"""Explicit offline EVM transaction inputs and opt-in signed-byte export."""

from __future__ import annotations

import re
from typing import Self

from eth_utils.address import is_checksum_address
from pydantic import ConfigDict, Field, field_validator, model_validator

from .models import _JsonBytes, _SafeModel

__all__ = ["EvmTransaction", "EvmSignResult"]

_MAX_UINT64 = (1 << 64) - 1
_MAX_UINT256 = (1 << 256) - 1
# This is an offline SDK resource bound, not an EVM protocol gas/data limit.
MAX_EVM_DATA_BYTES = 1 << 20


def _address_bytes(value: str) -> bytes:
    if re.fullmatch(r"0[xX][0-9a-fA-F]{40}", value) is None:
        raise ValueError("Expected a 20-byte hex address")
    payload = value[2:]
    if payload != payload.lower() and payload != payload.upper():
        if not is_checksum_address("0x" + payload):
            raise ValueError("Invalid mixed-case address checksum")
    return bytes.fromhex(payload)


class EvmTransaction(_SafeModel):
    """Legacy (type 0) or EIP-1559 (type 2), with no network-filled fields.

    Quantities are strict Python integers, not hex strings. Gas is uint64;
    nonce is below uint64 max (EIP-2681). Other quantities are uint256.
    ``to``, ``value`` and ``data`` are required: use None/empty string for contract
    creation, zero for no value, and ``0x`` for empty data. Access lists are not
    supported in this first offline surface. Field aliases use Ethereum spelling.
    """

    model_config = ConfigDict(revalidate_instances="always")

    nonce: int = Field(ge=0, lt=_MAX_UINT64)
    gas: int = Field(gt=0, le=_MAX_UINT64)
    chain_id: int = Field(validation_alias="chainId", serialization_alias="chainId", gt=0, le=_MAX_UINT256)
    to: str | None = Field(repr=False, exclude=True)
    value: int = Field(ge=0, le=_MAX_UINT256, repr=False, exclude=True)
    data: str = Field(repr=False, exclude=True)
    type: int = 0
    gas_price: int | None = Field(default=None, validation_alias="gasPrice", serialization_alias="gasPrice", ge=0, le=_MAX_UINT256)
    max_fee_per_gas: int | None = Field(default=None, validation_alias="maxFeePerGas", serialization_alias="maxFeePerGas", ge=0, le=_MAX_UINT256)
    max_priority_fee_per_gas: int | None = Field(default=None, validation_alias="maxPriorityFeePerGas", serialization_alias="maxPriorityFeePerGas", ge=0, le=_MAX_UINT256)
    from_address: str | None = Field(default=None, validation_alias="from", serialization_alias="from", repr=False, exclude=True)

    @field_validator("type", mode="before")
    @classmethod
    def _transaction_type(cls, value: object) -> object:
        if type(value) is not int or value not in (0, 2):
            raise ValueError("Unsupported transaction type")
        return value

    @field_validator("gas_price", "max_fee_per_gas", "max_priority_fee_per_gas", mode="before")
    @classmethod
    def _explicit_fee(cls, value: object) -> object:
        if value is not None and type(value) is not int:
            raise ValueError("Expected an explicit integer fee")
        return value

    @field_validator("to")
    @classmethod
    def _destination(cls, value: str | None) -> str | None:
        if value is not None and value != "":
            _address_bytes(value)
        return value

    @field_validator("from_address")
    @classmethod
    def _sender(cls, value: str | None) -> str | None:
        if value is not None:
            _address_bytes(value)
        return value

    @field_validator("data")
    @classmethod
    def _data(cls, value: str) -> str:
        if (len(value) > 2 + 2 * MAX_EVM_DATA_BYTES
                or re.fullmatch(r"0[xX](?:[0-9a-fA-F]{2})*", value) is None):
            raise ValueError("Expected bounded even-length hex data")
        return value

    @model_validator(mode="after")
    def _fee_shape(self) -> Self:
        for name in ("gas_price", "max_fee_per_gas", "max_priority_fee_per_gas"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError("Explicit fees cannot be null")
        if self.type == 0:
            if (self.gas_price is None or self.max_fee_per_gas is not None
                    or self.max_priority_fee_per_gas is not None):
                raise ValueError("Legacy transactions require only gasPrice")
        elif (self.gas_price is not None or self.max_fee_per_gas is None
                or self.max_priority_fee_per_gas is None
                or self.max_priority_fee_per_gas > self.max_fee_per_gas):
            raise ValueError("EIP-1559 transactions require consistent explicit fees")
        return self

    def to_transaction_dict(self) -> dict[str, object]:
        """Explicitly reveal the complete transaction for eth-account signing."""
        transaction: dict[str, object] = {
            "nonce": self.nonce, "gas": self.gas, "chainId": self.chain_id,
            "to": b"" if not self.to else _address_bytes(self.to),
            "value": self.value, "data": bytes.fromhex(self.data[2:]),
        }
        if self.type == 0:
            transaction["gasPrice"] = self.gas_price
        else:
            transaction.update({"type": 2, "maxFeePerGas": self.max_fee_per_gas,
                                "maxPriorityFeePerGas": self.max_priority_fee_per_gas})
        return transaction


class EvmSignResult(_SafeModel):
    """Signed transaction; default repr/dumps never reveal broadcastable bytes.

    Explicit attributes and ``to_signed_dict`` reveal the signed payload. The
    SDK neither sends it nor prevents its owner from broadcasting it elsewhere.
    """

    raw_transaction: _JsonBytes = Field(validation_alias="rawTransaction", serialization_alias="rawTransaction", repr=False, exclude=True, min_length=1)
    hash: _JsonBytes = Field(repr=False, exclude=True, min_length=32, max_length=32)
    r: int = Field(repr=False, exclude=True, gt=0, le=_MAX_UINT256)
    s: int = Field(repr=False, exclude=True, gt=0, le=_MAX_UINT256)
    v: int = Field(repr=False, exclude=True, ge=0)

    @property
    def rawTransaction(self) -> bytes:
        return self.raw_transaction

    def to_signed_dict(self) -> dict[str, object]:
        """Explicitly export eth-account-shaped bytes and signature integers."""
        return {"raw_transaction": self.raw_transaction, "hash": self.hash,
                "r": self.r, "s": self.s, "v": self.v}
