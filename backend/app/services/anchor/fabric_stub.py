from app.services.anchor.base import AnchorProvider, VerifyResult


class FabricAnchorProvider(AnchorProvider):
    """Interface stub for a Hyperledger Fabric network. Not implemented and not required to run: docs/FABRIC.md describes
    the chaincode (one `AnchorRoot` and one `GetAnchor` function) and the network a real deployment would need."""

    name = "fabric"
    label = "Hyperledger Fabric (stub, not implemented)"

    def anchor(self, anchor_id, merkle_root, from_seq, to_seq, anchored_at) -> dict:
        raise NotImplementedError("Fabric anchoring is a documented stub: see docs/FABRIC.md")

    def verify(self, anchor_id, merkle_root, reference) -> VerifyResult:
        return VerifyResult(False, "Fabric provider is a stub")
