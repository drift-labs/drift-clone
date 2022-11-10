## req 
- python >= 3.9
    - conda create -n tmp python=3.10
- pip install -r req.txt
- bash setup.sh:
    - inits other submodules
    - builds v2 & solana -- cli solana-test-validator doesnt have `--accounts-dir` flag yet so we need to clone full solana repo (grab some tea lol) ... 
    
## main files
- `python clone.py`: clones mainnet to local
- `close.py`: settles markets and all of the users positions
- `invariants.py`: assert invariants hold true (eg, market.net_baa = sum(user.baa))

## random notes
- when you scrape make sure... 
    - program_id is up to date in the driftpy sdk 
    - make sure program in driftpy/protocol-v2 is the same version as program_id 
    - idl is up to date (can use `update_idl.sh` to do this)
- sometimes the validator doesnt shutdown cleanly 
    - check the pid from `ps aux | grep solana` and kill
