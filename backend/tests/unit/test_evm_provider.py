"""EVMAnchorProvider against a tiny in-memory fake of web3 (the real chain is optional and needs a funded test key)."""

import pytest

from app.services.anchor import FabricAnchorProvider, build_anchor_provider
from app.services.anchor.evm import EVMAnchorProvider


class FakeFn:
    def __init__(self, chain, name, root):
        self.chain, self.name, self.root = chain, name, root

    def build_transaction(self, tx):
        return {**tx, "call": (self.name, self.root)}

    def call(self):
        return self.chain.anchored.get(self.root, 0)


class FakeFunctions:
    def __init__(self, chain):
        self.chain = chain

    def anchor(self, root):
        return FakeFn(self.chain, "anchor", root)

    def anchoredAt(self, root):  # noqa: N802 - matches the contract ABI
        return FakeFn(self.chain, "anchoredAt", root)


class FakeContract:
    def __init__(self, chain, address):
        self.functions, self.address = FakeFunctions(chain), address


class FakeAccount:
    address = "0xTEST"

    def sign_transaction(self, tx):
        class S:
            raw_transaction = tx

        return S()


class FakeEth:
    chain_id = 31337

    def __init__(self, chain):
        self.chain = chain
        self.account = type("A", (), {"from_key": staticmethod(lambda k: FakeAccount())})()

    def contract(self, address, abi):
        return FakeContract(self.chain, address)

    def get_transaction_count(self, addr):
        return 0

    def send_raw_transaction(self, tx):
        name, root = tx["call"]
        if name == "anchor":
            if root in self.chain.anchored:
                raise RuntimeError("already anchored")
            self.chain.anchored[root] = 1_700_000_000
        return type("H", (), {"hex": lambda s: "0xabc123"})()

    def wait_for_transaction_receipt(self, h, timeout=0):
        return {"blockNumber": 7}


class FakeW3:
    def __init__(self):
        self.anchored: dict[bytes, int] = {}
        self.eth = FakeEth(self)

    def to_checksum_address(self, a):
        return a


def provider():
    return EVMAnchorProvider("http://x", "0xCONTRACT", "0x01", w3=FakeW3())


def test_anchor_publishes_the_root_and_verify_finds_it():
    p = provider()
    root = "ab" * 32
    ref = p.anchor("ANC-0001", root, 1, 9, "t")
    assert ref["tx"] == "0xabc123" and ref["contract"] == "0xCONTRACT" and ref["chain_id"] == 31337 and ref["block"] == 7
    assert p.verify("ANC-0001", root, ref).ok
    assert not p.verify("ANC-0001", "cd" * 32, ref).ok            # a different root was never anchored
    with pytest.raises(RuntimeError):
        p.anchor("ANC-0002", root, 10, 12, "t")                    # a root can never be anchored twice or replaced


def test_chain_errors_are_reported_not_guessed():
    p = provider()

    def boom():
        raise ConnectionError("rpc down")

    p.contract.functions.anchoredAt = lambda r: type("F", (), {"call": staticmethod(boom)})()
    res = p.verify("ANC-1", "ab" * 32, {})
    assert not res.ok and "could not reach the chain" in res.detail


def test_provider_selection_and_stubs(tmp_path):
    from app.core.config import get_settings

    s = get_settings()
    assert build_anchor_provider(s.model_copy(update={"ledger_anchor_provider": "local"}), tmp_path).name == "local"
    fab = build_anchor_provider(s.model_copy(update={"ledger_anchor_provider": "fabric"}), tmp_path)
    assert isinstance(fab, FabricAnchorProvider) and not fab.verify("a", "b", {}).ok
    with pytest.raises(NotImplementedError):
        fab.anchor("a", "b", 1, 2, "t")
