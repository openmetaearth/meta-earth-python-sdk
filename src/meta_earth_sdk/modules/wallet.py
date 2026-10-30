"""Initialized wallet management and the existing wallet-free read facade."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError as PydanticValidationError

from ..api.wallet import WalletApi
from ..crypto import keys
from ..exceptions import ValidationError
from ..models import BalancePage, Coin
from ..wallet.evm import EvmSigner, checked_evm_account
from ..wallet.runtime import WalletRuntime
from ..wallet.signers import DirectSigner
from ..wallet_models import AddAccountResult, WalletAccount, WalletExport, WalletImport


class WalletService:
    def __init__(
        self, api: WalletApi, guard: Callable[[], None], runtime: WalletRuntime
    ) -> None:
        self._api = api
        self._guard = guard
        self._wallet_runtime = runtime

    async def generate_mnemonic(self, word_length: int = 12) -> str:
        self._guard()
        return await self._wallet_runtime.run(lambda: keys.generate_mnemonic(word_length))

    async def create_mnemonic_wallet(
        self, mnemonic: str | None = None, index: int = 0, address_type: str = "cosmos"
    ) -> WalletAccount:
        self._guard()

        def create() -> WalletAccount:
            phrase = keys.generate_mnemonic() if mnemonic is None else mnemonic
            return keys.make_account(mnemonic=phrase, index=index, address_type=address_type)

        account = await self._wallet_runtime.run(create)
        self._wallet_runtime.publish([account])
        return account

    async def create_private_key_wallet(
        self, private_key: str, address_type: str = "cosmos"
    ) -> WalletAccount:
        self._guard()
        account = await self._wallet_runtime.run(
            lambda: keys.make_account(private_key=private_key, address_type=address_type)
        )
        self._wallet_runtime.publish([account])
        return account

    async def batch_create_wallets(
        self, mnemonic: str, count: int, start_index: int = 0, address_type: str = "cosmos"
    ) -> list[WalletAccount]:
        self._guard()

        def create() -> list[WalletAccount]:
            size = keys.validate_count(count)
            start = keys.validate_index(start_index)
            phrase = keys.normalize_mnemonic(mnemonic)
            keys.profile_for(address_type)
            if size and start + size - 1 >= 2**31:
                raise ValidationError("Batch account index range is too large")
            return [keys.make_account(
                mnemonic=phrase, index=start + offset, address_type=address_type
            ) for offset in range(size)]

        accounts = await self._wallet_runtime.run(create)
        self._wallet_runtime.publish(accounts)
        return accounts

    async def add_account(self, mnemonic: str, index: int) -> AddAccountResult:
        self._guard()
        phrase = await self._wallet_runtime.run(lambda: keys.normalize_mnemonic(mnemonic))
        account = await self.create_mnemonic_wallet(phrase, index)
        return AddAccountResult(address=account.address, index=account.index)

    async def import_wallet(self, data: WalletImport | Mapping[str, Any]) -> WalletAccount:
        self._guard()
        self._wallet_runtime.check()
        try:
            request = WalletImport.model_validate(data)
        except PydanticValidationError:
            raise ValidationError("Invalid wallet import parameters") from None
        if request.mnemonic is not None and request.mnemonic != "":
            return await self.create_mnemonic_wallet(
                request.mnemonic, request.index, request.address_type
            )
        if request.private_key is not None:
            return await self.create_private_key_wallet(request.private_key, request.address_type)
        raise ValidationError("A mnemonic or private key is required")

    async def export_wallet(self, address: str) -> WalletExport:
        self._guard()
        return self._wallet_runtime.require(address).export_wallet()

    def get_wallet_addresses(self) -> list[str]:
        self._guard()
        return self._wallet_runtime.addresses()

    @staticmethod
    def convert_0x_to_me_address(address: str) -> str:
        return keys.convert_0x_to_me_address(address)

    @staticmethod
    def convert_me_to_0x_address(address: str) -> str:
        return keys.convert_me_to_0x_address(address)

    async def create_direct_secp256k1_wallet(
        self, data: Mapping[str, Any] | None = None, *, address: str | None = None,
        private_key: str | None = None, mnemonic: str | None = None,
    ) -> DirectSigner:
        self._guard()
        self._wallet_runtime.check()
        if data is not None:
            if (not isinstance(data, Mapping)
                    or any(key not in {"address", "private_key", "privateKey", "mnemonic"} for key in data)
                    or address is not None or private_key is not None or mnemonic is not None
                    or ("private_key" in data and "privateKey" in data)):
                raise ValidationError("Invalid Direct signer parameters")
            address = data.get("address")
            private_key = data.get("private_key", data.get("privateKey"))
            mnemonic = data.get("mnemonic")
        cached = self._wallet_runtime.find(address) if address is not None else None
        if cached is not None:
            # Revalidate the immutable record before publishing a signer capability.
            account = await self._wallet_runtime.run(lambda: keys.make_account(
                private_key=cached.private_key, address_type=cached.address_type
            ))
            if account.address != cached.address:
                raise ValidationError("Cached wallet credentials do not match its address")
        elif private_key is not None:
            account = await self._wallet_runtime.run(
                lambda: keys.make_account(private_key=private_key)
            )
        elif mnemonic is not None:
            account = await self._wallet_runtime.run(
                lambda: keys.make_account(mnemonic=mnemonic)
            )
        else:
            raise ValidationError("A cached wallet or explicit credential is required")
        capability = self._wallet_runtime.register(account)
        return DirectSigner(self._wallet_runtime, capability)

    async def create_evm_wallet(
        self, address: str, provider: object | None = None,
    ) -> EvmSigner:
        """Create a local-only capability for an existing cached ETH identity."""
        self._guard()
        cached = self._wallet_runtime.require(address)
        account, checksum_address = await self._wallet_runtime.run(
            lambda: checked_evm_account(cached)
        )
        if account.address != address:
            raise ValidationError("Cached wallet address does not match the request")
        capability = self._wallet_runtime.register(account)
        return EvmSigner(self._wallet_runtime, capability, checksum_address, provider)

    async def get_balance(self, address: str, layer: str = "hub") -> list[Coin]:
        self._guard()
        return await self._api.get_balance(address, layer)

    async def get_balances_page(
        self, address: str, layer: str = "hub", **kwargs: Any
    ) -> BalancePage:
        self._guard()
        return await self._api.get_balances_page(address, layer, **kwargs)

    async def get_all_balances(
        self, address: str, layer: str = "hub", **kwargs: Any
    ) -> list[Coin]:
        self._guard()
        return await self._api.get_all_balances(address, layer, **kwargs)

    async def get_node_version(self, layer: str = "hub") -> Any:
        self._guard()
        return await self._api.get_node_version(layer)

    async def get_network_status(self, layer: str = "hub") -> Any:
        self._guard()
        return await self._api.get_network_status(layer)

    generateMnemonic = generate_mnemonic
    createMnemonicWallet = create_mnemonic_wallet
    createPrivateKeyWallet = create_private_key_wallet
    batchCreateWallets = batch_create_wallets
    addAccount = add_account
    importWallet = import_wallet
    exportWallet = export_wallet
    getWalletAddresses = get_wallet_addresses
    convert0xToMeAddress = convert_0x_to_me_address
    convertMeTo0xAddress = convert_me_to_0x_address
    createDirectSecp256k1Wallet = create_direct_secp256k1_wallet
    createEvmWallet = create_evm_wallet
    getBalance = get_balance
    getBalancesPage = get_balances_page
    getAllBalances = get_all_balances
    getNodeVersion = get_node_version
    getNetworkStatus = get_network_status
