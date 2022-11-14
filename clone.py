#%%
# get all user accounts 
# get market accounts 
# save them locally 
# modify user & user stats pdas with kps which we own 
# run a local validator with accounts preloaded 
# run close all simulation 

#%%
import sys
sys.path.append('driftpy/src/')

import driftpy
print(driftpy.__path__)

from dotenv import load_dotenv
load_dotenv()

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
from tqdm import tqdm 
import shutil
from anchorpy import Instruction
import base64
from subprocess import Popen
import os 
import time
import signal
import yaml

accounts_dir = pathlib.Path('accounts/')
keypairs_dir = pathlib.Path('keypairs/')

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
    
def get_multiple_accounts_request(accounts):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getMultipleAccounts",
        "params": [
            accounts,
            {"encoding": "base64"}
        ]
    }

import requests
async def batch_get_account_infos(
    connection: AsyncClient,
    addresses,
    batch_size = 100,
):
    _slot = None
    is_same_slot = True
    account_infos = []

    acc_requests = []
    for i in tqdm(range(0, len(addresses), batch_size)):
        batch_addresses = addresses[i: i+batch_size]
        data = get_multiple_accounts_request(
            [str(addr) for addr in batch_addresses]
        )
        acc_requests.append(data)

    resp = requests.post(
        connection._provider.endpoint_uri,
        headers={"Content-Type": "application/json"}, 
        json=acc_requests
    )
    resp = json.loads(resp.text)

    for batch_account_infos in resp:
        batch_account_infos = batch_account_infos['result']
        slot = batch_account_infos['context']['slot']
        if _slot == None: 
            _slot = slot 
        elif slot != _slot: 
            is_same_slot = False
        account_infos += batch_account_infos['value']

    assert len(account_infos) == len(addresses)

    return account_infos, is_same_slot

def init_account_dir(account_type: str):
    path = accounts_dir/account_type
    # path.mkdir(parents=True, exist_ok=True)
    return accounts_dir

async def get_all_pks(ch, type):
    accounts = await ch.program.account[type].all()
    addrs = [u.public_key for u in accounts]
    indexs = [len(addrs)]
    types = [type]

    print(f'found {len(accounts)} accounts for type {type}...')
    
    if type == 'PerpMarket':
        oracles = [a.account.amm.oracle for a in accounts]
        addrs += oracles
        indexs.append(len(addrs))
        types.append("Oracles")

    elif type == 'SpotMarket':
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

def setup_validator_script(
    ch: ClearingHouse,
    validator_path: str,
    script_file: str
):
    # load accounts
    validator_str = f"#!/bin/bash\n{validator_path}"
    # for d in accounts_dir.iterdir():
    #     if '.so' not in str(d):
    #         validator_str += f' --account-dir {d}'    
    
    validator_str += f' --account-dir accounts/'    

    # load program
    # https://github.com/drift-labs/protocol-v2/blob/master/sdk/src/config.ts
    program_address = str(ch.program_id)
    program_path = f"{accounts_dir}/{program_address}.so"
    # d = devnet
    # m = mainnet
    print('scraping mainnet program...')
    command = f"solana program dump -u m {program_address} {program_path}"
    os.system(command)
    
    # program_path = f"driftpy/protocol-v2/target/deploy/clearing_house.so"
    validator_str += f' --bpf-program {program_address} {program_path}'

    # hard reset 
    validator_str += ' --reset'

    with open(script_file, 'w') as f: 
        f.write(validator_str)

async def scrape():
    config = configs['mainnet']
    key = os.getenv("API_KEY")
    url = f'https://drift-cranking.rpcpool.com/{key}'
    
    state_kp = Keypair() ## new admin kp
    wallet = Wallet(state_kp)
    connection = AsyncClient(url)
    provider = Provider(connection, wallet)
    ch = ClearingHouse.from_config(config, provider)
    print('reading from program:', ch.program_id)

    state = await get_state_account(ch.program)
    n_perps, n_spots = state.number_of_markets, state.number_of_spot_markets
    
    if accounts_dir.exists():
        print('removing existing accounts...')
        shutil.rmtree(accounts_dir)

    if keypairs_dir.exists():
        print('removing existing keypairs...')
        shutil.rmtree(keypairs_dir)

    accounts_dir.mkdir(parents=True, exist_ok=True)
    keypairs_dir.mkdir(parents=True, exist_ok=True)

    print('scraping...')
    types = []
    indexs = []
    addrs = []
    for k in ch.program.account.keys():
        k_addrs, k_indexs, k_types = await get_all_pks(ch, k)
        indexs += [i + len(addrs) for i in k_indexs]
        addrs += k_addrs
        types += k_types

    # include vaults 
    for i in range(n_spots): 
        vault_pk = get_spot_market_vault_public_key(
            ch.program_id, i
        )
        if_pk = get_insurance_fund_vault_public_key(
            ch.program_id, i
        )
        addrs.append(vault_pk)
        addrs.append(if_pk)
    
    print(f'found {len(addrs)} accounts...')

    success = False 
    attempt = 0
    while not success:
        attempt += 1 
        print(f'>> attempting to get accounts in same slot: attempt {attempt}...')
        account_infos, success = await batch_get_account_infos(
            connection,
            addrs, 
            batch_size=100
        )
        time.sleep(2)
    
    for addr, acc in zip(addrs, account_infos):
        if acc is None or acc['data'] is None: 
            print("rpc returned no value for addr acc:", addr, acc)
            print('failed: exiting...')
            return

    # pop off the vault addrs + save
    for i in list(range(n_spots))[::-1]:
        addr = addrs.pop(-1)
        acc_info = account_infos.pop(-1)
        save_account_info(
            accounts_dir/(str(addr) + '.json'), 
            acc_info, 
            str(addr)
        )
        addr = addrs.pop(-1)
        acc_info = account_infos.pop(-1)
        save_account_info(
            accounts_dir/(str(addr) + '.json'), 
            acc_info, 
            str(addr)
        )

    print("editing and saving accounts...")
    type_accounts = {}
    do_nothing_types = [
        "SpotMarket",
        "PerpMarket",
        "Oracles", 
        "InsuranceFundStake",
        "SerumV3FulfillmentConfig",
    ]
    for i in range(len(types)):
        account_type = types[i]
        if i == 0:
            acc_info = account_infos[:indexs[i]]
            addr_info = addrs[:indexs[i]]
        else:
            acc_info = account_infos[indexs[i-1]:indexs[i]]
            addr_info = addrs[indexs[i-1]:indexs[i]] 

        if account_type in do_nothing_types:
            assert len(acc_info) == len(addr_info)
            state_path = init_account_dir(account_type)
            print(f'saving {account_type} {len(acc_info)} types to {state_path}...')
            for acc, addr in zip(acc_info, addr_info):
                save_account_info(
                    state_path/(str(addr) + '.json'), 
                    acc, 
                    str(addr)
                )
        else:
            for addr, enc_account in zip(addr_info, acc_info):
                data = enc_account['data'][0]
                enc_account['decoded_data'] = decode(ch, account_type, data)
                enc_account['addr'] = addr

            type_accounts[account_type] = acc_info
    
    state_path = init_account_dir("State")
    accounts = type_accounts["State"]
    assert len(accounts) == 1
    for account_dict in accounts:
        obj = account_dict.pop('decoded_data')
        addr: PublicKey = account_dict.pop('addr')

        # update admin 
        obj.admin = state_kp.public_key
        with open(keypairs_dir/'state.secret', 'w') as f: 
            f.write(state_kp.secret_key.hex())

        account_dict['data'][0] = encode(ch, "State", obj)
        save_account_info(
            state_path/(str(addr) + '.json'), 
            account_dict, 
            str(addr)
        )

    user_type = "User"
    user_stats_type = "UserStats"
    user_path = init_account_dir(user_type)
    user_stats_path = init_account_dir(user_stats_type)

    auths_to_kps = {}
    auths_to_subacc = {}
    n_users = 0
    n_users_stats = 0

    # populate new authorities 
    for ty in [user_type, user_stats_type]:
        accounts = type_accounts[ty]
        save_path = user_path if ty == user_type else user_stats_path
        for account_dict in accounts:
            obj = account_dict.pop('decoded_data')
            addr: PublicKey = account_dict.pop('addr')
            old_auth = str(obj.authority)
            if old_auth not in auths_to_kps: 
                new_auth = Keypair()
                auths_to_kps[old_auth] = new_auth
                with open(keypairs_dir/f'{new_auth.public_key}.secret', 'w') as f: 
                    f.write(new_auth.secret_key.hex())
            else: 
                new_auth = auths_to_kps[old_auth]
            
            # save objects with new authorities
            obj.authority = new_auth.public_key

            if ty == user_type:
                n_users += 1
                auths_to_subacc[old_auth] = auths_to_subacc.get(old_auth, []) + [obj.sub_account_id]
                new_addr = get_user_account_public_key(
                    ch.program_id, 
                    new_auth.public_key,
                    obj.sub_account_id
                )
            elif ty == user_stats_type:
                n_users_stats += 1
                new_addr = get_user_stats_account_public_key(
                    ch.program_id, 
                    new_auth.public_key,
                )
            else: 
                raise

            account_dict['data'][0] = encode(ch, ty, obj)
            save_account_info(
                save_path/(str(new_addr) + '.json'), 
                account_dict, 
                str(new_addr)
            ) 

    # for k, v in auths_to_subacc.items(): 
    #     if 0 not in v: 
    #         print(k, v)

    print(
        n_users, 
        n_users_stats, 
        len(list(auths_to_kps.keys())),
        list(auths_to_subacc.values())
    )

    print('setting up validator scripts...')
    validator_path = 'solana-test-validator'
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
    asyncio.run(scrape())

# %%
