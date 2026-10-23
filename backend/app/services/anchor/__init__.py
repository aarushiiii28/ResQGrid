from pathlib import Path

from app.services.anchor.base import AnchorProvider, VerifyResult
from app.services.anchor.fabric_stub import FabricAnchorProvider
from app.services.anchor.local import LocalAnchorProvider

__all__ = ["AnchorProvider", "VerifyResult", "LocalAnchorProvider", "FabricAnchorProvider", "build_anchor_provider"]


def build_anchor_provider(settings, runtime_dir: Path) -> AnchorProvider:
    kind = settings.ledger_anchor_provider
    if kind == "evm":
        from app.services.anchor.evm import EVMAnchorProvider

        return EVMAnchorProvider(settings.evm_rpc_url, settings.evm_contract, settings.evm_private_key)
    if kind == "fabric":
        return FabricAnchorProvider()
    return LocalAnchorProvider(runtime_dir / "local_anchors.jsonl")
