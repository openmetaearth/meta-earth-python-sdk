"""Bounded ABI encoding/decoding using the already available eth-abi codec."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from eth_abi.abi import decode, encode
from eth_utils.crypto import keccak

from ..evm_models import MAX_EVM_DATA_BYTES
from ..exceptions import ProtocolError, ValidationError


def hex_data(value: object, *, maximum: int = MAX_EVM_DATA_BYTES) -> str:
    if not isinstance(value, str) or len(value) > 2 + maximum * 2 or re.fullmatch(r"0x(?:[a-fA-F0-9]{2})*", value) is None:
        raise ValidationError("Expected bounded even-length EVM hex data")
    return value.lower()


def abi_type(parameter: dict[str, Any], depth: int = 0) -> str:
    if depth > 16 or not isinstance(parameter, dict):
        raise ValidationError("Invalid ABI parameter")
    kind = parameter.get("type")
    if not isinstance(kind, str) or len(kind) > 128:
        raise ValidationError("Invalid ABI type")
    if kind.startswith("tuple"):
        suffix = kind[5:]
        if re.fullmatch(r"(?:\[[0-9]*\])*", suffix) is None:
            raise ValidationError("Invalid ABI tuple type")
        components = parameter.get("components")
        if not isinstance(components, list) or len(components) > 1024:
            raise ValidationError("Invalid ABI tuple components")
        return "(" + ",".join(abi_type(item, depth + 1) for item in components) + ")" + suffix
    # Function selectors require canonical uint/int spellings.
    kind = re.sub(r"^uint(?=\[|$)", "uint256", kind)
    return re.sub(r"^int(?=\[|$)", "int256", kind)


def _parameters(entry: dict[str, Any], name: str) -> list[dict[str, Any]]:
    result = entry.get(name, [])
    if not isinstance(result, list) or len(result) > 1024 or any(not isinstance(item, dict) for item in result):
        raise ValidationError("Invalid ABI parameters")
    return result


def _bounded_args(args: Sequence[Any]) -> None:
    stack: list[tuple[Any, int]] = [(args, 0)]
    budget = 0
    while stack:
        value, depth = stack.pop()
        budget += 1
        if budget > 32768 or depth > 16:
            raise ValueError
        if isinstance(value, str | bytes | bytearray):
            budget += len(value)
        elif isinstance(value, list | tuple):
            if len(value) > 32768:
                raise ValueError
            stack.extend((item, depth + 1) for item in value)
        elif type(value) not in (int, bool):
            raise ValueError
        if budget > MAX_EVM_DATA_BYTES:
            raise ValueError


def _encode(entry: dict[str, Any], args: Sequence[Any]) -> bytes:
    try:
        _bounded_args(args)
        types = [abi_type(item) for item in _parameters(entry, "inputs")]
        if len(types) != len(args):
            raise ValueError
        result = bytes(encode(types, args))
        if len(result) > MAX_EVM_DATA_BYTES:
            raise ValueError
        return result
    except Exception:
        raise ValidationError("ABI arguments do not match the selected signature") from None


def function(abi: list[dict[str, Any]], method: str, args: Sequence[Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(method, str) or not method or len(method) > 4096:
        raise ValidationError("Invalid ABI method")
    entries = [entry for entry in abi if entry.get("type") == "function"]
    matches: list[tuple[dict[str, Any], str]] = []
    for entry in entries:
        name = entry.get("name")
        if not isinstance(name, str) or re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$]*", name) is None:
            raise ValidationError("Invalid ABI function name")
        signature = name + "(" + ",".join(abi_type(item) for item in _parameters(entry, "inputs")) + ")"
        if method == signature or method == name:
            matches.append((entry, signature))
    # Never guess overloads using values: uint/address and numeric widths overlap.
    if len(matches) != 1:
        raise ValidationError("ABI method is missing or overloaded; use its full signature")
    entry, signature = matches[0]
    payload = keccak(text=signature)[:4] + _encode(entry, args)
    if len(payload) > MAX_EVM_DATA_BYTES:
        raise ValidationError("ABI call exceeds size limit")
    return "0x" + payload.hex(), entry


def constructor(abi: list[dict[str, Any]], bytecode: str, args: Sequence[Any]) -> str:
    code = hex_data(bytecode)
    if code == "0x":
        raise ValidationError("Contract bytecode cannot be empty")
    entries = [entry for entry in abi if entry.get("type") == "constructor"]
    if len(entries) > 1:
        raise ValidationError("ABI has multiple constructors")
    encoded = _encode(entries[0] if entries else {}, args)
    return hex_data(code + encoded.hex())


def decode_result(entry: dict[str, Any], result: object) -> Any:
    try:
        data = hex_data(result)
        types = [abi_type(item) for item in _parameters(entry, "outputs")]
        values = tuple(decode(types, bytes.fromhex(data[2:]), strict=True))
        # Reject non-canonical/trailing encodings rather than silently accepting.
        if "0x" + bytes(encode(types, values)).hex() != data:
            raise ValueError
        return values[0] if len(values) == 1 else values
    except Exception:
        raise ProtocolError("Invalid ABI call response") from None
