"""Integer-only fixed-JS fee policy; pricing gas and execution gas are distinct."""

from __future__ import annotations

import secrets
from collections.abc import Callable

from ..exceptions import ValidationError
from ..transaction_models import GasEstimate, TransactionOptions

_FIXED = frozenset({
    "transfer", "staking", "flexible_delegate", "flexible_undelegate", "reward",
    "fixed_deposit", "fixed_withdrawal", "wasm_execute",
})
_DYNAMIC = frozenset({
    "bind", "identity", "governance", "governance_submit", "governance_vote",
    "wasm_upload", "wasm_store", "wasm_instantiate",
})


def calculate_fee(
    gas_used: int, gas_wanted: int, operation: str, options: TransactionOptions,
    offset: Callable[[], int] | None = None,
) -> GasEstimate:
    if type(gas_used) is not int or not 0 <= gas_used < (1 << 64):
        raise ValidationError("Invalid simulation gas amount")
    pricing_gas = (3 * gas_used + 1) // 2
    if options.gas_limit is not None:
        gas_limit = options.gas_limit
    elif operation in _FIXED:
        gas_limit = 500_000
    elif operation in _DYNAMIC:
        gas_limit = max(1, pricing_gas)
    else:
        raise ValidationError("Unknown transaction operation requires an explicit gas limit")
    if options.fee_amount is None:
        # JS getFinalGas: minimum branch alone adds random 0..999; above
        # minimum the exact ceil(0.02 * ceil(1.5 * gas_used)) is returned.
        base = (pricing_gas + 49) // 50
        if base <= 10_000:
            extra = secrets.randbelow(1000) if offset is None else offset()
            if type(extra) is not int or not 0 <= extra <= 999:
                raise ValidationError("Invalid fee policy random offset")
            amount = 10_000 + extra
        else:
            amount = base
    else:
        amount = options.fee_amount
    if gas_limit >= (1 << 64) or amount >= (1 << 64):
        raise ValidationError("Transaction gas or fee exceeds supported integer bounds")
    if options.max_fee_amount is not None and amount > options.max_fee_amount:
        raise ValidationError("Transaction fee exceeds configured maximum")
    if options.max_gas_limit is not None and gas_limit > options.max_gas_limit:
        raise ValidationError("Transaction gas exceeds configured maximum")
    if gas_used > gas_limit:
        raise ValidationError("Simulated gas exceeds the execution gas limit")
    return GasEstimate(
        gas_used=gas_used, gas_wanted=gas_wanted, pricing_gas=pricing_gas,
        fee_amount=amount, gas_limit=gas_limit,
    )
