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

kp = Keypair()
await connection.request_airdrop(
    kp.public_key, 
    int(100 * 1e9)
)

config = configs['devnet']
# url = 'https://api.devnet.solana.com'
url = 'http://127.0.0.1:8899'
wallet = Wallet(kp)
connection = AsyncClient(url)
provider = Provider(connection, wallet)
ch = ClearingHouse.from_config(config, provider)
print(ch.program_id)

# %%
market = await get_market_account(
    ch.program, 
    0
)
market.amm.net_base_asset_amount, market.amm.net_unsettled_lp_base_asset_amount

# %%
user_accounts = await ch.program.account["User"].all()
net_baa = 0
for user in user_accounts:
    user: User = user.account
    position = [p for p in user.positions if p.market_index == 0 and p.base_asset_amount != 0]
    if len(position) > 0:
        # print(user.authority)
        # print(position[0].base_asset_amount/1e13)
        assert len(position) == 1
        net_baa += position[0].base_asset_amount
net_baa

# %%
net_baa == market.amm.net_unsettled_lp_base_asset_amount + market.amm.net_base_asset_amount

# %%
for user in user_accounts:
    user: User = user.account
    position = [p for p in user.positions if p.market_index == 0 and p.base_asset_amount != 0]
    if len(position) > 0:
        assert len(position) == 1
        position = position[0]
        _position = position
        if position.lp_shares > 0:
            print('settling...')
            sig = await ch.settle_lp(user.authority, 0)

while True:
    resp = await connection.get_transaction(sig)
    if resp['result'] is not None: 
        break 

#%%
market = await get_market_account(
    ch.program, 
    0
)
market.amm.net_base_asset_amount, market.amm.net_unsettled_lp_base_asset_amount

# %%
_position.last_net_base_asset_amount_per_lp, market.amm.market_position_per_lp.base_asset_amount

# %%
