import json
from pathlib import Path

from app.services.anchor.base import AnchorProvider, VerifyResult
from app.services.crypto_service import GENESIS, canonical, sha256_hex


class LocalAnchorProvider(AnchorProvider):
    """Default provider, needs no keys and no network. Each anchor is also appended to a hash-chained file kept
    OUTSIDE the database, so rewriting the database alone cannot hide a change.

    This is a LOCAL anchor. It is not a public blockchain and does not give third-party proof: whoever controls this
    machine controls the file. It shows the mechanism; the EVM provider gives an external witness."""

    name = "local"
    label = "local anchor, not a public blockchain"

    def __init__(self, path: Path):
        self.path = path

    def _lines(self) -> list[dict]:
        if not self.path.exists():
            return []
        out = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                out.append(json.loads(raw))
        return out

    def anchor(self, anchor_id, merkle_root, from_seq, to_seq, anchored_at) -> dict:
        lines = self._lines()
        prev = lines[-1]["line_hash"] if lines else GENESIS
        rec = {"anchor_id": anchor_id, "merkle_root": merkle_root, "from_seq": from_seq, "to_seq": to_seq,
               "anchored_at": anchored_at, "prev_line_hash": prev}
        rec["line_hash"] = sha256_hex(prev + canonical(rec))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, sort_keys=True) + "\n")
        return {"file": self.path.name, "line": len(lines) + 1, "line_hash": rec["line_hash"], "label": self.label}

    def verify(self, anchor_id, merkle_root, reference) -> VerifyResult:
        prev = GENESIS
        found = None
        for i, rec in enumerate(self._lines(), start=1):
            body = {k: v for k, v in rec.items() if k != "line_hash"}
            if rec.get("prev_line_hash") != prev or sha256_hex(prev + canonical(body)) != rec.get("line_hash"):
                return VerifyResult(False, f"anchor file chain broken at line {i}")
            prev = rec["line_hash"]
            if rec["anchor_id"] == anchor_id:
                found = rec
        if found is None:
            return VerifyResult(False, "anchor missing from the local anchor file")
        if found["merkle_root"] != merkle_root:
            return VerifyResult(False, "root in the anchor file differs from the database")
        return VerifyResult(True, "matches the local anchor file (hash-chained, kept outside the database)")

    def reset(self) -> None:
        if self.path.exists():
            self.path.unlink()
