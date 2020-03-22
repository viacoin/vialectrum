#!/usr/bin/env bash
export HOME=~
set -eux pipefail
mkdir -p ~/.viacoin
cat > ~/.viacoin/viacoin.conf <<EOF
regtest=1
txindex=1
printtoconsole=1
rpcuser=doggman
rpcpassword=donkey
rpcallowip=127.0.0.1
zmqpubrawblock=tcp://127.0.0.1:28332
zmqpubrawtx=tcp://127.0.0.1:28333
[regtest]
rpcbind=0.0.0.0
rpcport=18554
EOF
rm -rf ~/.viacoin/regtest
screen -S viacoind -X quit || true
screen -S viacoind -m -d viacoind -regtest
sleep 6
addr=$(viacoin-cli getnewaddress)
viacoin-cli generatetoaddress 150 $addr > /dev/null
