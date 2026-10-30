"""Private, exact-admitted chain descriptors with wire-only cross-pool bridges."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from importlib.resources import files
from typing import Protocol, cast

from google.protobuf import descriptor_pool, json_format, message_factory
from google.protobuf.descriptor import Descriptor, FieldDescriptor, FileDescriptor
from google.protobuf.message import DecodeError, Message

from meta_earth_sdk._generated import ADMISSION_SHA256, DESCRIPTOR_SHA256
from meta_earth_sdk.exceptions import ProtocolError as SDKProtocolError


class _TypedPool(Protocol):
    def AddSerializedFile(self, serialized_file_desc_proto: bytes) -> FileDescriptor: ...
    def FindMessageTypeByName(self, full_name: str) -> Descriptor: ...


class _UnknownFieldMessage(Protocol):
    def DiscardUnknownFields(self) -> None: ...


class _AnyFields(Protocol):
    type_url: str
    value: bytes


class ProtocolError(SDKProtocolError, ValueError):
    """A payload or type is outside the exact-reviewed protocol contract."""


_NAME = re.compile(r"[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)+\Z")
_MAX_DATA = 16 * 1024 * 1024
_MAX_DEPTH = 64
# Deliberate shared-wire surface, not permission for arbitrary same-name CosmPy
# schemas. ME staking/WASM collisions must originate in this private registry.
_FOREIGN_WIRE_TYPES = frozenset({
    "google.protobuf.Any", "cosmos.base.v1beta1.Coin", "cosmos.bank.v1beta1.MsgSend",
    "cosmos.crypto.secp256k1.PubKey", "ethermint.crypto.v1.ethsecp256k1.PubKey",
})


def _wire_signature(descriptor: Descriptor) -> tuple[object, ...]:
    return tuple((field.name, field.number, field.json_name, field.type, field.label,
                  field.message_type.full_name if field.message_type else None,
                  tuple((v.name, v.number) for v in field.enum_type.values) if field.enum_type else None,
                  field.containing_oneof.name if field.containing_oneof else None,
                  field.has_presence, field.is_packed,
                  None if field.label == FieldDescriptor.LABEL_REPEATED else field.default_value)
                 for field in sorted(descriptor.fields, key=lambda field: field.number))


def _full_name(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 33 for c in value):
        raise ProtocolError("Invalid protocol type name")
    name = value.rsplit("/", 1)[-1] if "/" in value else value
    if not _NAME.fullmatch(name):
        raise ProtocolError("Invalid protocol type name")
    return name


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("Duplicate protocol JSON key")
        result[key] = value
    return result


def _invalid_constant(value: str) -> object:
    raise ProtocolError("Nonfinite protocol JSON number")


def _json(data: str | bytes) -> dict[str, object]:
    if len(data) > _MAX_DATA:
        raise ProtocolError("Protocol JSON exceeds size bound")
    try:
        value: object = json.loads(data, object_pairs_hook=_unique_object,
                                   parse_constant=_invalid_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise ProtocolError("Invalid protocol JSON") from None
    if not isinstance(value, dict):
        raise ProtocolError("Protocol JSON must be an object")
    return cast(dict[str, object], value)


def _descriptor_chunks(data: bytes) -> list[bytes]:
    """Read only FileDescriptorSet's repeated length-delimited field #1.

    No descriptor_pb2 import/register in the global pool is necessary. Individual
    bytes are pinned before they are given to the private DescriptorPool.
    """
    chunks: list[bytes] = []
    position = 0
    while position < len(data):
        if data[position] != 10:
            raise ProtocolError("Invalid packaged descriptor set")
        position += 1
        length = 0
        for shift in range(0, 70, 7):
            if position >= len(data):
                raise ProtocolError("Truncated packaged descriptor set")
            byte = data[position]
            position += 1
            length |= (byte & 127) << shift
            if byte < 128:
                break
        else:
            raise ProtocolError("Invalid packaged descriptor length")
        end = position + length
        if end > len(data):
            raise ProtocolError("Truncated packaged descriptor set")
        chunks.append(data[position:end])
        position = end
    return chunks


class RuntimeRegistry:
    """Load the full immutable Go graph into an isolated pool.

    Only explicitly reviewed non-NFT business messages are public. Having a
    descriptor as a dependency does not admit it as a public message or Any.
    Instances have no network/compiler/tools/research dependencies.
    """

    def __init__(self) -> None:
        package = files("meta_earth_sdk._generated")
        try:
            raw = package.joinpath("protocol.pb").read_bytes()
            metadata = package.joinpath("admission.json").read_bytes()
        except OSError:
            raise ProtocolError("Packaged runtime protocol resources are unavailable") from None
        if (hashlib.sha256(raw).hexdigest() != DESCRIPTOR_SHA256
                or hashlib.sha256(metadata).hexdigest() != ADMISSION_SHA256):
            raise ProtocolError("Packaged runtime protocol integrity check failed")
        admission = _json(metadata)
        records = admission.get("files")
        names = admission.get("admitted_messages")
        if (admission.get("schema_version") != 2
                or admission.get("excluded_message_prefixes") != ["cosmos.nft.", "metaearth.wnft."]
                or admission.get("descriptor_sha256") != DESCRIPTOR_SHA256
                or not isinstance(records, list) or not isinstance(names, list)
                or not all(isinstance(name, str) for name in names)):
            raise ProtocolError("Invalid packaged runtime admission")
        self._admitted = frozenset(cast(list[str], names))
        if any(name.startswith(("cosmos.nft.", "metaearth.wnft.")) for name in self._admitted):
            raise ProtocolError("NFT functionality is not admitted")
        chunks = _descriptor_chunks(raw)
        if len(chunks) != len(records):
            raise ProtocolError("Packaged descriptor coverage mismatch")
        pending: dict[str, tuple[bytes, set[str]]] = {}
        for chunk, record in zip(chunks, records, strict=True):
            if not isinstance(record, dict):
                raise ProtocolError("Invalid packaged descriptor record")
            name, dependencies = record.get("name"), record.get("dependencies")
            if (not isinstance(name, str) or name in pending
                    or not isinstance(dependencies, list)
                    or not all(isinstance(dependency, str) for dependency in dependencies)
                    or hashlib.sha256(chunk).hexdigest() != record.get("sha256")):
                raise ProtocolError("Packaged descriptor identity mismatch")
            pending[name] = (chunk, set(cast(list[str], dependencies)))
        self._pool = descriptor_pool.DescriptorPool()
        loaded: set[str] = set()
        while pending:
            ready = sorted(name for name, (_, dependencies) in pending.items() if dependencies <= loaded)
            if not ready:
                raise ProtocolError("Incomplete or cyclic packaged descriptor graph")
            for name in ready:
                try:
                    descriptor = cast(_TypedPool, self._pool).AddSerializedFile(pending[name][0])
                except (TypeError, ValueError):
                    raise ProtocolError("Private pool rejected packaged descriptor") from None
                if descriptor.name != name:
                    raise ProtocolError("Packaged descriptor name mismatch")
                loaded.add(name)
                del pending[name]
        for name in self._admitted:
            try:
                cast(_TypedPool, self._pool).FindMessageTypeByName(name)
            except KeyError:
                raise ProtocolError("Admitted message missing from packaged graph") from None

    @property
    def admitted_types(self) -> frozenset[str]:
        return self._admitted

    def _descriptor(self, type_name: str) -> Descriptor:
        name = _full_name(type_name)
        if name not in self._admitted:
            raise ProtocolError("Protocol message type is not admitted")
        return cast(_TypedPool, self._pool).FindMessageTypeByName(name)

    def _new(self, type_name: str) -> Message:
        return message_factory.GetMessageClass(self._descriptor(type_name))()

    def _normalise(self, descriptor: Descriptor, value: Mapping[str, object], depth: int) -> object:
        if depth > _MAX_DEPTH:
            raise ProtocolError("Protocol message nesting exceeds bound")
        if descriptor.full_name == "google.protobuf.Any":
            if "@type" in value:
                type_url = value["@type"]
                if not isinstance(type_url, str):
                    raise ProtocolError("Invalid Any type URL")
                target = self._descriptor(type_url)
                body = {key: item for key, item in value.items() if key != "@type"}
                normal = self._normalise(target, body, depth + 1)
                if not isinstance(normal, dict):
                    return {"@type": type_url, "value": normal}
                return {"@type": type_url, **normal}
            type_url = value.get("type_url", value.get("typeUrl"))
            payload = value.get("value")
            if (set(value) - {"type_url", "typeUrl", "value"}
                    or ("type_url" in value and "typeUrl" in value)
                    or not isinstance(type_url, str)):
                raise ProtocolError("Invalid wire Any mapping")
            wire = self._wire_value(payload)
            decoded = self.decode(type_url, wire)
            return {"@type": type_url, **self.to_dict(decoded)}
        result: dict[str, object] = {}
        aliases = {field.json_name: field for field in descriptor.fields}
        aliases.update(descriptor.fields_by_name)
        used: set[str] = set()
        for key, item in value.items():
            if not isinstance(key, str) or key not in aliases:
                raise ProtocolError("Unknown protocol JSON field")
            field = aliases[key]
            if field.name in used:
                raise ProtocolError("Duplicate protocol JSON field alias")
            used.add(field.name)
            if field.label == FieldDescriptor.LABEL_REPEATED:
                if (field.message_type is not None
                        and field.message_type.GetOptions().map_entry):
                    if not isinstance(item, Mapping):
                        raise ProtocolError("Invalid protocol map field")
                    map_value = field.message_type.fields_by_name["value"]
                    result[key] = {str(k): self._normalise_field(map_value, v, depth + 1)
                                   for k, v in item.items()}
                else:
                    if not isinstance(item, list | tuple):
                        raise ProtocolError("Invalid repeated protocol field")
                    result[key] = [self._normalise_field(field, v, depth + 1) for v in item]
            else:
                result[key] = self._normalise_field(field, item, depth + 1)
        return result

    def bridge(self, message: Message) -> Message:
        """Validate pool/schema identity before a deliberate wire-only bridge.

        Foreign same-name schemas are accepted only on the tiny explicit shared
        surface; a concrete ME business message must use the exact Go schema.
        """
        if not isinstance(message, Message):
            raise ProtocolError("Expected protobuf message")
        descriptor = self._descriptor(message.DESCRIPTOR.full_name)
        if message.DESCRIPTOR.file.pool is not self._pool:
            if message.DESCRIPTOR.full_name not in _FOREIGN_WIRE_TYPES:
                # Another private RuntimeRegistry with the same exact descriptor
                # graph is valid; similarly named CosmPy conflicts are not.
                if message.DESCRIPTOR.file.serialized_pb != descriptor.file.serialized_pb:
                    raise ProtocolError("Foreign protocol schema is not shared-wire admitted")
            if _wire_signature(message.DESCRIPTOR) != _wire_signature(descriptor):
                raise ProtocolError("Foreign protocol field schema does not match authority")
        return self.decode(descriptor.full_name, message.SerializeToString(deterministic=True))

    def _normalise_field(self, field: FieldDescriptor, value: object, depth: int) -> object:
        if value is None:
            return None
        if field.message_type is not None:
            if isinstance(value, Message):
                if value.DESCRIPTOR.full_name != field.message_type.full_name:
                    raise ProtocolError("Nested message type does not match field")
                value = self.to_dict(self.bridge(value))
            if isinstance(value, Mapping):
                return self._normalise(field.message_type, cast(Mapping[str, object], value), depth)
            # Timestamp and Duration have canonical scalar ProtoJSON forms.
            if (field.message_type.full_name in {"google.protobuf.Timestamp", "google.protobuf.Duration"}
                    and isinstance(value, str)):
                return value
            raise ProtocolError("Invalid nested protocol message")
        if field.type == FieldDescriptor.TYPE_BYTES and isinstance(value, bytes):
            return base64.b64encode(value).decode("ascii")
        return value

    def make_message(self, type_name: str, mapping: Mapping[str, object]) -> Message:
        return self.parse_json(type_name, mapping)

    def parse_json(self, full_name: str, data: Mapping[str, object] | str | bytes) -> Message:
        descriptor = self._descriptor(full_name)
        value = _json(data) if isinstance(data, str | bytes) else data
        if not isinstance(value, Mapping):
            raise ProtocolError("Protocol JSON must be an object")
        try:
            normal = self._normalise(descriptor, value, 0)
            message = self._new(full_name)
            json_format.ParseDict(normal, message, ignore_unknown_fields=False,
                                  descriptor_pool=self._pool, max_recursion_depth=_MAX_DEPTH)
            self._validate(message, 0)
            if len(message.SerializeToString(deterministic=True)) > _MAX_DATA:
                raise ProtocolError("Protocol message exceeds size bound")
            return message
        except (json_format.ParseError, ValueError, TypeError, OverflowError, RecursionError) as exc:
            if isinstance(exc, ProtocolError):
                raise
            raise ProtocolError("Invalid protocol message JSON") from None

    def decode(self, full_name: str, wire: bytes) -> Message:
        return self._decode(full_name, wire, 0)

    def _decode(self, full_name: str, wire: bytes, depth: int) -> Message:
        if depth > _MAX_DEPTH:
            raise ProtocolError("Protocol message nesting exceeds bound")
        if not isinstance(wire, bytes) or len(wire) > _MAX_DATA:
            raise ProtocolError("Invalid or oversized protocol wire payload")
        message = self._new(full_name)
        try:
            message.ParseFromString(wire)
            canonical = message.SerializeToString(deterministic=True)
            clean = self._new(full_name)
            clean.ParseFromString(canonical)
            cast(_UnknownFieldMessage, clean).DiscardUnknownFields()
            if clean.SerializeToString(deterministic=True) != canonical:
                raise ProtocolError("Unknown protocol wire field")
            self._validate(message, depth)
        except (DecodeError, ValueError, RecursionError) as exc:
            if isinstance(exc, ProtocolError):
                raise
            raise ProtocolError("Invalid protocol wire payload") from None
        return message

    def _validate(self, message: Message, depth: int) -> None:
        if depth > _MAX_DEPTH:
            raise ProtocolError("Protocol message nesting exceeds bound")
        self._descriptor(message.DESCRIPTOR.full_name)
        if message.DESCRIPTOR.full_name == "google.protobuf.Any":
            type_url = cast(_AnyFields, message).type_url
            value = cast(_AnyFields, message).value
            if not type_url:
                raise ProtocolError("Any type URL must be explicit")
            self._decode(type_url, value, depth + 1)
            return
        for field, value in message.ListFields():
            if field.message_type is None:
                continue
            if field.label == FieldDescriptor.LABEL_REPEATED:
                if field.message_type.GetOptions().map_entry:
                    if field.message_type.fields_by_name["value"].message_type is not None:
                        for nested in cast(Mapping[object, Message], value).values():
                            self._validate(nested, depth + 1)
                else:
                    for nested in cast(Iterable[Message], value):
                        self._validate(nested, depth + 1)
            else:
                self._validate(cast(Message, value), depth + 1)

    @staticmethod
    def _wire_value(value: object) -> bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            if len(value) > ((_MAX_DATA + 2) // 3) * 4:
                raise ProtocolError("Any Base64 value exceeds size bound")
            try:
                return base64.b64decode(value, validate=True)
            except (ValueError, UnicodeError):
                raise ProtocolError("Invalid Any Base64 value") from None
        raise ProtocolError("Any value must be bytes or Base64")

    def decode_any(self, value: Message | Mapping[str, object]) -> Message:
        type_url: object
        if isinstance(value, Message):
            if value.DESCRIPTOR.full_name != "google.protobuf.Any":
                raise ProtocolError("Expected google.protobuf.Any")
            type_url = cast(_AnyFields, value).type_url
            wire = cast(_AnyFields, value).value
            return self.decode(type_url, wire)
        if not isinstance(value, Mapping):
            raise ProtocolError("Expected Any message or mapping")
        if "@type" in value:
            type_url = value["@type"]
            if not isinstance(type_url, str):
                raise ProtocolError("Invalid Any type URL")
            return self.parse_json(type_url, {key: item for key, item in value.items() if key != "@type"})
        if set(value) - {"type_url", "typeUrl", "value"} or ("type_url" in value and "typeUrl" in value):
            raise ProtocolError("Invalid wire Any mapping")
        type_url = value.get("type_url", value.get("typeUrl"))
        if not isinstance(type_url, str):
            raise ProtocolError("Invalid Any type URL")
        return self.decode(type_url, self._wire_value(value.get("value")))

    def pack(self, message: Message, *, type_url_prefix: str = "/") -> Message:
        if not isinstance(message, Message):
            raise ProtocolError("Expected protobuf message")
        validated = self.bridge(message)
        if validated.DESCRIPTOR.full_name == "google.protobuf.Any":
            return validated  # bridge an Any; never wrap it as a business payload again
        if not type_url_prefix or any(ord(c) < 33 for c in type_url_prefix):
            raise ProtocolError("Invalid Any type URL prefix")
        result = self._new("google.protobuf.Any")
        cast(_AnyFields, result).type_url = type_url_prefix.rstrip("/") + "/" + validated.DESCRIPTOR.full_name
        cast(_AnyFields, result).value = validated.SerializeToString(deterministic=True)
        return result

    def to_dict(self, message: Message) -> dict[str, object]:
        message = self.bridge(message)
        try:
            return cast(dict[str, object], json_format.MessageToDict(
                message, preserving_proto_field_name=True, descriptor_pool=self._pool,
                always_print_fields_with_no_presence=True))
        except (ValueError, TypeError, RecursionError):
            raise ProtocolError("Cannot encode protocol JSON") from None


@lru_cache(maxsize=1)
def get_registry() -> RuntimeRegistry:
    return RuntimeRegistry()


def make_message(type_name: str, mapping: Mapping[str, object]) -> Message:
    return get_registry().make_message(type_name, mapping)


def parse_json(full_name: str, data: Mapping[str, object] | str | bytes) -> Message:
    return get_registry().parse_json(full_name, data)


def decode_any(value: Message | Mapping[str, object]) -> Message:
    return get_registry().decode_any(value)


def pack(message: Message, *, type_url_prefix: str = "/") -> Message:
    return get_registry().pack(message, type_url_prefix=type_url_prefix)
