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
from driftpy.constants.numeric_constants import AMM_RESERVE_PRECISION
from solana.rpc import commitment
import pprint
from driftpy.clearing_house import is_available
from termcolor import colored
import pprint

async def view_logs(
    sig: str,
    provider: Provider,
    print: bool = True
):
    provider.connection._commitment = commitment.Confirmed 
    logs = ''
    try: 
        await provider.connection.confirm_transaction(sig, commitment.Confirmed)
        logs = (await provider.connection.get_transaction(sig))["result"]["meta"]["logMessages"]
    finally:
        provider.connection._commitment = commitment.Processed 

    if print:
        pprint.pprint(logs)

    return logs

def load_subaccounts(chs):
    accounts = [p.stem for p in pathlib.Path('accounts').iterdir()]
    active_chs = []
    for ch in chs:
        subaccount_ids = []
        for sid in range(10):
            user_pk = get_user_account_public_key(
                ch.program_id, ch.authority, sid
            )
            if str(user_pk) in accounts:
                subaccount_ids.append(sid)

        ch.subaccounts = subaccount_ids
        if len(subaccount_ids) != 0: 
            active_chs.append(ch)
    return active_chs

async def clone_close():
    config = configs['mainnet']
    url = 'http://127.0.0.1:8899'
    connection = AsyncClient(url)

    print('loading users...')
    chs, state_ch = await load_local_users(config, connection)
    provider = state_ch.program.provider
    program = state_ch.program

    chs = load_subaccounts(chs)
    state = await get_state_account(state_ch.program)
    n_markets, n_spot_markets = state.number_of_markets, state.number_of_spot_markets

    # update state 
    await state_ch.update_perp_auction_duration(0)
    await state_ch.update_lp_cooldown_time(0)

    print('delisting market...')
    slot = (await provider.connection.get_slot())['result']
    dtime: int = (await provider.connection.get_block_time(slot))['result']

    # + N seconds
    print('updating perp/spot market expiry...')
    seconds_time = 20 # inconsistent tbh 
    sigs = []
    for i in range(n_markets):
        sig = await state_ch.update_perp_market_expiry(i, dtime + seconds_time)
        sigs.append(sig)

    for i in range(n_spot_markets):
        sig = await state_ch.update_spot_market_expiry(i, dtime + seconds_time)
        sigs.append(sig)

    # close out lps
    _sigs = []
    ch: ClearingHouse
    for perp_market_idx in range(n_markets):
        for ch in chs:
            for sid in ch.subaccounts:
                position = await ch.get_user_position(perp_market_idx, sid)
                if position is not None and position.lp_shares > 0:
                    print('removing lp...', position.lp_shares)
                    sig = await ch.remove_liquidity(position.lp_shares, perp_market_idx, sid)
                    _sigs.append(sig)

    # verify 
    if len(_sigs) > 0:
        await connection.confirm_transaction(_sigs[-1])
    market = await get_perp_market_account(state_ch.program, perp_market_idx)
    print("market.amm.user_lp_shares == 0: ", market.amm.user_lp_shares == 0)

    # fully expire market
    print('waiting for expiry...')
    from solana.rpc import commitment
    for i, sig in enumerate(sigs):
        await provider.connection.confirm_transaction(sig, commitment.Confirmed)

    while True:
        slot = (await provider.connection.get_slot())['result']
        new_dtime: int = (await provider.connection.get_block_time(slot))['result']
        time.sleep(0.2)
        if new_dtime > dtime + seconds_time: 
            break 

    print('settling expired market')
    for i in range(n_markets):
        sig = await state_ch.settle_expired_market(i)
        await provider.connection.confirm_transaction(sig, commitment.Finalized)

        market = await get_perp_market_account(program, i)
        print(
            f'market {i} expiry_price vs twap/price', 
            market.status,
            market.expiry_price, 
            market.amm.historical_oracle_data.last_oracle_price_twap,
            market.amm.historical_oracle_data.last_oracle_price
        )


    for perp_market_idx in range(n_markets):
        success = False
        attempt = -1
        settle_sigs = []

        n_users = 0
        for ch in chs:
            for sid in ch.subaccounts:
                n_users += 1

        while not success:
            attempt += 1
            success = True
            i = 0
            routines = []

            print(colored(f' =>> market {i}: settle attempt {attempt}', "blue"))
            for ch in chs:
                for sid in ch.subaccounts:
                    position = await ch.get_user_position(perp_market_idx, sid)
                    if position is None:
                        i += 1
                        continue
                    routines.append(ch.settle_pnl(ch.authority, perp_market_idx, sid))

            for routine in routines:
                try:
                    sig = await routine
                    settle_sigs.append(sig)
                    i += 1
                    print(f'settled success... {i}/{n_users}')
                except Exception as e: 
                    success = False
                    print(f'settled failed... {i}/{n_users}')
                    pprint.pprint(e)

            print(f'settled fin... {i}/{n_users}')

        print('confirming...') 
        if len(settle_sigs) > 0:
            await connection.confirm_transaction(settle_sigs[-1])

    for ch in chs:
        for sid in ch.subaccounts:
            for i in range(n_markets):
                position = await ch.get_user_position(i, sid)
                if position is None: continue
                print(position)

    for i in range(n_markets):
        market = await get_perp_market_account(program, i)
        print(
            f'market {i} info:',
            "\n\t market.amm.total_fee_minus_distributions:", 
            market.amm.total_fee_minus_distributions,
            "\n\t net baa, net unsettled, (sum):", 
            market.amm.base_asset_amount_with_amm,
            market.amm.base_asset_amount_with_unsettled_lp,
            market.amm.base_asset_amount_with_amm + market.amm.base_asset_amount_with_unsettled_lp,
            '\n\t net long/short',
            market.amm.base_asset_amount_long, 
            market.amm.base_asset_amount_short, 
            '\n\t user lp shares',
            market.amm.user_lp_shares, 
            '\n\t cumulative_social_loss / funding:',
            market.amm.total_social_loss, 
            market.amm.cumulative_funding_rate_long, 
            market.amm.cumulative_funding_rate_short, 
            market.amm.last_funding_rate_long, 
            market.amm.last_funding_rate_short, 
            '\n\t fee pool:', market.amm.fee_pool.scaled_balance, 
            '\n\t pnl pool:', market.pnl_pool.scaled_balance
        )
    print('---')

    # print('canceling open orders...')
    # ch: ClearingHouse
    # for perp_market_idx in range(n_markets):
    #     sigs = []
    #     for ch in tqdm(chs):
    #         # cancel orders
    #         for sid in ch.subaccounts:
    #             position = await ch.get_user_position(perp_market_idx, sid)
    #             if position is not None and position.open_orders > 0:
    #                 sig = await ch.cancel_orders(sid)
    #                 sigs.append(sig)

    #     # verify 
    #     while True:
    #         resp = await connection.get_transaction(sigs[-1])
    #         if resp['result'] is not None: 
    #             break 

    #     for ch in tqdm(chs):
    #         # close position
    #         for sid in ch.subaccounts:
    #             position = await ch.get_user_position(perp_market_idx, sid)
    #             if position is not None and position.base_asset_amount != 0:
    #                 print('closing...', position.base_asset_amount / AMM_RESERVE_PRECISION)
    #                 sig = await ch.close_position(perp_market_idx, subaccount_id=sid)
    #                 sigs.append(sig)

    #     while True:
    #         resp = await connection.get_transaction(sigs[-1])
    #         if resp['result'] is not None: 
    #             break 

    #     market = await get_perp_market_account(state_ch.program, perp_market_idx)
    #     print("market.amm.base_asset_amount_with_amm", market.amm.base_asset_amount_with_amm)


async def main():
    script_file = 'start_local.sh'
    os.system(f'cat {script_file}')
    print()

    validator = LocalValidator(script_file)
    validator.start() # sometimes you gotta wait a bit for it to startup
    time.sleep(10)

    try:
        await clone_close()
    finally:
        validator.stop()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())


# %%
