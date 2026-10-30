# Meta Earth Python SDK

An asynchronous Python SDK for Meta Earth Hub. Python 3.11 or later is required.

The package provides node and balance queries, local wallets, Cosmos and
Ethermint Direct signing, transaction submission, identity, staking, governance
and WASM interfaces. Native EVM signing is local; EVM contract access requires
an explicitly injected provider. This package is a development preview.

Read the [usage guide](docs/SDK_USAGE.md) for configuration, API examples and
transaction handling. See [compatibility](docs/COMPATIBILITY.md) for supported
formats and limits, the [code guide](docs/CODE_READING_GUIDE.md) for the source
layout and the [protocol guide](docs/METAEARTH_PROTO_GUIDE.md) for message types.

## Installation

From the repository root:

```sh
python3.12 -m venv .venv
.venv/bin/python -I -m pip --isolated install \
  --index-url https://pypi.org/simple -c constraints-dev.txt .
```

The distribution name is `meta-earth-python-sdk`; the import name is
`meta_earth_sdk`. Use a published wheel when one is provided by the maintainers.

## Read-only queries

Supply the endpoints and Cosmos chain ID provided by your network operator:

```python
import asyncio

from meta_earth_sdk import EndpointConfig, MetaEarthSDK, NetworkConfig, SDKConfig


async def main():
    hub = EndpointConfig(
        restful_url="https://rest.example.com",
        rpc_url="https://rpc.example.com",
        grpc_target="grpc.example.com:443",
        grpc_tls=True,
        cosmos_chain_id="your-cosmos-chain-id",
    )
    network = NetworkConfig(name="my-hub", hub=hub, rollup=hub)
    config = SDKConfig(network="my-hub", network_config=network, timeout_ms=10_000)
    async with MetaEarthSDK(config) as sdk:
        status = await sdk.wallet.get_network_status("hub")
        page = await sdk.wallet.get_balances_page("your-public-me-address", "hub")
        print(status)
        print(page.model_dump_json())


if __name__ == "__main__":
    asyncio.run(main())
```

Replace the example values before running. The configuration model requires a
`rollup` endpoint set for API compatibility; the calls above use Hub only.
Initialization prepares local resources. Actual queries connect to the node.
Public queries do not need a wallet or private key.

See [read-only queries](examples/read_only.py),
[offline wallets](examples/offline_wallet.py) and
[Hub business queries](examples/hub_business.py) for executable examples.

## Transactions and limits

Amounts are exact strings or integers in the units specified by each API.
Cosmos transactions require an explicit chain ID. Simulation and broadcasting
are separate operations. Keep the original network context and pending handle
when reconciling an uncertain submission; a returned hash is not confirmation.

The runtime uses a private protobuf descriptor pool and an explicit message
allowlist. Bundled dependencies do not expose NFT operations or imply complete
compatibility with every chain deployment. Some vendored source schemas differ
from the runtime descriptors; do not substitute upstream Cosmos message types
for Meta Earth business messages.

The default Web3 provider is disabled. Installing Web3 alone does not enable
EVM contract calls. The coordinator does not provide cross-process locking or
durable recovery. Review the [usage guide](docs/SDK_USAGE.md) and validate the
required operations against your deployment before use.

## Contributing

Install the editable package with its development dependencies:

```sh
.venv/bin/python -I -m pip --isolated install \
  --index-url https://pypi.org/simple -c constraints-dev.txt -e '.[dev]'
.venv/bin/python -I -B -m pytest -m 'not live'
.venv/bin/python -I -m ruff check src tests examples tools
.venv/bin/python -I -m mypy
```

Live tests require explicit endpoint and public-address configuration and
`ME_SDK_LIVE=1`. They issue read-only requests.

`tools/generate_runtime_protocol.py` validates the vendored runtime descriptors,
message allowlist and integrity pins, and produces the package metadata in a
new output directory. It does not establish source-schema equivalence:

```sh
.venv/bin/python -I -B tools/generate_runtime_protocol.py \
  --output-dir /absolute/path/to/new-output
.venv/bin/python -I -m build --no-isolation
```
