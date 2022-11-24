import os
import datetime as dt
from collections import namedtuple
from typing import List
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from driftpy.types import PerpMarket, SpotMarket
from driftpy.constants.numeric_constants import (
    AMM_RESERVE_PRECISION,
    QUOTE_PRECISION,
    BASE_PRECISION,
    FUNDING_RATE_PRECISION,
    PRICE_PRECISION,
    SPOT_BALANCE_PRECISION,
    SPOT_CUMULATIVE_INTEREST_PRECISION
)

class Slack:
    def __init__(self) -> None:
        token = os.environ.get('SLACK_BOT_TOKEN')
        channel = os.environ.get('SLACK_CHANNEL')
        if token is None or channel is None:
            print("SLACK_BOT_TOKEN or SLACK_CHANNEL environment variables not set. Skipping slack notifications.")
            self.client = None
            self.channel = None
        else:
            self.client = WebClient(token=token)
            self.channel = channel

    def can_send_messages(self) -> bool:
        return self.client is not None and self.channel is not None

    def send_message(self, msg):
        if (self.client is None or self.channel is None):
            return

        try:
            self.client.chat_postMessage(
                channel=self.channel,
                text=msg
            )
        except SlackApiError as e:
            assert e.response["error"]  # str like 'invalid_auth', 'channel_not_found'


ExpiredMarket = namedtuple(
    'ExpiredMarket',
    ['market_idx', 'status', 'expiry_price', 'last_oracle_price_twap', 'last_oracle_price'],
)
PerpMarketTuple = namedtuple(
    'PerpMarketTuple',
    [
        'market_idx',
        'total_fee_minus_distributions',
        'base_asset_amount_with_amm',
        'base_asset_amount_with_unsettled_lp',
        'base_asset_amount_long', 
        'base_asset_amount_short', 
        'user_lp_shares', 
        'total_social_loss', 
        'cumulative_funding_rate_long', 
        'cumulative_funding_rate_short', 
        'last_funding_rate_long', 
        'last_funding_rate_short', 
        'fee_pool', 
        'pnl_pool',
    ]
)

SpotMarketTuple = namedtuple(
    'SpotMarketTuple',
    [
        'market_idx',
        'revenue_pool',
        'spot_fee_pool',
        'insurance_fund_balance',
        'total_spot_fee',
        'deposit_balance', 
        'borrow_balance', 
        'cumulative_deposit_interest', 
        'cumulative_borrow_interest', 
        'total_social_loss', 
        'total_quote_social_loss', 
        'liquidator_fee', 
        'if_liquidation_fee', 
        'status', 
    ]
)

class SimulationResultBuilder:
    '''
    SimulationResultBuilder takes in results of a simulation run and builds a nice text message to be sent to slack.
    '''
    def __init__(self, slack: Slack) -> None:
        self.slack = slack
        self.start_slot = 0
        self.start_time = dt.datetime.now()
        self.commit_hash = os.environ.get("COMMIT")
        self.settled_markets = []
        self.total_users = 0
        self.settle_user_success = 0
        self.settle_user_fail_reasons = []
        self.initial_perp_markets = []
        self.initial_spot_markets = []
        self.final_perp_markets = []
        self.final_spot_markets = []

        self.slack.send_message(f"Simulation run started at: {self.start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}\nCommit: `{self.commit_hash}`")

    def set_start_slot(self, slot: int):
        self.start_slot = slot

    def set_start_time(self, start_time: dt.datetime):
        self.start_time = start_time

    def set_end_time(self, end_time: dt.datetime):
        self.end_time = end_time

    def add_settled_expired_market(self, market: ExpiredMarket):
        self.settled_markets.append(market)

    def add_total_users(self, total_users: int):
        self.total_users = total_users

    def add_settle_user_success(self):
        self.settle_user_success = self.settle_user_success + 1

    def add_settle_user_fail(self, e: Exception):
        self.settle_user_fail_reasons.append(e)

    def perp_market_to_tuple(self, market: PerpMarket) -> PerpMarketTuple:
        return PerpMarketTuple(
            market.market_index,
            market.amm.total_fee_minus_distributions / QUOTE_PRECISION,
            market.amm.base_asset_amount_with_amm / BASE_PRECISION,
            market.amm.base_asset_amount_with_unsettled_lp / BASE_PRECISION,
            market.amm.base_asset_amount_long / BASE_PRECISION,
            market.amm.base_asset_amount_short / BASE_PRECISION,
            market.amm.user_lp_shares/ AMM_RESERVE_PRECISION,
            market.amm.total_social_loss / QUOTE_PRECISION,
            market.amm.cumulative_funding_rate_long / FUNDING_RATE_PRECISION,
            market.amm.cumulative_funding_rate_short / FUNDING_RATE_PRECISION,
            market.amm.last_funding_rate_long / FUNDING_RATE_PRECISION,
            market.amm.last_funding_rate_short / FUNDING_RATE_PRECISION,
            market.amm.fee_pool.scaled_balance / QUOTE_PRECISION,
            market.pnl_pool.scaled_balance / SPOT_BALANCE_PRECISION,
        )


    def spot_market_to_tuple(self, insurance_fund_balance: str, market: SpotMarket) -> SpotMarketTuple:
        precision = 1*10**market.decimals
        return SpotMarketTuple(
            market.market_index,
            market.revenue_pool.scaled_balance / precision,
            market.spot_fee_pool.scaled_balance / precision,
            insurance_fund_balance,
            market.total_spot_fee / precision,
            market.deposit_balance / precision,
            market.borrow_balance / precision,
            market.cumulative_deposit_interest / SPOT_CUMULATIVE_INTEREST_PRECISION,
            market.cumulative_borrow_interest/ SPOT_CUMULATIVE_INTEREST_PRECISION,
            market.total_social_loss / precision,
            market.total_quote_social_loss / precision,
            market.liquidator_fee / precision,
            market.if_liquidation_fee / precision,
            market.status,
        )


    def add_initial_perp_market(self, market: PerpMarket):
        self.initial_perp_markets.append(self.perp_market_to_tuple(market))

    def add_initial_spot_market(self, insurance_fund_balance: str, market: SpotMarket):
        self.initial_spot_markets.append(self.spot_market_to_tuple(insurance_fund_balance, market))

    def add_final_perp_market(self, market: PerpMarket):
        self.final_perp_markets.append(self.perp_market_to_tuple(market))

    def add_final_spot_market(self, insurance_fund_balance: str, market: SpotMarket):
        self.final_spot_markets.append(self.spot_market_to_tuple(insurance_fund_balance, market))

    def print_perp_markets(self, markets: List[PerpMarketTuple], final_markets) -> str:
        msg = ""
        for market, f_market in zip(markets, final_markets):
            msg += f" Perp Market {market.market_idx}\n"
            msg += f"  Total fee minus distributions: {market.total_fee_minus_distributions} -> {f_market.total_fee_minus_distributions}\n"
            msg += f"  Base asset amount with AMM:    {market.base_asset_amount_with_amm} -> {f_market.base_asset_amount_with_amm}\n"
            msg += f"  Base asset amount with LP:     {market.base_asset_amount_with_unsettled_lp} -> {f_market.base_asset_amount_with_unsettled_lp}\n"
            msg += f"  Base asset amount long:        {market.base_asset_amount_long} -> {f_market.base_asset_amount_long}\n"
            msg += f"  Base asset amount short:       {market.base_asset_amount_short} -> {f_market.base_asset_amount_short}\n"
            msg += f"  User LP shares:                {market.user_lp_shares} -> {f_market.user_lp_shares}\n"
            msg += f"  Total social loss:             {market.total_social_loss} -> {f_market.total_social_loss}\n"
            msg += f"  Cumulative funding rate long:  {market.cumulative_funding_rate_long} -> {f_market.cumulative_funding_rate_long}\n"
            msg += f"  Cumulative funding rate short: {market.cumulative_funding_rate_short} -> {f_market.cumulative_funding_rate_short}\n"
            msg += f"  Last funding rate long:        {market.last_funding_rate_long} -> {f_market.last_funding_rate_long}\n"
            msg += f"  Last funding rate short:       {market.last_funding_rate_short} -> {f_market.last_funding_rate_short}\n"
            msg += f"  Fee pool:                      {market.fee_pool} -> {f_market.fee_pool}\n"
            msg += f"  Pnl pool:                      {market.pnl_pool} -> {f_market.pnl_pool}\n"
        return msg

    def print_spot_markets(self, markets: List[SpotMarketTuple], f_markets) -> str:
        msg = ""
        for market, f_market in zip(markets, f_markets):
            msg += f" Spot Market {market.market_idx}\n"
            msg += f"  Revenue pool:                  {market.revenue_pool} -> {f_market.revenue_pool}\n"
            msg += f"  Spot fee pool:                 {market.spot_fee_pool} -> {f_market.spot_fee_pool}\n"
            msg += f"  Insurance fund balance:        {market.insurance_fund_balance} -> {f_market.insurance_fund_balance}\n"
            msg += f"  Total spot fee:                {market.total_spot_fee} -> {f_market.total_spot_fee}\n"
            msg += f"  Deposit balance:               {market.deposit_balance} -> {f_market.deposit_balance}\n"
            msg += f"  Borrow balance:                {market.borrow_balance} -> {f_market.borrow_balance}\n"
            msg += f"  Cumulative deposit interest:   {market.cumulative_deposit_interest} -> {f_market.cumulative_deposit_interest}\n"
            msg += f"  Cumulative borrow interest:    {market.cumulative_borrow_interest} -> {f_market.cumulative_borrow_interest}\n"
            msg += f"  Total social loss:             {market.total_social_loss} -> {f_market.total_social_loss}\n"
            msg += f"  Total quote social loss:       {market.total_quote_social_loss} -> {f_market.total_quote_social_loss}\n"
            msg += f"  Liquidator fee:                {market.liquidator_fee} -> {f_market.liquidator_fee}\n"
            msg += f"  Insurance fund liquidation fee:{market.if_liquidation_fee} -> {f_market.if_liquidation_fee}\n"
            msg += f"  Status:                        {market.status} -> {f_market.status}\n"
        return msg


    def build_message(self) -> str:
        msg = f"*Sim slot:       {self.start_slot}*\n"
        msg += f"*Time elapsed:  {self.end_time - self.start_time}*\n"
        msg += f"\n*Settled markets:*\n"
        msg += '```\n'
        for expired_market in self.settled_markets:
            msg += f" Market {expired_market.market_idx}, status: {expired_market.status}\n"
            msg += f"  Expiry price:           {expired_market.expiry_price}\n"
            msg += f"  Last oracle price:      {expired_market.last_oracle_price}\n"
            msg += f"  Last oracle price twap: {expired_market.last_oracle_price_twap}\n"
        msg += '```\n'

        total_users_with_positions = len(self.settle_user_fail_reasons) + self.settle_user_success
        msg += f"\n*Settled Users:*\n"
        msg += '```\n'
        msg += f" Total users: {self.total_users}, users with positions: {total_users_with_positions}\n"
        if len(self.settle_user_fail_reasons) == 0:
            msg += f" All {self.settle_user_success}/{total_users_with_positions}  users settled successfully ✅\n"
        else:
            msg += f" {len(self.settle_user_fail_reasons)}/{total_users_with_positions} users settled unsuccessfully ❌, reasons:\n"
            for (i, e) in enumerate(self.settle_user_fail_reasons):
                msg += f"  {i}: {e}\n"
        msg += '```\n'

        msg += f"\n*Perp Market Metrics:*\n"
        msg += '```\n'
        msg += self.print_perp_markets(self.initial_perp_markets, self.final_perp_markets)
        msg += '```\n'

        msg += f"\n*Spot Market Metrics:*\n"
        msg += '```\n'
        msg += self.print_spot_markets(self.initial_spot_markets, self.final_spot_markets)
        msg += '```\n'
        
        # # need to update deposits
        # msg += f"\n*Final State Invariants:*\n"
        # msg += '```\n'
        # total_market_money = 0
        # for market in range(self.final_perp_markets):
        #     total_market_money += market.amm.fee_pool.scaled_balance + market.pnl_pool.scaled_balance

        # quote_spot: SpotMarket = self.final_spot_markets[0]
        # total_market_money = quote_spot.revenue_pool.scaled_balance + total_market_money

        # msg += f'total market money: {total_market_money}'
        # msg += f'spot market 0 balance: {quote_spot.deposit_balance}'
        # msg += f'market $ == spot deposit $'

        # msg += '```\n'

        return msg

    def post_result(self):
        msg = self.build_message()
        print(msg)
        if self.slack.can_send_messages():
            self.slack.send_message(msg)