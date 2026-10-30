"""Gov v1 queries and v1beta1 writes with private-pool upgrade Any values."""
from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from ..business_models import (
    ProposalParams,
    ProposalStatus,
    VoteParams,
    checked_model,
    make_message,
    require_hub,
    signer_address,
    transaction_hash,
    validate_response,
)
from ..exceptions import ValidationError
from ..wallet.signers import DirectSigner

if TYPE_CHECKING:
    from ..transaction import TransactionEngine
    from ..transaction_models import TransactionOptions


class GovernanceApi:
    def __init__(self, engine: TransactionEngine) -> None:
        self._engine = engine

    async def create_software_upgrade_proposal(
        self, params: ProposalParams | Mapping[str, Any], signer: DirectSigner,
        *, options: TransactionOptions | None = None,
    ) -> str:
        request = checked_model(ProposalParams, params)
        await signer_address(signer, request.proposer)
        # Serialize the exact Plan (int64 height, optional Timestamp/Any) before
        # packing content once. No default deposit is invented.
        plan = request.content.plan.model_dump(exclude_none=True)
        plan["height"] = str(request.content.plan.height)
        content = make_message("cosmos.upgrade.v1beta1.SoftwareUpgradeProposal", {
            "title": request.content.title, "description": request.content.description,
            "plan": plan,
        })
        from ..protocol import get_registry

        message = make_message("cosmos.gov.v1beta1.MsgSubmitProposal", {
            "content": get_registry().pack(content),
            "initial_deposit": [coin.model_dump() for coin in request.initial_deposit],
            "proposer": request.proposer,
        })
        result = await self._engine.execute(
            [message], signer, operation="governance", memo=request.memo, options=options,
        )
        return transaction_hash(result)

    async def vote_proposal(
        self, params: VoteParams | Mapping[str, Any], signer: DirectSigner,
        *, options: TransactionOptions | None = None,
    ) -> str:
        request = checked_model(VoteParams, params)
        await signer_address(signer, request.voter)
        message = make_message("cosmos.gov.v1beta1.MsgVote", {
            "proposal_id": str(request.proposal_id), "voter": request.voter,
            "option": {"yes": 1, "abstain": 2, "no": 3, "no_with_veto": 4}[request.option],
        })
        # v1beta1 deliberately has no metadata or memo field.
        result = await self._engine.execute([message], signer, operation="governance", options=options)
        return transaction_hash(result)

    async def get_proposals_v1(
        self, status: str | None = None, layer: str = "hub",
    ) -> Any:
        require_hub(layer)
        if status and status not in {item.value for item in ProposalStatus}:
            raise ValidationError("Invalid proposal status")
        data = await self._engine.request(
            "/cosmos/gov/v1/proposals", params={"proposal_status": status} if status else {},
        )
        if data is not None:
            # v1 messages may themselves contain Any content and ME types. Do
            # not route them through CosmPy's conflicting global descriptors.
            validate_response("cosmos.gov.v1.QueryProposalsResponse", data)
        return data

    createSoftwareUpgradeProposal = create_software_upgrade_proposal
    voteProposal = vote_proposal
    getProposalsV1 = get_proposals_v1
