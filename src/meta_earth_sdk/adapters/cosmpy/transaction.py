"""The only CosmPy transaction/private-layout bridge.

CosmPy builds/seals concrete messages exactly once; its synchronous sign method
is never called. Frozen body/auth bytes are the authority for Direct and TxRaw.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import Any

from cosmpy.aerial.tx import SigningCfg, Transaction, TxFee, TxState
from cosmpy.crypto.keypairs import PublicKey
from cosmpy.protos.cosmos.tx.v1beta1.tx_pb2 import TxRaw
from google.protobuf.message import Message

from ...exceptions import ProtocolError, SigningError, ValidationError
from ...wallet_models import DirectSignDoc, DirectSignResult, PublicKeyAny


@dataclass(frozen=True)
class FrozenTransaction:
    document: DirectSignDoc = field(repr=False)
    public_key: PublicKeyAny = field(repr=False)

    def simulation_bytes(self) -> bytes:
        return bytes(TxRaw(
            body_bytes=self.document.body_bytes,
            auth_info_bytes=self.document.auth_info_bytes,
            signatures=[bytes(64)],
        ).SerializeToString(deterministic=True))

    def attach(self, result: DirectSignResult) -> bytes:
        """Reject signer document/key substitution before composing exact TxRaw."""
        if not isinstance(result, DirectSignResult) or result.signed != self.document:
            raise SigningError("Direct signer changed the frozen signing document")
        if result.signature.pub_key != self.public_key or len(result.signature.signature) != 64:
            raise SigningError("Direct signer returned mismatched signature metadata")
        return bytes(TxRaw(
            body_bytes=self.document.body_bytes,
            auth_info_bytes=self.document.auth_info_bytes,
            signatures=[result.signature.signature],
        ).SerializeToString(deterministic=True))


class MetaEarthTransaction(Transaction):  # type: ignore[misc]  # CosmPy has no py.typed.
    """Pinned CosmPy 0.12.2 seal extension for both admitted public-key profiles.

    Private `_tx` access and cross-pool bridges are intentionally confined here.
    Already-packed Any inputs are rejected: upstream wraps a concrete business
    message once, so passing an Any would double-wrap it.
    """

    def sign(self, *args: Any, **kwargs: Any) -> Any:
        raise SigningError("Use asynchronous Direct signing of frozen transaction bytes")

    def freeze(
        self, *, public_key: PublicKeyAny, sequence: int, account_number: int,
        chain_id: str, fee_amount: int, gas_limit: int, memo: str = "", fee_denom: str = "umec",
    ) -> FrozenTransaction:
        if version("cosmpy") != "0.12.2":
            raise ProtocolError("Unsupported CosmPy transaction adapter version")
        if self.state != TxState.Draft:
            raise ValidationError("Transaction is already sealed")
        try:
            self.seal(
                SigningCfg.direct(PublicKey(public_key.value[2:]), sequence),
                TxFee(amount=f"{fee_amount}{fee_denom}", gas_limit=gas_limit), memo=memo,
            )
            # CosmPy 0.12.2 hardcodes cosmos secp public-key Any. Replace only the
            # explicitly admitted profile bridge before freezing, never after.
            tx = self._tx
            if tx is None or len(tx.auth_info.signer_infos) != 1:
                raise ProtocolError("Unexpected CosmPy transaction layout")
            key = tx.auth_info.signer_infos[0].public_key
            key.type_url = public_key.type_url
            key.value = public_key.value
            document = DirectSignDoc(
                body_bytes=bytes(tx.body.SerializeToString(deterministic=True)),
                auth_info_bytes=bytes(tx.auth_info.SerializeToString(deterministic=True)),
                chain_id=chain_id,
                account_number=account_number,
            )
            return FrozenTransaction(document, public_key)
        except (ProtocolError, ValidationError):
            raise
        except Exception:
            raise ProtocolError("Could not seal the transaction safely") from None


def build_transaction(
    messages: Sequence[Message], *, public_key: PublicKeyAny, sequence: int,
    account_number: int, chain_id: str, fee_amount: int, gas_limit: int,
    memo: str = "", fee_denom: str = "umec",
) -> FrozenTransaction:
    from ...protocol import get_registry

    if not messages or len(messages) > 64:
        raise ValidationError("A transaction requires between one and 64 messages")
    registry = get_registry()
    transaction = MetaEarthTransaction()
    for message in messages:
        if not isinstance(message, Message) or message.DESCRIPTOR.full_name == "google.protobuf.Any":
            raise ValidationError("Transaction inputs must be concrete admitted messages")
        # Admission and wire bridge are checked even for externally constructed
        # standard CosmPy messages, without CopyFrom across descriptor pools.
        admitted = registry.bridge(message)
        transaction.add_message(admitted)
    return transaction.freeze(
        public_key=public_key, sequence=sequence, account_number=account_number,
        chain_id=chain_id, fee_amount=fee_amount, gas_limit=gas_limit, memo=memo, fee_denom=fee_denom,
    )
