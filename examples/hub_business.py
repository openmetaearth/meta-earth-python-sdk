"""Hub business queries only. This example never signs or broadcasts.

Writes are explicit async SDK methods (stake_fixed, vote_proposal, store_code,
...) and require a caller-owned wallet plus transaction fee/gas caps. No keys,
seed phrases, deployment payloads or automatic business writes live here.
"""
from __future__ import annotations

import argparse
import asyncio

from meta_earth_sdk import MetaEarthSDK


async def inspect(address: str) -> None:
    async with MetaEarthSDK() as sdk:
        identity = await sdk.identity.get_me_id_by_address(address)
        print("Has ME ID:", identity.has_me_id)
        if identity.info is not None:
            print("Region:", identity.info.region_id)
        positions = await sdk.staking.get_fixed_deposits(address)
        for position in positions:
            print("Fixed deposit:", position.id, position.term, position.rate)
        proposals = await sdk.governance.get_proposals()
        print("First-page proposals:", len(proposals))
        contracts = await sdk.contract.get_contracts_by_creator(address)
        print("First-page contracts:", contracts)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Read Hub business state without a wallet")
    parser.add_argument("address", help="Public ME account address")
    arguments = parser.parse_args()
    asyncio.run(inspect(arguments.address))
