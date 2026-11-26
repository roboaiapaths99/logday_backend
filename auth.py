from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
import os
from dotenv import load_dotenv

load_dotenv()

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

ph = PasswordHasher()

import logging
import hashlib

logger = logging.getLogger(__name__)

# Reject unsafe production configuration rather than silently deriving a key.
import secrets
from bson import ObjectId

_raw_secret = os.getenv("JWT_SECRET") or os.getenv("SECRET_KEY") or ""
_weak_secret = len(_raw_secret) < 32 or _raw_secret in {"supersecretkey", "CHANGE_ME_USE_A_LONG_RANDOM_STRING"}
if _weak_secret and os.getenv("APP_ENV", "development").lower() == "production":
    raise RuntimeError("JWT_SECRET must be an independently generated secret of at least 32 characters")
SECRET_KEY = secrets.token_hex(32) if _weak_secret else _raw_secret
ALGORITHM = "HS256"
TOKEN_ISSUER = os.getenv("JWT_ISSUER", "logday-api")
TOKEN_AUDIENCE = "logday-api-v2"
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "60").split("#")[0].strip())
if not 1 <= ACCESS_TOKEN_EXPIRE_MINUTES <= 1440:
    raise RuntimeError("ACCESS_TOKEN_EXPIRE_MINUTES must be between 1 and 1440")

import time
from collections import defaultdict
from fastapi import Request, HTTPException
from fastapi.concurrency import run_in_threadpool

def verify_password(plain_password, hashed_password):
    try:
        return ph.verify(hashed_password, plain_password)
    except VerifyMismatchError:
        return False
    except Exception:
        # Fallback for old pbkdf2 hashes during transition if needed
        # But for now, we'll assume Argon2 or fail
        return False

def get_password_hash(password):
    return ph.hash(password)

async def verify_password_async(plain_password, hashed_password):
    """Non-blocking Argon2 verification executed in a worker thread."""
    return await run_in_threadpool(verify_password, plain_password, hashed_password)

async def get_password_hash_async(password):
    """Non-blocking Argon2 hashing executed in a worker thread."""
    return await run_in_threadpool(get_password_hash, password)

# ─── In-Memory Sliding-Window Brute-Force Login Rate Limiter ─────────────────
_FAILED_LOGINS: dict[tuple[str, str], list[float]] = defaultdict(list)
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes
_MAX_FAILED_ATTEMPTS = 15

def _get_client_ip(request: Request) -> str:
    if not request:
        return "unknown"
    if request.client and request.client.host:
        return request.client.host
    return "unknown"

def check_login_rate_limit(request: Request, email: str):
    """Raise HTTP 429 if this (ip, email) exceeded 15 failed login attempts in the last 5 minutes."""
    ip = _get_client_ip(request)
    key = (ip, (email or "").strip().lower())
    now_ts = time.time()
    attempts = [t for t in _FAILED_LOGINS.get(key, []) if now_ts - t < _LOGIN_WINDOW_SECONDS]
    if attempts:
        _FAILED_LOGINS[key] = attempts
    elif key in _FAILED_LOGINS:
        del _FAILED_LOGINS[key]
    if len(attempts) >= _MAX_FAILED_ATTEMPTS:
        logger.warning(f"[SECURITY] Brute-force rate limit triggered for email='{key[1]}' from IP={ip}")
        raise HTTPException(
            status_code=429,
            detail="Too many failed login attempts. Please wait 5 minutes before trying again."
        )

def record_failed_login(request: Request, email: str):
    ip = _get_client_ip(request)
    key = (ip, (email or "").strip().lower())
    now_ts = time.time()
    attempts = [t for t in _FAILED_LOGINS.get(key, []) if now_ts - t < _LOGIN_WINDOW_SECONDS]
    attempts.append(now_ts)
    _FAILED_LOGINS[key] = attempts

def clear_failed_logins(request: Request, email: str):
    ip = _get_client_ip(request)
    key = (ip, (email or "").strip().lower())
    _FAILED_LOGINS.pop(key, None)

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from database import admins_collection, employees_collection, auth_sessions_collection
from models import Admin

admin_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="admin/login")
employee_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


def _unauthorized():
    return HTTPException(401, "Invalid or expired session. Please sign in again.", headers={"WWW-Authenticate": "Bearer"})


def ensure_active_account(account):
    if account.get("is_active") is False or str(account.get("status", "")).lower() in {"inactive", "disabled", "deleted"}:
        raise HTTPException(403, "Your account has been deactivated.")
    if not account.get("organization_id"):
        raise HTTPException(403, "Account has no organization. Contact your administrator.")


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    """Low-level signing; callers must supply a persisted session and immutable identity."""
    if data.get("account_type") not in {"employee", "admin"} or not ObjectId.is_valid(data.get("sub", "")) or not data.get("sid"):
        raise ValueError("Account ID, account type, and session ID are required")
    now = datetime.now(timezone.utc)
    claims = dict(data)
    claims.update(iss=TOKEN_ISSUER, aud=TOKEN_AUDIENCE, iat=now,
                  exp=now + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)))
    return jwt.encode(claims, SECRET_KEY, algorithm=ALGORITHM)


async def issue_access_token(account: dict, account_type: str):
    ensure_active_account(account)
    if account_type not in {"employee", "admin"}:
        raise ValueError("Invalid account type")
    sid = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    await auth_sessions_collection.insert_one({
        "_id": sid, "account_id": str(account["_id"]), "account_type": account_type,
        "organization_id": account["organization_id"], "expires_at": expires_at,
        "revoked": False,
    })
    return create_access_token({"sub": str(account["_id"]), "account_type": account_type,
                                "sid": sid, "ver": account.get("auth_version", 0)})


def decode_access_token(token: str):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], audience=TOKEN_AUDIENCE,
                             issuer=TOKEN_ISSUER, options={"require_exp": True, "require_iat": True,
                                                         "require_sub": True, "require_aud": True, "require_iss": True})
        if payload.get("account_type") not in {"employee", "admin"} or not ObjectId.is_valid(payload.get("sub", "")):
            raise _unauthorized()
        if not isinstance(payload.get("sid"), str) or not payload["sid"] or not isinstance(payload.get("ver"), int):
            raise _unauthorized()
        return payload
    except (JWTError, ValueError, TypeError):
        raise _unauthorized()


async def resolve_access_token(token: str, expected_type: Optional[str] = None):
    payload = decode_access_token(token)
    return await resolve_session_claims(payload, expected_type)


async def resolve_session_claims(payload, expected_type=None):
    """Resolve already verified access-token or file-grant claims against live state."""
    if payload.get("account_type") not in {"admin", "employee"} or not ObjectId.is_valid(payload.get("sub", "")):
        raise _unauthorized()
    if not isinstance(payload.get("sid"), str) or not isinstance(payload.get("ver"), int):
        raise _unauthorized()
    kind = payload["account_type"]
    if expected_type and kind != expected_type:
        raise _unauthorized()
    session = await auth_sessions_collection.find_one({
        "_id": payload["sid"], "account_id": payload["sub"], "account_type": kind,
        "revoked": False, "expires_at": {"$gt": datetime.now(timezone.utc)},
    })
    if not session:
        raise _unauthorized()
    collection = admins_collection if kind == "admin" else employees_collection
    account = await collection.find_one({"_id": ObjectId(payload["sub"])})
    if not account or payload["ver"] != account.get("auth_version", 0):
        raise _unauthorized()
    ensure_active_account(account)
    if session.get("organization_id") != account.get("organization_id"):
        raise _unauthorized()
    return account, kind


def _has_explicit_admin_policy(dependant):
    if getattr(getattr(dependant, "call", None), "admin_policy", False):
        return True
    return any(_has_explicit_admin_policy(child) for child in getattr(dependant, "dependencies", []))


async def get_current_admin(request: Request, token: str = Depends(admin_oauth2_scheme)):
    account, _ = await resolve_access_token(token, "admin")
    # New routes fail closed until they declare a permission dependency.
    route = request.scope.get("route")
    if not _has_explicit_admin_policy(getattr(route, "dependant", None)):
        raise HTTPException(403, "This route has no admin access policy")
    if account.get("organization_id") == "system_org" and account.get("role") != "superadmin":
        raise HTTPException(403, "System organization requires a superadmin account")
    account.setdefault("allowed_features", [])
    return Admin(**account)


async def get_current_employee(token: str = Depends(employee_oauth2_scheme)):
    account, _ = await resolve_access_token(token, "employee")
    return account
