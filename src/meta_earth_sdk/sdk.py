"""Asynchronous SDK lifecycle. Initialization is not a network health check."""

from __future__ import annotations

import asyncio
from typing import Any

from .api.evm_contract import EvmContractApi
from .api.facade import SDKApi
from .config import SDKConfig
from .exceptions import ClosedError, NotInitializedError, SDKError, ValidationError
from .modules.contract import ContractService
from .modules.governance import GovernanceService
from .modules.identity import IdentityService
from .modules.staking import StakingService
from .modules.transaction import TransactionService
from .modules.wallet import WalletService
from .transaction import TransactionEngine
from .transport.runtime import QueryRuntime
from .wallet.runtime import WalletRuntime

VERSION = "0.1.0.dev0"
JS_BASELINE_VERSION = "1.0.0"
JS_BASELINE_COMMIT = "a56f3e936396de8689c2fc8667ad7306765eee9f"


class MetaEarthSDK:
    def __init__(self, config: SDKConfig | None = None) -> None:
        if config is not None and not isinstance(config, SDKConfig):
            raise ValidationError("config must be an SDKConfig")
        self._runtime = QueryRuntime(config or SDKConfig())
        self._wallet_runtime = WalletRuntime(self._runtime.config.max_pending)
        self._initialized = False
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None
        self._transactions = TransactionEngine(self._runtime)
        self._evm_contract = EvmContractApi(
            self._runtime, coordinator=self._transactions.coordinator
        )
        self.api = SDKApi(self._runtime, self._transactions, self._evm_contract)
        self.wallet = WalletService(self.api, self._guard, self._wallet_runtime)
        self.transaction = TransactionService(self._transactions, self._guard, wallet=self.wallet)
        self.identity = IdentityService(self.api.identity, self.wallet, self._guard)
        self.staking = StakingService(
            self.api.staking, self.wallet, self._guard, identity_api=self.api.identity
        )
        self.governance = GovernanceService(self.api.governance, self.wallet, self._guard)
        self.contract = ContractService(
            self.api.contract, self.wallet, self._guard, evm=self._evm_contract
        )

    async def initialize(self) -> None:
        self._runtime._check()
        self._wallet_runtime.check()
        if self._closed:
            raise ClosedError("SDK is closed; create a new instance")
        self._initialized = True

    def _guard(self) -> None:
        if self._closed:
            raise ClosedError("SDK is closing or closed")
        if not self._initialized:
            raise NotInitializedError("Call initialize() before using SDK services")

    def get_version(self) -> str:
        return VERSION

    def get_environment(self) -> str:
        return "python"

    def is_initialized(self) -> bool:
        return self._initialized

    def get_config(self) -> SDKConfig:
        return self._runtime.config

    def get_current_network(self) -> str:
        return self.get_config().network

    async def set_config(self, config: dict[str, Any]) -> None:
        self._runtime._check()
        candidate = self.get_config().validated_patch(config)
        await self._runtime.replace_config(candidate)

    async def set_network(self, network: str) -> None:
        await self.set_config({"network": network})

    async def aclose(self) -> None:
        self._runtime._bind()
        self._wallet_runtime.bind()
        if self._close_task is None:
            self._closed = True
            self._initialized = False
            self._transactions.stop()
            self._evm_contract.stop()
            self._wallet_runtime.stop()
            self._runtime.stop()
            self._close_task = asyncio.create_task(self._close())
            self._close_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None
            )
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        results = await asyncio.gather(
            self._transactions.aclose(), self._evm_contract.aclose(),
            self._runtime.aclose(), self._wallet_runtime.aclose(), return_exceptions=True
        )
        if any(isinstance(result, BaseException) for result in results):
            raise SDKError("SDK resource cleanup failed") from None

    async def __aenter__(self) -> MetaEarthSDK:
        await self.initialize()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    getVersion = get_version
    getEnvironment = get_environment
    isInitialized = is_initialized
    getConfig = get_config
    getCurrentNetwork = get_current_network
    setConfig = set_config
    setNetwork = set_network
    destroy = aclose


_default_sdk: MetaEarthSDK | None = None


def get_default_sdk() -> MetaEarthSDK:
    """Create a compatibility instance lazily; explicit SDKs never share its runtime."""
    global _default_sdk
    if _default_sdk is None or _default_sdk._closed:
        _default_sdk = MetaEarthSDK()
    return _default_sdk
