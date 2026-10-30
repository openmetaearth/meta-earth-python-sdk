"""Standalone, initialization-independent wallet creation without shared state."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..crypto import keys
from ..exceptions import ValidationError
from ..wallet_models import WalletAccount
from .runtime import WalletRuntime


async def create_me_wallet(
    params: Mapping[str, Any] | None = None, *, mnemonic: str | None = None,
    priv: str | None = None, index: int = 0, address_type: str = "cosmos",
) -> WalletAccount:
    if params is not None:
        allowed = {"mnemonic", "priv", "index", "address_type", "addressType"}
        if (not isinstance(params, Mapping) or any(key not in allowed for key in params)
                or mnemonic is not None or priv is not None or index != 0
                or address_type != "cosmos"
                or ("address_type" in params and "addressType" in params)):
            raise ValidationError("Invalid standalone wallet parameters")
        mnemonic = params.get("mnemonic")
        priv = params.get("priv")
        index = params.get("index", 0)
        address_type = params.get("address_type", params.get("addressType", "cosmos"))
    if (mnemonic is None) == (priv is None):
        raise ValidationError("Provide exactly one mnemonic or private key")
    runtime = WalletRuntime(1)
    try:
        return await runtime.run(lambda: keys.make_account(
            mnemonic=mnemonic, private_key=priv, index=index, address_type=address_type
        ))
    finally:
        await runtime.aclose()


createMeWallet = create_me_wallet
