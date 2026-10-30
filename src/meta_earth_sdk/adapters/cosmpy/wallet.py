"""Narrow synchronous CosmPy adapter for offline standard SignDoc signing.

No ME schemas, default-pool changes, Any resolution, or transaction construction
are required. The caller owns worker admission and signer lifetime.
"""

from __future__ import annotations

from cosmpy.crypto.keypairs import PrivateKey
from cosmpy.protos.cosmos.crypto.secp256k1.keys_pb2 import PubKey
from cosmpy.protos.cosmos.tx.v1beta1.tx_pb2 import SignDoc

from ...crypto.keys import (
    SECP256K1_ORDER,
    make_account,
    profile_for,
    validate_public_key,
)
from ...exceptions import SigningError, ValidationError
from ...wallet_models import (
    MAX_SIGN_DOC_BYTES,
    DirectSignature,
    DirectSignDoc,
    DirectSignResult,
    PublicKeyAny,
    WalletAccount,
)

__all__ = ["sign_direct_record", "get_public_key_any"]


def _checked_account(account: WalletAccount) -> WalletAccount:
    """Recompute the public identity instead of trusting an editable record."""
    if not isinstance(account, WalletAccount):
        raise SigningError("Invalid wallet signing record.") from None
    try:
        derived = make_account(private_key=account.private_key, address_type=account.address_type)
        validate_public_key(bytes.fromhex(account.pub_key_any_string))
        if (
            derived.address != account.address
            or derived.public_key != account.public_key
            or derived.pub_key_any_string != account.pub_key_any_string
        ):
            raise ValueError
    except Exception:
        raise SigningError("Wallet key does not match its public identity.") from None
    return derived


def get_public_key_any(account: WalletAccount) -> PublicKeyAny:
    checked = _checked_account(account)
    profile = profile_for(checked.address_type)
    return PublicKeyAny(
        type_url=profile.public_key_type_url,
        value=PubKey(key=bytes.fromhex(checked.pub_key_any_string)).SerializeToString(deterministic=True),
    )


def sign_direct_record(
    account: WalletAccount, signer_address: str, sign_doc: DirectSignDoc
) -> DirectSignResult:
    """Sign one opaque, bounded standard SignDoc with the account's profile.

    RFC6979 retains CosmPy's SHA-256 nonce configuration for both profiles.
    Cosmos hashes SignDoc once with SHA-256, ETH-derived once with Keccak-256.
    The returned signature is low-S compact r||s, never DER or recovery bytes.
    """
    if not isinstance(signer_address, str) or signer_address != account.address:
        raise ValidationError("Signer address does not match the wallet.") from None
    if not isinstance(sign_doc, DirectSignDoc):
        raise ValidationError("A validated DirectSignDoc is required.") from None
    checked = _checked_account(account)
    try:
        # Revalidate at this security boundary, including objects made with
        # Pydantic's deliberately unchecked model_construct/model_copy APIs.
        document = DirectSignDoc(
            body_bytes=sign_doc.body_bytes,
            auth_info_bytes=sign_doc.auth_info_bytes,
            chain_id=sign_doc.chain_id,
            account_number=sign_doc.account_number,
        )
        wire = SignDoc(
            body_bytes=document.body_bytes,
            auth_info_bytes=document.auth_info_bytes,
            chain_id=document.chain_id,
            account_number=document.account_number,
        ).SerializeToString(deterministic=True)
        if len(wire) > MAX_SIGN_DOC_BYTES:
            raise ValueError
        digest = profile_for(checked.address_type).hash_sign_doc(wire)
        signature = PrivateKey(checked.private_key_buffer).sign_digest(
            digest, deterministic=True, canonicalise=True
        )
        if (
            len(signature) != 64
            or not 1 <= int.from_bytes(signature[:32], "big") < SECP256K1_ORDER
            or not 1 <= int.from_bytes(signature[32:], "big") <= SECP256K1_ORDER // 2
        ):
            raise ValueError
        return DirectSignResult(
            signed=document,
            signature=DirectSignature(signature=signature, pub_key=get_public_key_any(checked)),
        )
    except Exception:
        raise SigningError("Could not sign the document safely.") from None
