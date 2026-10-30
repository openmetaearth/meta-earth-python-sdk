"""Immutable SDK configuration and explicitly pinned network defaults.

``timeout`` is the JavaScript-compatible wire alias for ``timeout_ms`` (always
milliseconds). ``timeoutMs`` is also accepted as an input alias. Python callers
can use snake_case field names throughout.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, ValidationInfo, field_validator
from pydantic import ValidationError as PydanticValidationError

from .exceptions import ValidationError as SDKValidationError
from .models import _non_blank_token, _SafeModel

__all__ = ["EndpointConfig", "NetworkConfig", "SDKConfig"]


def _validate_host(host: str) -> None:
    if not host or "%" in host:
        raise ValueError("Invalid host")
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        pass
    # Numeric dotted addresses must really be valid IP addresses, not DNS.
    if re.fullmatch(r"[0-9.]+", host):
        raise ValueError("Invalid IP address")
    hostname = host.removesuffix(".")
    if len(hostname) > 253:
        raise ValueError("Invalid host")
    for label in hostname.split("."):
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label):
            raise ValueError("Invalid host")


def _validate_port(port: str) -> None:
    if re.fullmatch(r"[0-9]+", port) is None or not 1 <= int(port) <= 65535:
        raise ValueError("Invalid port")


def _http_url(value: str) -> str:
    _non_blank_token(value)
    # Even empty query/fragment delimiters are rejected deliberately.
    if "?" in value or "#" in value or "\\" in value:
        raise ValueError("URL queries and fragments are not supported")
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("Expected an HTTP or HTTPS URL")
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise ValueError("URL credentials are not supported")
        host = parsed.hostname
        if host is None:
            raise ValueError("Invalid URL host")
        _validate_host(host)
        if parsed.netloc.startswith("["):
            closing = parsed.netloc.find("]")
            ipaddress.IPv6Address(parsed.netloc[1:closing])
            suffix = parsed.netloc[closing + 1 :]
            if suffix:
                if not suffix.startswith(":"):
                    raise ValueError("Invalid port")
                _validate_port(suffix[1:])
        elif ":" in parsed.netloc:
            if parsed.netloc.count(":") != 1:
                raise ValueError("IPv6 URL addresses must be bracketed")
            _validate_port(parsed.netloc.rsplit(":", 1)[1])
        # Accessing .port also rejects malformed/non-numeric URL ports.
        _ = parsed.port
    except (ValueError, OverflowError):
        raise ValueError("Invalid HTTP or HTTPS endpoint") from None
    return value


def _grpc_target(value: str) -> str:
    _non_blank_token(value)
    if value.startswith("["):
        match = re.fullmatch(r"\[([^\]]+)\]:([0-9]+)", value)
        if match is None or "%" in match[1]:
            raise ValueError("Expected a bracketed IPv6 host and port")
        try:
            ipaddress.IPv6Address(match[1])
        except ValueError:
            raise ValueError("Invalid IPv6 host") from None
    else:
        match = re.fullmatch(r"([^:]+):([0-9]+)", value)
        if match is None:
            raise ValueError("Expected a host and port without a URL scheme")
        _validate_host(match[1])
    _validate_port(match[2])
    return value


class EndpointConfig(_SafeModel):
    """Transport endpoints for one layer; gRPC TLS is always explicit."""

    restful_url: str = Field(
        validation_alias=AliasChoices("restfulUrl", "restful_url"),
        serialization_alias="restfulUrl",
    )
    rpc_url: str = Field(
        validation_alias=AliasChoices("rpcUrl", "rpc_url"), serialization_alias="rpcUrl"
    )
    grpc_target: str = Field(
        validation_alias=AliasChoices("grpcTarget", "grpc_target"),
        serialization_alias="grpcTarget",
    )
    grpc_tls: bool = Field(
        validation_alias=AliasChoices("grpcTls", "grpc_tls"), serialization_alias="grpcTls"
    )
    cosmos_chain_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("cosmosChainId", "cosmos_chain_id"),
        serialization_alias="cosmosChainId",
    )

    @field_validator("restful_url", "rpc_url")
    @classmethod
    def _validate_urls(cls, value: str) -> str:
        return _http_url(value)

    @field_validator("grpc_target")
    @classmethod
    def _validate_grpc(cls, value: str) -> str:
        return _grpc_target(value)

    @field_validator("cosmos_chain_id")
    @classmethod
    def _validate_cosmos_chain_id(cls, value: str | None) -> str | None:
        return None if value is None else _non_blank_token(value)


class NetworkConfig(_SafeModel):
    """Immutable endpoint set for a named network, without guessed chain IDs."""

    name: str = Field(validation_alias="name", serialization_alias="name")
    hub: EndpointConfig = Field(validation_alias="hub", serialization_alias="hub")
    rollup: EndpointConfig = Field(validation_alias="rollup", serialization_alias="rollup")
    evm_rpc_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("evmRpcUrl", "evm_rpc_url"),
        serialization_alias="evmRpcUrl",
    )
    evm_chain_id: int | None = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices("evmChainId", "evm_chain_id"),
        serialization_alias="evmChainId",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _non_blank_token(value)

    @field_validator("evm_rpc_url")
    @classmethod
    def _validate_evm_url(cls, value: str | None) -> str | None:
        return None if value is None else _http_url(value)


def _endpoint(host: str, rest_port: int, rpc_port: int, grpc_port: int) -> EndpointConfig:
    return EndpointConfig(
        restful_url=f"http://{host}:{rest_port}",
        rpc_url=f"http://{host}:{rpc_port}",
        grpc_target=f"{host}:{grpc_port}",
        grpc_tls=False,
    )


# Verified JS src/config/define.ts values. No Cosmos chain IDs are inferred.
_TESTNET = NetworkConfig(
    name="testnet",
    hub=_endpoint("118.175.0.249", 1317, 26657, 9090),
    rollup=_endpoint("118.175.0.249", 3317, 46657, 9290),
    evm_rpc_url="http://118.175.0.249:8545",
    evm_chain_id=400,
)
_MAINNET = NetworkConfig(
    name="mainnet",
    hub=_endpoint("118.175.0.247", 11317, 16657, 19090),
    rollup=_endpoint("118.175.0.247", 23013, 23011, 23012),
)


class SDKConfig(_SafeModel):
    """Strict configuration, reusable safely across SDK instances.

    A non-default network name (including ``custom``) requires network_config.
    An explicit network_config overrides a built-in endpoint set. Top-level EVM
    fields override the selected network's EVM fields when non-None.
    """

    network: str = Field(default="testnet", validation_alias="network", serialization_alias="network")
    layer: Literal["hub", "rollup"] = Field(
        default="hub", validation_alias="layer", serialization_alias="layer"
    )
    timeout_ms: int = Field(
        default=60_000,
        gt=0,
        validation_alias=AliasChoices("timeout", "timeoutMs", "timeout_ms"),
        serialization_alias="timeout",
    )
    debug: bool = Field(default=False, validation_alias="debug", serialization_alias="debug")
    network_config: NetworkConfig | None = Field(
        default=None,
        validation_alias=AliasChoices("networkConfig", "network_config"),
        serialization_alias="networkConfig",
    )
    evm_rpc_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("evmRpcUrl", "evm_rpc_url"),
        serialization_alias="evmRpcUrl",
    )
    evm_chain_id: int | None = Field(
        default=None,
        gt=0,
        validation_alias=AliasChoices("evmChainId", "evm_chain_id"),
        serialization_alias="evmChainId",
    )
    transaction_service_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices("transactionServiceUrl", "transaction_service_url"),
        serialization_alias="transactionServiceUrl",
    )
    max_pending: int = Field(
        default=16,
        gt=0,
        validation_alias=AliasChoices("maxPending", "max_pending"),
        serialization_alias="maxPending",
    )

    @field_validator("network")
    @classmethod
    def _validate_network_name(cls, value: str) -> str:
        return _non_blank_token(value)

    @field_validator("evm_rpc_url")
    @classmethod
    def _validate_evm_url(cls, value: str | None) -> str | None:
        return None if value is None else _http_url(value)

    @field_validator("transaction_service_url")
    @classmethod
    def _validate_transaction_service(cls, value: str | None) -> str | None:
        return None if value is None else _http_url(value)

    @field_validator("network_config")
    @classmethod
    def _require_custom_network_config(
        cls, value: NetworkConfig | None, info: ValidationInfo
    ) -> NetworkConfig | None:
        network = info.data.get("network", "testnet")
        if network not in ("testnet", "mainnet") and value is None:
            raise ValueError("Custom networks require network_config")
        return value

    def resolved_network(self) -> NetworkConfig:
        """Resolve immutable endpoint defaults and optional top-level EVM overrides."""
        selected = self.network_config
        if selected is None:
            selected = _TESTNET if self.network == "testnet" else _MAINNET
        if self.evm_rpc_url is None and self.evm_chain_id is None:
            return selected
        data = selected.model_dump()
        if self.evm_rpc_url is not None:
            data["evm_rpc_url"] = self.evm_rpc_url
        if self.evm_chain_id is not None:
            data["evm_chain_id"] = self.evm_chain_id
        return NetworkConfig.model_validate(data)

    def validated_patch(self, patch: dict[str, Any]) -> SDKConfig:
        """Return a fully revalidated replacement, never an unchecked model_copy.

        Snake/camel aliases are canonicalized *before* merging, so a patch cannot
        silently lose an alias to the old value. Duplicate aliases are rejected.
        A network change clears an old network_config unless explicitly patched.
        This public boundary raises the safe SDK ValidationError without chaining
        a Pydantic error or retaining the patch in exception attributes.
        """
        if not isinstance(patch, dict):
            raise SDKValidationError("Configuration patch must be a dictionary") from None
        aliases: dict[str, str] = {}
        for field_name, field in type(self).model_fields.items():
            aliases[field_name] = field_name
            alias = field.validation_alias
            if isinstance(alias, str):
                aliases[alias] = field_name
            elif isinstance(alias, AliasChoices):
                for choice in alias.choices:
                    if isinstance(choice, str):
                        aliases[choice] = field_name
        canonical: dict[str, Any] = {}
        for key, value in patch.items():
            if not isinstance(key, str) or key not in aliases:
                raise SDKValidationError("Unknown configuration patch field") from None
            name = aliases[key]
            if name in canonical:
                raise SDKValidationError("Duplicate configuration patch aliases") from None
            canonical[name] = value
        if (
            "network" in canonical
            and canonical["network"] != self.network
            and "network_config" not in canonical
        ):
            canonical["network_config"] = None
        merged = self.model_dump()
        merged.update(canonical)
        try:
            return type(self).model_validate(merged)
        except PydanticValidationError:
            raise SDKValidationError("Invalid configuration patch") from None
