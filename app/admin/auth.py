"""Authentication helpers for Plane Alerts delegated administration."""
from __future__ import annotations

import base64
import hashlib
import hmac
from typing import Any

from fastapi import HTTPException, Request, status
from starlette.datastructures import MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.config import settings
from app.database import users_col

_TOKEN_PREFIX = "plane-admin-v1"


def make_delegated_admin_token(user_id: int) -> str:
    """Create a deterministic signed token for a delegated admin user."""
    secret = settings.admin_password.strip()
    if not secret:
        raise RuntimeError("ADMIN_PASSWORD must be configured before delegating admin access")
    payload = str(int(user_id))
    digest = hmac.new(
        secret.encode("utf-8"),
        f"{_TOKEN_PREFIX}:{payload}".encode("utf-8"),
        hashlib.sha256,
    ).digest()
    signature = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return f"{payload}.{signature}"


def verify_delegated_admin_token(token: str) -> int | None:
    """Validate token signature and return its user id when valid."""
    secret = settings.admin_password.strip()
    if not secret or not token or "." not in token:
        return None
    payload, supplied = token.split(".", 1)
    try:
        user_id = int(payload)
    except (TypeError, ValueError):
        return None
    expected = make_delegated_admin_token(user_id).split(".", 1)[1]
    if not hmac.compare_digest(expected, supplied):
        return None
    return user_id


async def delegated_admin_user(token: str) -> int | None:
    """Validate a signed token and confirm that access has not been revoked."""
    user_id = verify_delegated_admin_token(token)
    if user_id is None:
        return None
    doc = await users_col().find_one({"user_id": user_id}, {"is_admin": 1})
    if not doc or not bool(doc.get("is_admin")):
        return None
    return user_id


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )


async def get_admin_actor(request: Request) -> dict[str, Any]:
    """Return root/delegated actor metadata for administration endpoints."""
    delegated_id = getattr(request.state, "delegated_admin_user_id", None)
    if delegated_id is not None:
        return {"root": False, "kind": "delegated", "user_id": int(delegated_id)}

    configured = settings.admin_password.strip()
    if not configured:
        # Fail closed. Self-hosted deployments may expose the application port;
        # an empty password must never turn that into anonymous root access.
        raise _unauthorized()

    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("basic "):
        try:
            decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
            _, password = decoded.split(":", 1)
        except Exception:
            password = ""
        if hmac.compare_digest(password, configured):
            return {"root": True, "kind": "root", "user_id": None}

    raise _unauthorized()


def require_root(actor: dict[str, Any]) -> None:
    if not actor.get("root"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Root admin access required")


class DelegatedAdminMiddleware(BaseHTTPMiddleware):
    """Translate valid bearer admin links into the dashboard's existing Basic auth.

    Existing /admin endpoints remain unchanged. A delegated admin presents the
    signed bearer token from the administration link; after revocation the users
    collection check fails immediately.
    """

    async def dispatch(self, request: Request, call_next):  # type: ignore[override]
        if request.url.path.startswith("/admin"):
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer "):
                token = auth.split(" ", 1)[1].strip()
                try:
                    user_id = await delegated_admin_user(token)
                except Exception:
                    user_id = None
                if user_id is None:
                    return JSONResponse({"detail": "Unauthorized"}, status_code=401)

                request.state.delegated_admin_user_id = user_id
                basic_value = base64.b64encode(
                    f"delegated:{settings.admin_password}".encode("utf-8")
                ).decode("ascii")
                MutableHeaders(scope=request.scope)["authorization"] = f"Basic {basic_value}"

        return await call_next(request)
