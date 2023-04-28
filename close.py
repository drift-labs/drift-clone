import sys
import timeit
sys.path.append("driftpy/src/")
sys.path.append("drift-sim/")

import pathlib
from tqdm import tqdm
import os
import time

from anchorpy import Provider
from solana.transaction import (
    TransactionInstruction,
    AccountMeta,
)
from solana.rpc import commitment
from solana.rpc.async_api import AsyncClient
from spl.token.constants import TOKEN_PROGRAM_ID
from spl.token.instructions import get_associated_token_address
import pprint

from driftsim.backtest.liquidator import Liquidator
from driftsim.backtest.main import _send_ix
from driftpy.clearing_house_user import ClearingHouseUser
from driftpy.constants.numeric_constants import (
    QUOTE_PRECISION,
    PRICE_PRECISION,
)
from driftpy.constants.config import configs
from driftpy.clearing_house import ClearingHouse
from driftpy.accounts import (
    get_perp_market_account,
    get_spot_market_account,
    get_state_account,
    get_if_stake_account,
    get_insurance_fund_vault_public_key,
    get_user_account_public_key,
    get_insurance_fund_stake_public_key,
)
from driftpy.types import (
    SpotMarket,
)

from termcolor import colored
import datetime as dt
import numpy as np

from slack import Slack, SimulationResultBuilder, ExpiredMarket
from helpers import load_local_users, LocalValidator



async def view_logs(sig: str, provider: Provider, print: bool = True):
    provider.connection._commitment = commitment.Confirmed
    logs = ""
    try:
        await provider.connection.confirm_transaction(sig, commitment.Confirmed)
        tx = await provider.connection.get_transaction(sig)
        logs = tx["result"]["meta"]["logMessages"]
    finally:
        provider.connection._commitment = commitment.Processed

    if print:
        pprint.pprint(logs)

    return logs


def load_subaccounts(chs):
    accounts = [p.stem for p in pathlib.Path("accounts").iterdir()]
    active_chs = []
    for ch in chs:
        subaccount_ids = []
        for sid in range(10):
            user_pk = get_user_account_public_key(ch.program_id, ch.authority, sid)
            if str(user_pk) in accounts:
                subaccount_ids.append(sid)

        ch.subaccounts = subaccount_ids
        if len(subaccount_ids) != 0:
            active_chs.append(ch)
    return active_chs


async def get_insurance_fund_balance(connection: AsyncClient, spot_market: SpotMarket):
    balance = await connection.get_token_account_balance(
        spot_market.insurance_fund.vault
    )
    if "error" in balance:
        raise Exception(balance)
    return balance["result"]["value"]["uiAmount"]


async def get_spot_vault_balance(connection: AsyncClient, spot_market: SpotMarket):
    balance = await connection.get_token_account_balance(spot_market.vault)
    if "error" in balance:
        raise Exception(balance)
    return balance["result"]["value"]["uiAmount"]


async def clone_close(sim_results: SimulationResultBuilder):
    config = configs["mainnet"]
    url = "http://127.0.0.1:8899"
    connection = AsyncClient(url)

    print("loading users...")
    chs, state_ch = await load_local_users(config, connection)
    provider = state_ch.program.provider
    program = state_ch.program

    chs = load_subaccounts(chs)
    state = await get_state_account(state_ch.program)
    n_markets, n_spot_markets = state.number_of_markets, state.number_of_spot_markets

    sim_slot = (await connection.get_slot())["result"]
    sim_results.set_start_slot(sim_slot)

    n_users = 0
    for ch in chs:
        for sid in ch.subaccounts:
            n_users += 1
    sim_results.add_total_users(n_users)

    # record stats pre-closing
    for i in range(n_markets):
        perp_market = await get_perp_market_account(program, i)
        sim_results.add_initial_perp_market(perp_market)
    for i in range(n_spot_markets):
        spot_market = await get_spot_market_account(program, i)
        insurance_fund_balance = await get_insurance_fund_balance(
            connection, spot_market
        )
        spot_vault_balance = await get_spot_vault_balance(connection, spot_market)
        print(f" {i}: {insurance_fund_balance} {spot_vault_balance}")
        sim_results.add_initial_spot_market(
            insurance_fund_balance, spot_vault_balance, spot_market
        )

    # update state
    await state_ch.update_perp_auction_duration(0)
    await state_ch.update_lp_cooldown_time(0)
    for i in range(n_spot_markets):
        await state_ch.update_update_insurance_fund_unstaking_period(i, 0)
        await state_ch.update_withdraw_guard_threshold(i, 2**64 - 1)

    print("delisting market...")
    slot = (await provider.connection.get_slot())["result"]
    dtime: int = (await provider.connection.get_block_time(slot))["result"]

    # + N seconds
    print("updating perp/spot market expiry...")
    seconds_time = 50  # inconsistent tbh
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
                    print(
                        f"removing lp on market {perp_market_idx} "
                        f"for user: {str(ch.authority)} "
                        f"(subaccountId: {sid}), shares: {position.lp_shares}"
                    )

                    sig = await ch.remove_liquidity(
                        position.lp_shares, perp_market_idx, sid
                    )
                    _sigs.append(sig)

    # verify
    if len(_sigs) > 0:
        await connection.confirm_transaction(_sigs[-1])
    perp_market = await get_perp_market_account(state_ch.program, perp_market_idx)
    print("market.amm.user_lp_shares == 0: ", perp_market.amm.user_lp_shares == 0)

    # fully expire market
    print("waiting for expiry...")
    from solana.rpc import commitment

    for i, sig in enumerate(sigs):
        await provider.connection.confirm_transaction(sig, commitment.Confirmed)

    while True:
        slot = (await provider.connection.get_slot())["result"]
        new_dtime: int = (await provider.connection.get_block_time(slot))["result"]
        time.sleep(0.2)
        if new_dtime > dtime + seconds_time:
            break

    print("settling expired market")
    for i in range(n_markets):
        sig = await state_ch.settle_expired_market(i)
        await provider.connection.confirm_transaction(sig, commitment.Finalized)

        perp_market = await get_perp_market_account(program, i)
        print(
            f"market {i} expiry_price vs twap/price",
            perp_market.status,
            perp_market.expiry_price,
            perp_market.amm.historical_oracle_data.last_oracle_price_twap,
            perp_market.amm.historical_oracle_data.last_oracle_price,
        )
        expired_market = ExpiredMarket(
            i,
            perp_market.status,
            perp_market.expiry_price / PRICE_PRECISION,
            perp_market.amm.historical_oracle_data.last_oracle_price_twap
            / PRICE_PRECISION,
            perp_market.amm.historical_oracle_data.last_oracle_price / PRICE_PRECISION,
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

            # update cache to look at the correct user account
            cache["user"] = account

            chu.use_cache = True
            await chu.set_cache(cache)
            fc = await chu.get_free_collateral()

            ch_idx.append((i, sid))
            free_collateral.append(fc)

    liq_idx0 = np.argmax(free_collateral)
    liq_idx0, liq_idx1 = np.argsort(free_collateral)[::-1][:2]

    print("attempting liquidation round 1...")
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
    print("attempting liquidation round 2...")
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

    print("removing IF stakes...")

    async def remove_if_stake(clearing_house: ClearingHouse, market_index):
        spot = await get_spot_market_account(clearing_house.program, market_index)
        total_shares = spot.insurance_fund.total_shares
        if_stake = await get_if_stake_account(
            clearing_house.program, clearing_house.authority, market_index
        )
        n_shares = if_stake.if_shares

        conn = clearing_house.program.provider.connection
        vault_pk = get_insurance_fund_vault_public_key(
            clearing_house.program_id, market_index
        )

        balance = await conn.get_token_account_balance(vault_pk)
        v_amount = int(balance["result"]["value"]["amount"])

        print(
            f"vault_amount: {v_amount} "
            f"n_shares: {n_shares} "
            f"total_shares: {total_shares}"
        )
        withdraw_amount = int(v_amount * n_shares / total_shares)
        print(f"withdrawing {withdraw_amount/QUOTE_PRECISION}...")

        if round(withdraw_amount/QUOTE_PRECISION) == 0:
            print(f"IF stake too small: {round(withdraw_amount)}")
            return None

        ixs = []
        if if_stake.last_withdraw_request_shares == 0:
            ix = await clearing_house.get_request_remove_insurance_fund_stake_ix(
                market_index, withdraw_amount
            )
            ixs.append(ix)

        ix2 = await clearing_house.get_remove_insurance_fund_stake_ix(
            market_index,
        )
        ixs.append(ix2)
        sig = await clearing_house.send_ixs(ixs)

        return sig

    accounts = await ch.program.account["InsuranceFundStake"].all()
    print("n insurance fund stakes", len(accounts))

    for i in range(n_spot_markets):
        for ch in user_chs.values():
            if_position_pk = get_insurance_fund_stake_public_key(
                ch.program_id, ch.authority, i
            )
            resp = await ch.program.provider.connection.get_account_info(if_position_pk)
            if resp["result"]["value"] is None:
                continue

            if_account = await get_if_stake_account(ch.program, ch.authority, i)
            if if_account.if_shares > 0:
                from spl.token.instructions import create_associated_token_account

                spot_market = await get_spot_market_account(ch.program, i)

                ix = create_associated_token_account(
                    ch.authority, ch.authority, spot_market.mint
                )
                await ch.send_ixs(ix)

                ata = get_associated_token_address(ch.authority, spot_market.mint)
                ch.spot_market_atas[i] = ata

                sig = await remove_if_stake(ch, i)
                if sig is not None:
                    await connection.confirm_transaction(sig, commitment.Confirmed)

    for perp_market_idx in range(n_markets):
        success = False
        attempt = -1
        settle_sigs = []

        while not success:
            num_fails = 0
            success = True
            i = 0
            errors = []
            routines = []
            ids = []

            if attempt > 5:
                msg = "something went wrong during settle expired position with market "
                msg += f"{perp_market_idx}... \n"
                msg += f"failed to settle {num_fails} users... \n"
                msg += f"error msgs: {pprint.pformat(errors, indent=4)}"
                sim_results.post_fail(msg)
                return

            attempt += 1

            print(
                colored(
                    f" =>> market {perp_market_idx}: settle attempt {attempt}", "blue"
                )
            )
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
                    print(f"settled success... {i}/{n_users}")
                    sim_results.add_settle_user_success(perp_market_idx)
                except Exception as e:
                    success = False
                    if attempt > 0:
                        print(position, user_i, sid)
                    num_fails += 1
                    errors.append(e)

                    print(f"settled failed... {i}/{n_users}")
                    sim_results.add_settle_user_fail(e, perp_market_idx)
                    pprint.pprint(e)

            print(f"settled fin... {i}/{n_users}")

        print("confirming...")
        if len(settle_sigs) > 0:
            await connection.confirm_transaction(settle_sigs[-1])

    from spl.token.instructions import create_associated_token_account
    from driftpy.math.spot_market import get_token_amount
    from driftpy.setup.helpers import mint_ix

    # pay back all borrows
    print("paying back borrows...")
    pbar = tqdm(total=n_users)
    for _, ch in enumerate(chs):
        for sid in ch.subaccounts:
            sigs = []
            for spot_market_index in range(n_spot_markets):
                spot_market = await get_spot_market_account(program, spot_market_index)
                market_name = "".join(map(chr, spot_market.name)).strip(" ")

                position = await ch.get_user_spot_position(spot_market_index, sid)
                if position is None:
                    continue

                if spot_market_index not in ch.spot_market_atas:
                    ix = create_associated_token_account(
                        ch.authority, ch.authority, spot_market.mint
                    )
                    await ch.send_ixs(ix)
                    ata = get_associated_token_address(ch.authority, spot_market.mint)
                    ch.spot_market_atas[spot_market_index] = ata

                if str(position.balance_type) != "SpotBalanceType.Borrow()":
                    continue

                # print(f'paying back borrow for spot {spot_market.market_index}...')
                # mint to
                token_amount = get_token_amount(
                    position.scaled_balance, spot_market, position.balance_type
                )

                token_units = 10**spot_market.decimals
                token_amount = int(
                    token_amount + 0.01 * (10**spot_market.decimals)
                )  # todo: add .01 for rounding issues?
                print(
                    f"paying back borrow of {float(token_amount)/token_units} "
                    f"{market_name} ({spot_market.market_index})"
                )

                if spot_market_index == 0:
                    mint_tx = mint_ix(
                        spot_market.mint,
                        state_ch.authority,
                        token_amount,
                        ch.spot_market_atas[spot_market_index],
                    )
                    await state_ch.send_ixs(mint_tx)

                else:
                    b = await connection.get_balance(
                        ch.spot_market_atas[spot_market_index]
                    )
                    if b["result"]["value"] < token_amount:
                        sig = (
                            await connection.request_airdrop(
                                ch.spot_market_atas[spot_market_index], token_amount
                            )
                        )["result"]
                        await connection.confirm_transaction(sig)

                    # sync native ix
                    # https://github.dev/solana-labs/solana-program-library/token/js/src/ix/types.ts
                    keys = [
                        AccountMeta(
                            pubkey=ch.spot_market_atas[spot_market_index],
                            is_signer=False,
                            is_writable=True,
                        )
                    ]
                    data = int.to_bytes(17, 1, "little")
                    program_id = TOKEN_PROGRAM_ID
                    ix = TransactionInstruction(
                        keys=keys, program_id=program_id, data=data
                    )
                    await ch.send_ixs(ix)

                # deposit / pay back
                sig = await ch.deposit(
                    int(1e19),
                    spot_market_index,
                    ch.spot_market_atas[spot_market_index],
                    user_id=sid,
                    reduce_only=True,
                )
                sigs.append(sig)
            pbar.update(1)

    print("confirming...")
    if len(sigs) > 0:
        await connection.confirm_transaction(sigs[-1])

    # withdraw all the money
    print("withdrawing all the money...")
    sigs = []
    for spot_market_index in range(n_spot_markets):
        spot_market = await get_spot_market_account(program, spot_market_index)
        attempt = -1
        ch: ClearingHouse
        success = False
        while not success and attempt < 0:  # only try once for rn
            attempt += 1
            success = True
            user_withdraw_count = 0
            print(
                colored(
                    f" =>> spot market {spot_market_index}: withdraw attempt {attempt}",
                    "blue",
                )
            )

            for _, ch in enumerate(chs):
                for sid in ch.subaccounts:
                    position = await ch.get_user_spot_position(spot_market_index, sid)
                    if position is None:
                        user_withdraw_count += 1
                        continue

                    spot_market = await get_spot_market_account(
                        program, spot_market_index
                    )
                    token_amount = int(
                        get_token_amount(
                            position.scaled_balance, spot_market, position.balance_type
                        )
                    )
                    print("token amount", token_amount)

                    # withdraw all of collateral
                    ix = await ch.get_withdraw_collateral_ix(
                        int(1e19),
                        spot_market_index,
                        ch.spot_market_atas[spot_market_index],
                        True,
                        user_id=sid,
                    )
                    (failed, sig, _) = await _send_ix(ch, ix, "withdraw", {})

                    if not failed:
                        user_withdraw_count += 1
                        print(
                            colored(
                                f"withdraw success: {user_withdraw_count}/{n_users}",
                                "green",
                            )
                        )
                        sigs.append(sig)
                    else:
                        print(
                            colored(
                                f"withdraw failed: {user_withdraw_count}/{n_users}",
                                "red",
                            )
                        )
                        success = False
                    print("---")

    print("confirming...")
    if len(sigs) > 0:
        await connection.confirm_transaction(sigs[-1], commitment=commitment.Finalized)

    n_spot, n_perp = 0, 0
    for ch in chs:
        for sid in ch.subaccounts:
            for i in range(n_markets):
                position = await ch.get_user_position(i, sid)
                if position is None:
                    continue
                n_perp += 1
                # print(position)

            for i in range(n_spot_markets):
                position = await ch.get_user_spot_position(i, sid)
                if position is None:
                    continue
                n_spot += 1
                # print(position)
    print("n (spot, perp) positions:", n_spot, n_perp)

    for i in range(n_markets):
        await state_ch.update_state_settlement_duration(1)
        sig = await state_ch.settle_expired_market_pools_to_revenue_pool(i)
    await connection.confirm_transaction(sig, commitment=commitment.Finalized)

    for i in range(n_markets):
        perp_market = await get_perp_market_account(program, i)
        sim_results.add_final_perp_market(perp_market)

    for i in range(n_spot_markets):
        spot_market = await get_spot_market_account(program, i)
        insurance_fund_balance = await get_insurance_fund_balance(
            connection, spot_market
        )
        spot_vault_balance = await get_spot_vault_balance(connection, spot_market)

        sim_results.add_final_spot_market(
            insurance_fund_balance, spot_vault_balance, spot_market
        )

    print("---")

    sim_results.set_end_time(dt.datetime.utcnow())
    sim_results.post_result()


async def main():
    slack = Slack()
    sim_results = SimulationResultBuilder(slack)
    sim_results.set_start_time(dt.datetime.utcnow())

    script_file = "start_local.sh"
    os.system(f"cat {script_file}")
    print()

    validator = LocalValidator(script_file)
    validator.start()
    time.sleep(8)

    try:
        await clone_close(sim_results)
    finally:
        validator.stop()


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())


# %%
