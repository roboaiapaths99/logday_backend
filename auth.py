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

# JWT configuration
_raw_secret = os.getenv("JWT_SECRET") or os.getenv("SECRET_KEY") or ""
if not _raw_secret or _raw_secret in ("supersecretkey", "CHANGE_ME_USE_A_LONG_RANDOM_STRING"):
    if os.getenv("APP_ENV", "development") == "production":
        logger.critical("SECURITY WARNING: JWT_SECRET is not set or uses a weak default in production!")
    # Derive a machine/db-scoped fallback key rather than a static literal if env var was omitted
    _seed = f"logday-jwt-{os.getenv('MONGODB_URL', 'local')}-{os.getenv('ADMIN_EMAIL', 'admin')}"
    SECRET_KEY = _raw_secret if _raw_secret else hashlib.sha256(_seed.encode("utf-8")).hexdigest()
else:
    SECRET_KEY = _raw_secret
ALGORITHM = os.getenv("ALGORITHM", "HS256")
# Handle trailing comments in Docker --env-file
expire_minutes_str = str(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "43200")).split('#')[0].strip()
ACCESS_TOKEN_EXPIRE_MINUTES = int(expire_minutes_str)

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
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
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

def create_access_token(data: dict, expires_delta: Optional[timedelta] = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.now(timezone.utc) + expires_delta
    else:
        expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

from fastapi import Depends, HTTPException, status, Request
from fastapi.security import OAuth2PasswordBearer
from database import admins_collection, employees_collection
from models import Admin

admin_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="admin/login")
employee_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

async def get_current_admin(request: Request, token: str = Depends(admin_oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub or not isinstance(sub, str):
            raise credentials_exception
        email: str = sub.lower().strip()
    except JWTError:
        raise credentials_exception
        
    admin = await admins_collection.find_one({"email": email})
    if admin is None:
        # Check hardcoded superadmin fallback from .env
        fallback_email = os.getenv("ADMIN_EMAIL", "admin@officeflow.ai")
        if email == fallback_email:
             return Admin(
                  email=email, 
                  role="superadmin", 
                  full_name="System Super Admin",
                  organization_id="system_org",
                  allowed_features=["dashboard", "employees", "attendance", "leaves", "expenses", "reports", "war_room", "territory", "nudge", "leaderboard", "settings", "sub_admins", "onboarding", "exit_management", "payroll", "document_verification", "wfh_management", "wfh_monitoring"]
             )
        raise credentials_exception
    
    # Ensure role exists, default to 'hr' if missing for some reason
    if "role" not in admin:
        admin["role"] = "hr"
    
    # Ensure allowed_features exists
    if "allowed_features" not in admin:
        admin["allowed_features"] = ["dashboard"]

    # Perform automated permission checks based on URL path
    role_val = admin["role"]
    if role_val not in ["superadmin", "owner", "admin"]:
        allowed = admin.get("allowed_features") or []
        path = request.url.path
        
        # 1. Attendance & Dashboard
        if path.startswith("/admin/stats") or path.startswith("/admin/me") or path.startswith("/admin/system/diagnostics"):
            if "dashboard" not in allowed and "settings" not in allowed:
                raise HTTPException(403, "Access denied for 'dashboard'")
                
        elif path.startswith("/admin/logs") or path.startswith("/admin/attendance") or path.startswith("/admin/live-feed") or path.startswith("/admin/export-logs"):
            if "attendance" not in allowed:
                raise HTTPException(403, "Access denied for 'attendance'")
                
        # 2. Employees & Org Structure
        elif path.startswith("/admin/employees") or path.startswith("/admin/create-employee") or path.startswith("/admin/org"):
            if "employees" not in allowed:
                raise HTTPException(403, "Access denied for 'employees'")
                
        # 3. Leaves
        elif path.startswith("/admin/leave"):
            if "leaves" not in allowed:
                raise HTTPException(403, "Access denied for 'leaves'")
                
        # 4. Expenses
        elif path.startswith("/admin/expenses"):
            if "expenses" not in allowed:
                raise HTTPException(403, "Access denied for 'expenses'")
                
        # 5. Field Force (war_room, territory, nudge, leaderboard)
        elif path.startswith("/admin/field/trail") or path.startswith("/admin/field/live-status") or path.startswith("/admin/field/heatmap-data"):
            if "war_room" not in allowed:
                raise HTTPException(403, "Access denied for 'war_room'")
                
        elif path.startswith("/admin/field/reimbursements") or path.startswith("/admin/field/visit-plans") or path.startswith("/admin/field/visit"):
            if "war_room" not in allowed:
                raise HTTPException(403, "Access denied for 'war_room'")
                
        elif "/territory" in path:
            if "territory" not in allowed:
                raise HTTPException(403, "Access denied for 'territory'")
                
        elif path.startswith("/admin/nudge"):
            if "nudge" not in allowed:
                raise HTTPException(403, "Access denied for 'nudge'")
                
        elif path.startswith("/admin/leaderboard"):
            if "leaderboard" not in allowed:
                raise HTTPException(403, "Access denied for 'leaderboard'")

        elif path.startswith("/admin/flyfield"):
            if "flyfield" not in allowed and "war_room" not in allowed:
                raise HTTPException(403, "Access denied for 'flyfield'")
                
        # 6. HRMS (onboarding, exit_management, payroll, document_verification)
        elif path.startswith("/admin/onboarding") or path.startswith("/hrms/onboarding"):
            if "/documents" in path or "/verify" in path:
                if "document_verification" not in allowed:
                    raise HTTPException(403, "Access denied for 'document_verification'")
            else:
                if "onboarding" not in allowed:
                    raise HTTPException(403, "Access denied for 'onboarding'")
                    
        elif path.startswith("/admin/exit-management") or path.startswith("/hrms/exit"):
            if "exit_management" not in allowed:
                raise HTTPException(403, "Access denied for 'exit_management'")
                
        elif path.startswith("/admin/payroll"):
            if "payroll" not in allowed:
                raise HTTPException(403, "Access denied for 'payroll'")
                
        # 7. WFH (wfh_management, wfh_monitoring)
        elif path.startswith("/admin/wfh/request-stats") or path.startswith("/admin/wfh-requests"):
            if "wfh_management" not in allowed:
                raise HTTPException(403, "Access denied for 'wfh_management'")

        elif path.startswith("/admin/wfh/stats"):
            if "wfh_management" not in allowed and "wfh_monitoring" not in allowed:
                raise HTTPException(403, "Access denied. Requires 'wfh_management' or 'wfh_monitoring' permission.")
                
        elif path.startswith("/admin/wfh/requests") or path.startswith("/admin/wfh/calendar") or path.startswith("/admin/wfh/policies") or path.startswith("/admin/wfh/policy"):
            if "wfh_management" not in allowed:
                raise HTTPException(403, "Access denied for 'wfh_management'")
                
        elif path.startswith("/admin/wfh"):
            if "wfh_monitoring" not in allowed:
                raise HTTPException(403, "Access denied for 'wfh_monitoring'")
                
        # 8. Settings & Admin
        elif path.startswith("/admin/settings") or path.startswith("/admin/upload-logo") or path.startswith("/admin/absence-alerts"):
            if "settings" not in allowed and "attendance" not in allowed:
                raise HTTPException(403, "Access denied for 'settings'")
                
        elif path.startswith("/admin/sub-admins"):
            if "sub_admins" not in allowed:
                raise HTTPException(403, "Access denied for 'sub_admins'")
                
    return Admin(**admin)


async def get_current_employee(token: str = Depends(employee_oauth2_scheme)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        sub = payload.get("sub")
        if not sub or not isinstance(sub, str):
            raise credentials_exception
        email: str = sub.lower().strip()
    except JWTError:
        raise credentials_exception
        
    employee = await employees_collection.find_one({"email": email})
    if employee is None:
        raise credentials_exception
        
    if employee.get("status") == "Inactive" or employee.get("is_active") is False:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Your account has been deactivated."
        )
        
    return employee
