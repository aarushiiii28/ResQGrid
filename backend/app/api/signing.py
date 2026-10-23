"""Signed actions (M19). A route that matters (SOS, acknowledgments, approvals, accept/reject, arrival, handover)
declares which action it is. If the client sent X-Action-Signature, it is verified here (who, what, which body, when,
nonce, counter) and then attached to the first matching ledger entry, so "I never received it" is disproved by a record
anyone can verify. Unsigned calls still work unless REQUIRE_SIGNATURES is on."""

from fastapi import Depends, Request

from app.api.deps import get_ctx
from app.core.security import Principal, get_principal
from app.services.context import Ctx
from app.services.ledger_service import CURRENT_SIGNED, SignedAction


def signed(action_template: str, *event_types: str):
    async def dep(request: Request, p: Principal = Depends(get_principal), ctx: Ctx = Depends(get_ctx)) -> SignedAction | None:
        action = action_template.format(**request.path_params)
        sa = await ctx.ledger.verify_action(await request.body(), request.headers.get("x-action-signature"), p,
                                            action=action, event_types=event_types)
        if sa is not None:
            CURRENT_SIGNED.set(sa)
        return sa

    return dep
