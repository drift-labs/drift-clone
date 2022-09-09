#%%
# get all user accounts 
# get market accounts 
# save them locally 
# modify user & user stats pdas with kps which we own 
# run a local validator with accounts preloaded 
# run close all simulation 

# todo: full script -- rn only notebook
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

accounts_dir = pathlib.Path('accounts/')
keypairs_dir = pathlib.Path('keypairs/')

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
    connection,
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
            is_same_slot = False

        account_infos += batch_account_infos['value']

    return account_infos, is_same_slot

def init_account_dir(account_type: str):
    path = accounts_dir/account_type
    path.mkdir(parents=True, exist_ok=True)
    return path 

async def get_all_pks(ch, type):
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

def decode(ch, type, data):
    data = base64.b64decode(data)
    account = ch.program.account[type]._coder.accounts.parse(data).data
    return account

def encode(ch, type, account):
    anchor_data = Instruction(data=account, name=type)
    data = ch.program.account[type]._coder.accounts.build(anchor_data)
    data = base64.b64encode(data).decode("utf-8")
    return data

def modify_and_save_account(
    ch,
    type_accounts,
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

        account_dict['data'][0] = encode(ch, type, obj)
        save_account_info(
            state_path/(str(addr) + '.json'), 
            account_dict, 
            str(addr)
        )

def setup_validator_script(
    ch: ClearingHouse,
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

async def main():
    if accounts_dir.exists():
        print('removing existing accounts...')
        shutil.rmtree(accounts_dir)

    if keypairs_dir.exists():
        print('removing existing keypairs...')
        shutil.rmtree(keypairs_dir)

    accounts_dir.mkdir(parents=True, exist_ok=True)
    keypairs_dir.mkdir(parents=True, exist_ok=True)

    config = configs['devnet']

    # url = 'https://api.devnet.solana.com'
    url = "http://3.220.170.22:8899"

    state_kp = Keypair() ## new admin kp
    wallet = Wallet(state_kp)
    connection = AsyncClient(url)
    provider = Provider(connection, wallet)
    ch = ClearingHouse.from_config(config, provider)
    print('reading program:', ch.program_id)

    print('scraping...')
    types = []
    indexs = []
    addrs = []
    for k in ch.program.account.keys():
        k_addrs, k_indexs, k_types = await get_all_pks(ch, k)
        indexs += [i + len(addrs) for i in k_indexs]
        addrs += k_addrs
        types += k_types
    
    print(f'found {len(addrs)} accounts...')

    success = False 
    while not success:
        account_infos, success = await batch_get_account_infos(
            connection,
            addrs, 
            batch_size=40
        )
        time.sleep(2)
    
    for addr, acc in zip(addrs, account_infos):
        if acc is None or acc['data'] is None: 
            print("rpc returned no value for addr acc:", addr, acc)
            print('failed: exiting...')
            return

    print("editing and saving accounts...")
    type_accounts = {}
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
            assert len(acc_info) == len(addr_info)
            state_path = init_account_dir(type)
            for acc, addr in zip(acc_info, addr_info):
                save_account_info(
                    state_path/(str(addr) + '.json'), 
                    acc, 
                    str(addr)
                )
        else:
            for addr, enc_account in zip(addr_info, acc_info):
                data = enc_account['data'][0]
                enc_account['decoded_data'] = decode(ch, type, data)
                enc_account['addr'] = addr

            type_accounts[type] = acc_info
    
    def state_mod(state):
        state.admin = state_kp.public_key

    auth_to_new_kp = {}
    def user_user_stats_mod(
        type,
        user_or_stats,
    ):
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

    modify_and_save_account(ch, type_accounts, "State", state_mod)
    modify_and_save_account(ch, type_accounts, "User", lambda a: user_user_stats_mod("User", a))
    modify_and_save_account(ch, type_accounts, "UserStats", lambda a: user_user_stats_mod("UserStats", a))

    with open(keypairs_dir/f'state.secret', 'w') as f: 
        f.write(state_kp.secret_key.hex())

    print('setting up validator scripts...')
    validator_path = './solana/target/debug/solana-test-validator'
    script_file = 'start_local.sh'
    setup_validator_script(
        ch,
        validator_path,
        script_file
    )
    
    print(f'bash {script_file} to start the local validator...')
    print('done :)')

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())

#%%
#%%
#%%