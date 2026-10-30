"""Hub EVM contracts with injected async RPC, local ABI and a single-send gate.

Supply an async provider explicitly. The built-in Web3 adapter is disabled.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from typing import Any, Literal, TypeVar, cast

from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

from ..crypto.keys import convert_me_to_0x_address
from ..evm import abi as codec
from ..evm.provider import AsyncEvmProvider, AsyncWeb3Provider, ProviderResource
from ..evm_contract_models import (
    DeployEvmContractParams,
    EvmContractInfo,
    EvmDeploymentResult,
    EvmExecutionResult,
    EvmReceipt,
    EvmTransactionOptions,
    ExecuteEvmContractParams,
    QueryEvmContractParams,
)
from ..evm_models import EvmTransaction, _address_bytes
from ..exceptions import (
    BusyError,
    ClosedError,
    NetworkError,
    ProtocolError,
    SDKError,
    ValidationError,
)
from ..exceptions import TimeoutError as SDKTimeout
from ..models import _SafeModel
from ..transaction.coordinator import ExecutionCoordinator
from ..transaction_models import PendingTransaction
from ..transport.runtime import OperationLease, QueryRuntime
from ..wallet.evm import EvmSigner

M = TypeVar("M", bound=_SafeModel)
_UINT64 = (1 << 64) - 1
_UINT256 = (1 << 256) - 1


def _model(cls: type[M], value: M | Mapping[str, Any]) -> M:
    try:
        # Preserve excluded fields, and revalidate unchecked constructed models.
        source = {key: item for key, item in dict(value).items() if key in value.model_fields_set} if isinstance(value, cls) else value
        return cls.model_validate(source)
    except Exception:
        raise ValidationError("Invalid EVM contract parameters") from None


def _address(value: str) -> str:
    try:
        return str(to_checksum_address(_address_bytes(value)))
    except Exception:
        raise ValidationError("Invalid EVM address") from None


def _sender(value: str) -> str:
    try:
        return _address(convert_me_to_0x_address(value) if value.startswith("me1") else value)
    except Exception:
        raise ValidationError("Invalid EVM sender") from None


def _uint(value: Any, maximum: int = _UINT256) -> int:
    if isinstance(value, str):
        import re

        if len(value) > 66 or re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value) is None:
            raise ProtocolError("Invalid EVM quantity")
        value = int(value, 16)
    if type(value) is not int or not 0 <= value <= maximum:
        raise ProtocolError("Invalid EVM quantity")
    return value


def _hash(value: Any) -> str:
    try:
        result = codec.hex_data(value, maximum=32)
        if len(result) != 66:
            raise ValueError
        return result
    except Exception:
        raise ProtocolError("Invalid EVM hash") from None


def _wire(transaction: Mapping[str, Any]) -> dict[str, Any]:
    return {key: hex(value) if type(value) is int else value for key, value in transaction.items()}


class EvmContractApi:
    """One runtime and shared Direct/EVM coordinator; no global providers.

    A factory creates SDK-owned generation resources; ``borrowed_provider`` is
    caller-owned and never closed. The factory must not perform network I/O in
    its synchronous constructor. Exposed providers are a capability boundary:
    direct caller RPC is outside this API's single-send and uncertainty controls.
    ``destroy`` is asynchronous, refuses active operations, closes only owned
    resources, and permits lazy recreation without changing SDK initialization.
    """

    def __init__(
        self, runtime: QueryRuntime, *, coordinator: ExecutionCoordinator,
        provider_factory: Callable[[str], AsyncEvmProvider] | None = None,
        borrowed_provider: AsyncEvmProvider | None = None,
    ) -> None:
        if provider_factory is not None and borrowed_provider is not None:
            raise ValidationError("Choose a provider factory or a borrowed provider")
        self._runtime = runtime
        self.coordinator = coordinator
        self._factory = provider_factory or AsyncWeb3Provider
        self._borrowed = borrowed_provider
        self._namespace = uuid.uuid4().hex
        self._epoch = 0
        self._active = 0
        self._resources: list[ProviderResource] = []
        self._workers: set[asyncio.Task[EvmExecutionResult]] = set()
        self._known_chains: dict[str, int] = {}
        # Endpoint equality is conservative recovery provenance, not proof that
        # chain IDs uniquely identify deployments. Gates remain endpoint-agnostic.
        self._origins: dict[str, tuple[str, str]] = {}
        self._deployments: set[str] = set()
        self._destroying = False
        self._stopped = False

    def stop(self) -> None:
        """Terminal signing/sending barrier; receipt observation may still drain."""
        self._stopped = True

    async def aclose(self) -> None:
        """Drain retained workers; QueryRuntime closes generation-owned resources."""
        self.stop()
        if self._workers:
            try:
                async with asyncio.timeout(self._runtime.config.timeout_ms / 1000):
                    await asyncio.shield(asyncio.gather(*tuple(self._workers), return_exceptions=True))
            except TimeoutError:
                raise SDKTimeout("EVM worker drain budget expired") from None

    @property
    def pending(self) -> tuple[PendingTransaction, ...]:
        return tuple(item for item in self.coordinator.pending if item.protocol == "evm")

    @asynccontextmanager
    async def _operation(self, timeout_ms: int | None = None) -> AsyncIterator[OperationLease]:
        if self._destroying or self._stopped:
            raise ClosedError("EVM provider resources are closing or closed")
        self._active += 1
        try:
            async with self._runtime.operation() as lease:
                # EVM is explicitly Hub-scoped regardless of the query-layer default.
                if timeout_ms is not None:
                    lease.deadline = min(lease.deadline, time.monotonic() + timeout_ms / 1000)
                try:
                    async with asyncio.timeout_at(lease.deadline):
                        yield lease
                except TimeoutError:
                    raise SDKTimeout("EVM operation budget expired") from None
        finally:
            self._active -= 1

    async def _rpc(self, lease: OperationLease, provider: AsyncEvmProvider, method: str, params: list[Any]) -> Any:
        if time.monotonic() >= lease.deadline:
            raise SDKTimeout("EVM operation budget expired")
        try:
            return await provider.request(method, params)
        except SDKError:
            raise
        except Exception:
            raise NetworkError("EVM RPC request failed") from None

    async def _provider(self, lease: OperationLease) -> tuple[AsyncEvmProvider, int]:
        network = lease.config.resolved_network()
        url = network.evm_rpc_url
        if not url or network.evm_chain_id is None:
            raise ValidationError("EVM RPC URL and explicit chain ID are required")

        def create() -> ProviderResource:
            provider = self._borrowed if self._borrowed is not None else self._factory(url)
            resource = ProviderResource(provider, owned=self._borrowed is None, close_timeout=lease.config.timeout_ms / 1000)
            self._resources = [item for item in self._resources if not item.closed]
            self._resources.append(resource)
            return resource

        resource = lease.resource(f"evm:{self._namespace}:{self._epoch}", create)
        if resource.closed:
            raise ClosedError("EVM generation provider is closed")
        provider = resource.provider
        if provider.send_retries_disabled is not True:
            raise ValidationError("EVM provider must explicitly disable all send retries")
        chain_id = _uint(await self._rpc(lease, provider, "eth_chainId", []))
        if chain_id <= 0 or chain_id != network.evm_chain_id:
            raise ProtocolError("EVM chain ID does not match configuration")
        return provider, chain_id

    async def get_provider(self) -> AsyncEvmProvider:
        async with self._operation() as lease:
            provider, _ = await self._provider(lease)
            return provider

    async def destroy(self) -> None:
        if self._active or self._workers or self._destroying:
            raise BusyError("Cannot destroy EVM resources during an active operation")
        self._destroying = True
        self._epoch += 1
        resources, self._resources = self._resources, []
        try:
            async with asyncio.timeout(self._runtime.config.timeout_ms / 1000):
                results = await asyncio.gather(*(resource.aclose() for resource in resources), return_exceptions=True)
            if any(isinstance(item, BaseException) for item in results):
                raise ClosedError("EVM provider close failed")
        except TimeoutError:
            raise SDKTimeout("EVM provider close budget expired") from None
        finally:
            self._destroying = False

    async def query_contract(self, params: QueryEvmContractParams | Mapping[str, Any]) -> Any:
        parsed = _model(QueryEvmContractParams, params)
        address = _address(parsed.contract_address)
        data, entry = codec.function(parsed.abi, parsed.method, parsed.args)
        block: int | str = parsed.block_tag
        if type(block) is int:
            if not 0 <= block <= _UINT64:
                raise ValidationError("Invalid EVM block tag")
            block = hex(block)
        async with self._operation() as lease:
            provider, _ = await self._provider(lease)
            result = await self._rpc(lease, provider, "eth_call", [{"to": address, "data": data}, block])
            return codec.decode_result(entry, result)

    async def get_contract_info(self, contract_address: str) -> EvmContractInfo:
        address = _address(contract_address)
        async with self._operation() as lease:
            provider, chain_id = await self._provider(lease)
            # Sequential RPC keeps cancellation/deadline ownership simple; no orphan gathers.
            code = await self._rpc(lease, provider, "eth_getCode", [address, "latest"])
            try:
                code = codec.hex_data(code)
            except ValidationError:
                raise ProtocolError("Invalid EVM code response") from None
            balance = _uint(await self._rpc(lease, provider, "eth_getBalance", [address, "latest"]))
            nonce = _uint(await self._rpc(lease, provider, "eth_getTransactionCount", [address, "latest"]), _UINT64 - 1)
            return EvmContractInfo(address=address, chain_id=chain_id, bytecode=code, is_contract=code != "0x", balance=balance, transaction_count=nonce)

    async def _logical_chain(self, lease: OperationLease) -> str:
        endpoint = lease.config.resolved_network().hub
        expected = endpoint.cosmos_chain_id
        if expected is None:
            raise ValidationError("Verified Cosmos chain ID is required for EVM coordination")
        response = await lease.transport.request(endpoint.rpc_url, "/status", timeout=max(0.001, lease.deadline - time.monotonic()))
        try:
            actual = response["result"]["node_info"]["network"]
            if not isinstance(actual, str) or actual != expected:
                raise ValueError
        except (KeyError, TypeError, ValueError):
            raise ProtocolError("Cosmos chain ID does not match configuration") from None
        return actual

    async def deploy_contract(self, params: DeployEvmContractParams | Mapping[str, Any], signer: EvmSigner) -> EvmDeploymentResult:
        parsed = _model(DeployEvmContractParams, params)
        data = codec.constructor(parsed.abi, parsed.bytecode, parsed.constructor_args)
        constructors = [entry for entry in parsed.abi if entry.get("type") == "constructor"]
        if parsed.value and (not constructors or constructors[0].get("stateMutability") != "payable"):
            raise ValidationError("Nonpayable constructor cannot receive value")
        result = await self._launch(parsed, signer, data, None, simulate=False)
        address = result.receipt.contract_address if result.receipt is not None else None
        if result.state == "committed_success" and address is None:
            raise ProtocolError("Successful deployment receipt lacks a contract address")
        return EvmDeploymentResult(transaction_hash=result.transaction_hash, state=result.state, receipt=result.receipt, pending_handle=result.pending_handle, contract_address=address)

    async def execute_contract(self, params: ExecuteEvmContractParams | Mapping[str, Any], signer: EvmSigner) -> EvmExecutionResult:
        parsed = _model(ExecuteEvmContractParams, params)
        data, entry = codec.function(parsed.abi, parsed.method, parsed.args)
        if entry.get("stateMutability") in ("view", "pure"):
            raise ValidationError("Use query_contract for a read-only ABI method")
        if parsed.value and entry.get("stateMutability") != "payable":
            raise ValidationError("Nonpayable ABI method cannot receive value")
        return await self._launch(parsed, signer, data, _address(parsed.contract_address), simulate=parsed.simulate)

    async def _launch(self, params: DeployEvmContractParams | ExecuteEvmContractParams, signer: EvmSigner, data: str, to: str | None, *, simulate: bool) -> EvmExecutionResult:
        if self._stopped or self._destroying:
            raise ClosedError("EVM provider resources are closing or closed")
        if len(self._workers) >= self._runtime.config.max_pending:
            raise BusyError("EVM operation capacity is full")
        cancelled = asyncio.Event()
        task = asyncio.create_task(self._write(params, signer, data, to, simulate=simulate, cancelled=cancelled))
        self._workers.add(task)

        def complete(done: asyncio.Task[EvmExecutionResult]) -> None:
            self._workers.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(complete)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Worker owns lease/gate through real send drain or its total deadline.
            # This event prevents any later sign/send transition if not yet begun.
            cancelled.set()
            raise

    async def _write(self, params: DeployEvmContractParams | ExecuteEvmContractParams, signer: EvmSigner, data: str, to: str | None, *, simulate: bool, cancelled: asyncio.Event) -> EvmExecutionResult:
        handle: PendingTransaction | None = None
        try:
            async with self._operation(params.timeout_ms) as lease:
                provider, chain_id = await self._provider(lease)
                logical_chain = await self._logical_chain(lease)
                address = _address(signer.address)
                if _sender(params.sender) != address:
                    raise ValidationError("EVM sender does not match the signer")
                identity = _address_bytes(address)
                async with self.coordinator.acquire(logical_chain, identity) as token:
                    def active() -> None:
                        lease.check_active()
                        if self._stopped:
                            raise ClosedError("EVM signing and sending are stopped")
                        if cancelled.is_set():
                            raise asyncio.CancelledError

                    active()
                    transaction: dict[str, Any] = {"from": address, "data": data, "value": params.value}
                    if to is not None:
                        transaction["to"] = to
                    nonce = params.nonce
                    if nonce is None:
                        nonce = _uint(await self._rpc(lease, provider, "eth_getTransactionCount", [address, "pending"]), _UINT64 - 1)
                    transaction["nonce"] = nonce
                    if params.max_fee_per_gas is not None:
                        transaction.update(type=2, maxFeePerGas=params.max_fee_per_gas, maxPriorityFeePerGas=params.max_priority_fee_per_gas)
                    else:
                        gas_price = params.gas_price
                        if gas_price is None:
                            gas_price = _uint(await self._rpc(lease, provider, "eth_gasPrice", []))
                        transaction["gasPrice"] = gas_price
                    if simulate:
                        await self._rpc(lease, provider, "eth_call", [_wire(transaction), "pending"])
                    gas = params.gas_limit
                    if gas is None:
                        estimate = _uint(await self._rpc(lease, provider, "eth_estimateGas", [_wire(transaction)]), _UINT64)
                        gas = (estimate * 6 + 4) // 5
                    transaction.update(gas=gas, chainId=chain_id)
                    try:
                        document = EvmTransaction.model_validate({**transaction, "to": to})
                    except Exception:
                        raise ValidationError("Invalid completed EVM transaction") from None
                    active()
                    signed = await signer.sign_transaction(document)
                    raw = signed.raw_transaction
                    local_hash = "0x" + bytes(keccak(raw)).hex()
                    if signed.hash != bytes.fromhex(local_hash[2:]):
                        raise ProtocolError("Local EVM signature hash mismatch")
                    active()
                    handle = PendingTransaction(handle=uuid.uuid4().hex, transaction_hash=local_hash, logical_chain=logical_chain, identity=identity.hex(), protocol="evm")
                    token.mark_pending(handle)
                    self._known_chains[handle.handle] = chain_id
                    origin_network = lease.config.resolved_network()
                    assert origin_network.evm_rpc_url is not None
                    self._origins[handle.handle] = (origin_network.evm_rpc_url, origin_network.hub.rpc_url)
                    if to is None:
                        self._deployments.add(handle.handle)
                    # No awaits between last active check, marking pending and starting send.
                    remote_hash = _hash(await self._rpc(lease, provider, "eth_sendRawTransaction", ["0x" + raw.hex()]))
                    if remote_hash != local_hash:
                        raise ProtocolError("EVM submission hash mismatch")
                    receipt = await self._wait(lease, provider, local_hash, params.confirmations, params.poll_interval_ms)
                    if to is None and receipt.status == 1 and receipt.contract_address is None:
                        raise ProtocolError("Successful deployment receipt lacks a contract address")
                    token.resolve(handle)
                    self._known_chains.pop(handle.handle, None)
                    self._origins.pop(handle.handle, None)
                    self._deployments.discard(handle.handle)
                    return EvmExecutionResult(transaction_hash=local_hash, state="committed_success" if receipt.status else "committed_failed", receipt=receipt)
        except asyncio.CancelledError:
            raise
        except Exception:
            if handle is not None:
                return EvmExecutionResult(transaction_hash=handle.transaction_hash, state="unknown", pending_handle=handle)
            raise

    def _receipt(self, response: Any, transaction_hash: str) -> EvmReceipt:
        try:
            if not isinstance(response, dict) or _hash(response["transactionHash"]) != transaction_hash:
                raise ValueError
            status = cast(Literal[0, 1], _uint(response["status"], 1))
            contract = response.get("contractAddress")
            return EvmReceipt(transaction_hash=transaction_hash, block_hash=_hash(response["blockHash"]), block_number=_uint(response["blockNumber"], _UINT64), status=status, gas_used=_uint(response["gasUsed"], _UINT64), effective_gas_price=None if response.get("effectiveGasPrice") is None else _uint(response["effectiveGasPrice"]), contract_address=None if contract is None else _address(contract))
        except Exception:
            raise ProtocolError("Invalid EVM transaction receipt") from None

    async def _wait(self, lease: OperationLease, provider: AsyncEvmProvider, transaction_hash: str, confirmations: int, poll_ms: int) -> EvmReceipt:
        while True:
            response = await self._rpc(lease, provider, "eth_getTransactionReceipt", [transaction_hash])
            if response is not None:
                receipt = self._receipt(response, transaction_hash)
                head = _uint(await self._rpc(lease, provider, "eth_blockNumber", []), _UINT64)
                if head >= receipt.block_number + confirmations - 1:
                    block = await self._rpc(lease, provider, "eth_getBlockByNumber", [hex(receipt.block_number), False])
                    if isinstance(block, dict) and _hash(block.get("hash")) == receipt.block_hash:
                        # Re-read after canonical/head checks; receipt disappearance or a
                        # moved receipt is a reorg, never permission to resend.
                        again = await self._rpc(lease, provider, "eth_getTransactionReceipt", [transaction_hash])
                        if again is not None and self._receipt(again, transaction_hash) == receipt:
                            return receipt
            await asyncio.sleep(poll_ms / 1000)

    async def wait_for_transaction(self, handle: PendingTransaction, *, chain_id: int, confirmations: int = 1, poll_interval_ms: int = 250) -> EvmExecutionResult:
        options = _model(EvmTransactionOptions, {"confirmations": confirmations, "pollIntervalMs": poll_interval_ms})
        handle = _model(PendingTransaction, handle)
        if handle.protocol != "evm" or type(chain_id) is not int or not 0 < chain_id <= _UINT256:
            raise ValidationError("Invalid EVM recovery identity")
        known_chain = self._known_chains.get(handle.handle)
        if known_chain is not None and known_chain != chain_id:
            raise ValidationError("EVM recovery chain does not match submission")
        async with self._operation() as lease:
            provider, actual = await self._provider(lease)
            if actual != chain_id or await self._logical_chain(lease) != handle.logical_chain:
                raise ProtocolError("EVM recovery network does not match submission")
            receipt = await self._wait(lease, provider, _hash(handle.transaction_hash), options.confirmations, options.poll_interval_ms)
            origin = self._origins.get(handle.handle)
            network = lease.config.resolved_network()
            current_origin = (network.evm_rpc_url, network.hub.rpc_url)
            guarded = handle in self.coordinator.pending
            incomplete_deployment = handle.handle in self._deployments and receipt.status == 1 and receipt.contract_address is None
            if guarded and (origin is None or origin != current_origin or incomplete_deployment):
                # A same-ID deployment/alias receipt is not sufficient provenance
                # to release an earlier instance-local ambiguity gate.
                return EvmExecutionResult(transaction_hash=handle.transaction_hash, state="unknown", receipt=receipt, pending_handle=handle)
            self.coordinator.resolve(handle)
            self._known_chains.pop(handle.handle, None)
            self._origins.pop(handle.handle, None)
            self._deployments.discard(handle.handle)
            return EvmExecutionResult(transaction_hash=handle.transaction_hash, state="committed_success" if receipt.status else "committed_failed", receipt=receipt)

    getProvider = get_provider
    deployContract = deploy_contract
    executeContract = execute_contract
    queryContract = query_contract
    getContractInfo = get_contract_info
    waitForTransaction = wait_for_transaction


EvmContractService = EvmContractApi
