import hashlib

import pytest

from app.services import merkle
from app.services.anchor.local import LocalAnchorProvider
from app.services.crypto_service import (
    GENESIS,
    ServerKey,
    Vault,
    b64,
    canonical,
    iso,
    key_id_for,
    sha256_hex,
    valid_public_key,
    verify_signature,
)


def hashes(n):
    return [hashlib.sha256(f"entry-{i}".encode()).hexdigest() for i in range(n)]


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 7, 8, 13, 64, 100])
def test_every_leaf_has_a_valid_inclusion_proof_and_a_wrong_leaf_does_not(n):
    hs = hashes(n)
    root = merkle.merkle_root(hs)
    for i, h in enumerate(hs):
        assert merkle.verify_proof(h, merkle.merkle_proof(hs, i), root)
    assert not merkle.verify_proof(hashlib.sha256(b"forged").hexdigest(), merkle.merkle_proof(hs, 0), root)


def test_changing_one_entry_changes_the_root_and_breaks_old_proofs():
    hs = hashes(9)
    root = merkle.merkle_root(hs)
    path = merkle.merkle_proof(hs, 3)
    altered = list(hs)
    altered[5] = hashlib.sha256(b"changed").hexdigest()
    assert merkle.merkle_root(altered) != root
    assert not merkle.verify_proof(hs[3], path, merkle.merkle_root(altered))


def test_leaves_and_nodes_are_domain_separated():
    hs = hashes(2)
    # a node hash can never be passed off as a leaf (second-preimage guard)
    node = merkle.merkle_root(hs)
    assert merkle.merkle_root([node]) != node


def test_bad_proofs_do_not_crash():
    assert merkle.verify_proof("zz", [], "00") is False
    assert merkle.verify_proof(hashes(1)[0], [{"hash": "nothex", "position": "left"}], "00") is False
    with pytest.raises(IndexError):
        merkle.merkle_proof(hashes(3), 3)


def test_canonical_json_is_order_independent_and_timestamps_have_fixed_width():
    assert canonical({"b": 1, "a": [2, {"d": 1, "c": 2}]}) == canonical({"a": [2, {"c": 2, "d": 1}], "b": 1})
    from datetime import UTC, datetime

    assert iso(datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)) == "2026-01-02T03:04:05.000006Z"


def test_ed25519_sign_verify_and_key_ids(tmp_path):
    k = ServerKey(tmp_path / "k.key")
    sig = k.sign("hello")
    assert verify_signature(k.public_b64, "hello", sig)
    assert not verify_signature(k.public_b64, "hellp", sig)
    assert not verify_signature(k.public_b64, "hello", b64(b"x" * 64))
    assert valid_public_key(k.public_b64) and not valid_public_key("AAAA")
    assert key_id_for(k.public_b64).startswith("k-")
    again = ServerKey(tmp_path / "k.key")                 # persisted: the ledger stays verifiable after a restart
    assert again.public_b64 == k.public_b64


def test_vault_encrypts_per_subject_and_shredding_means_the_key_cannot_be_recovered():
    v = Vault(None, "secret")
    key, wrapped = v.new_subject_key()
    blob = v.encrypt(key, {"lat": 30.9}, aad="evt-1")
    assert v.decrypt(v.unwrap(wrapped), blob, "evt-1") == {"lat": 30.9}
    assert v.decrypt(v.unwrap(wrapped), blob, "evt-2") is None          # bound to its event
    other, _ = v.new_subject_key()
    assert v.decrypt(other, blob, "evt-1") is None
    assert Vault(None, "different-secret")._master != v._master


def test_local_anchor_file_is_hash_chained_and_detects_edits(tmp_path):
    p = LocalAnchorProvider(tmp_path / "anchors.jsonl")
    r1 = p.anchor("ANC-0001", "a" * 64, 1, 5, "t1")
    p.anchor("ANC-0002", "b" * 64, 6, 9, "t2")
    assert p.verify("ANC-0001", "a" * 64, r1).ok and p.verify("ANC-0002", "b" * 64, {}).ok
    assert not p.verify("ANC-0001", "c" * 64, r1).ok                    # different root
    assert not p.verify("ANC-0009", "a" * 64, {}).ok                    # missing
    text = (tmp_path / "anchors.jsonl").read_text()
    (tmp_path / "anchors.jsonl").write_text(text.replace("a" * 64, "d" * 64))
    bad = p.verify("ANC-0002", "b" * 64, {})
    assert not bad.ok and "broken" in bad.detail
    assert "not a public blockchain" in p.label


def test_genesis_is_all_zero():
    assert GENESIS == "0" * 64 and sha256_hex("") == hashlib.sha256(b"").hexdigest()
