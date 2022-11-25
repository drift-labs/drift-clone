import sys
sys.path.append('driftpy/src/')
sys.path.append('drift-sim/')

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
from driftpy.constants.numeric_constants import (
    AMM_RESERVE_PRECISION,
    QUOTE_PRECISION,
    BASE_PRECISION,
    FUNDING_RATE_PRECISION,
    PRICE_PRECISION,
    SPOT_BALANCE_PRECISION
)
from solana.rpc import commitment
from spl.token.instructions import get_associated_token_address
import pprint
from driftpy.clearing_house import is_available
from termcolor import colored
import pprint
from slack import Slack, SimulationResultBuilder, ExpiredMarket

import datetime as dt
import importlib  
from driftsim.backtest.liquidator import Liquidator
import numpy as np 
from driftpy.clearing_house_user import ClearingHouseUser
from driftsim.backtest.main import _send_ix

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

async def get_insurance_fund_balance(connection: AsyncClient, spot_market: SpotMarket):
    balance = await connection.get_token_account_balance(spot_market.insurance_fund.vault)
    if 'error' in balance:
        raise Exception(balance)
    return balance['result']['value']['uiAmount']

async def clone_close(sim_results: SimulationResultBuilder):
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

    sim_slot = (await connection.get_slot())['result']
    sim_results.set_start_slot(sim_slot)

    # record stats pre-closing
    for i in range(n_markets):
        perp_market = await get_perp_market_account(program, i)
        sim_results.add_initial_perp_market(perp_market)
    for i in range(n_spot_markets):
        spot_market = await get_spot_market_account(program, i)
        insurance_fund_balance = await get_insurance_fund_balance(connection, spot_market)
        print(f" {i}: {insurance_fund_balance}")
        sim_results.add_initial_spot_market(insurance_fund_balance, spot_market)

    # update state 
    await state_ch.update_perp_auction_duration(0)
    await state_ch.update_lp_cooldown_time(0)
    for i in range(n_spot_markets):
        await state_ch.update_update_insurance_fund_unstaking_period(i, 0)
        await state_ch.update_withdraw_guard_threshold(i, 2**64 - 1)

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

    n_users = 0
    for ch in chs:
        for sid in ch.subaccounts:
            n_users += 1
    sim_results.add_total_users(n_users)

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
    perp_market = await get_perp_market_account(state_ch.program, perp_market_idx)
    print("market.amm.user_lp_shares == 0: ", perp_market.amm.user_lp_shares == 0)

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

        perp_market = await get_perp_market_account(program, i)
        print(
            f'market {i} expiry_price vs twap/price', 
            perp_market.status,
            perp_market.expiry_price, 
            perp_market.amm.historical_oracle_data.last_oracle_price_twap,
            perp_market.amm.historical_oracle_data.last_oracle_price
        )
        expired_market = ExpiredMarket(
            i,
            perp_market.status,
            perp_market.expiry_price / PRICE_PRECISION,
            perp_market.amm.historical_oracle_data.last_oracle_price_twap / PRICE_PRECISION,
            perp_market.amm.historical_oracle_data.last_oracle_price / PRICE_PRECISION
        )
        sim_results.add_settled_expired_market(expired_market)

    # todo: liquidation 
    free_collateral = []
    ch_idx = []

    # set init cache
    for i, ch in enumerate(chs):
        for sid in ch.subaccounts:
            chu = ClearingHouseUser(ch, subaccount_id=sid, use_cache=False)
            await chu.set_cache()
            cache = chu.CACHE
            break 
        break 

    # most free collateral clearing house is the liquidator 
    # (tmp soln -- ideally would like to mint a new 'liq' user)
    user_chs = {}
    for i, ch in enumerate(chs):
        user_chs[i] = ch

        for sid in ch.subaccounts:
            chu = ClearingHouseUser(ch, subaccount_id=sid, use_cache=False)
            account = await chu.get_user()
            cache['user'] = account # update cache to look at the correct user account
            chu.use_cache = True
            await chu.set_cache(cache)
            fc = await chu.get_free_collateral()

            ch_idx.append((i, sid))
            free_collateral.append(fc)

    liq_idx0 = np.argmax(free_collateral)
    liq_idx0, liq_idx1 = np.argsort(free_collateral)[::-1][:2]

    print('attempting liquidation round 1...')
    liq_idx, liq_subacc = ch_idx[liq_idx0]
    liquidator = Liquidator(
        user_chs, 
        n_markets, 
        n_spot_markets, 
        liquidator_index=liq_idx,
        send_ix_fcn=_send_ix, 
        liquidator_subacc_id=liq_subacc,
    )
    await liquidator.liquidate_loop()

    # need to account for liq-ing the liquidator
    print('attempting liquidation round 2...')
    liq_idx, liq_subacc = ch_idx[liq_idx1]
    liquidator = Liquidator(
        user_chs, 
        n_markets, 
        n_spot_markets, 
        liquidator_index=liq_idx,
        send_ix_fcn=_send_ix, 
        liquidator_subacc_id=liq_subacc,
    )
    await liquidator.liquidate_loop()

    # remove if stakes 
    print('removing IF stakes...')
    async def remove_if_stake(clearing_house: ClearingHouse, market_index):
        spot = await get_spot_market_account(clearing_house.program, market_index)
        total_shares = spot.insurance_fund.total_shares
        if_stake = await get_if_stake_account(clearing_house.program, clearing_house.authority, market_index)
        n_shares = if_stake.if_shares

        conn = clearing_house.program.provider.connection
        vault_pk = get_insurance_fund_vault_public_key(clearing_house.program_id, market_index)
        v_amount = int((await conn.get_token_account_balance(vault_pk))['result']['value']['amount'])

        print(
            f'vault_amount: {v_amount} n_shares: {n_shares} total_shares: {total_shares}'
        )
        withdraw_amount = int(v_amount * n_shares / total_shares)
        print(f'withdrawing {withdraw_amount/QUOTE_PRECISION}...')

        ix1 = await clearing_house.get_request_remove_insurance_fund_stake_ix(
            market_index, 
            withdraw_amount
        )
        ix2 = await clearing_house.get_remove_insurance_fund_stake_ix(
            market_index, 
        )
        sig = await clearing_house.send_ixs([ix1, ix2])

        return sig

    accounts = await ch.program.account['InsuranceFundStake'].all()
    print("n insurance fund stakes", len(accounts))

    for i in range(n_spot_markets):
        for ch in user_chs.values():
            if_position_pk = get_insurance_fund_stake_public_key(
                ch.program_id, ch.authority, i
            )
            resp = await ch.program.provider.connection.get_account_info(
                if_position_pk
            )
            if resp["result"]["value"] is None: continue # if stake doesnt exist 

            if_account = await get_if_stake_account(ch.program, ch.authority, i)
            if if_account.if_shares > 0:
                from spl.token.instructions import create_associated_token_account
                spot_market = await get_spot_market_account(ch.program, i)

                ix = create_associated_token_account(ch.authority, ch.authority, spot_market.mint)
                await ch.send_ixs(ix)

                ata = get_associated_token_address(ch.authority, spot_market.mint)
                ch.spot_market_atas[i] = ata

                sig = await remove_if_stake(ch, i)
                await connection.confirm_transaction(sig, commitment.Confirmed)

    for perp_market_idx in range(n_markets):
        success = False
        attempt = -1
        settle_sigs = []

        while not success:
            if attempt > 5: 
                sim_results.post_fail('something went wrong during settle expired position...')
                return 

            attempt += 1
            success = True
            i = 0
            routines = []
            ids = []
            print(colored(f' =>> market {i}: settle attempt {attempt}', "blue"))
            for user_i, ch in enumerate(chs):
                for sid in ch.subaccounts:
                    position = await ch.get_user_position(perp_market_idx, sid)
                    if position is None:
                        i += 1
                        continue
                    routines.append(ch.settle_pnl(ch.authority, perp_market_idx, sid))
                    ids.append((position, user_i, sid))

            for (position, user_i, sid), routine in zip(ids, routines):
                try:
                    sig = await routine
                    settle_sigs.append(sig)
                    i += 1
                    print(f'settled success... {i}/{n_users}')
                    sim_results.add_settle_user_success()
                except Exception as e: 
                    success = False
                    if attempt > 0:
                        print(position, user_i, sid)

                    print(f'settled failed... {i}/{n_users}')
                    sim_results.add_settle_user_fail(e)
                    pprint.pprint(e)

            print(f'settled fin... {i}/{n_users}')

        print('confirming...') 
        if len(settle_sigs) > 0:
            await connection.confirm_transaction(settle_sigs[-1])

    print('withdrawing...')
    sigs = []
    for spot_market_index in range(n_spot_markets):
        success = False
        attempt = -1
        spot_market = await get_spot_market_account(program, spot_market_index)

        while not success and attempt < 0: # only try once for rn 
            attempt += 1
            success = True
            user_withdraw_count = 0

            print(colored(f' =>> spot market {spot_market_index}: withdraw attempt {attempt}', "blue"))
            ch: ClearingHouse
            for _, ch in enumerate(chs):                
                for sid in ch.subaccounts:
                    position = await ch.get_user_spot_position(spot_market_index, sid)
                    if position is None: 
                        user_withdraw_count += 1
                        continue
                    
                    if str(position.balance_type) == "SpotBalanceType.Borrow()":
                        print('skipping borrow...')
                        user_withdraw_count += 1
                        continue

                    # # balance: int, spot_market: SpotMarket, balance_type: SpotBalanceType
                    from driftpy.math.spot_market import get_token_amount
                    spot_market = await get_spot_market_account(program, spot_market_index)
                    token_amount = int(get_token_amount(
                        position.scaled_balance, 
                        spot_market,
                        position.balance_type
                    ))
                    print('token amount', token_amount)

                    from spl.token.instructions import create_associated_token_account
                    if spot_market_index not in ch.spot_market_atas:
                        ix = create_associated_token_account(ch.authority, ch.authority, spot_market.mint)
                        await ch.send_ixs(ix)
                        ata = get_associated_token_address(ch.authority, spot_market.mint)
                        ch.spot_market_atas[spot_market_index] = ata

                    # withdraw all of collateral
                    ix = await ch.get_withdraw_collateral_ix(
                        int(1e19),
                        spot_market_index, 
                        ch.spot_market_atas[spot_market_index],
                        True, 
                        user_id=sid,
                    )
                    (failed, sig, _) = await _send_ix(ch, ix, 'withdraw', {})

                    if not failed:
                        user_withdraw_count += 1
                        print(colored(f'withdraw success: {user_withdraw_count}/{n_users}', 'green'))
                        sigs.append(sig)
                    else: 
                        print(colored(f'withdraw failed: {user_withdraw_count}/{n_users}', 'red'))
                        success = False
                    print('---')

    print('confirming...') 
    if len(sigs) > 0:
        await connection.confirm_transaction(sigs[-1])    

    for ch in chs:
        for sid in ch.subaccounts:
            for i in range(n_markets):
                position = await ch.get_user_position(i, sid)
                if position is None: continue
                print(position)
            
            for i in range(n_spot_markets):
                position = await ch.get_user_spot_position(i, sid)
                if position is None: continue
                print(position)

    for i in range(n_markets):
        perp_market = await get_perp_market_account(program, i)
        sim_results.add_final_perp_market(perp_market)

    print(f"spot market if after:")
    for i in range(n_spot_markets):
        spot_market = await get_spot_market_account(program, i)
        insurance_fund_balance = await get_insurance_fund_balance(connection, spot_market)
        sim_results.add_final_spot_market(insurance_fund_balance, spot_market)
    
    print('---')

    sim_results.set_end_time(dt.datetime.utcnow())
    sim_results.post_result()

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

    slack = Slack()
    sim_results = SimulationResultBuilder(slack)
    sim_results.set_start_time(dt.datetime.utcnow())

    script_file = 'start_local.sh'
    os.system(f'cat {script_file}')
    print()

    validator = LocalValidator(script_file)
    validator.start() # sometimes you gotta wait a bit for it to startup
    time.sleep(5)

    try:
        await clone_close(sim_results)
    finally:
        validator.stop()

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())


# %%
