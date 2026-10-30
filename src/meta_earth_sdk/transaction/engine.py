"""One leased, bounded, at-most-once Cosmos Direct transaction pipeline."""

from __future__ import annotations

import asyncio
import base64
import builtins
import hashlib
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar, cast

from google.protobuf.message import Message
from pydantic import ValidationError as PydanticValidationError

from ..adapters.cosmpy.transaction import build_transaction
from ..config import SDKConfig
from ..crypto.keys import convert_me_to_0x_address, public_key_to_address
from ..exceptions import (
    BusyError,
    ClosedError,
    NetworkError,
    ProtocolError,
    SDKError,
    SigningError,
    ValidationError,
)
from ..exceptions import TimeoutError as SDKTimeout
from ..models import Coin
from ..transaction_models import (
    GasEstimate,
    PendingTransaction,
    TransactionAttribute,
    TransactionEvent,
    TransactionHistoryService,
    TransactionOptions,
    TransactionResult,
    TransactionState,
)
from ..transport.runtime import OperationLease, QueryRuntime
from ..wallet.signers import DirectSigner
from ..wallet_models import PublicKeyAny, SignerAccount
from .coordinator import ExecutionCoordinator
from .fees import calculate_fee

T = TypeVar("T")


class _CoinFields(Protocol):
    amount: str
    denom: str


class _SendFields(Protocol):
    to_address: str
    amount: Sequence[_CoinFields]
_SIGNERS = {
    "cosmos.bank.v1beta1.MsgSend": "from_address",
    "metaearth.kyc.MsgCreateSubAccount": "creator",
    "cosmos.staking.v1beta1.MsgDelegate": "delegator_address",
    "cosmos.staking.v1beta1.MsgUndelegate": "delegator_address",
    "metaearth.wstaking.MsgWithdrawDelegatorReward": "delegator_address",
    "metaearth.wstaking.MsgDoFixedDeposit": "account",
    "metaearth.wstaking.MsgWithdrawFixedDeposit": "account",
    "cosmos.gov.v1beta1.MsgSubmitProposal": "proposer",
    "cosmos.gov.v1beta1.MsgVote": "voter",
    "cosmwasm.wasm.v1.MsgStoreCode": "sender",
    "cosmwasm.wasm.v1.MsgInstantiateContract": "sender",
    "cosmwasm.wasm.v1.MsgExecuteContract": "sender",
}


def _uint(value: object, *, bits: int = 64) -> int:
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,20}", value):
        result = int(value)
    else:
        raise ProtocolError("Transaction response contains an invalid integer")
    if not 0 <= result < (1 << bits):
        raise ProtocolError("Transaction response integer is out of range")
    return result


def _hash(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[a-fA-F0-9]{64}", value) is None:
        raise ValidationError("A transaction hash must be 32-byte hexadecimal")
    return value.upper()


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ProtocolError("Transaction response has an invalid structure")
    return value


@dataclass
class _Control:
    cancelled: bool = False
    sent: bool = False


class TransactionContext:
    """Business preflight view of the exact lease used for the subsequent write."""

    def __init__(self, lease: OperationLease, deadline: float, address: str = "") -> None:
        self._lease = lease
        self._deadline = min(deadline, lease.deadline)
        self.address = address

    @property
    def config(self) -> SDKConfig:
        return self._lease.config

    def remaining(self) -> float:
        value = self._deadline - time.monotonic()
        if value <= 0:
            raise SDKTimeout("Transaction operation budget expired")
        return value

    async def request(
        self, path: str, *, params: Mapping[str, Any] | None = None,
        method: str = "GET", json: Any = None,
    ) -> Any:
        if method != "GET":
            raise ValidationError("Business preflight only permits read operations")
        self._lease.check_active()
        return await self._lease.transport.request(
            self.config.resolved_network().hub.restful_url, path,
            method=method, params=params, json=json, timeout=self.remaining(),
        )


class TransactionEngine:
    def __init__(
        self, runtime: QueryRuntime, *, coordinator: ExecutionCoordinator | None = None,
        fee_offset: Callable[[], int] | None = None,
        history_service: TransactionHistoryService | None = None,
    ) -> None:
        self._runtime = runtime
        self.coordinator = coordinator or ExecutionCoordinator(runtime.config.max_pending)
        self._fee_offset = fee_offset
        self._history_service = history_service
        self._tasks: dict[asyncio.Task[Any], _Control] = {}
        # Origin evidence is not a deployment fingerprint. In its absence only
        # the exact original Hub REST+RPC pair may clear this engine's handle.
        # The shared uncertainty gate remains keyed by chain ID+20-byte payload.
        self._origins: dict[str, tuple[str, str]] = {}
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._close_task: asyncio.Task[None] | None = None

    @property
    def pending_transactions(self) -> tuple[PendingTransaction, ...]:
        return self.coordinator.pending

    def _check(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise ClosedError("Transaction engine belongs to another event loop")
        if self._closed:
            raise ClosedError("Transaction engine is closing or closed")

    def _active(self, context: TransactionContext, control: _Control) -> None:
        if self._closed or control.cancelled:
            raise ClosedError("Transaction operation was stopped before submission")
        context._lease.check_active()
        context.remaining()

    async def _launch(self, operation: Callable[[_Control], Awaitable[T]]) -> T:
        self._check()
        if len(self._tasks) >= self._runtime.config.max_pending:
            raise BusyError("Transaction operation capacity is full")
        control = _Control()
        async def run() -> T:
            try:
                return await operation(control)
            except (SDKError, asyncio.CancelledError):
                raise
            except Exception:
                raise ProtocolError("Transaction operation could not be completed safely") from None
        task = asyncio.create_task(run())
        self._tasks[task] = control

        def finished(done: asyncio.Task[T]) -> None:
            self._tasks.pop(done, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # The worker retains lease, coordination token and capacity. Its next
            # safety boundary prevents any late sign/send; already-started sends
            # are observed to the original deadline, never replayed.
            control.cancelled = True
            raise

    @staticmethod
    def _options(options: TransactionOptions | None) -> TransactionOptions:
        try:
            return TransactionOptions() if options is None else TransactionOptions.model_validate(options)
        except PydanticValidationError:
            raise ValidationError("Invalid transaction options") from None

    @staticmethod
    def _deadline(config: SDKConfig, options: TransactionOptions) -> float:
        milliseconds = min(config.timeout_ms, options.timeout_ms or config.timeout_ms)
        return time.monotonic() + milliseconds / 1000

    async def _verified_chain(self, context: TransactionContext) -> str:
        endpoint = context.config.resolved_network().hub
        expected = endpoint.cosmos_chain_id
        if expected is None:
            raise ValidationError("Transactions require an explicitly configured Cosmos chain ID")
        response = await context._lease.transport.request(
            endpoint.rpc_url, "/status", timeout=context.remaining(),
        )
        result = _mapping(_mapping(response).get("result"))
        node = _mapping(result.get("node_info"))
        if node.get("network") != expected:
            raise ProtocolError("Node chain identity does not match transaction configuration")
        # No endpoint or display name enters coordination. Same-ID deployments
        # conservatively share the uncertainty gate until a proven fingerprint
        # scheme is supplied by both Cosmos and EVM engines.
        return expected

    async def _signer(self, signer: DirectSigner) -> tuple[SignerAccount, PublicKeyAny]:
        accounts = await signer.get_accounts()
        if len(accounts) != 1 or not isinstance(accounts[0], SignerAccount):
            raise ValidationError("Direct transactions require exactly one signer account")
        account = accounts[0]
        key = signer.public_key_any
        if not isinstance(key, PublicKeyAny) or key.value[2:] != account.pubkey:
            raise SigningError("Signer account and public key metadata do not match")
        if key.type_url not in {
            "/cosmos.crypto.secp256k1.PubKey", "/ethermint.crypto.v1.ethsecp256k1.PubKey",
        }:
            raise SigningError("Signer public key profile is not admitted")
        address_type = "eth" if key.type_url == "/ethermint.crypto.v1.ethsecp256k1.PubKey" else "cosmos"
        if public_key_to_address(account.pubkey, address_type) != account.address:
            raise SigningError("Signer public key does not derive the signer address")
        convert_me_to_0x_address(account.address)
        return account, key

    @staticmethod
    def _messages(messages: Sequence[Message], address: str) -> tuple[Message, ...]:
        from ..protocol import get_registry

        if not isinstance(messages, Sequence) or not 1 <= len(messages) <= 64:
            raise ValidationError("A transaction requires between one and 64 messages")
        registry = get_registry()
        copied: list[Message] = []
        for message in messages:
            if not isinstance(message, Message):
                raise ValidationError("Transaction inputs must be protocol messages")
            name = message.DESCRIPTOR.full_name
            field = _SIGNERS.get(name)
            if field is None:
                raise ValidationError("Transaction message signer policy is not admitted")
            clone = registry.bridge(message)
            if getattr(clone, field, None) != address:
                raise ValidationError("Transaction message does not use the actual signer account")
            if name == "cosmos.bank.v1beta1.MsgSend":
                transfer = cast(_SendFields, clone)
                convert_me_to_0x_address(transfer.to_address)
                amounts = transfer.amount
                if not amounts or any(
                    not re.fullmatch(r"[0-9]+", coin.amount) or int(coin.amount) <= 0
                    or not coin.denom or any(c.isspace() for c in coin.denom)
                    for coin in amounts
                ):
                    raise ValidationError("Transfers require positive integer coin amounts")
            copied.append(clone)
        return tuple(copied)

    async def _account(self, context: TransactionContext, address: str) -> Any:
        from ..protocol import decode_account

        convert_me_to_0x_address(address)
        data = await context.request("/cosmos/auth/v1beta1/accounts/" + address)
        account = decode_account(_mapping(data))
        if account.address != address:
            raise ProtocolError("Account query returned a different address")
        return account

    async def _checked_account(
        self, context: TransactionContext, signer: SignerAccount, key: PublicKeyAny,
    ) -> Any:
        account = await self._account(context, signer.address)
        if (account.wrapper_type == "ethermint.types.v1.EthAccount"
                and key.type_url != "/ethermint.crypto.v1.ethsecp256k1.PubKey"):
            raise SigningError("On-chain account wrapper does not match the signer profile")
        if account.public_key is not None and (
            account.public_key.type_url != key.type_url or account.public_key.key != signer.pubkey
        ):
            raise SigningError("On-chain public key does not match the actual signer")
        return account

    async def _simulate(
        self, context: TransactionContext, messages: Sequence[Message], key: PublicKeyAny,
        account: Any, chain: str, operation: str, memo: str, options: TransactionOptions,
    ) -> GasEstimate:
        frozen = await context._lease.run_sync(lambda: build_transaction(
            messages, public_key=key, sequence=account.sequence,
            account_number=account.account_number, chain_id=chain,
            fee_amount=0, gas_limit=options.gas_limit or 500_000, memo=memo,
        ))
        response = await context._lease.transport.request(
            context.config.resolved_network().hub.restful_url,
            "/cosmos/tx/v1beta1/simulate", method="POST",
            json={"tx_bytes": base64.b64encode(frozen.simulation_bytes()).decode("ascii")},
            timeout=context.remaining(),
        )
        info = _mapping(_mapping(response).get("gas_info"))
        return calculate_fee(_uint(info.get("gas_used")), _uint(info.get("gas_wanted")),
                             operation, options, self._fee_offset)

    async def execute(
        self, messages: Sequence[Message], signer: DirectSigner, *, operation: str = "transfer",
        memo: str = "", options: TransactionOptions | None = None,
        preflight: Callable[[TransactionContext], Awaitable[None]] | None = None,
    ) -> TransactionResult:
        selected = self._options(options)
        if not isinstance(memo, str) or len(memo.encode("utf-8")) > 4096:
            raise ValidationError("Transaction memo exceeds supported limits")
        # Snapshot message wire before the first await to prevent caller mutation
        # while waiting for a lease/account lock.
        if not isinstance(messages, Sequence) or not 1 <= len(messages) <= 64:
            raise ValidationError("A transaction requires between one and 64 messages")
        from ..protocol import get_registry

        snapshots: list[tuple[str, bytes]] = []
        for message in messages:
            if not isinstance(message, Message):
                raise ValidationError("Transaction inputs must be protocol messages")
            admitted = get_registry().bridge(message)
            wire = admitted.SerializeToString(deterministic=True)
            if len(wire) > 1 << 20:
                raise ValidationError("Transaction message exceeds supported size")
            snapshots.append((message.DESCRIPTOR.full_name, wire))

        async def work(control: _Control) -> TransactionResult:
            async with self._runtime.operation() as lease:
                context = TransactionContext(lease, self._deadline(lease.config, selected))
                try:
                    async with asyncio.timeout(context.remaining()):
                        self._active(context, control)
                        local, key = await self._signer(signer)
                        context.address = local.address
                        from ..protocol import get_registry

                        concrete = self._messages(tuple(get_registry().decode(n, w) for n, w in snapshots), local.address)
                        chain = await self._verified_chain(context)
                        identity = bytes.fromhex(convert_me_to_0x_address(local.address)[2:])
                        async with self.coordinator.acquire(chain, identity) as token:
                            self._active(context, control)
                            if preflight is not None:
                                await preflight(context)
                            account = await self._checked_account(context, local, key)
                            self._active(context, control)
                            estimate = await self._simulate(context, concrete, key, account, chain, operation, memo, selected)
                            self._active(context, control)
                            frozen = await lease.run_sync(lambda: build_transaction(
                                concrete, public_key=key, sequence=account.sequence,
                                account_number=account.account_number, chain_id=chain,
                                fee_amount=estimate.fee_amount, gas_limit=estimate.gas_limit, memo=memo,
                            ))
                            self._active(context, control)
                            # Retain the lease through the actual offline signer
                            # worker, even if the operation's deadline expires.
                            sign_task = asyncio.create_task(signer.sign_direct(local.address, frozen.document))
                            try:
                                signed = await asyncio.shield(sign_task)
                            except asyncio.CancelledError:
                                await asyncio.shield(sign_task)
                                raise
                            self._active(context, control)
                            raw = frozen.attach(signed)
                            transaction_hash = hashlib.sha256(raw).hexdigest().upper()
                            handle = PendingTransaction(
                                handle=uuid.uuid4().hex, transaction_hash=transaction_hash,
                                logical_chain=chain, identity=identity.hex(), protocol="cosmos",
                            )
                            fee = Coin(amount=str(estimate.fee_amount), denom="umec")
                            unknown = TransactionResult(
                                transaction_hash=transaction_hash, state="unknown", fee=fee,
                                pending_handle=handle, signed_bytes=raw,
                            )
                            self._active(context, control)
                            token.mark_pending(handle)
                            endpoint = context.config.resolved_network().hub
                            self._origins[handle.handle] = (endpoint.restful_url, endpoint.rpc_url)
                            control.sent = True
                            # From this boundary any failure is unknown, not a
                            # retry invitation. A possible send is protected.
                            try:
                                response = await lease.transport.request(
                                    context.config.resolved_network().hub.restful_url,
                                    "/cosmos/tx/v1beta1/txs", method="POST",
                                    json={"tx_bytes": base64.b64encode(raw).decode("ascii"), "mode": "BROADCAST_MODE_SYNC"},
                                    timeout=context.remaining(),
                                )
                                accepted = self._broadcast(response, transaction_hash, fee, handle, raw)
                                if accepted.state == "check_tx_rejected":
                                    token.resolve(handle)
                                    self._origins.pop(handle.handle, None)
                                    return accepted
                                if accepted.state == "unknown":
                                    return accepted
                                if not selected.wait_for_commit:
                                    return accepted
                                receipt = await self._wait(context, transaction_hash, selected.poll_interval_ms)
                                token.resolve(handle)
                                self._origins.pop(handle.handle, None)
                                return receipt.model_copy(update={"fee": fee, "signed_bytes": raw})
                            except (Exception, asyncio.CancelledError):
                                return unknown
                except builtins.TimeoutError:
                    raise SDKTimeout("Transaction operation budget expired") from None

        return await self._launch(work)

    @staticmethod
    def _broadcast(
        data: Any, expected: str, fee: Coin, handle: PendingTransaction, raw: bytes,
    ) -> TransactionResult:
        response = _mapping(_mapping(data).get("tx_response"))
        try:
            actual = _hash(response.get("txhash"))
        except ValidationError:
            raise ProtocolError("Broadcast response has no valid transaction hash") from None
        if actual != expected:
            raise ProtocolError("Broadcast returned a different transaction hash")
        code = _uint(response.get("code"), bits=32)
        state: TransactionState = "submitted" if code == 0 else "unknown" if code == 19 else "check_tx_rejected"
        codespace = response.get("codespace", "")
        if not isinstance(codespace, str) or re.fullmatch(r"[A-Za-z0-9_.-]{0,64}", codespace) is None:
            raise ProtocolError("Broadcast response has an invalid codespace")
        return TransactionResult(
            transaction_hash=expected, state=state, code=code, codespace=codespace,
            fee=fee, pending_handle=handle if state != "check_tx_rejected" else None,
            signed_bytes=raw,
        )

    @staticmethod
    def _receipt(data: Any, expected: str) -> TransactionResult:
        response = _mapping(_mapping(data).get("tx_response"))
        try:
            actual = _hash(response.get("txhash"))
        except ValidationError:
            raise ProtocolError("Transaction receipt has an invalid hash") from None
        if actual != expected:
            raise ProtocolError("Transaction receipt hash does not match query")
        # REST providers may include the raw bytes or decoded Tx. When present,
        # bind that payload to the requested hash rather than trusting a label.
        envelope = _mapping(data)
        encoded = envelope.get("tx_bytes", response.get("tx_bytes"))
        if encoded is not None:
            try:
                if not isinstance(encoded, str):
                    raise ValueError
                wire = base64.b64decode(encoded, validate=True)
            except ValueError:
                raise ProtocolError("Transaction receipt contains invalid transaction bytes") from None
            if hashlib.sha256(wire).hexdigest().upper() != expected:
                raise ProtocolError("Transaction receipt bytes do not match the queried hash")
        tx_data = envelope.get("tx", response.get("tx"))
        if tx_data is not None:
            from ..protocol import get_registry

            mapping = dict(_mapping(tx_data))
            type_url = mapping.pop("@type", "/cosmos.tx.v1beta1.Tx")
            if type_url != "/cosmos.tx.v1beta1.Tx":
                raise ProtocolError("Transaction receipt contains an unsupported transaction type")
            tx = get_registry().parse_json("cosmos.tx.v1beta1.Tx", mapping)
            # Tx message fields 1/2 are length-delimited body/auth; TxRaw uses
            # exactly those same wire fields as opaque bytes. Canonical decoded
            # Tx serialization therefore produces the canonical TxRaw wire.
            raw = tx.SerializeToString(deterministic=True)
            if hashlib.sha256(raw).hexdigest().upper() != expected:
                raise ProtocolError("Transaction receipt payload does not match the queried hash")
        height = _uint(response.get("height"))
        if height == 0:
            raise ProtocolError("Transaction query did not return a committed height")
        code = _uint(response.get("code"), bits=32)
        codespace = response.get("codespace", "")
        if not isinstance(codespace, str) or re.fullmatch(r"[A-Za-z0-9_.-]{0,64}", codespace) is None:
            raise ProtocolError("Transaction receipt has an invalid codespace")
        events: list[TransactionEvent] = []
        remote_events = response.get("events", [])
        if not isinstance(remote_events, list) or len(remote_events) > 1024:
            raise ProtocolError("Transaction receipt has invalid events")
        for item in remote_events:
            event = _mapping(item)
            event_type, attributes = event.get("type"), event.get("attributes", [])
            if not isinstance(event_type, str) or len(event_type) > 256 or not isinstance(attributes, list) or len(attributes) > 1024:
                raise ProtocolError("Transaction receipt has invalid event fields")
            parsed: list[TransactionAttribute] = []
            for attr in attributes:
                attr = _mapping(attr)
                k, v = attr.get("key"), attr.get("value")
                if not isinstance(k, str) or not isinstance(v, str) or len(k) > 4096 or len(v) > 32768:
                    raise ProtocolError("Transaction receipt has invalid event attributes")
                parsed.append(TransactionAttribute(key=k, value=v))
            events.append(TransactionEvent(type=event_type, attributes=tuple(parsed)))
        return TransactionResult(
            transaction_hash=actual, state="committed_success" if code == 0 else "committed_failed",
            code=code, codespace=codespace, height=height,
            gas_used=_uint(response["gas_used"]) if "gas_used" in response else None,
            gas_wanted=_uint(response["gas_wanted"]) if "gas_wanted" in response else None,
            events=tuple(events),
        )

    async def _get(self, context: TransactionContext, transaction_hash: str) -> TransactionResult | None:
        try:
            # Already-submitted observers deliberately remain usable after stop;
            # they only read the pinned generation to its original deadline.
            data = await context._lease.transport.request(
                context.config.resolved_network().hub.restful_url,
                "/cosmos/tx/v1beta1/txs/" + transaction_hash, timeout=context.remaining(),
            )
        except NetworkError as exc:
            if exc.status_code == 404:
                return None
            raise
        return self._receipt(data, transaction_hash)

    async def _wait(self, context: TransactionContext, transaction_hash: str, poll_ms: int) -> TransactionResult:
        while True:
            result = await self._get(context, transaction_hash)
            if result is not None:
                return result
            await asyncio.sleep(min(poll_ms / 1000, context.remaining()))

    def _resolve(self, context: TransactionContext, chain: str, result: TransactionResult) -> None:
        endpoint = context.config.resolved_network().hub
        origin = (endpoint.restful_url, endpoint.rpc_url)
        for handle in self.pending_transactions:
            if (handle.protocol == "cosmos" and handle.logical_chain == chain
                    and handle.transaction_hash == result.transaction_hash
                    and self._origins.get(handle.handle) == origin):
                self.coordinator.resolve(handle)
                self._origins.pop(handle.handle, None)

    async def query_account(self, address: str) -> Any:
        async def work(control: _Control) -> Any:
            async with self._runtime.operation() as lease:
                context = TransactionContext(lease, self._deadline(lease.config, TransactionOptions()))
                self._active(context, control)
                await self._verified_chain(context)
                return await self._account(context, address)
        return await self._launch(work)

    async def get_transaction(self, transaction_hash: str) -> TransactionResult | None:
        selected = _hash(transaction_hash)
        async def work(control: _Control) -> TransactionResult | None:
            async with self._runtime.operation() as lease:
                context = TransactionContext(lease, self._deadline(lease.config, TransactionOptions()))
                self._active(context, control)
                chain = await self._verified_chain(context)
                result = await self._get(context, selected)
                if result is not None:
                    self._resolve(context, chain, result)
                return result
        return await self._launch(work)

    async def wait_for_transaction(
        self, transaction_hash: str, *, options: TransactionOptions | None = None,
    ) -> TransactionResult:
        selected_hash, selected = _hash(transaction_hash), self._options(options)
        async def work(control: _Control) -> TransactionResult:
            async with self._runtime.operation() as lease:
                context = TransactionContext(lease, self._deadline(lease.config, selected))
                try:
                    async with asyncio.timeout(context.remaining()):
                        self._active(context, control)
                        chain = await self._verified_chain(context)
                        result = await self._wait(context, selected_hash, selected.poll_interval_ms)
                        self._resolve(context, chain, result)
                        return result
                except builtins.TimeoutError:
                    raise SDKTimeout("Transaction observation budget expired") from None
        return await self._launch(work)

    async def simulate_gas(
        self, messages: Sequence[Message], signer: DirectSigner, *, operation: str = "transfer",
        memo: str = "", options: TransactionOptions | None = None,
    ) -> GasEstimate:
        selected = self._options(options)
        async def work(control: _Control) -> GasEstimate:
            async with self._runtime.operation() as lease:
                context = TransactionContext(lease, self._deadline(lease.config, selected))
                try:
                    async with asyncio.timeout(context.remaining()):
                        self._active(context, control)
                        local, key = await self._signer(signer)
                        concrete = self._messages(messages, local.address)
                        chain = await self._verified_chain(context)
                        account = await self._checked_account(context, local, key)
                        self._active(context, control)
                        return await self._simulate(context, concrete, key, account, chain, operation, memo, selected)
                except builtins.TimeoutError:
                    raise SDKTimeout("Transaction simulation budget expired") from None
        return await self._launch(work)

    async def request(
        self, path: str, *, params: Mapping[str, Any] | None = None,
        method: str = "GET", json: Any = None,
    ) -> Any:
        async def work(control: _Control) -> Any:
            async with self._runtime.operation() as lease:
                context = TransactionContext(lease, self._deadline(lease.config, TransactionOptions()))
                self._active(context, control)
                return await context.request(path, params=params, method=method, json=json)
        return await self._launch(work)

    async def _service(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        async def work(control: _Control) -> Any:
            async with self._runtime.operation() as lease:
                base = self._history_service.base_url if self._history_service is not None else lease.config.transaction_service_url
                if base is None:
                    raise ValidationError("Transaction history/fees require an explicitly configured external service")
                context = TransactionContext(lease, self._deadline(lease.config, TransactionOptions()))
                self._active(context, control)
                return await lease.transport.request(base, path, params=params, timeout=context.remaining())
        return await self._launch(work)

    async def get_transactions(self, address: str, limit: int = 10, offset: int = 0) -> Any:
        convert_me_to_0x_address(address)
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or not 0 <= offset < (1 << 64):
            raise ValidationError("Invalid transaction history pagination")
        return await self._service("/api/v1/transactions/" + address, {"limit": limit, "offset": offset})

    async def get_network_fees(self) -> Any:
        return await self._service("/api/v1/network/fees")

    def stop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            raise ClosedError("Transaction engine belongs to another event loop")
        self._loop = loop
        self._closed = True
        for control in self._tasks.values():
            control.cancelled = True

    async def aclose(self) -> None:
        if self._close_task is None:
            self.stop()
            async def drain() -> None:
                if self._tasks:
                    await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
            self._close_task = asyncio.create_task(drain())
        await asyncio.shield(self._close_task)
