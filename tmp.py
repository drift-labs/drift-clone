# %%
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

# %%
config = configs['mainnet']
url = config.default_http
state_kp = Keypair() ## new admin kp
wallet = Wallet(state_kp)
connection = AsyncClient(url)
provider = Provider(connection, wallet)
ch = ClearingHouse.from_config(config, provider)
print('reading from program:', ch.program_id)

# %%
authority = PublicKey("6aiE94djwgR72ozpDfFUdcSCpCuVAxkxTArWmfyzay6d")
await get_user_account(
    ch.program, authority, 1
)

# %%
