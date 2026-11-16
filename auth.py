from datetime import datetime, timedelta, timezone
from typing import Optional
from jose import JWTError, jwt
import os
from dotenv import load_dotenv

load_dotenv()

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

ph = PasswordHasher()

# JWT configuration
SECRET_KEY = os.getenv("JWT_SECRET", "supersecretkey")
ALGORITHM = os.getenv("ALGORITHM", "HS256")
# Handle trailing comments in Docker --env-file
expire_minutes_str = str(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "43200")).split('#')[0].strip()
ACCESS_TOKEN_EXPIRE_MINUTES = int(expire_minutes_str)

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
        email: str = payload.get("sub").lower().strip()
        if email is None:
            raise credentials_exception
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
        if path.startswith("/admin/stats") or path.startswith("/admin/me"):
            if "dashboard" not in allowed:
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
        elif path.startswith("/admin/settings") or path.startswith("/admin/upload-logo"):
            if "settings" not in allowed:
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
        email: str = payload.get("sub").lower().strip()
        if email is None:
            raise credentials_exception
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
