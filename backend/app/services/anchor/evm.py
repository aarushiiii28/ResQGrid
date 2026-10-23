"""Optional EVM anchoring through web3.py. Off unless LEDGER_ANCHOR_PROVIDER=evm and the `evm` extra is installed
(`pip install -e ".[evm]"`). Works with a local Anvil/Hardhat node or a testnet (Polygon Amoy, Sepolia). It needs a
funded test key. Nothing in the core demo requires it."""

from app.services.anchor.base import AnchorProvider, VerifyResult

ABI = [
    {"type": "function", "name": "anchor", "stateMutability": "nonpayable", "inputs": [{"name": "root", "type": "bytes32"}], "outputs": []},
    {"type": "function", "name": "anchoredAt", "stateMutability": "view", "inputs": [{"name": "root", "type": "bytes32"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "event", "name": "Anchored", "anonymous": False,
     "inputs": [{"name": "root", "type": "bytes32", "indexed": True}, {"name": "by", "type": "address", "indexed": True}, {"name": "at", "type": "uint256", "indexed": False}]},
]


class EVMAnchorProvider(AnchorProvider):
    name = "evm"
    label = "EVM chain anchor (testnet or local node); verify on a block explorer"

    def __init__(self, rpc_url: str, contract_address: str, private_key: str, *, w3=None, chain_id: int | None = None):
        if w3 is None:
            try:
                from web3 import Web3
            except ImportError as e:  # pragma: no cover - exercised only without the extra
                raise RuntimeError('web3 is not installed: pip install -e ".[evm]"') from e
            w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 15}))
        self.w3 = w3
        self.account = w3.eth.account.from_key(private_key)
        self.contract = w3.eth.contract(address=w3.to_checksum_address(contract_address), abi=ABI)
        self.chain_id = chain_id

    def anchor(self, anchor_id, merkle_root, from_seq, to_seq, anchored_at) -> dict:
        w3 = self.w3
        tx = self.contract.functions.anchor(bytes.fromhex(merkle_root)).build_transaction({
            "from": self.account.address,
            "nonce": w3.eth.get_transaction_count(self.account.address),
            "chainId": self.chain_id or w3.eth.chain_id,
        })
        signed = self.account.sign_transaction(tx)
        h = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(h, timeout=120)
        return {"tx": h.hex(), "block": receipt["blockNumber"], "contract": self.contract.address,
                "chain_id": self.chain_id or w3.eth.chain_id, "label": self.label}

    def verify(self, anchor_id, merkle_root, reference) -> VerifyResult:
        try:
            ts = self.contract.functions.anchoredAt(bytes.fromhex(merkle_root)).call()
        except Exception as e:  # network or contract problem: report, do not guess
            return VerifyResult(False, f"could not reach the chain: {e}")
        if ts and int(ts) > 0:
            return VerifyResult(True, f"root found on chain (block time {int(ts)}), tx {reference.get('tx', '?')}")
        return VerifyResult(False, "root not found on chain")
