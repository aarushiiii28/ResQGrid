"""Demo auth: synthetic users per role, JWT, least privilege."""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

import jwt
from fastapi import Depends, HTTPException, Request, status

from app.core.config import get_settings

ROLES = ("caller", "dispatcher", "driver", "hospital", "admin")


@dataclass(frozen=True)
class Principal:
    sub: str
    role: str
    name: str
    vehicle_id: str | None = None
    hospital_id: str | None = None


def demo_users() -> list[Principal]:
    users = [
        Principal("caller-1", "caller", "Caller 1 (synthetic)"),
        Principal("caller-2", "caller", "Caller 2 (synthetic)"),
        Principal("disp-1", "dispatcher", "Dispatcher 1 (synthetic)"),
        Principal("admin-1", "admin", "Admin (synthetic)"),
    ]
    users += [
        Principal(f"driver-AMB-{i:02d}", "driver", f"Driver AMB-{i:02d}", vehicle_id=f"AMB-{i:02d}")
        for i in range(1, 21)
    ]
    users += [
        Principal(f"hosp-HOSP-{i:02d}", "hospital", f"HOSP-{i:02d} desk", hospital_id=f"HOSP-{i:02d}")
        for i in range(1, 13)
    ]
    return users


def find_user(user_id: str | None = None, role: str | None = None) -> Principal | None:
    for u in demo_users():
        if (user_id and u.sub == user_id) or (not user_id and role and u.role == role):
            return u
    return None


def create_token(p: Principal) -> str:
    s = get_settings()
    claims = asdict(p) | {"exp": datetime.now(UTC) + timedelta(seconds=s.jwt_ttl_s)}
    return jwt.encode(claims, s.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> Principal:
    try:
        c = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
        return Principal(
            c["sub"], c["role"], c["name"], c.get("vehicle_id"), c.get("hospital_id")
        )
    except (jwt.PyJWTError, KeyError) as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token") from e


def get_principal(request: Request) -> Principal:
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    return decode_token(h[7:])


def require(*roles: str):
    def dep(p: Principal = Depends(get_principal)) -> Principal:
        if p.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"role '{p.role}' not permitted")
        return p

    return dep
