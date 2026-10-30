"""Hub WASM models, bounded JSON/Base64 encoding and explicit pagination."""
from __future__ import annotations

import base64
import binascii
import json
import math
from typing import Any, Literal

from pydantic import Field, field_validator

from .business_models import UINT64_MAX, required_string
from .exceptions import ValidationError
from .models import Coin, _SafeModel

_MAX_WASM = 8 * 1024 * 1024
_MAX_JSON = 1024 * 1024


def json_bytes(value: Any) -> bytes:
    """Encode JSON, not Python coercions (integer keys, NaN or arbitrary objects).

    UTF-8 is emitted directly like JSON.stringify. Cycle/depth/size errors use
    SDK-authored summaries and never interpolate caller contract payloads.
    """
    active: set[int] = set()

    def check(item: Any, depth: int) -> None:
        if depth > 64:
            raise ValueError
        if item is None or type(item) in (str, bool, int):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError
            return
        if type(item) not in (dict, list):
            raise ValueError
        identity = id(item)
        if identity in active:
            raise ValueError
        active.add(identity)
        try:
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError
                    check(child, depth + 1)
            else:
                for child in item:
                    check(child, depth + 1)
        finally:
            active.remove(identity)

    try:
        check(value, 0)
        result = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
        if len(result) > _MAX_JSON:
            raise ValueError
        return result
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        raise ValidationError("Invalid or oversized WASM JSON message") from None


def wasm_bytes(value: bytes | str) -> bytes:
    try:
        if isinstance(value, str):
            if len(value) > ((_MAX_WASM + 2) // 3) * 4:
                raise ValueError
            decoded = base64.b64decode(value, validate=True)
            if base64.b64encode(decoded).decode("ascii") != value:
                raise ValueError
        elif isinstance(value, bytes):
            decoded = value
        else:
            raise ValueError
        if not decoded or len(decoded) > _MAX_WASM:
            raise ValueError
        return decoded
    except (ValueError, binascii.Error, UnicodeError):
        raise ValidationError("WASM bytecode must be nonempty bounded bytes or canonical Base64") from None


class AccessConfig(_SafeModel):
    permission: Literal[
        "ACCESS_TYPE_UNSPECIFIED", "ACCESS_TYPE_NOBODY", "ACCESS_TYPE_EVERYBODY", "ACCESS_TYPE_ANY_OF_ADDRESSES",
    ]
    addresses: list[str] = Field(default_factory=list)

    @field_validator("addresses")
    @classmethod
    def _addresses(cls, value: list[str]) -> list[str]:
        for address in value:
            required_string(address)
        return value


class StoreCodeParams(_SafeModel):
    sender: str
    wasm_byte_code: bytes | str = Field(alias="wasmByteCode", repr=False)
    instantiate_permission: AccessConfig | None = Field(default=None, alias="instantiatePermission")
    layer: Literal["wasm"] = "wasm"
    network_layer: Literal["hub"] = Field(default="hub", alias="networkLayer")
    memo: str = ""

    _sender = field_validator("sender")(required_string)

    @field_validator("wasm_byte_code")
    @classmethod
    def _wasm(cls, value: bytes | str) -> bytes:
        return wasm_bytes(value)


class DeployContractParams(_SafeModel):
    sender: str
    code_id: int = Field(alias="codeId", gt=0, le=UINT64_MAX)
    init_msg: Any = Field(alias="initMsg", repr=False)
    label: str = ""
    funds: list[Coin] = Field(default_factory=list)
    admin: str = ""
    layer: Literal["wasm"] = "wasm"
    network_layer: Literal["hub"] = Field(default="hub", alias="networkLayer")
    memo: str = ""

    _sender = field_validator("sender")(required_string)

    @field_validator("init_msg")
    @classmethod
    def _json(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("An instantiate message is required")
        json_bytes(value)
        return value


class ExecuteContractParams(_SafeModel):
    sender: str
    contract_address: str = Field(alias="contractAddress")
    msg: Any = Field(repr=False)
    funds: list[Coin] = Field(default_factory=list)
    layer: Literal["wasm"] = "wasm"
    network_layer: Literal["hub"] = Field(default="hub", alias="networkLayer")
    memo: str = ""

    _addresses = field_validator("sender", "contract_address")(required_string)

    @field_validator("msg")
    @classmethod
    def _json(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("An execute message is required")
        json_bytes(value)
        return value


class WasmPagination(_SafeModel):
    """One page by default; no implicit unbounded server-side scan."""

    key: bytes = b""
    limit: int = Field(default=100, gt=0, le=1000)
    offset: int = Field(default=0, ge=0, le=UINT64_MAX)
    reverse: bool = False
    count_total: bool = Field(default=False, alias="countTotal")

    def query_params(self) -> dict[str, str]:
        if self.key and self.offset:
            raise ValidationError("Pagination key cannot be combined with offset")
        result = {
            "pagination.limit": str(self.limit),
            "pagination.reverse": str(self.reverse).lower(),
            "pagination.count_total": str(self.count_total).lower(),
        }
        if self.key:
            result["pagination.key"] = base64.b64encode(self.key).decode("ascii")
        if self.offset:
            result["pagination.offset"] = str(self.offset)
        return result
