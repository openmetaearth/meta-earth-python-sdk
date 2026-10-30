"""Public, insecure test vector only. Never fund this wallet or reuse its phrase."""

import asyncio

from meta_earth_sdk import DirectSignDoc, MetaEarthSDK

# Public BIP39 vector. It is not a secret and must never hold assets.
PUBLIC_TEST_MNEMONIC = "abandon " * 11 + "about"


async def main() -> None:
    async with MetaEarthSDK() as sdk:
        cosmos = await sdk.wallet.create_mnemonic_wallet(PUBLIC_TEST_MNEMONIC)
        eth = await sdk.wallet.create_mnemonic_wallet(
            PUBLIC_TEST_MNEMONIC, index=1000, address_type="eth"
        )
        print("Public test addresses:", cosmos.address, eth.address)
        signer = await sdk.wallet.create_direct_secp256k1_wallet(address=eth.address)
        result = await signer.sign_direct(eth.address, DirectSignDoc(
            body_bytes=b"offline-test-body",
            auth_info_bytes=b"offline-test-auth",
            chain_id="offline-test-only",
            account_number=0,
        ))
        assert len(result.signature.signature) == 64
        print("Local Ethermint Direct signature produced; no connection or broadcast.")
        evm = await sdk.wallet.create_evm_wallet(eth.address)
        signed = await evm.sign_transaction({
            "type": 2, "chainId": 1337, "nonce": 0, "gas": 21000,
            "to": "0x" + "11" * 20, "value": 1, "data": "0x",
            "maxFeePerGas": 20, "maxPriorityFeePerGas": 1,
        })
        assert signed.raw_transaction[0] == 2
        print("Local EIP-1559 signature produced; no provider or broadcast.")
        # No secrets in default model representations or serialization. To back up
        # YOUR wallet use explicit export, and protect that output yourself.
        assert "mnemonic" not in cosmos.model_dump()
        print("Cached public addresses:", sdk.wallet.get_wallet_addresses())


if __name__ == "__main__":
    asyncio.run(main())
