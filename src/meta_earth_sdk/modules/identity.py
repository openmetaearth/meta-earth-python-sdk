"""Initialized identity facade; never exports wallet secret material."""
from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping
from typing import Any

from ..api.identity import IdentityApi
from ..business_models import BindSubAccountParams, MeIdLookupResult, checked_model
from ..exceptions import ValidationError
from ..transaction_models import TransactionOptions
from .wallet import WalletService


class IdentityService:
    def __init__(self, api: IdentityApi, wallet: WalletService, guard: Callable[[], None]) -> None:
        self._api = api
        self._wallet = wallet
        self._guard = guard

    async def bind_sub_account(
        self, params: BindSubAccountParams | Mapping[str, Any],
        *, options: TransactionOptions | None = None,
    ) -> str:
        self._guard()
        request = checked_model(BindSubAccountParams, params)
        creator = await self._wallet.create_direct_secp256k1_wallet(address=request.creator)
        sub = await self._wallet.create_direct_secp256k1_wallet(address=request.sub_account)
        if (creator.public_key_type_url != "/cosmos.crypto.secp256k1.PubKey"
                or sub.public_key_type_url != "/ethermint.crypto.v1.ethsecp256k1.PubKey"):
            raise ValidationError("Binding requires a Cosmos creator and ETH sub-account")
        main_accounts, sub_accounts = await creator.get_accounts(), await sub.get_accounts()
        if (len(main_accounts) != 1 or len(sub_accounts) != 1
                or not main_accounts[0].pubkey
                or main_accounts[0].pubkey != sub_accounts[0].pubkey):
            raise ValidationError("Binding requires accounts with the same secp256k1 public key")
        key_json = json.dumps({
            "@type": sub.public_key_type_url,
            "key": base64.b64encode(sub_accounts[0].pubkey).decode("ascii"),
        }, separators=(",", ":"))
        return await self._api.bind_sub_account({
            **request.model_dump(), "sub_account_pubkey": key_json,
        }, creator, options=options)

    async def get_me_id_by_address(self, address: str, layer: str = "hub") -> MeIdLookupResult:
        self._guard()
        self._check_text(address)
        return await self._api.get_me_id_by_address(address, layer)

    async def get_me_id_by_did(self, did: str, layer: str = "hub") -> MeIdLookupResult:
        self._guard()
        self._check_text(did)
        return await self._api.get_me_id_by_did(did, layer)

    async def get_me_id_by_sub_account(self, sub_account: str, layer: str = "hub") -> MeIdLookupResult:
        self._guard()
        self._check_text(sub_account)
        return await self._api.get_me_id_by_sub_account(sub_account, layer)

    @staticmethod
    def _check_text(value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("A nonblank ME ID lookup value is required")

    bindSubAccount = bind_sub_account
    getMeIdByAddress = get_me_id_by_address
    getMeIdByDid = get_me_id_by_did
    getMeIdBySubAccount = get_me_id_by_sub_account
