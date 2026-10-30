"""Offline Direct capability; key ownership remains with the wallet runtime."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from ..adapters.cosmpy.wallet import get_public_key_any, sign_direct_record
from ..exceptions import ValidationError
from ..wallet_models import DirectSignDoc, DirectSignResult, PublicKeyAny, SignerAccount
from .runtime import WalletRuntime


class DirectSigner:
    def __init__(self, runtime: WalletRuntime, capability: int) -> None:
        self._runtime = runtime
        self._capability = capability

    def __repr__(self) -> str:
        return "DirectSigner(<offline capability>)"

    @property
    def public_key_type_url(self) -> str:
        return self.public_key_any.type_url

    @property
    def publicKeyTypeUrl(self) -> str:
        return self.public_key_type_url

    @property
    def public_key_any(self) -> PublicKeyAny:
        return get_public_key_any(self._runtime.resolve(self._capability))

    async def get_accounts(self) -> list[SignerAccount]:
        account = self._runtime.resolve(self._capability)
        return [SignerAccount(
            address=account.address,
            algo="secp256k1",
            pubkey=bytes.fromhex(account.pub_key_any_string),
        )]

    async def sign_direct(
        self, signer_address: str, sign_doc: DirectSignDoc | Mapping[str, Any]
    ) -> DirectSignResult:
        account = self._runtime.resolve(self._capability)
        try:
            document = DirectSignDoc.model_validate(sign_doc)
        except PydanticValidationError:
            raise ValidationError("Invalid Direct SignDoc") from None
        return await self._runtime.run(
            lambda: sign_direct_record(account, signer_address, document)
        )

    getAccounts = get_accounts
    signDirect = sign_direct
