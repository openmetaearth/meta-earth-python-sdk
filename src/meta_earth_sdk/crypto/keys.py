"""Offline secp256k1 identities and fail-closed numeric BIP32 derivation.

Only English BIP39 with an empty passphrase is supported. The numeric path is
always m/44'/118'/0'/0/index for both identities; CosmPy's string path parser is
not used. Invalid BIP32 candidates fail closed rather than silently advancing
an index. No network, global wallet cache, or descriptor registration lives here.
"""

from __future__ import annotations

import hashlib
import hmac
import string
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import bech32
from cosmpy.crypto.keypairs import PrivateKey
from cosmpy.mnemonic import derive_seed_from_mnemonic, entropy_to_mnemonic, generate_entropy
from cosmpy.mnemonic.words import ENGLISH_MNEMONIC_WORDS_LIST
from Crypto.Hash import keccak
from ecdsa import SECP256k1, VerifyingKey  # type: ignore[import-untyped]

from ..exceptions import SigningError, ValidationError
from ..wallet_models import AddressType, WalletAccount

__all__ = [
    "SignerProfile", "COSMOS_PROFILE", "ETH_PROFILE", "profile_for",
    "validate_index", "validate_count", "normalize_mnemonic", "generate_mnemonic",
    "normalize_private_key", "derive_private_key", "validate_public_key",
    "public_key_to_address", "make_account", "convert_0x_to_me_address",
    "convert_me_to_0x_address",
]

SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_MAX_INDEX = (1 << 31) - 1
_HARDENED = 1 << 31
_WORD_INDEX: dict[str, int] = {word: index for index, word in enumerate(ENGLISH_MNEMONIC_WORDS_LIST)}
HmacFunction = Callable[[bytes, bytes], bytes]


@dataclass(frozen=True, slots=True)
class SignerProfile:
    """Bind address identity, Direct digest, and protobuf public-key URL."""

    address_type: AddressType
    algo: Literal["secp256k1"]
    digest: Literal["sha256", "keccak256"]
    public_key_type_url: Literal[
        "/cosmos.crypto.secp256k1.PubKey",
        "/ethermint.crypto.v1.ethsecp256k1.PubKey",
    ]
    public_key_length: Literal[33, 64]

    def hash_sign_doc(self, value: bytes) -> bytes:
        if self.digest == "sha256":
            return hashlib.sha256(value).digest()
        return _keccak256(value)


COSMOS_PROFILE = SignerProfile(
    "cosmos", "secp256k1", "sha256", "/cosmos.crypto.secp256k1.PubKey", 33
)
ETH_PROFILE = SignerProfile(
    "eth", "secp256k1", "keccak256", "/ethermint.crypto.v1.ethsecp256k1.PubKey", 64
)


def profile_for(address_type: str) -> SignerProfile:
    if address_type == "cosmos":
        return COSMOS_PROFILE
    if address_type == "eth":
        return ETH_PROFILE
    raise ValidationError("Unsupported wallet address type.") from None


def validate_index(value: object) -> int:
    if type(value) is not int or not 0 <= value <= _MAX_INDEX:
        raise ValidationError("Wallet index must be an integer between 0 and 2**31-1.") from None
    return value


def validate_count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 1000:
        raise ValidationError("Wallet count must be an integer between 0 and 1000.") from None
    return value


def normalize_mnemonic(value: object) -> str:
    """Normalize whitespace and verify the English words and BIP39 checksum."""
    if not isinstance(value, str):
        raise ValidationError("Invalid English BIP39 mnemonic.") from None
    words = value.split()
    if len(words) not in (12, 15, 18, 21, 24):
        raise ValidationError("Invalid English BIP39 mnemonic.") from None
    bits = 0
    for word in words:
        word_index = _WORD_INDEX.get(word)
        if word_index is None:
            raise ValidationError("Invalid English BIP39 mnemonic.") from None
        bits = (bits << 11) | word_index
    checksum_length = len(words) // 3
    entropy_length = len(words) * 11 - checksum_length
    entropy = (bits >> checksum_length).to_bytes(entropy_length // 8, "big")
    expected = hashlib.sha256(entropy).digest()[0] >> (8 - checksum_length)
    if bits & ((1 << checksum_length) - 1) != expected:
        raise ValidationError("Invalid English BIP39 mnemonic.") from None
    return " ".join(words)


def generate_mnemonic(word_length: int = 12) -> str:
    """Generate a 12- or 24-word English phrase without caching it."""
    if type(word_length) is not int or word_length not in (12, 24):
        raise ValidationError("Generated mnemonics must contain 12 or 24 words.") from None
    try:
        result = entropy_to_mnemonic(generate_entropy(128 if word_length == 12 else 256))
        return normalize_mnemonic(result)
    except Exception:
        raise SigningError("Could not generate a mnemonic safely.") from None


def normalize_private_key(value: object) -> bytes:
    """Validate a complete 32-byte hex scalar, stripping only outer whitespace."""
    if not isinstance(value, str):
        raise ValidationError("Invalid secp256k1 private key.") from None
    text = value.strip()
    if text.startswith(("0x", "0X")):
        text = text[2:]
    if len(text) != 64 or any(character not in string.hexdigits for character in text):
        raise ValidationError("Invalid secp256k1 private key.") from None
    raw = bytes.fromhex(text)
    if not 1 <= int.from_bytes(raw, "big") < SECP256K1_ORDER:
        raise ValidationError("Invalid secp256k1 private key.") from None
    return raw


def _hmac_sha512(key: bytes, data: bytes) -> bytes:
    return hmac.digest(key, data, "sha512")


def _hmac_parts(hmac_fn: HmacFunction, key: bytes, data: bytes) -> tuple[int, bytes]:
    output = hmac_fn(key, data)
    if not isinstance(output, bytes) or len(output) != 64:
        raise SigningError("Invalid HD derivation output.") from None
    return int.from_bytes(output[:32], "big"), output[32:]


def derive_private_key(mnemonic: str, index: int = 0, *, hmac_fn: HmacFunction | None = None) -> bytes:
    """Derive exactly the requested 118-path key, or fail without retry.

    ``hmac_fn`` is a narrow injection point for rare invalid-candidate tests;
    callers do not need or receive a mutable third-party HD wallet object.
    """
    phrase = normalize_mnemonic(mnemonic)
    account_index = validate_index(index)
    digest = hmac_fn if hmac_fn is not None else _hmac_sha512
    try:
        seed = derive_seed_from_mnemonic(phrase)
        scalar, chain_code = _hmac_parts(digest, b"Bitcoin seed", seed)
        if not 1 <= scalar < SECP256K1_ORDER:
            raise SigningError("Invalid HD master key.")
        for component in (_HARDENED | 44, _HARDENED | 118, _HARDENED, 0, account_index):
            private_bytes = scalar.to_bytes(32, "big")
            prefix = (
                b"\x00" + private_bytes
                if component & _HARDENED
                else PrivateKey(private_bytes).public_key.public_key_bytes
            )
            left, chain_code = _hmac_parts(digest, chain_code, prefix + component.to_bytes(4, "big"))
            if left >= SECP256K1_ORDER:
                raise SigningError("Invalid HD child key.")
            scalar = (left + scalar) % SECP256K1_ORDER
            if scalar == 0:
                raise SigningError("Invalid HD child key.")
        return scalar.to_bytes(32, "big")
    except Exception:
        raise SigningError("Could not derive the requested wallet key safely.") from None


def validate_public_key(value: object) -> bytes:
    """Validate compressed33 or uncompressed65 SEC1 via ecdsa's public API.

    Raw X||Y, hybrid SEC1, infinity and non-curve points are rejected. ETH's
    displayed X||Y is produced only after validating the underlying SEC1 key.
    """
    if not isinstance(value, bytes) or not (
        (len(value) == 33 and value[0] in (2, 3)) or (len(value) == 65 and value[0] == 4)
    ):
        raise ValidationError("Invalid SEC1 public key.") from None
    try:
        VerifyingKey.from_string(
            value, curve=SECP256k1, validate_point=True,
            valid_encodings={"compressed", "uncompressed"},
        )
    except Exception:
        raise ValidationError("Invalid SEC1 public key.") from None
    return value


def _public_key_forms(value: bytes) -> tuple[bytes, bytes]:
    validate_public_key(value)
    key = VerifyingKey.from_string(
        value, curve=SECP256k1, validate_point=True, valid_encodings={"compressed", "uncompressed"}
    )
    compressed: bytes = key.to_string("compressed")
    raw: bytes = key.to_string("raw")
    return compressed, raw


def _keccak256(value: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=value).digest()


def _encode_me(payload: bytes) -> str:
    converted = bech32.convertbits(payload, 8, 5)
    if converted is None:
        raise ValidationError("Invalid address payload.") from None
    encoded: str = bech32.bech32_encode("me", converted)
    return encoded


def public_key_to_address(public_key: bytes, address_type: str = "cosmos") -> str:
    profile = profile_for(address_type)
    compressed, raw = _public_key_forms(public_key)
    payload = (
        hashlib.new("ripemd160", hashlib.sha256(compressed).digest()).digest()
        if profile.address_type == "cosmos"
        else _keccak256(raw)[-20:]
    )
    return _encode_me(payload)


def convert_0x_to_me_address(value: str) -> str:
    if not isinstance(value, str) or len(value) != 42 or not value.startswith(("0x", "0X")):
        raise ValidationError("Invalid 20-byte hex address.") from None
    text = value[2:]
    if any(character not in string.hexdigits for character in text):
        raise ValidationError("Invalid 20-byte hex address.") from None
    return _encode_me(bytes.fromhex(text))


def convert_me_to_0x_address(value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError("Invalid ME address.") from None
    hrp, data = bech32.bech32_decode(value)
    if hrp != "me" or data is None:
        raise ValidationError("Invalid ME address.") from None
    payload = bech32.convertbits(data, 5, 8, False)
    if payload is None or len(payload) != 20:
        raise ValidationError("Invalid ME address.") from None
    return "0x" + bytes(payload).hex()


def make_account(
    *, mnemonic: str | None = None, private_key: str | None = None,
    index: int = 0, address_type: str = "cosmos",
) -> WalletAccount:
    """Create an immutable identity from exactly one explicit credential."""
    account_index = validate_index(index)
    profile = profile_for(address_type)
    if (mnemonic is None) == (private_key is None):
        raise ValidationError("Exactly one wallet credential is required.") from None
    phrase = normalize_mnemonic(mnemonic) if mnemonic is not None else ""
    if private_key is not None:
        raw = normalize_private_key(private_key)
        account_index = 0
    else:
        raw = derive_private_key(phrase, account_index)
    try:
        compressed, uncompressed = _public_key_forms(PrivateKey(raw).public_key.public_key_bytes)
        address = public_key_to_address(compressed, profile.address_type)
        return WalletAccount(
            address=address,
            address_type=profile.address_type,
            public_key=(compressed if profile.address_type == "cosmos" else uncompressed).hex(),
            mnemonic=phrase,
            private_key_buffer=raw,
            pub_key_any_string=compressed.hex(),
            index=account_index,
        )
    except Exception:
        raise SigningError("Could not create a wallet identity safely.") from None
