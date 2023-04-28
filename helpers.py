from anchorpy import Provider
from anchorpy import Wallet
from solana.rpc.async_api import AsyncClient
from driftpy.clearing_house import ClearingHouse
from solana.keypair import Keypair
import pathlib
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
    connection: AsyncClient,
    keypairs_path='keypairs/',
):
    admin_ch = None
    chs = []
    sigs = []
    for p in pathlib.Path(keypairs_path).iterdir():
        with open(p, 'r') as f:
            s = f.read()
        kp = Keypair().from_secret_key(bytearray.fromhex(s))

        sig = (await connection.request_airdrop(
            kp.public_key,
            int(100 * 1e9)
        ))['result']
        sigs.append(sig)

        # save clearing house
        wallet = Wallet(kp)
        provider = Provider(connection, wallet)

        if p.name == 'state.secret':
            print('found admin...')
            admin_ch = Admin.from_config(config, provider)
        else:
            ch = ClearingHouse.from_config(config, provider)
            chs.append(ch)

    print('confirming SOL airdrops...')
    await connection.confirm_transaction(sigs[-1])

    return chs, admin_ch
