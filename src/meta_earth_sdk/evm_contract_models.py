"""Strict EVM contract inputs; quantities are integers in wei, never umec.

ABI, arguments and bytecode are deliberately omitted from ordinary repr/dumps.
Web3 is an optional adapter, not required by these models or the ABI codec.
"""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import Field, model_validator

from .models import _SafeModel
from .transaction_models import PendingTransaction

_UINT64 = (1 << 64) - 1
_UINT256 = (1 << 256) - 1


class EvmTransactionOptions(_SafeModel):
    value: int = Field(default=0, ge=0, le=_UINT256, repr=False, exclude=True)
    gas_limit: int | None = Field(default=None, validation_alias="gasLimit", gt=0, le=_UINT64)
    nonce: int | None = Field(default=None, ge=0, lt=_UINT64)
    gas_price: int | None = Field(default=None, validation_alias="gasPrice", ge=0, le=_UINT256)
    max_fee_per_gas: int | None = Field(default=None, validation_alias="maxFeePerGas", ge=0, le=_UINT256)
    max_priority_fee_per_gas: int | None = Field(default=None, validation_alias="maxPriorityFeePerGas", ge=0, le=_UINT256)
    confirmations: int = Field(default=1, ge=1, le=_UINT64)
    timeout_ms: int | None = Field(default=None, validation_alias="timeoutMs", gt=0)
    poll_interval_ms: int = Field(default=250, validation_alias="pollIntervalMs", gt=0, le=60_000)

    @model_validator(mode="after")
    def _fees(self) -> Self:
        for name in ("gas_limit", "nonce", "gas_price", "max_fee_per_gas", "max_priority_fee_per_gas"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError("Explicit EVM overrides cannot be null")
        if self.gas_price is not None and (self.max_fee_per_gas is not None or self.max_priority_fee_per_gas is not None):
            raise ValueError("Cannot mix legacy and EIP-1559 fees")
        if (self.max_fee_per_gas is None) != (self.max_priority_fee_per_gas is None):
            raise ValueError("Both EIP-1559 fees must be explicit")
        if self.max_fee_per_gas is not None and self.max_priority_fee_per_gas is not None and self.max_priority_fee_per_gas > self.max_fee_per_gas:
            raise ValueError("Priority fee exceeds maximum fee")
        return self


class DeployEvmContractParams(EvmTransactionOptions):
    sender: str
    abi: list[dict[str, Any]] = Field(repr=False, exclude=True, max_length=1024)
    bytecode: str = Field(repr=False, exclude=True)
    constructor_args: list[Any] = Field(default_factory=list, validation_alias="constructorArgs", repr=False, exclude=True, max_length=1024)


class ExecuteEvmContractParams(EvmTransactionOptions):
    sender: str
    contract_address: str = Field(validation_alias="contractAddress")
    abi: list[dict[str, Any]] = Field(repr=False, exclude=True, max_length=1024)
    method: str
    args: list[Any] = Field(default_factory=list, repr=False, exclude=True, max_length=1024)
    simulate: bool = True


class QueryEvmContractParams(_SafeModel):
    contract_address: str = Field(validation_alias="contractAddress")
    abi: list[dict[str, Any]] = Field(repr=False, exclude=True, max_length=1024)
    method: str
    args: list[Any] = Field(default_factory=list, repr=False, exclude=True, max_length=1024)
    block_tag: int | Literal["latest", "pending", "earliest", "safe", "finalized"] = Field(default="latest", validation_alias="blockTag")


class EvmReceipt(_SafeModel):
    transaction_hash: str = Field(validation_alias="transactionHash")
    block_hash: str = Field(validation_alias="blockHash")
    block_number: int = Field(validation_alias="blockNumber", ge=0)
    status: Literal[0, 1]
    gas_used: int = Field(validation_alias="gasUsed", ge=0)
    effective_gas_price: int | None = Field(default=None, validation_alias="effectiveGasPrice", ge=0)
    contract_address: str | None = Field(default=None, validation_alias="contractAddress")


class EvmExecutionResult(_SafeModel):
    transaction_hash: str = Field(validation_alias="transactionHash")
    state: Literal["committed_success", "committed_failed", "unknown"]
    receipt: EvmReceipt | None = None
    pending_handle: PendingTransaction | None = None


class EvmDeploymentResult(EvmExecutionResult):
    contract_address: str | None = Field(default=None, validation_alias="contractAddress")


class EvmContractInfo(_SafeModel):
    address: str
    chain_id: int = Field(validation_alias="chainId", gt=0)
    is_contract: bool = Field(validation_alias="isContract")
    bytecode: str = Field(repr=False, exclude=True)
    balance: int = Field(ge=0)
    transaction_count: int = Field(validation_alias="transactionCount", ge=0)
