from dataclasses import asdict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.security import create_token, demo_users, find_user

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    user_id: str | None = None
    role: str | None = None


@router.get("/users")
async def users() -> list[dict]:
    """Synthetic demo users for the role switcher."""
    return [asdict(u) for u in demo_users()]


@router.post("/demo-login")
async def demo_login(body: LoginBody) -> dict:
    if not body.user_id and not body.role:
        raise HTTPException(422, "user_id or role required")
    user = find_user(body.user_id, body.role)
    if user is None:
        raise HTTPException(404, "unknown synthetic user")
    return {"token": create_token(user), "user": asdict(user), "mode": "DEMO / SIMULATED"}
