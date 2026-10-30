"""Initialized transaction service over the shared engine."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from google.protobuf.message import Message

from ..api.transaction import TransactionApi
from ..exceptions import ValidationError
from ..transaction import TransactionEngine
from ..transaction_models import (
    GasEstimate,
    PendingTransaction,
    TransactionOptions,
    TransactionResult,
)
from ..wallet.signers import DirectSigner
from .wallet import WalletService


class TransactionService:
    def __init__(
        self, engine: TransactionEngine, guard: Callable[[], None], *, wallet: WalletService | None = None,
    ) -> None:
        self._engine = engine
        self._api = TransactionApi(engine)
        self._guard = guard
        self._wallet = wallet

    @property
    def pending_transactions(self) -> tuple[PendingTransaction, ...]:
        return self._engine.pending_transactions

    async def transfer(
        self, params: Mapping[str, Any], signer: DirectSigner | None = None, *,
        options: TransactionOptions | None = None,
    ) -> TransactionResult:
        self._guard()
        if signer is None:
            if self._wallet is None or not isinstance(params, Mapping):
                raise ValidationError("Transfer requires an explicit signer or a cached wallet")
            address = params.get("from_address", params.get("fromAddress"))
            if not isinstance(address, str):
                raise ValidationError("Cached-wallet transfer requires a source address")
            signer = await self._wallet.create_direct_secp256k1_wallet(address=address)
        return await self._api.send_transfer(params, signer, options=options)

    async def send_transaction(
        self, messages: Sequence[Message], signer: DirectSigner, *, operation: str = "transfer",
        memo: str = "", options: TransactionOptions | None = None,
    ) -> TransactionResult:
        self._guard()
        return await self._api.send_transaction(messages, signer, operation=operation, memo=memo, options=options)

    async def get_transaction(self, transaction_hash: str) -> TransactionResult | None:
        self._guard()
        return await self._api.get_transaction(transaction_hash)

    async def wait_for_transaction(
        self, transaction_hash: str, *, options: TransactionOptions | None = None,
    ) -> TransactionResult:
        self._guard()
        return await self._api.wait_for_transaction(transaction_hash, options=options)

    async def query_account(self, address: str) -> Any:
        self._guard()
        return await self._api.query_account(address)

    async def simulate_gas(
        self, messages: Sequence[Message], signer: DirectSigner, *, operation: str = "transfer",
        memo: str = "", options: TransactionOptions | None = None,
    ) -> GasEstimate:
        self._guard()
        return await self._api.simulate_gas(messages, signer, operation=operation, memo=memo, options=options)

    async def get_transactions(self, address: str, limit: int = 10, offset: int = 0) -> Any:
        self._guard()
        return await self._api.get_transactions(address, limit, offset)

    async def get_network_fees(self) -> Any:
        self._guard()
        return await self._api.get_network_fees()

    send_transfer = transfer
    sendTransfer = transfer
    sendTransaction = send_transaction
    getTransaction = get_transaction
    getTransactions = get_transactions
    getNetworkFees = get_network_fees
    simulateGas = simulate_gas
    queryAccount = query_account
    waitForTransaction = wait_for_transaction
