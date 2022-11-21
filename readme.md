## req 
```bash
# setup env
conda create -n tmp python=3.10
pip install -r req.txt
# inits other submodules
git submodule update --init 
```

note: need solana-cli v1.14.7 or greater for local validator's --account-dir flag to work 
(`sh -c "$(curl -sSfL https://release.solana.com/v1.14.7/install)"`)


## Environment Variables

These environment varibles are required to run the scripts

**Required**:
* one of the following are required
    * `RPC_URL`, full Solana RPC node URL 
    * `API_KEY`, API token for https://drift-cranking.rpcpool.com/

**Optional**:
* `SLACK_BOT_TOKEN` optional, slack messages will be no-op without this 
* `SLACK_CHANNEL` optional, slack messages will be no-op without this

    
## main files
- `clone.py`: clones mainnet accounts to disk (is later loaded into a local validator)
- `close.py`: closes all of the users positions
- `invariants.py`: assert invariants hold true (eg, market.net_baa = sum(user.baa))

## random notes
- when you scrape make sure... 
    - program_id is up to date in the driftpy sdk 
    - make sure program in driftpy/protocol-v2 is the same version as program_id 
    - idl is up to date (can use `update_idl.sh` to do this)
- sometimes the validator doesnt shutdown cleanly 
    - check the pid from `ps aux | grep solana` and kill
