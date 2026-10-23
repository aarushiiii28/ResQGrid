"""Deploy contracts/ResQGridAnchor.sol to an EVM chain (local Anvil/Hardhat node, Polygon Amoy or Sepolia testnet).

    pip install -e ".[evm]" py-solc-x
    set EVM_RPC_URL=http://127.0.0.1:8545       (anvil)       or your testnet RPC
    set EVM_PRIVATE_KEY=0x...                  (a funded TEST key; never a real one)
    python scripts/deploy_anchor_contract.py

Prints the contract address. Then run the app with LEDGER_ANCHOR_PROVIDER=evm, EVM_CONTRACT=<address>,
EVM_RPC_URL and EVM_PRIVATE_KEY. This is optional: the default local anchor needs no keys and no network."""

import os
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "contracts" / "ResQGridAnchor.sol"


def main() -> None:
    try:
        import solcx
        from web3 import Web3
    except ImportError:
        sys.exit('missing packages: pip install -e ".[evm]" py-solc-x')
    rpc = os.environ.get("EVM_RPC_URL", "http://127.0.0.1:8545")
    key = os.environ.get("EVM_PRIVATE_KEY")
    if not key:
        sys.exit("set EVM_PRIVATE_KEY to a funded TEST key")
    version = "0.8.20"
    if version not in [str(v) for v in solcx.get_installed_solc_versions()]:
        solcx.install_solc(version)  # downloads the compiler once
    compiled = solcx.compile_source(SOURCE.read_text(encoding="utf-8"), output_values=["abi", "bin"], solc_version=version)
    _, iface = next(iter(compiled.items()))
    w3 = Web3(Web3.HTTPProvider(rpc))
    acct = w3.eth.account.from_key(key)
    contract = w3.eth.contract(abi=iface["abi"], bytecode=iface["bin"])
    tx = contract.constructor().build_transaction({"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
                                                   "chainId": w3.eth.chain_id})
    receipt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(acct.sign_transaction(tx).raw_transaction), timeout=180)
    print(f"deployed ResQGridAnchor at {receipt['contractAddress']} (chain {w3.eth.chain_id}, block {receipt['blockNumber']})")


if __name__ == "__main__":
    main()
