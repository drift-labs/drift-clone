import requests
import json
from solana.rpc.async_api import AsyncClient
from solana.publickey import PublicKey
from solana.keypair import Keypair

import sys
sys.path.append('driftpy/src/')

from driftpy.constants.config import configs
from driftpy.clearing_house import ClearingHouse
from driftpy.types import (
    InsuranceFundStake
)
from driftpy.accounts import (
    get_state_account,
    get_spot_market_vault_public_key,
    get_insurance_fund_vault_public_key,
    get_spot_market_account,
    get_user_account_public_key,
    get_user_stats_account_public_key,
    get_insurance_fund_stake_public_key,
)

from anchorpy import Provider
from anchorpy import Wallet
from anchorpy import Instruction
from anchorpy.coder.accounts import (
    _account_discriminator,
)

import pathlib
from tqdm import tqdm
import shutil
import base64
import os
from dotenv import load_dotenv
from typing import List, Optional


# get all user accounts
# get market accounts
# save them locally
# modify user & user stats pdas with kps which we own
# run a local validator with accounts preloaded
# run close all simulation
load_dotenv()


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


def get_program_accounts_request(program_id):
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getProgramAccounts",
        "params": [
            program_id,
            {
                "encoding": "base64",
                "withContext": True,
            }
        ]
    }


def get_discriminator_for_account_type(ch: ClearingHouse, account_type: str) -> bytes:
    acc = ch.program.account[account_type]
    return _account_discriminator(acc._idl_account.name)


def does_discriminator_match(discriminator: bytes, base64_data: str) -> bool:
    bytes_data = base64.b64decode(base64_data)
    return bytes_data.startswith(discriminator)


async def get_accounts_from_batch_account_infos(
    ch: ClearingHouse,
    account_type: str,
    addrs: list[str], account_infos: list[dict]) -> \
        list[tuple[list[str], list[dict]]]:
    """Compute the discriminator for the account type, and filter for all
    accounts that match the discriminator.
    """
    matches = []
    discriminator = get_discriminator_for_account_type(ch, account_type)
    for addr, account_info in zip(addrs, account_infos):
        if does_discriminator_match(discriminator, account_info['data'][0]):
            matches.append((addr, account_info))

    return matches


async def batch_get_account_infos_with_gpa_gma(
        connection: AsyncClient,
        program_id: str,
        additional_accounts:
        Optional[List[str]]) -> tuple[list[str], list[dict]]:
    """Use getProgramAccounts to get all accounts from program_id, and
    getMultipleAccounts to get all accounts from additional_accounts.
    """
    batch_reqs = []
    batch_reqs.append(get_program_accounts_request(str(program_id)))
    if additional_accounts is not None:
        batch_reqs.append(get_multiple_accounts_request(additional_accounts))

    resp = requests.post(
        connection._provider.endpoint_uri,
        headers={"Content-Type": "application/json"},
        json=batch_reqs
    )

    try:
        resp = json.loads(resp.text)
    except Exception as e:
        print(resp.text)
        raise e

    # extract value from batch requests (first is gpa, then gma)
    account_infos = []
    pubkeys = []
    slots = []
    idx_gma = 0
    for batch_resp in resp:
        batch_resp = batch_resp['result']
        assert len(batch_resp['value']) > 0, "no accounts found"
        slots.append(batch_resp['context']['slot'])
        for account_info in batch_resp['value']:
            if 'account' in account_info:
                # getPrgoramAccounts resp
                account_infos.append(account_info['account'])
                pubkeys.append(account_info['pubkey'])
            else:
                # getMultipleAccounts resp
                account_infos.append(account_info)
                pubkeys.append(additional_accounts[idx_gma])
                idx_gma += 1

    min_slot = min(slots)
    print(f'getProgramAccounts slot:  {slots[0]} '
          f'(delta from min: {slots[0] - min_slot})')
    print(f'getMultipleAccounts slot: {slots[1]} '
          f'(delta from min: {slots[1] - min_slot})')

    return (pubkeys, account_infos)


async def batch_get_account_infos(
    connection: AsyncClient,
    addresses,
    batch_size=100,
):
    _slot = None
    is_same_slot = True
    account_infos = []

    _resp = []
    for i in tqdm(range(0, len(addresses), batch_size)):
        rpc_requests = []
        for j in range(i, i+batch_size, 100):
            batch_addresses = addresses[j: j+100]
            data = get_multiple_accounts_request(
                [str(addr) for addr in batch_addresses]
            )
            rpc_requests.append(data)

        resp = requests.post(
            connection._provider.endpoint_uri,
            headers={"Content-Type": "application/json"},
            json=rpc_requests
        )

        try:
            resp = json.loads(resp.text)
        except Exception as e:
            print(resp.text)
            raise e
        _resp += resp

    for batch_account_infos in _resp:
        batch_account_infos = batch_account_infos['result']
        slot = batch_account_infos['context']['slot']
        if _slot is None:
            _slot = slot
        elif slot != _slot:
            print('found different slots (difference):', abs(slot - _slot))
            if abs(slot - _slot) > 3:
                return [], False
            else:
                print('acceptable slot delta...')
        account_infos += batch_account_infos['value']

    assert len(account_infos) == len(addresses)

    return account_infos, is_same_slot


def init_account_dir(account_type: str):
    return accounts_dir


async def get_oracle_addrs(ch: ClearingHouse) -> list[str]:
    perp_accounts = await ch.program.account["PerpMarket"].all()
    spot_accounts = await ch.program.account["SpotMarket"].all()

    print(f'{len(perp_accounts)} perp accounts')
    print(f'{len(spot_accounts)} spot accounts')

    oracle_addrs = []
    [oracle_addrs.append(a.account.amm.oracle) for a in perp_accounts]
    [oracle_addrs.append(a.account.oracle) for a in spot_accounts]
    return oracle_addrs


def decode_b64_data_to_account(ch, account_type, data):
    """Decode an account from a base64 string into the specified account_type
    """
    data = base64.b64decode(data)
    account = ch.program.account[account_type]._coder.accounts.parse(data).data
    return account


def encode_account_to_b64_data(ch, type, account):
    """Encode a DriftClient owned account into a base64 string
    """
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

    validator_str += ' --account-dir accounts/'

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
    if "RPC_URL" in os.environ:
        url = os.getenv("RPC_URL")
    elif "API_KEY" in os.environ:
        url = f'https://drift-cranking.rpcpool.com/{os.getenv("API_KEY")}'
    else:
        raise Exception("Must set API_KEY or RPC_URL environment variables")

    state_kp = Keypair()  # new admin kp
    wallet = Wallet(state_kp)
    connection = AsyncClient(url)
    provider = Provider(connection, wallet)
    ch = ClearingHouse.from_config(config, provider)
    print('reading from program:', ch.program_id)

    state = await get_state_account(ch.program)
    _, n_spots = state.number_of_markets, state.number_of_spot_markets

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
    all_account_types = ch.program.account.keys()
    print(f'all_account_types: {all_account_types}')

    additional_addrs = []

    # include oracles
    oracle_addrs = await get_oracle_addrs(ch)
    print(f'found {len(oracle_addrs)} oracle addrs...')
    print(oracle_addrs)
    addrs += oracle_addrs
    additional_addrs += oracle_addrs

    # include vaults (PDA not owned by drift program)
    for i in range(n_spots):
        vault_pk = get_spot_market_vault_public_key(ch.program_id, i)
        if_pk = get_insurance_fund_vault_public_key(ch.program_id, i)
        print(f'Vaults for spot market {i}: {vault_pk}, {if_pk}')
        addrs.append(vault_pk)
        addrs.append(if_pk)
        additional_addrs.append(vault_pk)
        additional_addrs.append(if_pk)

        # # accounts for spot token SPL accounts -- already included in above (if_pk)
        spot_market_account = await get_spot_market_account(ch.program, i)
        additional_addrs.append(spot_market_account.mint)

    print(types)
    print(f'found {len(indexs)} indexs...')
    print(f'found {len(addrs)} accounts...')
    print(f'found {len(additional_addrs)} additional addrs...')

    addrs, account_infos = await batch_get_account_infos_with_gpa_gma(
        connection,
        ch.program_id,
        [str(a) for a in additional_addrs])

    # pop off the vault addrs + save (these are getMultipleAccounts responses)
    spot_count = 0
    pop_count = 0
    for i in list(range(n_spots)):
        addr = addrs.pop(-1)
        acc_info = account_infos.pop(-1)
        pop_count += 1

        print('cloning mint', addr)
        # allow state_kp to mint more
        # this is wSOL so we cant mint from it -- we care about usdc
        if spot_count != 0:
            # from spl.token._layouts import MINT_LAYOUT
            byte_data = base64.b64decode(acc_info['data'][0])
            byte_data = bytearray(byte_data)

            # set mint authority option = True (32bits = 8 bytes)
            one = int.to_bytes(1, 4, 'little')
            byte_data[:4] = one
            # set mint authority = state_ch
            byte_data[4:4+32] = bytes(state_kp.public_key)

            # repack
            data = base64.b64encode(byte_data).decode('utf-8')
            acc_info['data'][0] = data

        save_account_info(
            accounts_dir/(str(addr) + '.json'),
            acc_info,
            str(addr)
        )
        spot_count += 1

        # 3 accounts per spot market: spot vault, IF vault
        for _ in range(2):
            addr = addrs.pop(-1)
            acc_info = account_infos.pop(-1)
            pop_count += 1
            save_account_info(
                accounts_dir/(str(addr) + '.json'),
                acc_info,
                str(addr)
            )
    print(f"popped {pop_count} token accounts for spot markets")

    # pop off and save oracles (these are getMultipleAccounts responses)
    pop_count = 0
    for i in list(range(len(oracle_addrs)))[::-1]:
        addr = addrs.pop(-1)
        acc_info = account_infos.pop(-1)
        pop_count += 1

        assert str(oracle_addrs[i]) == str(
            addr), f'oracle addr mismatch: {oracle_addrs[i]} != {addr}'

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
        "SerumV3FulfillmentConfig",
    ]

    for account_type in all_account_types:
        account_matches = await get_accounts_from_batch_account_infos(
            ch,
            account_type,
            addrs,
            account_infos)

        state_path = init_account_dir(account_type)
        print(
            f'for account type: {account_type} '
            f'(do nothing: {account_type in do_nothing_types}) '
            f'found {len(account_matches)} accounts, '
            f'saving to {state_path}...'
        )

        if account_type not in do_nothing_types and \
                account_type not in type_accounts:
            type_accounts[account_type] = []

        for ty_acc_addr, ty_acc_info in account_matches:
            if account_type in do_nothing_types:
                '''
                For ["SpotMarket", "PerpMarket", "Oracles", "SerumV3FulfillmentConfig"]
                just save the account data
                '''
                save_account_info(
                    state_path/(str(ty_acc_addr) + '.json'),
                    ty_acc_info,
                    str(ty_acc_addr)
                )
            else:
                '''
                For ['InsuranceFundStake', 'State', 'User', 'UserStats', 'ReferrerName']
                decode the data, and save it in type_accounts for further processing
                '''
                # for addr, enc_account in zip(addr_info, acc_info):
                data = ty_acc_info['data'][0]
                ty_acc_info['decoded_data'] = decode_b64_data_to_account(
                    ch, account_type, data)
                ty_acc_info['addr'] = ty_acc_addr

                type_accounts[account_type].append(ty_acc_info)

    print("total size of type_accounts to modify")
    for k in type_accounts:
        print(f"{k}: {len(type_accounts[k])}")

    # type_accounts: account_type -> []acc_info
    # now modify the accounts (change authority etc.) before saving them

    state_path = init_account_dir("State")
    accounts = type_accounts["State"]
    assert len(accounts) == 1
    print(f"Modifying State accounts: {len(accounts)}...")
    for account_dict in accounts:
        obj = account_dict.pop('decoded_data')
        addr: PublicKey = account_dict.pop('addr')

        # update admin key of the state account
        print(f"Updating State admin key from {obj.admin} to {state_kp.public_key}...")
        obj.admin = state_kp.public_key
        with open(keypairs_dir/'state.secret', 'w') as f:
            f.write(state_kp.secret_key.hex())

        account_dict['data'][0] = encode_account_to_b64_data(ch, "State", obj)
        print(f'saving {account_type} {len(accounts)} types to {state_path}...')
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

    # populate new authorities for users
    for ty in [user_type, user_stats_type]:
        accounts = type_accounts[ty]
        save_path = user_path if ty == user_type else user_stats_path
        print(f"Modifying {ty} accounts: {len(accounts)}...")
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
                auths_to_subacc[old_auth] = auths_to_subacc.get(
                    old_auth, []
                ) + [obj.sub_account_id]
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
                raise Exception(f"Unknown type {ty}")

            account_dict['data'][0] = encode_account_to_b64_data(ch, ty, obj)
            save_account_info(
                save_path/(str(new_addr) + '.json'),
                account_dict,
                str(new_addr)
            )

    # same thing with insurance fund
    ty = "InsuranceFundStake"
    if_path = init_account_dir(ty)
    accounts = type_accounts[ty]
    print(f"Modifying {ty} accounts: {len(accounts)}...")
    for account_dict in accounts:
        obj: InsuranceFundStake = account_dict.pop('decoded_data')
        addr: PublicKey = account_dict.pop('addr')
        old_auth = str(obj.authority)
        assert old_auth in auths_to_kps
        new_auth: Keypair = auths_to_kps[old_auth]

        obj.authority = new_auth.public_key
        new_addr = get_insurance_fund_stake_public_key(
            ch.program_id, new_auth.public_key, obj.market_index
        )

        account_dict['data'][0] = encode_account_to_b64_data(ch, ty, obj)
        save_account_info(
            if_path/(str(new_addr) + '.json'),
            account_dict,
            str(new_addr)
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
