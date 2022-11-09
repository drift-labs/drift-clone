# %%
%load_ext autoreload
%autoreload 2

import sys
sys.path.append('driftpy/src/')

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

script_file = 'start_local.sh'
os.system(f'cat {script_file}')
print()

validator = LocalValidator(script_file)
validator.start() # sometimes you gotta wait a bit for it to startup

# %%
config = configs['mainnet']
url = 'http://127.0.0.1:8899'
connection = AsyncClient(url)
chs, state_ch = await load_local_users(config, connection)
len(chs)

# %%
accounts = [
    p.stem
    for p in pathlib.Path('accounts').iterdir()
]

active_chs = []
for ch in chs:
    subaccount_ids = []
    stats = False
    for sid in range(10):
        user_pk = get_user_account_public_key(
            ch.program_id, ch.authority, sid
        )
        if str(user_pk) in accounts:
            subaccount_ids.append(sid)
        
        stats_pk = get_user_stats_account_public_key(
            ch.program_id, 
            ch.authority,
        )
        if str(stats_pk) in accounts:
            stats = True

    ch.subaccounts = subaccount_ids
    if len(subaccount_ids) != 0: 
        active_chs.append(ch)

# %%
state = await get_state_account(state_ch.program)
state.number_of_markets, state.number_of_spot_markets, state.admin

# %%
state_ch.authority

# %%
for i in range(state.number_of_markets): 
    await get_perp_market_account(state_ch.program, i)

for i in range(state.number_of_spot_markets): 
    await get_spot_market_account(state_ch.program, i)

# %%
await state_ch.update_perp_auction_duration(0)
await state_ch.update_lp_cooldown_time(0);

# %%
# note: sometimes need to run this twice for lps (remove on first loop then close on second)
sigs = []
ch: ClearingHouse
perp_market_idx = 0
for ch in tqdm(chs):
    for sid in ch.subaccounts:
        position = await ch.get_user_position(perp_market_idx, sid)
        if position is not None and position.lp_shares > 0:
            print('removing lp...', position.lp_shares)
            sig = await ch.remove_liquidity(position.lp_shares, perp_market_idx, sid)
            sigs.append(sig)

# verify 
while True:
    resp = await connection.get_transaction(sigs[-1])
    if resp['result'] is not None: 
        break 

market = await get_perp_market_account(state_ch.program, perp_market_idx)
market.amm.user_lp_shares

#%%
from driftpy.constants.numeric_constants import AMM_RESERVE_PRECISION

for ch in tqdm(chs):
    for sid in ch.subaccounts:
        position = await ch.get_user_position(perp_market_idx, sid)
        if position is not None and position.base_asset_amount != 0:
            print('closing...', position.base_asset_amount / AMM_RESERVE_PRECISION)
            sig = await ch.close_position(perp_market_idx, subaccount_id=sid)
            sigs.append(sig)

market = await get_perp_market_account(state_ch.program, perp_market_idx)
market.amm.base_asset_amount_with_amm

# %%
if close_out:
    # wait for txs to confirm
    while True:
        resp = await connection.get_transaction(sigs[-1])
        if resp['result'] is not None: 
            break 

market = await get_market_account(state_ch.program, 0)
net_baa, market.amm.net_base_asset_amount

# %%
# shutdown validator
validator.stop()

# %%



