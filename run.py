
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

class LocalValidator:
    def __init__(self, script_file) -> None:
        self.script_file = script_file
        
    def start(self):
        """
        starts a new solana-test-validator by running the given script path 
        and logs the stdout/err to the logfile 
        """
        self.log_file = open('node.txt', 'w')
        self.proc = Popen(
            f'bash {self.script_file}'.split(' '), 
            stdout=self.log_file, 
            stderr=self.log_file, 
            preexec_fn=os.setsid
        )
        time.sleep(5)

    def stop(self):
        self.log_file.close()
        os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)  

script_file = 'start_local.sh'
os.system(f'cat {script_file}')
validator = LocalValidator(script_file)

config = configs['devnet']
url = 'http://127.0.0.1:8899'
connection = AsyncClient(url)

#%%
validator.start()

#%%
state_ch = None
chs = []

for p in pathlib.Path('keypairs/').iterdir():
    with open(p, 'r') as f: 
        s = f.read()
    kp = Keypair().from_secret_key(bytearray.fromhex(s))
    
    await connection.request_airdrop(
        kp.public_key, 
        int(100 * 1e9)
    )

    # save clearing house
    wallet = Wallet(kp)
    provider = Provider(connection, wallet)

    if p.name == 'state.secret':
        print('found admin...')
        state_kp = kp 
        state_ch = Admin.from_config(config, provider)
    else:
        ch = ClearingHouse.from_config(config, provider)
        chs.append(ch)

assert state_kp is not None

#%%
await state_ch.update_auction_duration(0, 0)
await state_ch.update_max_base_asset_amount_ratio(1, 0)
await state_ch.update_market_base_asset_amount_step_size(1, 0)

#%%
from tqdm.notebook import tqdm 

net_baa = 0 
sigs = []
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

        # if position.lp_shares > 0:
        #     print('removing...', position.lp_shares)
        #     sig = await ch.remove_liquidity(position.lp_shares, 0)
        #     sigs.append(sig)

        if baa != 0:
            print('closing...', baa/1e13)
            sig = await ch.close_position(0)
            sigs.append(sig)
            net_baa += baa

net_baa

# #%%
# await ch.add_liquidity(100, 0)

#%%
market = await get_market_account(
    ch.program, 0
)
market.amm.net_base_asset_amount

#%%
while True:
    resp = await connection.get_transaction(sigs[-1])
    if resp['result'] is not None: 
        break 

#%%
msg: str = resp['result']['meta']['logMessages']
msg

#%%
total_baa = 0
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
        total_baa += abs(baa)
total_baa

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
