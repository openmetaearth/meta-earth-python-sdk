"""Initialized Hub WASM facade and optional instance-owned EVM composition."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from ..api.contract import ContractApi
from ..business_models import checked_model
from ..contract_models import (
    DeployContractParams,
    ExecuteContractParams,
    StoreCodeParams,
    WasmPagination,
)
from ..exceptions import ValidationError
from ..transaction_models import TransactionOptions
from .wallet import WalletService

if TYPE_CHECKING:
    from ..api.evm_contract import EvmContractApi
    from ..evm_contract_models import (
        DeployEvmContractParams,
        EvmContractInfo,
        EvmDeploymentResult,
        EvmExecutionResult,
        ExecuteEvmContractParams,
        QueryEvmContractParams,
    )


class ContractService:
    def __init__(
        self, api: ContractApi, wallet: WalletService, guard: Callable[[], None],
        *, evm: EvmContractApi | None = None,
    ) -> None:
        self._api = api
        self._wallet = wallet
        self._guard = guard
        self._evm = evm

    def attach_evm(self, evm: EvmContractApi) -> None:
        """Attach the same SDK instance's EVM API, without creating a provider."""
        if self._evm is not None and self._evm is not evm:
            raise ValidationError("An EVM contract API is already attached")
        self._evm = evm

    async def store_code(
        self, params: StoreCodeParams | Mapping[str, Any], *, options: TransactionOptions | None = None,
    ) -> str:
        self._guard()
        request = checked_model(StoreCodeParams, params)
        signer = await self._wallet.create_direct_secp256k1_wallet(address=request.sender)
        return await self._api.create_store_code(request, signer, options=options)

    async def deploy_contract(
        self, params: DeployContractParams | Mapping[str, Any], *, options: TransactionOptions | None = None,
    ) -> str:
        self._guard()
        request = checked_model(DeployContractParams, params)
        signer = await self._wallet.create_direct_secp256k1_wallet(address=request.sender)
        return await self._api.create_instantiate_contract(request, signer, options=options)

    async def execute_contract(
        self, params: ExecuteContractParams | Mapping[str, Any], *, options: TransactionOptions | None = None,
    ) -> str:
        self._guard()
        request = checked_model(ExecuteContractParams, params)
        signer = await self._wallet.create_direct_secp256k1_wallet(address=request.sender)
        return await self._api.create_execute_contract(request, signer, options=options)

    async def get_code_id_by_hash(
        self, data_hash: str, layer: str = "hub", *,
        pagination: WasmPagination | Mapping[str, Any] | None = None, max_pages: int = 1,
    ) -> str | None:
        self._guard()
        return await self._api.get_code_id_by_hash(data_hash, layer, pagination=pagination, max_pages=max_pages)

    async def get_contracts_by_code_id(
        self, code_id: int, layer: str = "hub", *,
        pagination: WasmPagination | Mapping[str, Any] | None = None,
    ) -> Any:
        self._guard()
        return await self._api.get_contracts_by_code_id(code_id, layer, pagination=pagination)

    async def get_contracts_by_creator(
        self, creator_address: str, layer: str = "hub", *,
        pagination: WasmPagination | Mapping[str, Any] | None = None,
    ) -> Any:
        self._guard()
        return await self._api.get_contracts_by_creator(creator_address, layer, pagination=pagination)

    async def query_contract_smart(self, contract_address: str, query_data: Any, layer: str = "hub") -> Any:
        self._guard()
        return await self._api.query_contract_smart(contract_address, query_data, layer)

    async def get_contract_info(self, contract_address: str, layer: str = "hub") -> Any:
        self._guard()
        return await self._api.get_contract_info(contract_address, layer)

    def _require_evm(self) -> EvmContractApi:
        if self._evm is None:
            raise ValidationError("An EVM contract API must be configured")
        return self._evm

    async def deploy_evm_contract(
        self, params: DeployEvmContractParams | Mapping[str, Any],
    ) -> EvmDeploymentResult:
        self._guard()
        from ..evm_contract_models import DeployEvmContractParams

        request = checked_model(DeployEvmContractParams, params)
        api = self._require_evm()
        sender = self._wallet.convert_0x_to_me_address(request.sender) if request.sender.startswith("0x") else request.sender
        signer = await self._wallet.create_evm_wallet(sender)
        return await api.deploy_contract(request, signer)

    async def execute_evm_contract(
        self, params: ExecuteEvmContractParams | Mapping[str, Any],
    ) -> EvmExecutionResult:
        self._guard()
        from ..evm_contract_models import ExecuteEvmContractParams

        request = checked_model(ExecuteEvmContractParams, params)
        api = self._require_evm()
        sender = self._wallet.convert_0x_to_me_address(request.sender) if request.sender.startswith("0x") else request.sender
        signer = await self._wallet.create_evm_wallet(sender)
        return await api.execute_contract(request, signer)

    async def query_evm_contract(self, params: QueryEvmContractParams | Mapping[str, Any]) -> Any:
        self._guard()
        return await self._require_evm().query_contract(params)

    async def get_evm_contract_info(self, contract_address: str) -> EvmContractInfo:
        self._guard()
        return await self._require_evm().get_contract_info(contract_address)

    async def destroy(self) -> None:
        # No initialized guard: SDK close must be able to drain owned providers.
        if self._evm is not None:
            await self._evm.destroy()

    storeCode = store_code
    deployContract = deploy_contract
    executeContract = execute_contract
    getCodeIdByHash = get_code_id_by_hash
    getContractsByCodeId = get_contracts_by_code_id
    getContractsByCreator = get_contracts_by_creator
    queryContractSmart = query_contract_smart
    getContractInfo = get_contract_info
    deployEvmContract = deploy_evm_contract
    executeEvmContract = execute_evm_contract
    queryEvmContract = query_evm_contract
    getEvmContractInfo = get_evm_contract_info
