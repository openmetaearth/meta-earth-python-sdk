"""Strict REST signer-account decoding; absent counters never become zero."""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, cast

from meta_earth_sdk.protocol.registry import ProtocolError, RuntimeRegistry, get_registry


class _KeyFields(Protocol):
    key: bytes


@dataclass(frozen=True, slots=True)
class AccountPublicKey:
    type_url: str
    key: bytes


@dataclass(frozen=True, slots=True)
class AccountInfo:
    address: str
    account_number: int
    sequence: int
    public_key: AccountPublicKey | None
    wrapper_type: str


_BASE = "cosmos.auth.v1beta1.BaseAccount"
_ETH = "ethermint.types.v1.EthAccount"
_VESTING_BASE = "cosmos.vesting.v1beta1.BaseVestingAccount"
_VESTING = frozenset("cosmos.vesting.v1beta1." + name for name in (
    "ContinuousVestingAccount", "DelayedVestingAccount", "PeriodicVestingAccount", "PermanentLockedAccount"))
_KEYS = frozenset({"cosmos.crypto.secp256k1.PubKey", "ethermint.crypto.v1.ethsecp256k1.PubKey"})
_UINT = re.compile(r"(?:0|[1-9][0-9]*)\Z")


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ProtocolError("Account wrapper must be an object")
    return cast(Mapping[str, object], value)


def _field(data: Mapping[str, object], snake: str, camel: str) -> object:
    if snake != camel and snake in data and camel in data:
        raise ProtocolError("Duplicate account field alias")
    if snake in data:
        return data[snake]
    if camel in data:
        return data[camel]
    raise ProtocolError("Required account field is missing")


def _counter(value: object) -> int:
    if isinstance(value, bool):
        raise ProtocolError("Invalid account counter")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and _UINT.fullmatch(value):
        # uint64 has at most 20 decimal digits: bound before integer conversion.
        if len(value) > 20:
            raise ProtocolError("Account counter exceeds uint64")
        result = int(value)
    else:
        raise ProtocolError("Invalid account counter")
    if not 0 <= result < 2**64:
        raise ProtocolError("Account counter exceeds uint64")
    return result


def decode_account(data: Mapping[str, object], *, registry: RuntimeRegistry | None = None) -> AccountInfo:
    """Decode BaseAccount, EthAccount or explicit Cosmos vesting wrappers.

    REST numeric fields and pub_key must be explicit (null pub_key is legal).
    This function validates protocol/key shape, not signer ownership, chain
    identity, address HRP/profile or local-key equality; the engine checks those.
    Module accounts, arbitrary wrapper recursion and unknown nonempty keys fail.
    """
    registry = registry or get_registry()
    account = _mapping(data)
    if "account" in account:
        if set(account) != {"account"}:
            raise ProtocolError("Unexpected account response fields")
        account = _mapping(account["account"])
    type_url = account.get("@type")
    if not isinstance(type_url, str):
        raise ProtocolError("Account type URL must be explicit")
    wrapper_type = type_url.rsplit("/", 1)[-1]
    if wrapper_type not in {_BASE, _ETH, _VESTING_BASE} | _VESTING:
        raise ProtocolError("Account wrapper type is not supported")
    base = account
    if wrapper_type in _VESTING:
        base = _mapping(_field(base, "base_vesting_account", "baseVestingAccount"))
    if wrapper_type in _VESTING or wrapper_type == _VESTING_BASE or wrapper_type == _ETH:
        base = _mapping(_field(base, "base_account", "baseAccount"))
    address = _field(base, "address", "address")
    if (not isinstance(address, str) or not address or len(address) > 256
            or any(ord(char) < 33 or ord(char) > 126 for char in address)):
        raise ProtocolError("Invalid account address")
    account_number = _counter(_field(base, "account_number", "accountNumber"))
    sequence = _counter(_field(base, "sequence", "sequence"))
    key_data = _field(base, "pub_key", "pubKey")
    public_key = None
    if key_data is not None:
        key_map = _mapping(key_data)
        key_type = key_map.get("@type", key_map.get("type_url", key_map.get("typeUrl")))
        if not isinstance(key_type, str) or key_type.rsplit("/", 1)[-1] not in _KEYS:
            raise ProtocolError("Account public key type is not supported")
        key_message = registry.decode_any(key_map)
        raw_key = cast(_KeyFields, key_message).key
        if not isinstance(raw_key, bytes) or len(raw_key) != 33 or raw_key[0] not in (2, 3):
            raise ProtocolError("Account public key must be compressed secp256k1")
        public_key = AccountPublicKey("/" + key_message.DESCRIPTOR.full_name, raw_key)
    # Parse the complete original wrapper too: unknown fields, invalid nested
    # values, malformed vesting data or Any types are not silently discarded.
    registry.decode_any(account)
    return AccountInfo(address, account_number, sequence, public_key, wrapper_type)
