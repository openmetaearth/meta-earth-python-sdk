"""Initialized staking facade, delegating every write to the shared engine."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..api.identity import IdentityApi
from ..api.staking import StakingApi
from ..business_models import (
    FixedDepositConfigs,
    FixedDepositParams,
    FixedDepositRecord,
    FlexibleStakingParams,
    WithdrawFixedDepositParams,
    checked_model,
)
from ..exceptions import ValidationError
from ..transaction_models import TransactionOptions
from .wallet import WalletService


class StakingService:
    def __init__(
        self, api: StakingApi, wallet: WalletService, guard: Callable[[], None],
        identity_api: IdentityApi | None = None,
    ) -> None:
        self._api = api
        self._wallet = wallet
        self._guard = guard
        if identity_api is not None:
            self._api.identity = identity_api

    async def stake_flexible(
        self, params: FlexibleStakingParams | Mapping[str, Any],
        *, options: TransactionOptions | None = None,
    ) -> str:
        self._guard()
        request = checked_model(FlexibleStakingParams, params)
        signer = await self._wallet.create_direct_secp256k1_wallet(address=request.address)
        return await self._api.create_delegation(request, signer, options=options)

    async def claim_staking_reward(
        self, address: str, *, options: TransactionOptions | None = None,
    ) -> str:
        self._guard()
        signer = await self._wallet.create_direct_secp256k1_wallet(address=address)
        return await self._api.create_withdraw_delegator_reward(address, "hub", signer, options=options)

    async def stake_fixed(
        self, params: FixedDepositParams | Mapping[str, Any],
        *, options: TransactionOptions | None = None,
    ) -> str:
        self._guard()
        request = checked_model(FixedDepositParams, params)
        signer = await self._wallet.create_direct_secp256k1_wallet(address=request.address)
        return await self._api.create_fixed_deposit(request, signer, options=options)

    async def withdraw_fixed(
        self, params: WithdrawFixedDepositParams | Mapping[str, Any],
        *, options: TransactionOptions | None = None,
    ) -> str:
        self._guard()
        request = checked_model(WithdrawFixedDepositParams, params)
        signer = await self._wallet.create_direct_secp256k1_wallet(address=request.address)
        return await self._api.withdraw_fixed_deposit(request, signer, options=options)

    async def get_fixed_deposit_configs(self, address: str) -> FixedDepositConfigs:
        self._guard()
        signer = await self._wallet.create_direct_secp256k1_wallet(address=address)
        region = await self._api.active_region(address, signer)
        configs = await self._api.get_fixed_deposit_configs(region)
        return FixedDepositConfigs(regionId=region, configs=configs)

    async def get_fixed_deposits(self, address: str, state: str = "ALL_STATE") -> list[FixedDepositRecord]:
        self._guard()
        if not isinstance(address, str) or not address.strip():
            raise ValidationError("A nonblank fixed-deposit account is required")
        return await self._api.get_fixed_deposits(address, state)

    async def unstake_flexible(
        self, params: FlexibleStakingParams | Mapping[str, Any],
        *, options: TransactionOptions | None = None,
    ) -> str:
        self._guard()
        request = checked_model(FlexibleStakingParams, params)
        signer = await self._wallet.create_direct_secp256k1_wallet(address=request.address)
        return await self._api.create_undelegation(
            request.address, request.amount, request.layer, signer, options=options,
        )

    async def get_flexible_delegation(self, delegator_addr: str, layer: str = "hub") -> Any:
        self._guard()
        return await self._api.get_flexible_delegation(delegator_addr, layer)

    async def get_flexible_delegation_rewards(self, delegator_addr: str, layer: str = "hub") -> Any:
        self._guard()
        return await self._api.get_flexible_delegation_rewards(delegator_addr, layer)

    stakeFlexible = stake_flexible
    claimStakingReward = claim_staking_reward
    stakeFixed = stake_fixed
    withdrawFixed = withdraw_fixed
    getFixedDepositConfigs = get_fixed_deposit_configs
    getFixedDeposits = get_fixed_deposits
    unstakeFlexible = unstake_flexible
    getFlexibleDelegation = get_flexible_delegation
    getFlexibleDelegationRewards = get_flexible_delegation_rewards
