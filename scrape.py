#%%
# get all user accounts 
# get market accounts 
# save them locally 
# modify user & user stats pdas with kps which we own 
# run a local validator with accounts preloaded 
# run close all simulation 

# todo: full script -- rn only notebook
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
# url = 'https://api.devnet.solana.com'
url = "http://3.220.170.22:8899"
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

    if path.exists(): 
        print(f'overwriting path {path}...')

    with open(path, 'w') as f: 
        json.dump(local_account, f)
    
async def batch_get_account_infos(
    addresses,
    batch_size = 100,
):
    _slot = None
    is_same_slot = True
    account_infos = []
    for i in tqdm(range(0, len(addresses), batch_size)):
        batch_addresses = addresses[i: i+batch_size]
        # TODO: batch these multi account info requests?
        batch_account_infos = (await connection.get_multiple_accounts(
            batch_addresses
        ))['result']
        slot = batch_account_infos['context']['slot']
        if _slot == None: 
            _slot = slot 
        elif slot != _slot: 
            print(_slot, slot)
            is_same_slot = False

        account_infos += batch_account_infos['value']

    return account_infos, is_same_slot

def init_account_dir(account_type: str):
    path = accounts_dir/account_type
    path.mkdir(parents=True, exist_ok=True)
    return path 

async def download_all_accounts(
    account_type: str, 
    batch_size: int = 100
): 
    path = init_account_dir(account_type)

    accounts = await ch.program.account[account_type].all()
    addresses = [a.public_key for a in accounts]
    if account_type == 'Market':
        addresses += [a.account.amm.oracle for a in accounts]
    elif account_type == 'Bank':
        addresses += [a.account.oracle for a in accounts]

    print(f'found {len(accounts)} accounts...')
    
    account_infos, _ = await batch_get_account_infos(
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
async def get_all_pks(type):
    accounts = await ch.program.account[type].all()
    addrs = [u.public_key for u in accounts]
    indexs = [len(addrs)]
    types = [type]
    
    if type == 'Market':
        oracles = [a.account.amm.oracle for a in accounts]
        addrs += oracles
        indexs.append(len(addrs))
        types.append("Oracles")
    elif type == 'Bank':
        oracles = [a.account.oracle for a in accounts]
        addrs += oracles
        indexs.append(len(addrs))
        types.append("Oracles")

    return addrs, indexs, types

types = []
indexs = []
addrs = []
for k in ch.program.account.keys():
    k_addrs, k_indexs, k_types = await get_all_pks(k)
    indexs += [i + len(addrs) for i in k_indexs]
    addrs += k_addrs
    types += k_types

len(addrs), indexs, types

#%%
success = False 
while not success:
    account_infos, success = await batch_get_account_infos(
        addrs, 
        batch_size=40
    )
    time.sleep(2)
len(account_infos), success

#%%
for addr, acc in zip(addrs, account_infos):
    print(addr, acc)

#%%
type_accounts = {}

def decode(type, data):
    data = base64.b64decode(data)
    account = ch.program.account[type]._coder.accounts.parse(data).data
    return account

def encode(type, account):
    anchor_data = Instruction(data=account, name=type)
    data = ch.program.account[type]._coder.accounts.build(anchor_data)
    data = base64.b64encode(data).decode("utf-8")
    return data

do_nothing_types = [
    "Bank",
    "Market",
    "Oracles"
]

for i in range(len(types)):
    type = types[i]
    if i == 0:
        acc_info = account_infos[:indexs[i]]
        addr_info = addrs[:indexs[i]]
    else:
        acc_info = account_infos[indexs[i-1]:indexs[i]]
        addr_info = addrs[indexs[i-1]:indexs[i]] 

    if type in do_nothing_types:
        print(f'saving {len(acc_info)} without mod: {type}...')
        assert len(acc_info) == len(addr_info)
        state_path = init_account_dir(type)
        for acc, addr in zip(acc_info, addr_info):
            print(f'saving addr {addr}')
            save_account_info(
                state_path/(str(addr) + '.json'), 
                acc, 
                str(addr)
            )
    else:
        for addr, enc_account in zip(addr_info, acc_info):
            data = enc_account['data'][0]
            enc_account['decoded_data'] = decode(type, data)
            enc_account['addr'] = addr

        type_accounts[type] = acc_info

#%%
## dont do anything to the market or bank 

def state_mod(state):
    state.admin = state_kp.public_key

def modify_and_save_account(
    type, 
    mod_fcn
):
    state_path = init_account_dir(type)

    accounts = type_accounts[type]
    for account_dict in accounts:
        obj = account_dict.pop('decoded_data')
        addr: PublicKey = account_dict.pop('addr')

        new_addr = mod_fcn(obj)
        if new_addr is not None:
            addr = new_addr

        account_dict['data'][0] = encode(type, obj)
        save_account_info(
            state_path/(str(addr) + '.json'), 
            account_dict, 
            str(addr)
        )

auth_to_new_kp = {}
def user_user_stats_mod(
    type,
    user_or_stats,
):
    global auth_to_new_kp

    old_admin = user_or_stats.authority
    if str(old_admin) in auth_to_new_kp:
        kp = auth_to_new_kp[str(old_admin)]
    else:
        kp = Keypair()
        auth_to_new_kp[str(old_admin)] = kp
        with open(keypairs_dir/f'{kp.public_key}.secret', 'w') as f: 
            f.write(kp.secret_key.hex())
    
    user_or_stats.authority = kp.public_key

    if type == 'User':
        new_addr = get_user_account_public_key(
            ch.program_id, 
            kp.public_key,
            user_or_stats.user_id
        )
    elif type == 'UserStats':
        new_addr = get_user_stats_account_public_key(
            ch.program_id, 
            kp.public_key,
        )

    return new_addr

modify_and_save_account("State", state_mod)
modify_and_save_account("User", lambda a: user_user_stats_mod("User", a))
modify_and_save_account("UserStats", lambda a: user_user_stats_mod("UserStats", a))

with open(keypairs_dir/f'state.secret', 'w') as f: 
    f.write(state_kp.secret_key.hex())

#%%
# #%%
# ## change the admin 
# lamports = 6674640
# state_accounts = await ch.program.account["State"].all()
# assert len(state_accounts) == 1
# state = state_accounts[0]

# state_path = init_account_dir("State")

# state.account.admin = state_kp.public_key

# anchor_state = Instruction(data=state.account, name="State")
# data = ch.program.account["State"]._coder.accounts.build(anchor_state)
# acc_data = base64.b64encode(data).decode("utf-8")
# account_info = {
#     'data': [acc_data, 'base64'],
#     'executable': False,
#     'lamports': lamports,
#     'owner': str(ch.program_id),
#     'rentEpoch': 367
# }
# save_account_info(
#     state_path/(str(state.public_key) + '.json'), 
#     account_info, 
#     str(state.public_key)
# )

#%%
user_path = init_account_dir("User")
user_stats_path = init_account_dir("UserStats")

rent_epoch = 365 # hardcoded for now
user_lamports = 63231600 # hardcoded for now
user_accounts = await ch.program.account["User"].all()

user_stats_lamports = 2289840 # hardcoded 
user_stats_accounts = await ch.program.account["UserStats"].all()

print(f'found {len(user_accounts) + len(user_stats_accounts)} number of accounts...')

full_users = {}
for user in user_accounts:
    user_stats = [us for us in user_stats_accounts if us.account.authority == user.account.authority][0]
    full_users[str(user.account.authority)] = {"user": user, "user_stats": user_stats}

net_baa = 0
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

    position = [p for p in user_obj.positions if p.market_index == 0 and p.base_asset_amount != 0]
    if len(position) > 0:
        assert len(position) == 1
        net_baa += position[0].base_asset_amount

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
        f.write(kp.secret_key.hex())


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
    
    # program_path = f"driftpy/protocol-v2/target/deploy/clearing_house.so"
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

#%%
#%%
#%%