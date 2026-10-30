"""Strict Hub identity, staking and governance inputs and public query values.

Amounts/rates stay strings. IDs use the chain's uint64 and terms/heights its
int64, not JavaScript's lossy safe-integer approximation. Remote validation
failures are translated to ProtocolError by the API boundary.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, TypeVar

from google.protobuf.message import Message

if TYPE_CHECKING:
    from .transaction_models import TransactionResult
    from .wallet.signers import DirectSigner

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic import ValidationError as ModelValidationError

from .exceptions import ProtocolError, RPCError, ValidationError
from .models import Coin, _SafeModel

UINT64_MAX = (1 << 64) - 1
INT64_MAX = (1 << 63) - 1
ModelT = TypeVar("ModelT", bound=_SafeModel)


def checked_model(model: type[ModelT], value: object, *, remote: bool = False) -> ModelT:
    try:
        return model.model_validate(value)
    except ModelValidationError:
        if remote:
            raise ProtocolError("Invalid Hub business response") from None
        raise ValidationError("Invalid Hub business parameters") from None


def make_message(name: str, mapping: Mapping[str, Any]) -> Message:
    from .protocol import ProtocolError as RegistryError
    from .protocol import get_registry

    try:
        return get_registry().make_message(name, mapping)
    except RegistryError:
        raise ValidationError("Invalid Hub business message") from None


def validate_response(name: str, data: Any) -> None:
    from .protocol import ProtocolError as RegistryError
    from .protocol import get_registry

    try:
        get_registry().parse_json(name, data)
    except RegistryError:
        raise ProtocolError("Invalid Hub business response") from None


async def signer_address(signer: DirectSigner, expected: str | None = None) -> str:
    accounts = await signer.get_accounts()
    if len(accounts) != 1 or (expected is not None and accounts[0].address != expected):
        raise ValidationError("Signer must match the requested account")
    return accounts[0].address


def transaction_hash(result: TransactionResult) -> str:
    """Hash-returning facades must never turn a rejected write into success.

    An accepted/unknown hash is still useful for query-only recovery. It does
    not imply inclusion, and the engine retains its uncertainty gate.
    """
    if (result.state in {"check_tx_rejected", "committed_failed"}
            or result.code not in (None, 0) and not (result.state == "unknown" and result.code == 19)):
        raise RPCError("Hub business transaction was rejected", status_code=result.code)
    return result.transaction_hash


def require_hub(layer: str) -> None:
    if layer != "hub":
        raise ValidationError("This business operation supports Hub only", layer="hub")


def required_string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected a nonblank string")
    return value


def protocol_integer(value: Any) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    if type(value) is int:
        return value
    raise ValueError("Expected an exact decimal integer")


class MeIdInfo(_SafeModel):
    did: str
    address: str = ""
    pubkey: str = Field(default="", repr=False)
    status: str = ""
    region_id: str = Field(default="", alias="regionId")
    kyc_level: str = Field(default="", alias="kycLevel")
    sub_account: str = Field(default="", alias="subAccount")

    _did = field_validator("did")(required_string)


class MeIdLookupResult(_SafeModel):
    has_me_id: bool = Field(alias="hasMeId")
    info: MeIdInfo | None

    @model_validator(mode="after")
    def _consistent(self) -> MeIdLookupResult:
        if self.has_me_id != (self.info is not None):
            raise ValueError("Inconsistent ME ID result")
        return self

    @property
    def hasMeId(self) -> bool:
        return self.has_me_id


class BindSubAccountParams(_SafeModel):
    creator: str
    sub_account: str = Field(alias="subAccount")
    memo: str = ""
    layer: Literal["hub"] = "hub"

    _addresses = field_validator("creator", "sub_account")(required_string)


class CreateSubAccountParams(BindSubAccountParams):
    sub_account_pubkey: str = Field(alias="subAccountPubkey", repr=False)
    _public_key = field_validator("sub_account_pubkey")(required_string)


class FlexibleStakingParams(_SafeModel):
    address: str
    amount: Coin
    layer: Literal["hub"] = "hub"

    _address = field_validator("address")(required_string)

    @field_validator("amount")
    @classmethod
    def _positive(cls, value: Coin) -> Coin:
        if int(value.amount) <= 0:
            raise ValueError("A positive staking amount is required")
        return value


class FixedDepositParams(_SafeModel):
    address: str
    principal: Coin
    term: int = Field(gt=0, le=INT64_MAX)
    memo: str = ""

    _address = field_validator("address")(required_string)

    @field_validator("principal")
    @classmethod
    def _principal(cls, value: Coin) -> Coin:
        if value.denom != "umec" or re.fullmatch(r"[1-9][0-9]*", value.amount) is None:
            raise ValueError("Expected a positive umec principal")
        if int(value.amount) < 1_000_000:
            raise ValueError("Fixed deposits require at least 1000000 umec")
        return value


class WithdrawFixedDepositParams(_SafeModel):
    address: str
    id: int = Field(gt=0, le=UINT64_MAX)
    memo: str = ""

    _address = field_validator("address")(required_string)


class FixedDepositState(str, Enum):
    ALL_STATE = "ALL_STATE"
    NOT_EXPIRED = "NOT_EXPIRED"
    EXPIRED = "EXPIRED"


class FixedDepositConfigStatus(str, Enum):
    ACTIVE = "FIXED_DEPOSIT_CFG_ACTIVE"
    INACTIVE = "FIXED_DEPOSIT_CFG_INACTIVE"
    UNRECOGNIZED = "UNRECOGNIZED"


class FixedDepositConfig(_SafeModel):
    term: int = Field(ge=0, le=INT64_MAX)
    rate: str
    status: str

    _term = field_validator("term", mode="before")(protocol_integer)
    _rate = field_validator("rate")(required_string)

    @field_validator("status")
    @classmethod
    def _status(cls, value: str) -> str:
        required_string(value)
        return value if value in {item.value for item in FixedDepositConfigStatus} else "UNRECOGNIZED"


class FixedDepositConfigs(_SafeModel):
    region_id: str = Field(alias="regionId")
    configs: list[FixedDepositConfig]


class FixedDepositRecord(_SafeModel):
    id: int = Field(ge=0, le=UINT64_MAX)
    account: str
    principal: Coin | None = None
    interest: Coin | None = None
    start_time: str = Field(validation_alias=AliasChoices("startTime", "start_time"), serialization_alias="startTime")
    end_time: str = Field(validation_alias=AliasChoices("endTime", "end_time"), serialization_alias="endTime")
    term: int = Field(ge=0, le=INT64_MAX)
    rate: str

    _integers = field_validator("id", "term", mode="before")(protocol_integer)
    _strings = field_validator("account", "start_time", "end_time", "rate")(required_string)

    @model_validator(mode="before")
    @classmethod
    def _optional_coin(cls, value: Any) -> Any:
        if isinstance(value, Mapping) and any(key in value and value[key] is None for key in ("principal", "interest")):
            raise ValueError("Provided coins must not be null")
        return value


class ProposalStatus(str, Enum):
    UNSPECIFIED = "PROPOSAL_STATUS_UNSPECIFIED"
    DEPOSIT_PERIOD = "PROPOSAL_STATUS_DEPOSIT_PERIOD"
    VOTING_PERIOD = "PROPOSAL_STATUS_VOTING_PERIOD"
    PASSED = "PROPOSAL_STATUS_PASSED"
    REJECTED = "PROPOSAL_STATUS_REJECTED"
    FAILED = "PROPOSAL_STATUS_FAILED"


class UpgradePlan(_SafeModel):
    name: str
    height: int = Field(gt=0, le=INT64_MAX)
    time: str | None = None
    info: str = ""
    upgraded_client_state: dict[str, Any] | None = Field(default=None, alias="upgradedClientState", repr=False)

    _height = field_validator("height", mode="before")(protocol_integer)
    _name = field_validator("name")(required_string)


class SoftwareUpgradeContent(_SafeModel):
    title: str
    description: str = Field(repr=False)
    plan: UpgradePlan

    _strings = field_validator("title", "description")(required_string)


class ProposalParams(_SafeModel):
    proposer: str
    content: SoftwareUpgradeContent
    initial_deposit: list[Coin] = Field(alias="initialDeposit")
    memo: str = ""
    layer: Literal["hub"] = "hub"

    _proposer = field_validator("proposer")(required_string)


class VoteParams(_SafeModel):
    proposal_id: int = Field(alias="proposalId", gt=0, le=UINT64_MAX)
    voter: str
    option: Literal["yes", "abstain", "no", "no_with_veto"]
    metadata: str | None = Field(default=None, repr=False)
    layer: Literal["hub"] = "hub"

    _voter = field_validator("voter")(required_string)
