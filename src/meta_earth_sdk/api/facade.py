"""Module APIs while preserving every original direct WalletApi entry point."""

from __future__ import annotations

from ..transaction.engine import TransactionEngine
from ..transport.runtime import QueryRuntime
from .contract import ContractApi
from .evm_contract import EvmContractApi
from .governance import GovernanceApi
from .identity import IdentityApi
from .staking import StakingApi
from .transaction import TransactionApi
from .wallet import WalletApi


class SDKApi(WalletApi):
    def __init__(self, runtime: QueryRuntime, engine: TransactionEngine, evm: EvmContractApi) -> None:
        super().__init__(runtime=runtime)
        self.wallet = self
        self.transaction = TransactionApi(engine)
        self.identity = IdentityApi(engine)
        self.staking = StakingApi(engine)
        self.governance = GovernanceApi(engine)
        self.contract = ContractApi(engine)
        self.evm_contract = evm
