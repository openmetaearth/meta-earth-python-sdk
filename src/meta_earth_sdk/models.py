"""Strict public bank models and shared privacy-safe model validation."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Annotated, Any, Self

from pydantic import (
    AliasChoices,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic import (
    ValidationError as PydanticValidationError,
)
from pydantic_core import InitErrorDetails, PydanticCustomError

__all__ = ["Coin", "BalanceInfo", "BalancePage", "Pagination"]

_UINT64_MAX = (1 << 64) - 1


def _json_bytes(value: Any, info: ValidationInfo) -> Any:
    # Model wrap validators receive JSON as Python values. Decode only in JSON
    # mode; Python callers must still supply actual bytes under strict validation.
    if info.mode == "json" and isinstance(value, str):
        try:
            return base64.b64decode(value, altchars=b"-_", validate=True)
        except (ValueError, binascii.Error):
            raise ValueError("Expected Base64 bytes") from None
    return value


_JsonBytes = Annotated[bytes, BeforeValidator(_json_bytes)]


class _SafeModel(BaseModel):
    """Frozen strict models with redacted public model-validation errors.

    ``hide_input_in_errors`` protects string/repr formatting. Model validation
    and frozen mutation boundaries also redact structured error inputs, custom
    contexts and unknown-field locations, which may contain credentials.
    Boundary callers can translate this to SDK ValidationError with ``from None``.
    """

    model_config = ConfigDict(
        
        strict=True,
        extra="forbid",
        frozen=True,
        hide_input_in_errors=True,
        populate_by_name=True,
        validate_default=True,
        ser_json_bytes="base64",
        val_json_bytes="base64",
    )

    @classmethod
    def _safe_validation_error(cls, exc: PydanticValidationError) -> PydanticValidationError:
        # Known field/alias locations are safe. Arbitrary keys and validator
        # contexts may themselves contain credentials.
        safe_names: set[str] = set()
        for field_name, field in cls.model_fields.items():
            safe_names.add(field_name)
            if field.alias:
                safe_names.add(field.alias)
            if isinstance(field.validation_alias, str):
                safe_names.add(field.validation_alias)
            elif isinstance(field.validation_alias, AliasChoices):
                safe_names.update(
                    alias for alias in field.validation_alias.choices if isinstance(alias, str)
                )
        errors: list[InitErrorDetails] = []
        for detail in exc.errors(include_url=False, include_context=False, include_input=False):
            location = tuple(
                part if isinstance(part, int) or part in safe_names else "<field>"
                for part in detail["loc"]
            )
            errors.append({
                "type": PydanticCustomError("invalid_value", "Invalid value"),
                "loc": location,
                "input": None,
            })
        return PydanticValidationError.from_exception_data(cls.__name__, errors, hide_input=True)

    @model_validator(mode="wrap")
    @classmethod
    def _redact_validation_errors(cls, value: Any, handler: Any) -> Any:
        try:
            return handler(value)
        except PydanticValidationError as exc:
            raise cls._safe_validation_error(exc) from None

    @classmethod
    def model_validate(cls, obj: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate(obj, **kwargs)
        except PydanticValidationError as exc:
            raise cls._safe_validation_error(exc) from None

    @classmethod
    def model_validate_json(cls, json_data: str | bytes | bytearray, **kwargs: Any) -> Self:
        # JSON syntax errors occur before model wrap validators run.
        try:
            return super().model_validate_json(json_data, **kwargs)
        except PydanticValidationError as exc:
            raise cls._safe_validation_error(exc) from None

    @classmethod
    def model_validate_strings(cls, obj: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate_strings(obj, **kwargs)
        except PydanticValidationError as exc:
            raise cls._safe_validation_error(exc) from None

    def __setattr__(self, name: str, value: Any) -> None:
        # Frozen assignment/deletion also bypass the model wrap validator.
        try:
            super().__setattr__(name, value)
        except PydanticValidationError as exc:
            raise self._safe_validation_error(exc) from None

    def __delattr__(self, item: str) -> None:
        try:
            super().__delattr__(item)
        except PydanticValidationError as exc:
            raise self._safe_validation_error(exc) from None


def _non_blank_token(value: str) -> str:
    if not value or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Expected a non-empty value without whitespace")
    return value


class Coin(_SafeModel):
    """A denomination and an exact, arbitrary-precision unsigned amount.

    Amounts are strings, not numbers: neither floats nor integers are coerced.
    The original decimal spelling (including leading zeros) is preserved.
    """

    amount: str = Field(validation_alias="amount", serialization_alias="amount")
    denom: str = Field(validation_alias="denom", serialization_alias="denom")

    @field_validator("amount")
    @classmethod
    def _validate_amount(cls, value: str) -> str:
        if re.fullmatch(r"[0-9]+", value) is None:
            raise ValueError("Expected a non-negative integer string")
        return value

    @field_validator("denom")
    @classmethod
    def _validate_denom(cls, value: str) -> str:
        return _non_blank_token(value)


# Compatibility name, intentionally the same class (no divergent validation).
BalanceInfo = Coin


class BalancePage(_SafeModel):
    """A bank page; input and serialized lists are defensively copied.

    The page is frozen, while ``balances`` remains an ordinary per-page list.
    It is not shared with the caller's list or with another page. Coin values
    are frozen. JSON encodes next_key as URL-safe Base64, as Pydantic specifies.
    """

    balances: list[Coin] = Field(
        default_factory=list, validation_alias="balances", serialization_alias="balances"
    )
    next_key: _JsonBytes = Field(
        default=b"",
        validation_alias=AliasChoices("nextKey", "next_key"),
        serialization_alias="nextKey",
    )
    total: int = Field(
        default=0,
        ge=0,
        le=_UINT64_MAX,
        validation_alias="total",
        serialization_alias="total",
    )


class Pagination(_SafeModel):
    """Optional bank-query pagination, accepted by APIs as keyword fields.

    ``key`` is raw bytes in Python and Base64 in JSON. A non-empty key cannot
    be combined with a positive offset, and the page limit must be positive.
    """

    key: _JsonBytes = Field(default=b"", validation_alias="key", serialization_alias="key")
    limit: int = Field(
        default=100,
        gt=0,
        le=_UINT64_MAX,
        validation_alias="limit",
        serialization_alias="limit",
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=_UINT64_MAX,
        validation_alias="offset",
        serialization_alias="offset",
    )
    reverse: bool = Field(default=False, validation_alias="reverse", serialization_alias="reverse")
    count_total: bool = Field(
        default=False,
        validation_alias=AliasChoices("countTotal", "count_total"),
        serialization_alias="countTotal",
    )

    @field_validator("offset")
    @classmethod
    def _validate_key_offset(cls, value: int, info: ValidationInfo) -> int:
        if value > 0 and info.data.get("key", b""):
            raise ValueError("Pagination key and offset are mutually exclusive")
        return value
