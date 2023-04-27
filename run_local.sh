if [ -d "accounts" ]; then
  echo "accounts/ folder exists, skipping mainnet-beta clone"
else
  echo "accounts/ folder doesn't exists, cloning mainnet-beta"
  python clone.py 
fi
python close.py