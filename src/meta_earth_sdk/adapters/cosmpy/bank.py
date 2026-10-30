"""Synchronous, worker-owned bank queries using CosmPy's generated protocol.

Construction performs no I/O. The channel and stub are created by the first
query, on its calling thread. Calls and ``close`` must use the same worker;
this adapter intentionally provides no concurrent-use or retry machinery.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

import grpc
from cosmpy.protos.cosmos.bank.v1beta1 import query_pb2, query_pb2_grpc
from cosmpy.protos.cosmos.base.query.v1beta1 import pagination_pb2
from google.protobuf.message import DecodeError

from ...config import EndpointConfig
from ...exceptions import NetworkError, ProtocolError, RPCError
from ...exceptions import TimeoutError as SDKTimeout
from ...models import BalancePage, Coin

_OPERATION = "bank.get_page"
_AMOUNT = re.compile(r"[0-9]+\Z", re.ASCII)
_UINT64_MAX = (1 << 64) - 1


def _new_channel(target: str, *, secure: bool) -> grpc.Channel:
    # Disable gRPC retry policy too; the caller owns the deadline and retry policy.
    options = (("grpc.enable_retries", 0),)
    if secure:
        return grpc.secure_channel(target, grpc.ssl_channel_credentials(), options=options)
    return grpc.insecure_channel(target, options=options)


class BankClient:
    """Lazy bank transport, owning every channel returned by ``channel_factory``.

    The optional factory has signature ``factory(target, *, secure: bool)``.
    It runs on the first querying worker, not at construction. Returned channels
    are always owned by this client, including when QueryStub creation fails.
    """

    def __init__(
        self, endpoint: EndpointConfig, *, channel_factory: Callable[..., grpc.Channel] | None = None
    ) -> None:
        self._endpoint = endpoint
        self._channel_factory = channel_factory if channel_factory is not None else _new_channel
        self._channel: grpc.Channel | None = None
        self._stub: query_pb2_grpc.QueryStub | None = None
        self._closed = False

    def _ensure_stub(self) -> query_pb2_grpc.QueryStub:
        if self._closed:
            raise NetworkError("Bank client is closed.", operation=_OPERATION, layer="grpc") from None
        if self._stub is not None:
            return self._stub
        channel = None
        try:
            channel = self._channel_factory(
                self._endpoint.grpc_target, secure=self._endpoint.grpc_tls
            )
            stub = query_pb2_grpc.QueryStub(channel)
        except Exception:
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass
            raise NetworkError(
                "Could not initialize bank transport.", operation=_OPERATION, layer="grpc"
            ) from None
        self._channel = channel
        self._stub = stub
        return stub

    def get_page(
        self,
        address: str,
        *,
        key: bytes = b"",
        limit: int = 100,
        offset: int = 0,
        reverse: bool = False,
        count_total: bool = False,
        timeout: float,
    ) -> BalancePage:
        """Fetch exactly one page with an explicit positive per-RPC deadline."""
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, int | float)
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a finite positive number")
        if not isinstance(address, str) or not address:
            raise ValueError("address must be a nonempty string")
        if not isinstance(key, bytes):
            raise ValueError("key must be bytes")
        for name, value, minimum in (("limit", limit, 0), ("offset", offset, 0)):
            if type(value) is not int or not minimum <= value <= _UINT64_MAX:
                raise ValueError(f"{name} is outside the supported uint64 range")
        if key and offset:
            raise ValueError("key and offset cannot be combined")
        if type(reverse) is not bool or type(count_total) is not bool:
            raise ValueError("reverse and count_total must be booleans")
        request = query_pb2.QueryAllBalancesRequest(
            address=address,
            pagination=pagination_pb2.PageRequest(
                key=key, limit=limit, offset=offset, reverse=reverse, count_total=count_total
            ),
        )
        stub = self._ensure_stub()
        try:
            # No wait_for_ready, metadata, fallback, or automatic application retry.
            response = stub.AllBalances(request, timeout=timeout)
        except DecodeError:
            raise ProtocolError(
                "Invalid bank query response.", operation=_OPERATION, layer="grpc"
            ) from None
        except grpc.RpcError as error:
            try:
                code = error.code()
            except Exception:
                code = None
            status = code.name if isinstance(code, grpc.StatusCode) else None
            if code == grpc.StatusCode.DEADLINE_EXCEEDED:
                raise SDKTimeout(
                    "Bank query deadline exceeded.",
                    operation=_OPERATION,
                    layer="grpc",
                    status_code=status,
                ) from None
            raise RPCError(
                "Bank query failed.", operation=_OPERATION, layer="grpc", status_code=status
            ) from None
        except OSError:
            raise NetworkError(
                "Bank transport failed.", operation=_OPERATION, layer="grpc"
            ) from None
        except Exception:
            raise RPCError("Bank query failed.", operation=_OPERATION, layer="grpc") from None
        return self._page(response)

    @staticmethod
    def _page(response: query_pb2.QueryAllBalancesResponse) -> BalancePage:
        try:
            if not isinstance(response, query_pb2.QueryAllBalancesResponse):
                raise ValueError
            if not response.HasField("pagination"):
                raise ValueError
            balances = []
            for coin in response.balances:
                if not _AMOUNT.fullmatch(coin.amount) or not coin.denom:
                    raise ValueError
                balances.append(Coin(amount=coin.amount, denom=coin.denom))
            return BalancePage(
                balances=balances,
                next_key=bytes(response.pagination.next_key),
                total=response.pagination.total,
            )
        except Exception:
            raise ProtocolError(
                "Invalid bank query response.", operation=_OPERATION, layer="grpc"
            ) from None

    def close(self) -> None:
        """Close the owned channel once; unused clients never create a channel."""
        if self._closed:
            return
        self._closed = True
        channel, self._channel = self._channel, None
        self._stub = None
        if channel is not None:
            try:
                channel.close()
            except Exception:
                raise NetworkError(
                    "Could not close bank transport.", operation="bank.close", layer="grpc"
                ) from None
