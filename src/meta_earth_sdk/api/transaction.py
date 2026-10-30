"""Low-level transaction facade; aliases share the exact same execution path."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from google.protobuf.message import Message
from pydantic import ValidationError as PydanticValidationError

from ..exceptions import ValidationError
from ..models import Coin
from ..transaction import TransactionEngine
from ..transaction_models import GasEstimate, TransactionOptions, TransactionResult
from ..wallet.signers import DirectSigner


class TransactionApi:
    def __init__(self, engine: TransactionEngine) -> None:
        self._engine = engine

    async def send_transfer(
        self, params: Mapping[str, Any], signer: DirectSigner, *,
        options: TransactionOptions | None = None,
    ) -> TransactionResult:
        from ..protocol import get_registry

        if not isinstance(params, Mapping) or any(k not in {
            "from_address", "fromAddress", "to_address", "toAddress", "amount", "memo", "layer",
        } for k in params):
            raise ValidationError("Invalid transfer parameters")
        if "to_address" in params and "toAddress" in params or "from_address" in params and "fromAddress" in params:
            raise ValidationError("Duplicate transfer parameter aliases")
        if params.get("layer", "hub") != "hub":
            raise ValidationError("The transaction engine supports Hub writes only")
        accounts = await signer.get_accounts()
        if len(accounts) != 1:
            raise ValidationError("Transfers require exactly one signer account")
        # fromAddress is never authority for either the message, sequence or key.
        address = accounts[0].address
        destination = params.get("to_address", params.get("toAddress"))
        amounts = params.get("amount")
        if not isinstance(amounts, Sequence) or isinstance(amounts, str | bytes) or not 1 <= len(amounts) <= 64:
            raise ValidationError("Transfer amount must be a non-empty coin sequence")
        try:
            coins = [Coin.model_validate(coin) for coin in amounts]
        except PydanticValidationError:
            raise ValidationError("Invalid transfer coin amount") from None
        message = get_registry().make_message("/cosmos.bank.v1beta1.MsgSend", {
            "from_address": address, "to_address": destination,
            "amount": [coin.model_dump() for coin in coins],
        })
        return await self._engine.execute(
            [message], signer, operation="transfer", memo=params.get("memo", ""), options=options,
        )

    async def send_transaction(
        self, messages: Sequence[Message], signer: DirectSigner, *, operation: str = "transfer",
        memo: str = "", options: TransactionOptions | None = None,
    ) -> TransactionResult:
        """Prepare/sign/send concrete messages; raw byte rebroadcast is not exposed."""
        return await self._engine.execute(messages, signer, operation=operation, memo=memo, options=options)

    async def get_transaction(self, transaction_hash: str) -> TransactionResult | None:
        return await self._engine.get_transaction(transaction_hash)

    async def wait_for_transaction(
        self, transaction_hash: str, *, options: TransactionOptions | None = None,
    ) -> TransactionResult:
        return await self._engine.wait_for_transaction(transaction_hash, options=options)

    async def simulate_gas(
        self, messages: Sequence[Message], signer: DirectSigner, *, operation: str = "transfer",
        memo: str = "", options: TransactionOptions | None = None,
    ) -> GasEstimate:
        return await self._engine.simulate_gas(messages, signer, operation=operation, memo=memo, options=options)

    async def query_account(self, address: str) -> Any:
        return await self._engine.query_account(address)

    async def get_transactions(self, address: str, limit: int = 10, offset: int = 0) -> Any:
        return await self._engine.get_transactions(address, limit, offset)

    async def get_network_fees(self) -> Any:
        return await self._engine.get_network_fees()

    sendTransfer = send_transfer
    sendTransaction = send_transaction
    getTransaction = get_transaction
    getTransactions = get_transactions
    getNetworkFees = get_network_fees
    simulateGas = simulate_gas
    queryAccount = query_account
    waitForTransaction = wait_for_transaction
