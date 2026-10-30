"""Initialized governance facade with four exact v1beta1 voting options."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..api.governance import GovernanceApi
from ..business_models import ProposalParams, VoteParams, checked_model
from ..exceptions import ProtocolError
from ..transaction_models import TransactionOptions
from .wallet import WalletService


class GovernanceService:
    def __init__(self, api: GovernanceApi, wallet: WalletService, guard: Callable[[], None]) -> None:
        self._api = api
        self._wallet = wallet
        self._guard = guard

    async def submit_software_upgrade_proposal(
        self, params: ProposalParams | Mapping[str, Any],
        *, options: TransactionOptions | None = None,
    ) -> str:
        self._guard()
        request = checked_model(ProposalParams, params)
        signer = await self._wallet.create_direct_secp256k1_wallet(address=request.proposer)
        return await self._api.create_software_upgrade_proposal(request, signer, options=options)

    async def vote_proposal(
        self, params: VoteParams | Mapping[str, Any],
        *, options: TransactionOptions | None = None,
    ) -> str:
        self._guard()
        request = checked_model(VoteParams, params)
        signer = await self._wallet.create_direct_secp256k1_wallet(address=request.voter)
        return await self._api.vote_proposal(request, signer, options=options)

    async def get_proposals(self, status: str | None = None, layer: str = "hub") -> list[dict[str, Any]]:
        self._guard()
        data = await self._api.get_proposals_v1(status, layer)
        if not isinstance(data, Mapping) or not isinstance(data.get("proposals"), list):
            raise ProtocolError("Missing governance proposals response field")
        return list(data["proposals"])

    submitSoftwareUpgradeProposal = submit_software_upgrade_proposal
    voteProposal = vote_proposal
    getProposals = get_proposals
