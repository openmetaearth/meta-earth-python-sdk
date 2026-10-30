"""Explicit read-only example. No key material is needed."""

import argparse
import asyncio
import json

from meta_earth_sdk import EndpointConfig, MetaEarthSDK, NetworkConfig, SDKConfig


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True, help="Explicit private-chain host")
    parser.add_argument("--address", required=True, help="Public me address")
    args = parser.parse_args()
    network = NetworkConfig(
        name="local-v3-beta",
        hub=EndpointConfig(
            restful_url=f"http://{args.host}:1317",
            rpc_url=f"http://{args.host}:26657",
            grpc_target=f"{args.host}:9090",
            grpc_tls=False,
        ),
        rollup=EndpointConfig(
            restful_url=f"http://{args.host}:3317",
            rpc_url=f"http://{args.host}:46657",
            grpc_target=f"{args.host}:9290",
            grpc_tls=False,
        ),
    )
    async with MetaEarthSDK(
        SDKConfig(network="local-v3-beta", network_config=network, timeout_ms=5000)
    ) as sdk:
        status = await sdk.wallet.get_network_status()
        print(json.dumps(status, ensure_ascii=False))
        page = await sdk.wallet.get_balances_page(args.address)
        print(page.model_dump_json())
        if page.next_key:
            print("More balances exist; use get_all_balances() to follow pagination.")


if __name__ == "__main__":
    asyncio.run(main())
