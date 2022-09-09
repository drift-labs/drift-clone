## req 
- python >= 3.9
- pip install -r req.txt

- init other repos: git submodule update --init && cd driftpy && git submodule update --init
- build solana validator: cd solana/validator && cargo build 
    - cli solana-test-validator doesnt have `--accounts-dir` flag yet so we need to clone ...
    - solana/target/debug/solana-test-validator should exist 
- `python scrape.py`
- `close_all.ipynb`

## notes
- when you scrape make sure... 
    - program_id is up to date in the driftpy sdk 
    - make sure program in driftpy/protocol-v2 is the same version as program_id 
    - idl is up to date (can use `update_idl.sh` to do this)
- sometimes the validator doesnt shutdown cleanly 
    - check the pid from `ps aux | grep solana` and kill
