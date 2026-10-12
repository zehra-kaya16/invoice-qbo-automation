"""Database-backed browser authentication and per-request company scope."""
import hashlib
import hmac
import secrets
from contextvars import ContextVar
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, Header
from sqlalchemy.exc import SQLAlchemyError

COOKIE_NAME = "receipt_ai_session"
current_principal = ContextVar("receipt_ai_principal", default=None)


@dataclass(frozen=True)
class Principal:
    user_id: str
    company_id: str
    company_key: str
    company_name: str
    email: str
    name: str
    role: str
    session_hash: str
    csrf_token: str


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def hash_password(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValueError("Password must contain 12–128 characters")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600000)
    return f"pbkdf2_sha256$600000${salt}${digest.hex()}"


def verify_password(password, stored):
    try:
        algorithm, count, salt, expected = stored.split("$")
        if algorithm != "pbkdf2_sha256" or int(count) != 600000:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(count))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, AttributeError, TypeError):
        return False


def check_origin(request):
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(403, "Cross-origin authentication is not allowed")


async def get_current_user(request: Request, csrf_token: str | None = Header(None, alias="X-CSRF-Token")):
    from app.services.persistence.auth_repository import AuthRepository
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie or len(cookie) > 256:
        raise HTTPException(401, "Sign in is required")
    try:
        principal = AuthRepository().principal(token_hash(cookie))
    except SQLAlchemyError:
        raise HTTPException(503, "Authentication storage is unavailable") from None
    if principal is None:
        raise HTTPException(401, "Session expired or user is inactive; sign in again")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        if not hmac.compare_digest((csrf_token or "").encode(), principal.csrf_token.encode()):
            raise HTTPException(403, "Invalid CSRF token; reload the signed-in page")
    return principal


async def company_access(user: Principal = Depends(get_current_user)):
    token = current_principal.set(user)
    try:
        yield user
    finally:
        current_principal.reset(token)


async def require_admin(user: Principal = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(403, "Company administrator permission is required")
    return user


def company_key(requested=None):
    user = current_principal.get()
    if user is None:
        return requested or "default"  # Trusted local CLI/repository calls only.
    if requested is not None and requested != user.company_key:
        raise HTTPException(403, "Another company's scope cannot be requested")
    return user.company_key
