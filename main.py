#%%
# get all user accounts 
# get market accounts 
# save them locally 
# modify user & user stats pdas with kps which we own 
# run a local validator with accounts preloaded 
# run close all simulation 

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

state_kp = Keypair()

accounts_dir = pathlib.Path('accounts/')
if accounts_dir.exists():
    print('removing existing accounts...')
    shutil.rmtree(accounts_dir)

keypairs_dir = pathlib.Path('keypairs/')
if keypairs_dir.exists():
    print('removing existing keypairs...')
    shutil.rmtree(keypairs_dir)

accounts_dir.mkdir(parents=True, exist_ok=True)
keypairs_dir.mkdir(parents=True, exist_ok=True)

config = configs['devnet']
url = 'https://api.devnet.solana.com'
wallet = Wallet(state_kp)
connection = AsyncClient(url)
provider = Provider(connection, wallet)
ch = ClearingHouse.from_config(config, provider)
print(ch.program_id)

# %%
def save_account_info(
    path: pathlib.Path,
    account_info, 
    pubkey: PublicKey
):
    pubkey = str(pubkey)
    local_account = {
        'account': account_info,
        'pubkey': pubkey
    }
    with open(path, 'w') as f: 
        json.dump(local_account, f)
    
async def batch_get_account_infos(
    addresses,
    batch_size = 100,
):
    account_infos = []
    for i in tqdm(range(0, len(addresses), batch_size)):
        batch_addresses = addresses[i: i+batch_size]
        batch_account_infos = (await connection.get_multiple_accounts(
            batch_addresses
        ))['result']['value']
        account_infos += batch_account_infos
    return account_infos

async def download_all_accounts(
    account_type: str, 
    account_dir: pathlib.Path,
    batch_size: int = 100
): 
    path = account_dir/account_type
    path.mkdir(parents=True, exist_ok=True)

    accounts = await ch.program.account[account_type].all()
    addresses = [a.public_key for a in accounts]
    if account_type == 'Market':
        addresses += [a.account.amm.oracle for a in accounts]
    elif account_type == 'Bank':
        addresses += [a.account.oracle for a in accounts]
    elif account_type == 'State':
        assert len(addresses) == 1 

    print(f'found {len(accounts)} accounts...')
    
    account_infos = await batch_get_account_infos(
        addresses,
        batch_size
    )
    
    for account_info, pubkey in zip(account_infos, addresses):
        save_account_info(
            path/(str(pubkey) + '.json'), 
            account_info, 
            str(pubkey)
        )

#%%
# types = ch.program.account.keys()
## clone with no mods
types = [
    "Bank", 
    "Market", 
]
for account_type in types: 
    print(f'saving account type: {account_type}')
    await download_all_accounts(
        account_type,
        accounts_dir
    )

#%%
lamports = 6674640
state_accounts = await ch.program.account["State"].all()
assert len(state_accounts) == 1
state = state_accounts[0]

state_path = accounts_dir/"State"
state_path.mkdir(parents=True, exist_ok=True)

state.account.admin = state_kp.public_key

anchor_state = Instruction(data=state.account, name="State")
data = ch.program.account["State"]._coder.accounts.build(anchor_state)
acc_data = base64.b64encode(data).decode("utf-8")
account_info = {
    'data': [acc_data, 'base64'],
    'executable': False,
    'lamports': lamports,
    'owner': str(ch.program_id),
    'rentEpoch': 367
}
save_account_info(
    state_path/(str(state.public_key) + '.json'), 
    account_info, 
    str(state.public_key)
)

#%%
rent_epoch = 365 # hardcoded for now
user_lamports = 63231600 # hardcoded for now
user_accounts = await ch.program.account["User"].all()

# user_pks = [a.public_key for a in user_accounts]
# user_account_infos = await batch_get_account_infos(user_pks, 100)

user_stats_lamports = 2289840 # hardcoded 
user_stats_accounts = await ch.program.account["UserStats"].all()

# user_stats_pks = [a.public_key for a in user_stats_accounts]
# user_stats_account_infos = await batch_get_account_infos(user_stats_pks, 100)

full_users = {}
for user in user_accounts:
    user_stats = [us for us in user_stats_accounts if us.account.authority == user.account.authority][0]
    full_users[str(user.account.authority)] = {"user": user, "user_stats": user_stats}

#%%
user_path = accounts_dir/"User"
user_path.mkdir(parents=True, exist_ok=True)

user_stats_path = accounts_dir/"UserStats"
user_stats_path.mkdir(parents=True, exist_ok=True)

pk_2_kp = {}
for old_auth in full_users.keys():
    user = full_users[old_auth]['user']
    user_stats = full_users[old_auth]['user_stats']

    kp = Keypair()
    pk_2_kp[str(kp.public_key)] = kp
    user_obj: User = user.account
    user_stats_obj: UserStats = user_stats.account

    # change authority key 
    user_obj.authority = kp.public_key
    user_stats_obj.authority = kp.public_key

    # rederive pda addresses 
    new_user_pk = get_user_account_public_key(
        ch.program_id, 
        kp.public_key
    )
    new_user_stats_pk = get_user_stats_account_public_key(
        ch.program_id, 
        kp.public_key
    )

    # save account infos for solana-test-validator
    anchor_user = Instruction(data=user_obj, name="User")
    data = ch.program.account["User"]._coder.accounts.build(anchor_user)
    user_acc_data = base64.b64encode(data).decode("utf-8")
    user_account_info = {
        'data': [user_acc_data, 'base64'],
        'executable': False,
        'lamports': user_lamports,
        'owner': str(ch.program_id),
        'rentEpoch': rent_epoch
    }
    save_account_info(
        user_path/(str(new_user_pk) + '.json'), 
        user_account_info, 
        str(new_user_pk)
    )

    anchor_user_stats = Instruction(data=user_stats_obj, name="UserStats")
    data = ch.program.account["UserStats"]._coder.accounts.build(anchor_user_stats)
    user_stats_acc_data = base64.b64encode(data).decode("utf-8")
    user_stats_account_info = {
        'data': [user_stats_acc_data, 'base64'],
        'executable': False,
        'lamports': user_stats_lamports,
        'owner': str(ch.program_id),
        'rentEpoch': rent_epoch
    }
    save_account_info(
        user_stats_path/(str(new_user_stats_pk) + '.json'), 
        user_stats_account_info, 
        str(new_user_stats_pk)
    )

    # save auth kp
    with open(keypairs_dir/f'{kp.public_key}.secret', 'w') as f: 
        f.write(str(kp.secret_key))

#%%
def setup_validator_script(
    validator_path: str,
    script_file: str
):
    # load accounts
    validator_str = f"#!/bin/bash\n{validator_path}"
    for d in accounts_dir.iterdir():
        if '.so' not in str(d):
            validator_str += f' --account-dir {d}'    

    # load program
    # https://github.com/drift-labs/protocol-v2/blob/master/sdk/src/config.ts
    program_address = str(ch.program_id)
    program_path = f"{accounts_dir}/{program_address}.so"
    # d = devnet
    command = f"solana program dump -u d {program_address} {program_path}"
    os.system(command)
    validator_str += f' --bpf-program {program_address} {program_path}'

    # hard reset 
    validator_str += ' --reset'

    with open(script_file, 'w') as f: 
        f.write(validator_str)

validator_path = './solana/target/debug/solana-test-validator'
script_file = 'start_local.sh'
setup_validator_script(
    validator_path,
    script_file
)

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

validator = LocalValidator(script_file)
validator.start()

#%%
config = configs['devnet']
url = 'http://127.0.0.1:8899'
connection = AsyncClient(url)

#%%
chs = {}
for kp in pk_2_kp.values():
    await connection.request_airdrop(
        kp.public_key, 
        int(100 * 1e9)
    )

    # save clearing house
    wallet = Wallet(kp)
    provider = Provider(connection, wallet)
    ch = ClearingHouse.from_config(config, provider)
    chs[str(kp.public_key)] = ch

#%%
await connection.request_airdrop(
    state_kp.public_key, 
    int(100 * 1e9)
)

#%%
wallet = Wallet(state_kp)
provider = Provider(connection, wallet)
ch = ClearingHouse.from_config(config, provider, admin=True)
await ch.update_auction_duration(0, 0)

#%%
ch = chs[list(chs.keys())[1]]
user = await get_user_account(
    ch.program, 
    ch.authority
)
[p for p in user.positions if p.market_index == 0][0].base_asset_amount

#%%
state = await get_state_account(ch.program)
state.max_auction_duration, state.min_auction_duration

#%%
sig = await ch.close_position(0)

#%%
sig = await ch.add_liquidity(100, 0)

#%%
await connection.get_transaction(sig)

#%%
#%%
from tqdm.notebook import tqdm 

total_baa = 0 
sigs = []
for ch in tqdm(chs.values()):
    user = await get_user_account(
        ch.program, 
        ch.authority
    )
    position = [p for p in user.positions if p.market_index == 0][0]
    baa = position.base_asset_amount

    if position.lp_shares > 0:
        print('removeing...', position.lp_shares)
        sig = await ch.remove_liquidity(position.lp_shares, 0)
        sigs.append(sig)

    if baa != 0:
        print('closing...', baa/1e13)
        sig = await ch.close_position(0)
        sigs.append(sig)
        total_baa += abs(baa)
total_baa

#%%
while True:
    resp = await connection.get_transaction(sigs[0])
    if resp['result'] is not None: 
        break 

#%%
total_baa = 0
for ch in tqdm(chs.values()):
    user = await get_user_account(
        ch.program, 
        ch.authority
    )
    baa = [p for p in user.positions if p.market_index == 0][0].base_asset_amount
    total_baa += abs(baa)
total_baa

#%%
market = await get_market_account(
    ch.program, 
    0
)
market.amm.net_base_asset_amount

# %%
await get_bank_account(
    ch.program, 0
)

# %%
ch.program.program_id

# %%
validator.stop()

# %%


# %%
