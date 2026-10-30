"""Hub WASM API. Writes return hashes, never guessed code IDs or addresses."""
from __future__ import annotations

import base64
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from ..business_models import (
    checked_model,
    make_message,
    require_hub,
    signer_address,
    transaction_hash,
    validate_response,
)
from ..contract_models import (
    DeployContractParams,
    ExecuteContractParams,
    StoreCodeParams,
    WasmPagination,
    json_bytes,
)
from ..exceptions import ProtocolError, ValidationError
from ..wallet.signers import DirectSigner

if TYPE_CHECKING:
    from ..transaction import TransactionEngine
    from ..transaction_models import TransactionOptions


class ContractApi:
    def __init__(self, engine: TransactionEngine) -> None:
        self._engine = engine

    async def create_store_code(
        self, params: StoreCodeParams | Mapping[str, Any], signer: DirectSigner,
        *, options: TransactionOptions | None = None,
    ) -> str:
        request = checked_model(StoreCodeParams, params)
        await signer_address(signer, request.sender)
        value: dict[str, Any] = {"sender": request.sender, "wasm_byte_code": request.wasm_byte_code}
        if request.instantiate_permission is not None:
            value["instantiate_permission"] = request.instantiate_permission.model_dump()
        message = make_message("cosmwasm.wasm.v1.MsgStoreCode", value)
        result = await self._engine.execute(
            [message], signer, operation="wasm_upload", memo=request.memo, options=options,
        )
        return transaction_hash(result)

    async def create_instantiate_contract(
        self, params: DeployContractParams | Mapping[str, Any], signer: DirectSigner,
        *, options: TransactionOptions | None = None,
    ) -> str:
        request = checked_model(DeployContractParams, params)
        address = await signer_address(signer)
        message = make_message("cosmwasm.wasm.v1.MsgInstantiateContract", {
            "sender": address, "admin": request.admin, "code_id": str(request.code_id),
            "label": request.label, "msg": json_bytes(request.init_msg),
            "funds": [coin.model_dump() for coin in request.funds],
        })
        result = await self._engine.execute(
            [message], signer, operation="wasm_instantiate", memo=request.memo, options=options,
        )
        return transaction_hash(result)

    async def create_execute_contract(
        self, params: ExecuteContractParams | Mapping[str, Any], signer: DirectSigner,
        *, options: TransactionOptions | None = None,
    ) -> str:
        request = checked_model(ExecuteContractParams, params)
        await signer_address(signer, request.sender)
        message = make_message("cosmwasm.wasm.v1.MsgExecuteContract", {
            "sender": request.sender, "contract": request.contract_address,
            "msg": json_bytes(request.msg), "funds": [coin.model_dump() for coin in request.funds],
        })
        result = await self._engine.execute(
            [message], signer, operation="wasm_execute", memo=request.memo, options=options,
        )
        return transaction_hash(result)

    async def get_code_id_by_hash(
        self, data_hash: str, layer: str = "hub", *,
        pagination: WasmPagination | Mapping[str, Any] | None = None,
        max_pages: int = 1,
    ) -> str | None:
        require_hub(layer)
        if not isinstance(data_hash, str) or re.fullmatch(r"[0-9a-fA-F]{64}", data_hash) is None:
            raise ValidationError("A 32-byte hexadecimal WASM code hash is required")
        if type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValidationError("WASM hash lookup requires a bounded page count")
        page = checked_model(WasmPagination, pagination) if pagination is not None else None
        query = page.query_params() if page is not None else {}
        seen: set[str] = set()
        for _ in range(max_pages):
            data = await self._engine.request("/cosmwasm/wasm/v1/code", params=query)
            if not isinstance(data, Mapping) or not isinstance(data.get("code_infos"), list):
                raise ProtocolError("Missing WASM code information response field")
            normalized = dict(data)
            infos = data["code_infos"]
            wire_infos: list[dict[str, Any]] = []
            for info in infos:
                if not isinstance(info, Mapping):
                    raise ProtocolError("Invalid WASM code information")
                entry = dict(info)
                value = entry.get("data_hash")
                # The fixed JS gateway contract presents data_hash as uppercase
                # hex, while the protobuf field is bytes. Bridge only this exact
                # legacy spelling for descriptor validation, not the raw result.
                if isinstance(value, str) and re.fullmatch(r"[0-9a-fA-F]{64}", value):
                    entry["data_hash"] = base64.b64encode(bytes.fromhex(value)).decode("ascii")
                wire_infos.append(entry)
            normalized["code_infos"] = wire_infos
            validate_response("cosmwasm.wasm.v1.QueryCodesResponse", normalized)
            for info in infos:
                if info.get("data_hash") == data_hash.upper():
                    code_id = info.get("code_id")
                    if (not isinstance(code_id, str) or not code_id.isdecimal()
                            or not 0 < int(code_id) < 1 << 64):
                        raise ProtocolError("Invalid WASM code ID")
                    return code_id
            raw_pagination = data.get("pagination")
            next_key = raw_pagination.get("next_key") if isinstance(raw_pagination, Mapping) else None
            if not next_key:
                return None
            if not isinstance(next_key, str) or next_key in seen:
                raise ProtocolError("Invalid or repeated WASM pagination key")
            seen.add(next_key)
            query = {**query, "pagination.key": next_key}
            query.pop("pagination.offset", None)
        # None means no match within the explicit page window, not proof that no
        # code exists anywhere. The default preserves JS's first-page behavior.
        return None

    async def get_contracts_by_code_id(
        self, code_id: int, layer: str = "hub", *,
        pagination: WasmPagination | Mapping[str, Any] | None = None,
    ) -> Any:
        require_hub(layer)
        if type(code_id) is not int or not 0 < code_id < 1 << 64:
            raise ValidationError("A positive uint64 WASM code ID is required")
        query = checked_model(WasmPagination, pagination).query_params() if pagination is not None else {}
        data = await self._engine.request(f"/cosmwasm/wasm/v1/code/{code_id}/contracts", params=query)
        if data is not None:
            validate_response("cosmwasm.wasm.v1.QueryContractsByCodeResponse", data)
        return data

    async def get_contracts_by_creator(
        self, creator_address: str, layer: str = "hub", *,
        pagination: WasmPagination | Mapping[str, Any] | None = None,
    ) -> Any:
        require_hub(layer)
        query = checked_model(WasmPagination, pagination).query_params() if pagination is not None else {}
        data = await self._engine.request(
            f"/cosmwasm/wasm/v1/contracts/creator/{quote(creator_address, safe='')}", params=query,
        )
        if data is not None:
            validate_response("cosmwasm.wasm.v1.QueryContractsByCreatorResponse", data)
        return data

    async def query_contract_smart(self, contract_address: str, query_data: Any, layer: str = "hub") -> Any:
        require_hub(layer)
        query = quote(base64.b64encode(json_bytes(query_data)).decode("ascii"), safe="")
        # Keep the business {data: ...} wrapper; arbitrary JSON contract data is
        # not protobuf-JSON bytes and must not be decoded as QuerySmartResponse.
        return await self._engine.request(
            f"/cosmwasm/wasm/v1/contract/{quote(contract_address, safe='')}/smart/{query}", params={},
        )

    async def get_contract_info(self, contract_address: str, layer: str = "hub") -> Any:
        """Validate ME ContractInfo.extension #7, including its nested Any."""
        require_hub(layer)
        data = await self._engine.request(
            f"/cosmwasm/wasm/v1/contract/{quote(contract_address, safe='')}", params={},
        )
        if data is not None:
            validate_response("cosmwasm.wasm.v1.QueryContractInfoResponse", data)
        return data

    createStoreCode = create_store_code
    createInstantiateContract = create_instantiate_contract
    createExecuteContract = create_execute_contract
    getCodeIdByHash = get_code_id_by_hash
    getContractsByCodeId = get_contracts_by_code_id
    getContractsByCreator = get_contracts_by_creator
    queryContractSmart = query_contract_smart
    getContractInfo = get_contract_info
