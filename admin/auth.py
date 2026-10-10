"""Admin JWT auth: login, cookie + Bearer, role gates."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any
from uuid import UUID

import bcrypt
import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, EmailStr, Field

from admin.db import get_admin_pool
from api.settings import get_api_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/auth", tags=["admin-auth"])

COOKIE_NAME = "reflexo_admin"
ROLE_RANK = {"support": 1, "admin": 2, "super_admin": 3}


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class AdminUser(BaseModel):
    id: str
    email: str
    name: str
    role: str
    is_active: bool = True


class LoginResponse(BaseModel):
    access_token: str
    admin: AdminUser


def _secret() -> str:
    secret = (get_api_settings().admin_jwt_secret or "").strip()
    if not secret or secret == "change-this-to-a-random-64-char-string":
        env = (get_api_settings().environment or "development").lower()
        if env in ("production", "prod"):
            raise HTTPException(status_code=503, detail="ADMIN_JWT_SECRET is not configured")
        logger.warning("ADMIN_JWT_SECRET is weak or unset; using a process-local fallback")
        return "dev-only-reflexo-admin-jwt-secret-do-not-use-in-prod"
    return secret


def _expiry_hours() -> int:
    return max(1, int(get_api_settings().admin_jwt_expiry_hours or 8))


def create_token(admin: AdminUser) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": admin.id,
        "email": admin.email,
        "role": admin.role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=_expiry_hours())).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm="HS256")


def decode_token(token: str) -> dict[str, Any]:
    try:
        return jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise HTTPException(status_code=401, detail="Token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc


def _extract_token(request: Request) -> str | None:
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    cookie = request.cookies.get(COOKIE_NAME)
    return cookie.strip() if cookie else None


def _verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return secrets.compare_digest(plain, hashed)


def _cookie_kwargs() -> dict[str, Any]:
    settings = get_api_settings()
    secure = (settings.environment or "").lower() in ("production", "prod")
    return {
        "key": COOKIE_NAME,
        "httponly": True,
        "samesite": "lax",
        "secure": secure,
        "path": "/",
        "max_age": _expiry_hours() * 3600,
    }


async def get_current_admin(request: Request) -> AdminUser:
    token = _extract_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(token)
    admin_id = str(payload.get("sub") or "")
    if not admin_id:
        raise HTTPException(status_code=401, detail="Invalid token")
    pool = await get_admin_pool()
    row = await pool.fetchrow(
        """
        SELECT id, email, name, role, is_active
        FROM admin_users
        WHERE id = $1::uuid
        """,
        admin_id,
    )
    if not row:
        raise HTTPException(status_code=401, detail="Account not found")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="Account disabled")
    return AdminUser(
        id=str(row["id"]),
        email=row["email"],
        name=row["name"],
        role=row["role"],
        is_active=bool(row["is_active"]),
    )


def require_role(*roles: str):
    allowed = set(roles)

    async def _dep(admin: Annotated[AdminUser, Depends(get_current_admin)]) -> AdminUser:
        if admin.role not in allowed:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return admin

    return _dep


def require_min_role(min_role: str):
    needed = ROLE_RANK.get(min_role, 99)

    async def _dep(admin: Annotated[AdminUser, Depends(get_current_admin)]) -> AdminUser:
        if ROLE_RANK.get(admin.role, 0) < needed:
            raise HTTPException(status_code=403, detail="Insufficient permissions")
        return admin

    return _dep


CurrentAdmin = Annotated[AdminUser, Depends(get_current_admin)]
AdminPlus = Annotated[AdminUser, Depends(require_min_role("admin"))]
SuperAdmin = Annotated[AdminUser, Depends(require_role("super_admin"))]


@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, response: Response) -> LoginResponse:
    pool = await get_admin_pool()
    row = await pool.fetchrow(
        """
        SELECT id, email, password_hash, name, role, is_active
        FROM admin_users
        WHERE lower(email) = lower($1)
        """,
        body.email,
    )
    if not row or not _verify_password(body.password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    if not row["is_active"]:
        raise HTTPException(status_code=403, detail="Account disabled")

    admin = AdminUser(
        id=str(row["id"]),
        email=row["email"],
        name=row["name"],
        role=row["role"],
        is_active=True,
    )
    token = create_token(admin)
    await pool.execute(
        "UPDATE admin_users SET last_login_at = now() WHERE id = $1::uuid",
        admin.id,
    )
    response.set_cookie(value=token, **_cookie_kwargs())
    return LoginResponse(access_token=token, admin=admin)


@router.post("/logout")
async def logout(response: Response) -> dict[str, str]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"status": "ok"}


@router.get("/me", response_model=AdminUser)
async def me(admin: CurrentAdmin) -> AdminUser:
    return admin
