"""ME ID queries and same-public-key binding, through the shared Hub engine."""
from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..business_models import (
    CreateSubAccountParams,
    MeIdInfo,
    MeIdLookupResult,
    checked_model,
    make_message,
    require_hub,
    signer_address,
    transaction_hash,
    validate_response,
)
from ..crypto.keys import public_key_to_address
from ..exceptions import NetworkError, ProtocolError, ValidationError
from ..wallet.signers import DirectSigner

if TYPE_CHECKING:
    from ..transaction import TransactionContext, TransactionEngine
    from ..transaction_models import TransactionOptions

_ETH_KEY = "/ethermint.crypto.v1.ethsecp256k1.PubKey"


def normalize_me_id(data: Any, response_type: str) -> MeIdLookupResult:
    if not isinstance(data, Mapping):
        raise ProtocolError("Invalid ME ID response")
    info = data.get("info")
    if info is None:
        return MeIdLookupResult(hasMeId=False, info=None)
    if not isinstance(info, Mapping):
        raise ProtocolError("Invalid ME ID response")
    did = info.get("did")
    if did is None or did == "":
        # A malformed nonempty object is not silently converted into absence.
        validate_response(response_type, data)
        return MeIdLookupResult(hasMeId=False, info=None)
    normalized = dict(info)
    if "subAccount" in normalized:
        normalized["sub_account"] = normalized.pop("subAccount")
    validate_response(response_type, {**data, "info": normalized})
    # ProtoJSON permits enum numbers. Public values are the exact enum names.
    from ..protocol import get_registry

    decoded = get_registry().parse_json("metaearth.did.DidInfo", normalized)
    canonical = get_registry().to_dict(decoded)
    result = checked_model(MeIdInfo, {
        "did": did,
        "address": canonical.get("address", ""),
        "pubkey": canonical.get("pubkey", ""),
        "status": canonical.get("status", "") if normalized.get("status") is not None else "",
        "regionId": normalized.get("regionId") or "",
        "kycLevel": canonical.get("kycLevel", "") if normalized.get("kycLevel") is not None else "",
        "subAccount": canonical.get("sub_account", ""),
    }, remote=True)
    return MeIdLookupResult(hasMeId=True, info=result)


class IdentityApi:
    def __init__(self, engine: TransactionEngine) -> None:
        self._engine = engine

    async def bind_sub_account(
        self, params: CreateSubAccountParams | Mapping[str, Any], signer: DirectSigner,
        *, options: TransactionOptions | None = None,
    ) -> str:
        request = checked_model(CreateSubAccountParams, params)
        await signer_address(signer, request.creator)
        # The lower-level API is also a security boundary: do not let callers
        # bypass the Service's same-secp-key or account-type checks.
        accounts = await signer.get_accounts()
        key = accounts[0].pubkey
        if (signer.public_key_type_url != "/cosmos.crypto.secp256k1.PubKey"
                or public_key_to_address(key, "cosmos") != request.creator
                or public_key_to_address(key, "eth") != request.sub_account):
            raise ValidationError("Binding requires the same public key for Cosmos and ETH accounts")
        try:
            provided = json.loads(request.sub_account_pubkey)
            if (not isinstance(provided, dict) or set(provided) != {"@type", "key"}
                    or provided["@type"] != _ETH_KEY
                    or base64.b64decode(provided["key"], validate=True) != key):
                raise ValueError
        except (ValueError, TypeError, UnicodeError):
            raise ValidationError("Invalid sub-account public key") from None
        message = make_message("metaearth.kyc.MsgCreateSubAccount", {
            "creator": request.creator,
            "subAccount": request.sub_account,
            "subAccountPubkey": request.sub_account_pubkey,
        })
        result = await self._engine.execute(
            [message], signer, operation="bind", memo=request.memo, options=options,
        )
        return transaction_hash(result)

    async def _lookup(
        self, path: str, params: Mapping[str, str], response_type: str,
        *, context: TransactionContext | None = None,
    ) -> MeIdLookupResult:
        try:
            requester = context if context is not None else self._engine
            data = await requester.request(path, params=params)
        except NetworkError as exc:
            # Transport recognizes the two bounded known missing messages only
            # on the exact identity routes. A plain 404 is NOT a missing ME ID.
            if exc.operation == "me_id_not_found":
                return MeIdLookupResult(hasMeId=False, info=None)
            raise
        return normalize_me_id(data, response_type)

    async def get_me_id_by_address(
        self, address: str, layer: str = "hub", *, context: TransactionContext | None = None,
    ) -> MeIdLookupResult:
        require_hub(layer)
        return await self._lookup("/metaearth/did/did", {"address": address},
                                  "metaearth.did.QueryDidResponse", context=context)

    async def get_me_id_by_did(
        self, did: str, layer: str = "hub", *, context: TransactionContext | None = None,
    ) -> MeIdLookupResult:
        require_hub(layer)
        return await self._lookup("/metaearth/did/didInfo", {"did": did},
                                  "metaearth.did.QueryDidInfoResponse", context=context)

    async def get_me_id_by_sub_account(
        self, sub_account: str, layer: str = "hub", *, context: TransactionContext | None = None,
    ) -> MeIdLookupResult:
        require_hub(layer)
        return await self._lookup("/metaearth/kyc/QuerySubAccountDidResponse",
                                  {"sub_account": sub_account},
                                  "metaearth.kyc.QuerySubAccountDidResponse", context=context)

    bindSubAccount = bind_sub_account
    getMeIdByAddress = get_me_id_by_address
    getMeIdByDid = get_me_id_by_did
    getMeIdBySubAccount = get_me_id_by_sub_account
