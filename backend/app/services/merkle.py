"""Merkle tree over ledger entry hashes. Leaves and inner nodes are domain-separated (0x00 / 0x01) so a leaf can never be
mistaken for a node. An odd node at any level is promoted unchanged. Pure functions: usable offline."""

import hashlib


def _h(prefix: bytes, *parts: bytes) -> bytes:
    return hashlib.sha256(prefix + b"".join(parts)).digest()


def leaf(entry_hash_hex: str) -> bytes:
    return _h(b"\x00", bytes.fromhex(entry_hash_hex))


def _levels(entry_hashes: list[str]) -> list[list[bytes]]:
    if not entry_hashes:
        raise ValueError("no entries")
    level = [leaf(h) for h in entry_hashes]
    levels = [level]
    while len(level) > 1:
        nxt = [_h(b"\x01", level[i], level[i + 1]) if i + 1 < len(level) else level[i] for i in range(0, len(level), 2)]
        levels.append(nxt)
        level = nxt
    return levels


def merkle_root(entry_hashes: list[str]) -> str:
    return _levels(entry_hashes)[-1][0].hex()


def merkle_proof(entry_hashes: list[str], index: int) -> list[dict]:
    """Sibling path for entry_hashes[index]: [{'hash': hex, 'position': 'left'|'right'}], bottom to top."""
    if not 0 <= index < len(entry_hashes):
        raise IndexError(index)
    path: list[dict] = []
    idx = index
    for level in _levels(entry_hashes)[:-1]:
        sib = idx ^ 1
        if sib < len(level):
            path.append({"hash": level[sib].hex(), "position": "left" if sib < idx else "right"})
        idx //= 2
    return path


def verify_proof(entry_hash_hex: str, path: list[dict], root_hex: str) -> bool:
    try:
        node = leaf(entry_hash_hex)
        for step in path:
            sib = bytes.fromhex(step["hash"])
            node = _h(b"\x01", sib, node) if step["position"] == "left" else _h(b"\x01", node, sib)
        return node.hex() == root_hex
    except (ValueError, KeyError, TypeError):
        return False
