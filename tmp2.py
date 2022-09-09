
#%%
%load_ext autoreload
%autoreload 2

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

script_file = 'start_local.sh'
os.system(f'cat {script_file}')
validator = LocalValidator(script_file)

config = configs['devnet']
url = 'http://127.0.0.1:8899'
connection = AsyncClient(url)

#%%
validator.start()

#%%
chs, state_ch = load_local_users(config, connection)

#%%
await state_ch.update_auction_duration(0, 0)
await state_ch.update_max_base_asset_amount_ratio(1, 0)
# await state_ch.update_market_base_asset_amount_step_size(1, 0)

#%%
net_baa = 0 
sigs = []
for ch in tqdm(chs):
    position = ch.get_user_position(0)

    if len(position) > 0:
        assert len(position) == 1
        position = position[0]
        baa = position.base_asset_amount

        if position.lp_shares > 0:
            print('removing...', position.lp_shares)
            sig = await ch.remove_liquidity(position.lp_shares, 0)
            sigs.append(sig)

        # if baa != 0:
        #     print('closing...', baa/1e13)
        #     sig = await ch.close_position(0)
        #     sigs.append(sig)
        #     net_baa += baa

net_baa

#%%
while True:
    resp = await connection.get_transaction(sigs[-1])
    if resp['result'] is not None: 
        break 

#%%
market = await get_market_account(
    ch.program, 0
)
market.amm.net_base_asset_amount, market.amm.net_unsettled_lp_base_asset_amount

#%%
msg: str = resp['result']['meta']['logMessages']
msg

#%%
net_baa = 0
for ch in tqdm(chs):
    user = await get_user_account(
        ch.program, 
        ch.authority
    )
    position = [p for p in user.positions if p.market_index == 0 and (p.base_asset_amount != 0 or p.lp_shares > 0)]
    if len(position) > 0:
        assert len(position) == 1
        position = position[0]
        baa = position.base_asset_amount
        print('baa:', baa/1e13)
        net_baa += baa
net_baa

#%%
market = await get_market_account(
    ch.program, 
    0
)
market.amm.net_base_asset_amount

# %%
validator.stop()

# %%
config = configs['devnet']
url = 'https://api.devnet.solana.com'
wallet = Wallet(state_kp)
connection = AsyncClient(url)
provider = Provider(connection, wallet)
ch = ClearingHouse.from_config(config, provider)

# %%
# from driftpy.src.driftpy.types import Market
market = await get_market_account(
    ch.program, 
    0
)
net_baa = market.amm.net_base_asset_amount + market.amm.net_unsettled_lp_base_asset_amount
bar = market.amm.base_asset_reserve
qar = market.amm.quote_asset_reserve

# net_baa > 0 then users need to short & so net_baa + bar = tbar 
# net-baa < 0 then users need to long & so net_baa - b
net_baa + bar < market.amm.max_base_asset_reserve
net_baa + bar > market.amm.min_base_asset_reserve

# %%
