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

class LocalValidator:
    def __init__(self, script_file) -> None:
        self.script_file = script_file
        
    def start(self):
        """
        starts a new solana-test-validator by running the given script path 
        and logs the stdout/err to the logfile 
        """
        self.log_file = open('node.txt', 'w')
        self.proc = Popen(
            f'bash {self.script_file}'.split(' '), 
            stdout=self.log_file, 
            stderr=self.log_file, 
            preexec_fn=os.setsid
        )
        time.sleep(5)

    def stop(self):
        self.log_file.close()
        os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)  

async def load_local_users(
    config,
    connection, 
    keypairs_path='keypairs/',
):
    admin_ch = None
    chs = []
    for p in pathlib.Path(keypairs_path).iterdir():
        with open(p, 'r') as f: 
            s = f.read()
        kp = Keypair().from_secret_key(bytearray.fromhex(s))
        
        await connection.request_airdrop(
            kp.public_key, 
            int(100 * 1e9)
        )

        # save clearing house
        wallet = Wallet(kp)
        provider = Provider(connection, wallet)

        if p.name == 'state.secret':
            print('found admin...')
            admin_ch = Admin.from_config(config, provider)
        else:
            ch = ClearingHouse.from_config(config, provider)
            chs.append(ch)

    return chs, admin_ch