import os
import datetime as dt
from collections import namedtuple
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError

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
ResultingMarket = namedtuple(
    'ResultingMarket',
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
        'pnl_pool'
    ]
)


class SimulationResultBuilder:
    '''
    SimulationResultBuilder takes in results of a simulation run and builds a nice text message to be sent to slack.
    '''
    def __init__(self, slack: Slack) -> None:
        self.slack = slack
        self.start_time = dt.datetime.now()
        self.commit_hash = os.environ.get("COMMIT")
        self.settled_markets = []
        self.total_users = 0
        self.settle_user_success = 0
        self.settle_user_fail_reasons = []
        self.resulting_markets = []

        self.slack.send_message(f"Simulation run started at: {self.start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}\nCommit: `{self.commit_hash}`")

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

    def add_resulting_market(self, market: ResultingMarket):
        self.resulting_markets.append(market)

    def build_message(self) -> str:
        msg = f"*Time elapsed: {self.end_time - self.start_time}*\n"
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

        msg += f"\n*Resulting markets:*\n"
        msg += '```\n'
        for resulting_market in self.resulting_markets:
            msg += f" Market {resulting_market.market_idx}\n"
            msg += f"  Total fee minus distributions: {resulting_market.total_fee_minus_distributions}\n"
            msg += f"  Base asset amount with AMM:    {resulting_market.base_asset_amount_with_amm}\n"
            msg += f"  Base asset amount with LP:     {resulting_market.base_asset_amount_with_unsettled_lp}\n"
            msg += f"  Base asset amount long:        {resulting_market.base_asset_amount_long}\n"
            msg += f"  Base asset amount short:       {resulting_market.base_asset_amount_short}\n"
            msg += f"  User LP shares:                {resulting_market.user_lp_shares}\n"
            msg += f"  Total social loss:             {resulting_market.total_social_loss}\n"
            msg += f"  Cumulative funding rate long:  {resulting_market.cumulative_funding_rate_long}\n"
            msg += f"  Cumulative funding rate short: {resulting_market.cumulative_funding_rate_short}\n"
            msg += f"  Last funding rate long:        {resulting_market.last_funding_rate_long}\n"
            msg += f"  Last funding rate short:       {resulting_market.last_funding_rate_short}\n"
            msg += f"  Fee pool:                      {resulting_market.fee_pool}\n"
            msg += f"  Pnl pool:                      {resulting_market.pnl_pool}\n"
        msg += '```\n'

        return msg

    def post_result(self):
        msg = self.build_message()
        if self.slack.can_send_messages():
            self.slack.send_message(msg)
        else:
            print(msg)