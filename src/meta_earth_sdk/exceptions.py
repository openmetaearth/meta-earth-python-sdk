"""Public SDK exceptions with a deliberately small, explicit context surface.

Messages must be SDK-authored summaries, never response bodies, credentials, or
``str(original_exception)``. At an SDK boundary use ``raise ... from None`` so
transport/Pydantic exceptions are not exposed through the exception chain.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

__all__ = [
    "SDKError",
    "ValidationError",
    "NotInitializedError",
    "ClosedError",
    "BusyError",
    "NetworkError",
    "TimeoutError",
    "RPCError",
    "ProtocolError",
    "WalletNotFoundError",
    "SigningError",
]


class SDKError(Exception):
    """Base error; only the named, non-payload context fields are accepted.

    ``str(error)`` and ``repr(error)`` contain only the SDK-authored message.
    Context is available explicitly via attributes and the read-only ``context``
    mapping; it is never implicitly formatted into an error message.
    """

    def __init__(
        self,
        message: str,
        *,
        operation: str | None = None,
        network: str | None = None,
        layer: str | None = None,
        status_code: int | str | None = None,
    ) -> None:
        if not isinstance(message, str):
            raise TypeError("SDK error messages must be strings")
        for value in (operation, network, layer):
            if value is not None and not isinstance(value, str):
                raise TypeError("SDK error context labels must be strings")
        if status_code is not None and type(status_code) not in (int, str):
            raise TypeError("SDK status codes must be integers or strings")
        super().__init__(message)
        self.message = message
        self.operation = operation
        self.network = network
        self.layer = layer
        self.status_code = status_code
        self.context: Mapping[str, str | int] = MappingProxyType(
            {
                key: value
                for key, value in (
                    ("operation", operation),
                    ("network", network),
                    ("layer", layer),
                    ("status_code", status_code),
                )
                if value is not None
            }
        )


class ValidationError(SDKError):
    """An SDK input did not pass public-boundary validation."""


class NotInitializedError(SDKError):
    """An operation requires an initialized SDK."""


class ClosedError(SDKError):
    """An operation was attempted after the SDK was closed."""


class BusyError(SDKError):
    """The configured pending-operation capacity has been reached."""


class NetworkError(SDKError):
    """A transport operation failed without exposing the underlying error."""


class TimeoutError(NetworkError):
    """An operation exceeded its deadline."""


class RPCError(SDKError):
    """An RPC reported failure; status_code may be an integer or a string."""


class ProtocolError(SDKError):
    """A remote response did not conform to the expected protocol."""


class WalletNotFoundError(SDKError):
    """No local wallet exists for the requested address."""


class SigningError(SDKError):
    """An offline cryptographic operation could not be completed safely."""
