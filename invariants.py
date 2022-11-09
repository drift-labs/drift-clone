#%%
import sys
sys.path.append('driftpy/src/')

import driftpy
print(driftpy.__path__)

from driftpy.types import User
from driftpy.constants.config import configs
from anchorpy import Provider
import json 
from anchorpy import Wallet
from solana.rpc.async_api import AsyncClient
from driftpy.clearing_house import ClearingHouse
from driftpy.accounts import *
from solana.publickey import PublicKey
from solana.keypair import Keypair
import pathlib 
from tqdm.notebook import tqdm 
import shutil
from anchorpy import Instruction
import base64
from subprocess import Popen
import os 
import time
import signal
from driftpy.admin import Admin
from helpers import *
from tqdm.notebook import tqdm 

from driftpy.clearing_house import is_available

async def validate_market_metrics(program, config):
    user_accounts = await program.account["User"].all()
    n_markets = len(config.markets)

    for market_index in range(n_markets):
        market = await get_perp_market_account(
            program, 
            market_index
        )
        market_total_baa = market.amm.net_base_asset_amount + market.amm.net_unsettled_lp_base_asset_amount 

        lp_shares = 0
        user_total_baa = 0 
        for user in user_accounts:
            user: User = user.account
            position: list[PerpPosition] = [p for p in user.perp_positions if p.market_index == market_index and not is_available(p)]
            if len(position) == 0: continue
            assert len(position) == 1
            position = position[0]
            
            user_total_baa += position.base_asset_amount
            lp_shares += position.lp_shares

        assert lp_shares == market.amm.user_lp_shares, f"lp shares out of wack: {lp_shares} {market.amm.user_lp_shares}"
        assert user_total_baa == market_total_baa, f"market {market_index}: user baa != market baa ({user_total_baa} {market_total_baa})"
    
    print('market invariants validated!')

async def main():
    script_file = 'start_local.sh'
    os.system(f'cat {script_file}')
    print()
    validator = LocalValidator(script_file)
    validator.start()

    config = configs['mainnet'] # cloned 
    url = 'http://127.0.0.1:8899'
    connection = AsyncClient(url)

    kp = Keypair()
    wallet = Wallet(kp)
    provider = Provider(connection, wallet)
    ch = ClearingHouse.from_config(config, provider)

    print('validating...')
    await validate_market_metrics(ch.program, config)

    validator.stop()
    print('done :)')

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())    
