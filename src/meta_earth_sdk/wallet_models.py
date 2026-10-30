"""Frozen offline wallet models with opt-in secret export.

Account attributes intentionally expose credentials to their owner. Ordinary
repr, model_dump and JSON do not. These protections cannot erase Python memory
or revoke credentials that a caller has already exported.
"""

from __future__ import annotations

import base64
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from .models import _JsonBytes, _non_blank_token, _SafeModel

__all__ = [
    "WalletAccount", "WalletExport", "WalletImport", "AddAccountResult",
    "DirectSignDoc", "SignerAccount", "PublicKeyAny", "DirectSignature", "DirectSignResult",
]

AddressType = Literal["cosmos", "eth"]
_MAX_INDEX = (1 << 31) - 1
_MAX_UINT64 = (1 << 64) - 1
MAX_SIGN_DOC_BYTES = 1 << 20


class WalletExport(_SafeModel):
    """Six-field export; credentials require explicit ``to_secret_dict``."""

    address: str
    address_type: AddressType = Field(validation_alias="addressType", serialization_alias="addressType")
    public_key: str = Field(validation_alias="publicKey", serialization_alias="publicKey")
    mnemonic: str = Field(default="", repr=False, exclude=True)
    private_key: str = Field(default="", validation_alias="privateKey", serialization_alias="privateKey", repr=False, exclude=True)
    index: int = Field(default=0, ge=0, le=_MAX_INDEX)

    def to_secret_dict(self, *, by_alias: bool = False) -> dict[str, object]:
        """Explicitly return all six fields, including plaintext credentials."""
        return {
            "address": self.address,
            "addressType" if by_alias else "address_type": self.address_type,
            "publicKey" if by_alias else "public_key": self.public_key,
            "mnemonic": self.mnemonic,
            "privateKey" if by_alias else "private_key": self.private_key,
            "index": self.index,
        }


class WalletAccount(_SafeModel):
    """Nine public account attributes, backed by one copy of each credential.

    ``private_key`` (hex) and ``mnemonic_arr`` (tuple) are derived views rather
    than stored duplicate secrets. Like the stored credentials, neither view is
    included in default serialization. The strict constructor takes the seven
    stored fields (and their aliases), not the two derived views.
    ``pub_key_any_string`` is compressed SEC1 hex, not a serialized protobuf Any.
    """

    address: str
    address_type: AddressType = Field(validation_alias="addressType", serialization_alias="addressType")
    public_key: str = Field(validation_alias="publicKey", serialization_alias="publicKey")
    mnemonic: str = Field(default="", repr=False, exclude=True)
    private_key_buffer: bytes = Field(validation_alias="privateKeyBuffer", serialization_alias="privateKeyBuffer", repr=False, exclude=True)
    pub_key_any_string: str = Field(validation_alias="pubKeyAnyString", serialization_alias="pubKeyAnyString")
    index: int = Field(default=0, ge=0, le=_MAX_INDEX)

    @property
    def private_key(self) -> str:
        return self.private_key_buffer.hex()

    @property
    def mnemonic_arr(self) -> tuple[str, ...]:
        return tuple(self.mnemonic.split())

    # Attribute aliases for the two derived credential views. Stored fields use
    # Pydantic's usual input/output aliases, just like other SDK models.
    @property
    def privateKey(self) -> str:
        return self.private_key

    @property
    def mnemonicArr(self) -> tuple[str, ...]:
        return self.mnemonic_arr

    def export_wallet(self) -> WalletExport:
        return WalletExport(
            address=self.address,
            address_type=self.address_type,
            public_key=self.public_key,
            mnemonic=self.mnemonic,
            private_key=self.private_key,
            index=self.index,
        )

    def export(self) -> WalletExport:
        """Explicit export alias; the returned model still has a safe repr."""
        return self.export_wallet()


class AddAccountResult(_SafeModel):
    address: str
    index: int = Field(ge=0, le=_MAX_INDEX)


class WalletImport(_SafeModel):
    """Strict import shape; a non-empty mnemonic takes precedence at import.

    Credential content is validated by the crypto boundary, not coerced here.
    In particular an invalid mnemonic must never trigger private-key fallback.
    """

    mnemonic: str | None = Field(default=None, repr=False, exclude=True)
    private_key: str | None = Field(default=None, validation_alias="privateKey", serialization_alias="privateKey", repr=False, exclude=True)
    index: int = Field(default=0, ge=0, le=_MAX_INDEX)
    address_type: AddressType = Field(default="cosmos", validation_alias="addressType", serialization_alias="addressType")


class DirectSignDoc(_SafeModel):
    """Standard four-field SignDoc; body/auth bytes remain opaque.

    Python requires bytes and an integer account number. JSON uses Base64
    bytes; the explicit JS conversion uses a decimal account-number string.
    The guard counts the complete protobuf encoding, including field overhead.
    """

    body_bytes: _JsonBytes = Field(validation_alias="bodyBytes", serialization_alias="bodyBytes")
    auth_info_bytes: _JsonBytes = Field(validation_alias="authInfoBytes", serialization_alias="authInfoBytes")
    chain_id: str = Field(validation_alias="chainId", serialization_alias="chainId")
    account_number: int = Field(validation_alias="accountNumber", serialization_alias="accountNumber", ge=0, le=_MAX_UINT64)

    @field_validator("chain_id")
    @classmethod
    def _chain_id(cls, value: str) -> str:
        return _non_blank_token(value)

    @model_validator(mode="after")
    def _encoded_size(self) -> Self:
        def varint_size(value: int) -> int:
            return max(1, (value.bit_length() + 6) // 7)

        size = 0
        for length in (len(self.body_bytes), len(self.auth_info_bytes), len(self.chain_id.encode("utf-8"))):
            if length:
                size += 1 + varint_size(length) + length
        if self.account_number:
            size += 1 + varint_size(self.account_number)
        if size > MAX_SIGN_DOC_BYTES:
            raise ValueError("Encoded SignDoc is too large")
        return self

    def to_js(self) -> dict[str, object]:
        """CosmJS-shaped SignDoc, with opaque bytes and exact account number."""
        return {
            "bodyBytes": self.body_bytes,
            "authInfoBytes": self.auth_info_bytes,
            "chainId": self.chain_id,
            "accountNumber": str(self.account_number),
        }


class SignerAccount(_SafeModel):
    address: str
    algo: Literal["secp256k1"] = "secp256k1"
    pubkey: _JsonBytes = Field(min_length=33, max_length=33)

    @field_validator("pubkey")
    @classmethod
    def _compressed_key(cls, value: bytes) -> bytes:
        from .crypto.keys import validate_public_key

        try:
            return validate_public_key(value)
        except Exception:
            raise ValueError("Invalid compressed public key") from None


class PublicKeyAny(_SafeModel):
    """Explicit protobuf Any payload, without modifying descriptor pools."""

    type_url: Literal[
        "/cosmos.crypto.secp256k1.PubKey",
        "/ethermint.crypto.v1.ethsecp256k1.PubKey",
    ] = Field(validation_alias="typeUrl", serialization_alias="typeUrl")
    value: _JsonBytes = Field(min_length=35, max_length=35)

    @field_validator("value")
    @classmethod
    def _public_key_wire(cls, value: bytes) -> bytes:
        from .crypto.keys import validate_public_key

        # The admitted bytes key=1 message is precisely tag, length, SEC1.
        # Reject unknown/duplicate fields and bound input before parsing.
        if value[:2] != b"\x0a\x21":
            raise ValueError("Invalid public key payload")
        try:
            validate_public_key(value[2:])
        except Exception:
            raise ValueError("Invalid public key payload") from None
        return value


class DirectSignature(_SafeModel):
    """Raw compact r||s bytes plus the protobuf public-key metadata."""

    signature: _JsonBytes = Field(min_length=64, max_length=64)
    pub_key: PublicKeyAny = Field(validation_alias="pubKey", serialization_alias="pubKey")

    def to_js(self) -> dict[str, object]:
        # bytes key = 1 is the common public-key wire shape. Decode only this
        # standard CosmPy message, never body/auth bytes or ME descriptors.
        from cosmpy.protos.cosmos.crypto.secp256k1.keys_pb2 import PubKey

        public_key = PubKey.FromString(self.pub_key.value).key
        return {
            "pub_key": {
                "type": "tendermint/PubKeySecp256k1",
                "value": base64.b64encode(public_key).decode("ascii"),
            },
            "signature": base64.b64encode(self.signature).decode("ascii"),
        }


class DirectSignResult(_SafeModel):
    signed: DirectSignDoc
    signature: DirectSignature

    def to_js(self) -> dict[str, object]:
        """Explicit CosmJS-shaped result, not a claim of JS fixture parity."""
        return {"signed": self.signed.to_js(), "signature": self.signature.to_js()}
