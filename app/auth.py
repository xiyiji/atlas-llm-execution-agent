"""API-key authentication and tenant resolution."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass

from fastapi import Cookie, Header, HTTPException, status

from . import config


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str


def _configured_keys() -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in config.API_KEYS.split(","):
        if ":" not in item:
            continue
        tenant, key = item.split(":", 1)
        if tenant.strip() and key.strip():
            pairs.append((tenant.strip(), key.strip()))
    return pairs


def _same_secret(left: str, right: str) -> bool:
    left_hash = hashlib.sha256(left.encode()).digest()
    right_hash = hashlib.sha256(right.encode()).digest()
    return hmac.compare_digest(left_hash, right_hash)


async def tenant_context(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    atlas_session: str | None = Cookie(default=None),
) -> TenantContext:
    if not config.AUTH_REQUIRED:
        return TenantContext("default")
    session_tenant = verify_session(atlas_session or "")
    if session_tenant:
        return TenantContext(session_tenant)
    supplied = x_api_key or ""
    for tenant_id, expected in _configured_keys():
        if _same_secret(supplied, expected):
            return TenantContext(tenant_id)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="A valid API key is required",
        headers={"WWW-Authenticate": "ApiKey"},
    )


def issue_session(tenant_id: str) -> str:
    expires = int(time.time()) + config.SESSION_TTL_SECONDS
    payload = f"{tenant_id}:{expires}"
    signature = hmac.new(config.SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{payload}:{signature}".encode()).decode()


def verify_session(token: str) -> str | None:
    if not token:
        return None
    try:
        decoded = base64.urlsafe_b64decode(token.encode()).decode()
        tenant_id, expires_raw, signature = decoded.rsplit(":", 2)
        payload = f"{tenant_id}:{expires_raw}"
        expected = hmac.new(config.SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected) or int(expires_raw) < int(time.time()):
            return None
        return tenant_id
    except (ValueError, UnicodeDecodeError):
        return None
