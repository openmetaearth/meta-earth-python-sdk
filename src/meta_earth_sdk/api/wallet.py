"""Read-only APIs; balance queries do not require a local wallet."""

from __future__ import annotations

import threading
import time
from typing import Any

from pydantic import ValidationError as ModelValidationError

from ..adapters.cosmpy.bank import BankClient
from ..config import SDKConfig
from ..exceptions import ProtocolError, TimeoutError, ValidationError
from ..models import BalancePage, Coin, Pagination
from ..transport.runtime import QueryRuntime


def _layer(value: str) -> str:
    if value not in ("hub", "rollup"):
        raise ValidationError("layer must be hub or rollup")
    return value


def _address(value: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValidationError("address must be a nonempty string without surrounding whitespace")
    return value


def _pagination(**kwargs: Any) -> Pagination:
    try:
        return Pagination(**kwargs)
    except ModelValidationError:
        raise ValidationError("Invalid pagination parameters") from None


class WalletApi:
    def __init__(
        self, config: SDKConfig | None = None, *, runtime: QueryRuntime | None = None
    ) -> None:
        self._owns_runtime = runtime is None
        self._runtime = runtime if runtime is not None else QueryRuntime(config or SDKConfig())

    async def get_balances_page(
        self,
        address: str,
        layer: str = "hub",
        *,
        key: bytes = b"",
        limit: int = 100,
        offset: int = 0,
        reverse: bool = False,
        count_total: bool = False,
    ) -> BalancePage:
        address = _address(address)
        layer = _layer(layer)
        pagination = _pagination(
            key=key, limit=limit, offset=offset, reverse=reverse, count_total=count_total
        )

        def query(client: BankClient, deadline: float, cancelled: threading.Event) -> BalancePage:
            return client.get_page(
                address,
                key=pagination.key,
                limit=pagination.limit,
                offset=pagination.offset,
                reverse=pagination.reverse,
                count_total=pagination.count_total,
                timeout=_remaining(deadline, cancelled),
            )

        return await self._runtime.bank(layer, query)

    async def get_balance(self, address: str, layer: str = "hub") -> list[Coin]:
        return list((await self.get_balances_page(address, layer)).balances)

    async def get_all_balances(
        self,
        address: str,
        layer: str = "hub",
        *,
        page_size: int = 100,
        max_pages: int = 1000,
        max_records: int = 100000,
    ) -> list[Coin]:
        address = _address(address)
        layer = _layer(layer)
        pagination = _pagination(limit=page_size)
        for value in (max_pages, max_records):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValidationError("Pagination safety limits must be positive integers")

        def query(client: BankClient, deadline: float, cancelled: threading.Event) -> list[Coin]:
            balances: list[Coin] = []
            key = b""
            seen: set[bytes] = set()
            for _ in range(max_pages):
                page = client.get_page(
                    address, key=key, limit=pagination.limit,
                    timeout=_remaining(deadline, cancelled),
                )
                if len(balances) + len(page.balances) > max_records:
                    raise ProtocolError("Balance pagination record limit exceeded")
                balances.extend(page.balances)
                key = page.next_key
                if not key:
                    return balances
                if key in seen:
                    raise ProtocolError("Balance pagination cursor repeated")
                seen.add(key)
            raise ProtocolError("Balance pagination page limit exceeded")

        return await self._runtime.bank(layer, query)

    async def get_account_info(self, address: str, layer: str = "hub") -> Any:
        """Return the account envelope; transaction signing validates it separately."""
        from urllib.parse import quote

        address = _address(address)
        if layer != "hub":
            raise ValidationError("Account API supports Hub only")
        async with self._runtime.operation() as lease:
            return await lease.transport.request(
                lease.config.resolved_network().hub.restful_url,
                f"/cosmos/auth/v1beta1/accounts/{quote(address, safe='')}",
                timeout=lease.config.timeout_ms / 1000,
            )

    async def get_node_info(self, layer: str = "hub") -> Any:
        return await self._runtime.node(
            _layer(layer), "/cosmos/base/tendermint/v1beta1/node_info", rest=True
        )

    async def get_node_version(self, layer: str = "hub") -> Any:
        return await self._runtime.node(_layer(layer), "/abci_info")

    async def get_network_status(self, layer: str = "hub") -> Any:
        return await self._runtime.node(_layer(layer), "/status")

    async def aclose(self) -> None:
        if self._owns_runtime:
            await self._runtime.aclose()

    async def __aenter__(self) -> WalletApi:
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    getBalance = get_balance
    getBalancesPage = get_balances_page
    getAllBalances = get_all_balances
    getAccountInfo = get_account_info
    getNodeInfo = get_node_info
    getNodeVersion = get_node_version
    getNetworkStatus = get_network_status
    destroy = aclose


def _remaining(deadline: float, cancelled: threading.Event) -> float:
    remaining = deadline - time.monotonic()
    if cancelled.is_set() or remaining <= 0:
        raise TimeoutError("Balance query budget expired", operation="balance")
    return remaining
