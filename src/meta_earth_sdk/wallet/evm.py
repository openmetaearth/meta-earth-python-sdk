"""Controlled local eth-account signing; no RPC, provider methods or send API."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from Crypto.Hash import keccak
from eth_account import Account
from eth_account.types import TransactionDictType

from ..crypto import keys
from ..evm_models import EvmSignResult, EvmTransaction
from ..exceptions import SigningError, ValidationError
from ..wallet_models import WalletAccount
from .runtime import WalletRuntime

__all__ = ["EvmSigner"]


def checked_evm_account(account: WalletAccount) -> tuple[WalletAccount, str]:
    """Validate even unchecked model_construct/model_copy records and rederive.

    Do not trust cached profile, address or displayed keys. eth-account receives
    the existing 118-path private key, never a mnemonic or an ETH/60 HD path.
    """
    try:
        if not isinstance(account, WalletAccount):
            raise ValueError
        # Iterating the model preserves excluded credential fields and catches
        # unknown __dict__ keys inserted through unchecked model_copy updates.
        record = WalletAccount(**dict(account))
        if record.address_type != "eth":
            raise ValueError
        derived = keys.make_account(private_key=record.private_key, address_type="eth")
        local = Account.from_key(derived.private_key_buffer)
        if (derived.address != record.address or derived.public_key != record.public_key
                or derived.pub_key_any_string != record.pub_key_any_string
                or keys.convert_me_to_0x_address(record.address).lower() != local.address.lower()):
            raise ValueError
        return derived, local.address
    except Exception:
        raise SigningError("Cached wallet is not a valid ETH signing identity") from None


def _transaction(value: EvmTransaction | Mapping[str, Any]) -> EvmTransaction:
    try:
        # revalidate_instances=always also rechecks deliberately unchecked model
        # construction/copies; no excluded model_dump fields are lost here.
        return EvmTransaction.model_validate(value)
    except Exception:
        raise ValidationError("Invalid explicit EVM transaction") from None


def sign_evm_record(account: WalletAccount, transaction: EvmTransaction) -> EvmSignResult:
    checked, address = checked_evm_account(account)
    document = _transaction(transaction)
    if document.from_address is not None and document.from_address.lower() != address.lower():
        raise ValidationError("Transaction sender does not match the wallet") from None
    try:
        local = Account.from_key(checked.private_key_buffer)
        signed = local.sign_transaction(cast(TransactionDictType, document.to_transaction_dict()))
        raw = bytes(signed.raw_transaction)
        transaction_hash = bytes(signed.hash)
        expected_v = (
            (0, 1) if document.type == 2
            else (document.chain_id * 2 + 35, document.chain_id * 2 + 36)
        )
        if (not raw or (document.type == 2 and raw[0] != 2)
                or (document.type == 0 and raw[0] < 0xC0)
                or type(signed.r) is not int or not 1 <= signed.r < keys.SECP256K1_ORDER
                or type(signed.s) is not int or not 1 <= signed.s <= keys.SECP256K1_ORDER // 2
                or type(signed.v) is not int or signed.v not in expected_v
                or transaction_hash != keccak.new(digest_bits=256, data=raw).digest()
                or Account.recover_transaction(raw) != address):
            raise ValueError
        return EvmSignResult(raw_transaction=raw, hash=transaction_hash,
                             r=signed.r, s=signed.s, v=signed.v)
    except Exception:
        raise SigningError("Could not sign the EVM transaction safely") from None


class EvmSigner:
    """Runtime-owned ETH capability, not an ethers/Web3 provider wrapper.

    The optional provider is borrowed only: no method is called, no reference is
    exposed by the public API, and SDK closure never closes the external object.
    """

    def __init__(self, runtime: WalletRuntime, capability: int, address: str,
                 provider: object | None = None) -> None:
        self._runtime = runtime
        self._capability = capability
        self._address = address
        self._provider = provider

    def __repr__(self) -> str:
        return "EvmSigner(<offline capability>)"

    @property
    def address(self) -> str:
        self._runtime.resolve_snapshot(self._capability)
        return self._address

    async def sign_transaction(
        self, transaction: EvmTransaction | Mapping[str, Any]
    ) -> EvmSignResult:
        account = self._runtime.resolve(self._capability)
        document = _transaction(transaction)
        return await self._runtime.run(lambda: sign_evm_record(account, document))

    signTransaction = sign_transaction
