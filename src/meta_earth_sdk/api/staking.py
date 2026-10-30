"""Hub wstaking business API using exact ME messages and the shared write lease.

The pinned Hub registers standard staking MsgDelegate/MsgUndelegate on its
wstaking wrapper. Neither message implements ValidateBasic in the pinned SDK;
the wrapper fills validator_address from the account's region before decoding
it. No synthetic validator or replacement type URL is used here. Region state,
validator capacity and withdrawal ownership/maturity remain chain decisions.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from ..business_models import (
    FixedDepositConfig,
    FixedDepositParams,
    FixedDepositRecord,
    FixedDepositState,
    FlexibleStakingParams,
    WithdrawFixedDepositParams,
    checked_model,
    make_message,
    require_hub,
    signer_address,
    transaction_hash,
    validate_response,
)
from ..exceptions import ProtocolError, ValidationError
from ..models import Coin
from ..wallet.signers import DirectSigner
from .identity import IdentityApi

if TYPE_CHECKING:
    from ..transaction import TransactionContext, TransactionEngine
    from ..transaction_models import TransactionOptions


class StakingApi:
    def __init__(self, engine: TransactionEngine) -> None:
        self._engine = engine
        self.identity = IdentityApi(engine)

    async def create_delegation(
        self, params: FlexibleStakingParams | Mapping[str, Any], signer: DirectSigner,
        *, options: TransactionOptions | None = None,
    ) -> str:
        request = checked_model(FlexibleStakingParams, params)
        address = await signer_address(signer)
        if request.amount.denom != "umec" or int(request.amount.amount) < 1_000_000:
            raise ValidationError("Flexible delegation requires at least 1000000 umec")
        message = make_message("cosmos.staking.v1beta1.MsgDelegate", {
            "delegator_address": address,
            "amount": request.amount.model_dump(),
        })

        async def preflight(context: TransactionContext) -> None:
            result = await self.identity.get_me_id_by_sub_account(address, context=context)
            if result.has_me_id:
                raise ValidationError("Bound sub-accounts cannot use flexible delegation")

        result = await self._engine.execute(
            [message], signer, operation="staking", options=options, preflight=preflight,
        )
        return transaction_hash(result)

    async def create_undelegation(
        self, delegator_address: str, amount: Coin | Mapping[str, Any], layer: str = "hub",
        signer: DirectSigner | None = None, *, options: TransactionOptions | None = None,
    ) -> str:
        require_hub(layer)
        if signer is None:
            raise ValidationError("A Direct signer is required")
        request = checked_model(FlexibleStakingParams, {
            "address": delegator_address, "amount": amount, "layer": layer,
        })
        await signer_address(signer, delegator_address)
        if request.amount.denom != "umec":
            raise ValidationError("Flexible undelegation requires umec")
        message = make_message("cosmos.staking.v1beta1.MsgUndelegate", {
            "delegator_address": delegator_address, "amount": request.amount.model_dump(),
        })
        result = await self._engine.execute([message], signer, operation="staking", options=options)
        return transaction_hash(result)

    async def create_withdraw_delegator_reward(
        self, delegator_address: str, layer: str = "hub", signer: DirectSigner | None = None,
        *, options: TransactionOptions | None = None,
    ) -> str:
        require_hub(layer)
        if signer is None:
            raise ValidationError("A Direct signer is required")
        await signer_address(signer, delegator_address)
        message = make_message("metaearth.wstaking.MsgWithdrawDelegatorReward", {
            "delegator_address": delegator_address,
        })
        result = await self._engine.execute([message], signer, operation="staking", options=options)
        return transaction_hash(result)

    async def active_region(
        self, address: str, signer: DirectSigner, *, context: TransactionContext | None = None,
    ) -> str:
        await signer_address(signer, address)
        key_type = signer.public_key_type_url
        if key_type == "/ethermint.crypto.v1.ethsecp256k1.PubKey":
            result = await self.identity.get_me_id_by_sub_account(address, context=context)
        elif key_type == "/cosmos.crypto.secp256k1.PubKey":
            result = await self.identity.get_me_id_by_address(address, context=context)
        else:
            raise ValidationError("Unsupported staking signer profile")
        if (not result.has_me_id or result.info is None
                or result.info.status != "DID_STATUS_ACTIVE" or not result.info.region_id):
            raise ValidationError("Fixed staking requires an active ME ID with a region")
        return result.info.region_id

    async def create_fixed_deposit(
        self, params: FixedDepositParams | Mapping[str, Any], signer: DirectSigner,
        *, options: TransactionOptions | None = None,
    ) -> str:
        request = checked_model(FixedDepositParams, params)
        await signer_address(signer, request.address)
        message = make_message("metaearth.wstaking.MsgDoFixedDeposit", {
            "account": request.address, "principal": request.principal.model_dump(),
            "term": str(request.term),
        })

        async def preflight(context: TransactionContext) -> None:
            region = await self.active_region(request.address, signer, context=context)
            configs = await self.get_fixed_deposit_configs(region, context=context)
            if not any(config.term == request.term and config.status == "FIXED_DEPOSIT_CFG_ACTIVE"
                       for config in configs):
                raise ValidationError("No active fixed-deposit configuration for the requested term")

        result = await self._engine.execute(
            [message], signer, operation="staking", memo=request.memo,
            options=options, preflight=preflight,
        )
        return transaction_hash(result)

    async def withdraw_fixed_deposit(
        self, params: WithdrawFixedDepositParams | Mapping[str, Any], signer: DirectSigner,
        *, options: TransactionOptions | None = None,
    ) -> str:
        request = checked_model(WithdrawFixedDepositParams, params)
        await signer_address(signer, request.address)
        message = make_message("metaearth.wstaking.MsgWithdrawFixedDeposit", {
            "account": request.address, "id": str(request.id),
        })
        result = await self._engine.execute(
            [message], signer, operation="staking", memo=request.memo, options=options,
        )
        return transaction_hash(result)

    async def get_fixed_deposit_configs(
        self, region_id: str, *, context: TransactionContext | None = None,
    ) -> list[FixedDepositConfig]:
        requester = context if context is not None else self._engine
        data = await requester.request("/metaearth/wstaking/fixed_deposit_cfg", params={"regionIds": region_id})
        if not isinstance(data, Mapping):
            raise ProtocolError("Invalid fixed-deposit configuration response")
        # Optional repeated fields preserve the documented no-config result;
        # existing entries must still pass the exact private ME descriptor.
        normalized = dict(data)
        if normalized.get("RegionFixedDepositCfgs") is None:
            normalized["RegionFixedDepositCfgs"] = []
        regions = normalized["RegionFixedDepositCfgs"]
        # JS explicitly maps future nonempty status names to UNRECOGNIZED.
        # Protobuf JSON cannot parse an unknown symbolic enum; validate a copy
        # using an unknown numeric enum value, while retaining the raw spelling
        # for the public normalizer below. Never mutate the transport response.
        wire_regions: list[dict[str, Any]] = []
        if isinstance(regions, list):
            for region in regions:
                if not isinstance(region, Mapping):
                    raise ProtocolError("Invalid fixed-deposit configuration response")
                wire_region = dict(region)
                raw_configs = region.get("RegionFixedDepositCfg")
                if isinstance(raw_configs, list):
                    wire_configs: list[Any] = []
                    for config in raw_configs:
                        if isinstance(config, Mapping):
                            wire_config = dict(config)
                            status = config.get("status")
                            if isinstance(status, str) and status.strip() and status not in {
                                "FIXED_DEPOSIT_CFG_ACTIVE", "FIXED_DEPOSIT_CFG_INACTIVE",
                            }:
                                wire_config["status"] = (1 << 31) - 1
                            wire_configs.append(wire_config)
                        else:
                            wire_configs.append(config)
                    wire_region["RegionFixedDepositCfg"] = wire_configs
                wire_regions.append(wire_region)
        validate_response("metaearth.wstaking.QueryFixedDepositCfgResponse", {
            **normalized, "RegionFixedDepositCfgs": wire_regions if isinstance(regions, list) else regions,
        })
        if not isinstance(regions, list):
            raise ProtocolError("Invalid fixed-deposit configuration response")
        for region in regions:
            if isinstance(region, Mapping) and region.get("regionId") == region_id:
                configs = region.get("RegionFixedDepositCfg")
                if configs is None:
                    return []
                if not isinstance(configs, list):
                    raise ProtocolError("Invalid fixed-deposit configuration response")
                return [checked_model(FixedDepositConfig, value, remote=True) for value in configs]
        return []

    async def get_fixed_deposits(
        self, address: str, state: str = "ALL_STATE",
    ) -> list[FixedDepositRecord]:
        if state not in {item.value for item in FixedDepositState}:
            raise ValidationError("Invalid fixed-deposit state")
        data = await self._engine.request(
            f"/metaearth/wstaking/fixed_deposit_by_acct/{quote(address, safe='')}/{state}", params={},
        )
        if not isinstance(data, Mapping):
            raise ProtocolError("Invalid fixed-deposit response")
        normalized = dict(data)
        if normalized.get("FixedDeposit") is None:
            normalized["FixedDeposit"] = []
        # The chain's nonnullable Go times/coins are checked when present. The
        # public record deliberately preserves absent optional coins as None.
        validate_response("metaearth.wstaking.QueryFixedDepositByAcctResponse", normalized)
        values = normalized["FixedDeposit"]
        if not isinstance(values, list):
            raise ProtocolError("Invalid fixed-deposit response")
        return [checked_model(FixedDepositRecord, record, remote=True) for record in values]

    async def get_flexible_delegation(self, delegator_addr: str, layer: str = "hub") -> Any:
        require_hub(layer)
        data = await self._engine.request(
            f"/metaearth/wstaking/delegation/{quote(delegator_addr, safe='')}", params={},
        )
        if data is not None:
            validate_response("cosmos.staking.v1beta1.QueryDelegationResponse", data)
        return data

    async def get_flexible_delegation_rewards(self, delegator_addr: str, layer: str = "hub") -> Any:
        require_hub(layer)
        data = await self._engine.request(
            f"/metaearth/wstaking/delegation-rewards/{quote(delegator_addr, safe='')}", params={},
        )
        if data is not None:
            validate_response("metaearth.wstaking.QueryDelegationRewardsResponse", data)
        return data

    createDelegation = create_delegation
    createUndelegation = create_undelegation
    createWithdrawDelegatorReward = create_withdraw_delegator_reward
    createFixedDeposit = create_fixed_deposit
    withdrawFixedDeposit = withdraw_fixed_deposit
    getFixedDepositConfigs = get_fixed_deposit_configs
    getFixedDeposits = get_fixed_deposits
    getFlexibleDelegation = get_flexible_delegation
    getFlexibleDelegationRewards = get_flexible_delegation_rewards
