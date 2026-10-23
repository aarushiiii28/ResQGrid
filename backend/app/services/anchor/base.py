"""Anchor providers publish a Merkle root somewhere that the application cannot quietly rewrite. A provider only
needs to store a root and later say whether it still holds the same root."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class VerifyResult:
    ok: bool
    detail: str


class AnchorProvider(ABC):
    name: str = "base"
    label: str = "anchor"  # shown to people; must not overstate what the provider is

    @abstractmethod
    def anchor(self, anchor_id: str, merkle_root: str, from_seq: int, to_seq: int, anchored_at: str) -> dict:
        """Publish the root. Returns a JSON-able reference stored with the anchor record."""

    @abstractmethod
    def verify(self, anchor_id: str, merkle_root: str, reference: dict) -> VerifyResult:
        """Does the provider still hold this exact root?"""

    def reset(self) -> None:  # noqa: B027  (optional hook, intentionally a no-op by default)
        """Forget everything (demo reset). Real providers cannot and must not do this."""
