from fastapi import APIRouter, FastAPI, HTTPException, File, Depends, BackgroundTasks, Request, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse, Response
from starlette import status
import asyncio
from datetime import datetime, timezone, timedelta
import os
import calendar
import csv
import math
import re
import io
import time
import random
import traceback
import zipfile
import tempfile
import urllib.request
from urllib.parse import unquote
try:
    import pandas as pd
except ImportError:
    pd = None
from bson import ObjectId
try:
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet
except ImportError:
    letter = landscape = SimpleDocTemplate = Table = TableStyle = Paragraph = Spacer = colors = getSampleStyleSheet = None
from dotenv import load_dotenv
from typing import Optional, List, Dict, Any
import logging
import base64
import uuid
import requests
import httpx
try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None
try:
    import boto3
    from botocore.exceptions import NoCredentialsError
except ImportError:
    boto3 = None
    NoCredentialsError = Exception

from jose import jwt
from database import (
    employees_collection, attendance_logs_collection, settings_collection, admins_collection, 
    organizations_collection, visit_plans_collection, visit_logs_collection, 
    location_pings_collection, km_reimbursements_collection, expense_claims_collection, otps_collection,
    alerts_collection, leave_requests_collection,
    visit_plan_templates_collection, nudge_logs_collection,
    wfh_sessions_collection,
    wfh_screenshots_collection,
    wfh_activity_collection,
    wfh_app_usage_collection,
    wfh_productivity_collection,
    wfh_alerts_collection,
    wfh_meetings_collection,
    wfh_device_info_collection,
    wfh_signals_collection,
    wfh_commands_collection,
    wfh_face_checks_collection,
    wfh_requests_collection,
    ensure_employee_indexes,
    onboardings_collection,
    payrolls_collection,
    exit_managements_collection,
    salary_structures_collection,
    holidays_collection,
    leave_policies_collection,
    tasks_collection
)
from models import (
    RegisterRequest, LoginRequest, VerifyPresenceRequest, Token, LoginResponse, EmployeeProfile, UpdateFaceRequest,
    AdminLoginRequest, EmployeeUpdate, SystemSettings, Admin, AdminRole, Organization, OrganizationRegisterRequest, SubAdminCreate,
    EmployeeType, TerritoryType, AttendanceType, CheckInMethod, PlanStatus, VisitPlan, Visit, LocationPing, ExpenseClaim,
    LeaveType, LeaveStatus, DiscussionMessage, LeaveRequest, SyncBatchRequest, ChangePasswordRequest, SalaryStructure,
    HolidayCreateRequest, HolidayBulkCreateRequest, LeavePolicyUpdateRequest, EmployeeLeaveQuotaUpdateRequest, ConvertAbsentToLeaveRequest
)
from auth import (
    get_password_hash, verify_password, create_access_token, 
    get_current_admin, get_current_employee, admin_oauth2_scheme, employee_oauth2_scheme,
    SECRET_KEY, ALGORITHM
)
from face_utils import get_face_embedding, verify_face, compare_faces, is_dummy_embedding
from sheets_sync import sync_to_google_sheets, sync_visit_to_google_sheets
from attendance_service import classify_attendance_day

logger = logging.getLogger("main")
APP_ENV = os.getenv("APP_ENV", "development")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

GEOCODE_CACHE = {}
MAX_GEOCODE_CACHE_SIZE = 1000

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs("uploads/wfh_view", exist_ok=True)
PUBLIC_UPLOAD_FOLDERS = {"logos", "public"}
os.makedirs(os.path.join(UPLOAD_DIR, "logos"), exist_ok=True)

S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL")
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID") or os.getenv("AWS_ACCESS_KEY_ID")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY") or os.getenv("AWS_SECRET_ACCESS_KEY")
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME") or os.getenv("AWS_STORAGE_BUCKET_NAME", "logday-wfh-screenshots")
S3_REGION_NAME = os.getenv("S3_REGION_NAME", "us-east-1")
S3_PUBLIC_URL_PREFIX = os.getenv("S3_PUBLIC_URL_PREFIX") or os.getenv("R2_PUBLIC_URL_PREFIX")

_SENSITIVE_SETTINGS_KEYS = {
    "brevo_api_key",
    "brevo_sender_email",
    "brevo_sender_name",
    "smtp_password",
    "smtp_user",
    "smtp_host",
    "smtp_port",
    "smtp_from_email",
    "smtp_from_name",
    "absence_notification_emails",
}


def sanitize_public_settings(settings_doc: Optional[dict]) -> dict:
    """Strip secret API keys, SMTP credentials, and admin alert email lists from public settings responses."""
    if not settings_doc or not isinstance(settings_doc, dict):
        return SystemSettings().dict()
    cleaned = {k: v for k, v in settings_doc.items() if k not in _SENSITIVE_SETTINGS_KEYS}
    if "_id" in cleaned:
        cleaned["_id"] = str(cleaned["_id"])
    return cleaned


def build_static_upload_url(request: Request, relative_path: str) -> str:
    base_url = str(request.base_url).rstrip("/")
    forwarded_proto = request.headers.get("x-forwarded-proto")
    if forwarded_proto == "https" and base_url.startswith("http://"):
        base_url = base_url.replace("http://", "https://", 1)
    if not relative_path.startswith("/"):
        relative_path = "/" + relative_path
    return f"{base_url}{relative_path}"


async def send_security_alert_notification(alert_type: str, employee_email: str, detail: str):
    """
    Mock function for sending SMS/WhatsApp alerts.
    In production, this would integrate with Twilio, AWS SNS, etc.
    """
    logger.info(f"CRITICAL SECURITY NOTIFICATION sent to Admin: [{alert_type}] User: {employee_email} - {detail}")


async def trigger_alert(alert_type: str, employee_id: str, organization_id: str, detail: str, severity: str = "medium", metadata: dict = None):
    """Log a security or operational alert to the database."""
    try:
        alert = {
            "type": alert_type, # Identity, Territory, Productivity, Compliance
            "employee_id": employee_id,
            "organization_id": organization_id,
            "detail": detail,
            "severity": severity, # low, medium, high, critical
            "timestamp": datetime.now(timezone.utc),
            "status": "pending", # pending, resolved, dismissed
            "metadata": metadata or {}
        }
        await alerts_collection.insert_one(alert)
        logger.warning(f"ALERT TRIGGERED [{alert_type}]: {detail} for {employee_id}")
        
        # If high severity, send out external notification
        if severity in ["high", "critical"]:
            await send_security_alert_notification(alert_type, employee_id, detail)
            
    except Exception as e:
        logger.error(f"Failed to trigger alert: {e}")


def get_s3_key_from_url(url: str) -> str:
    for folder in ["wfh_view", "wfh_face_checks"]:
        marker = f"/{folder}/"
        if marker in url:
            idx = url.find(marker)
            return url[idx+1:]
    return ""


def get_screenshot_display_url(url: str, request: Request = None) -> str:
    if not url:
        return ""
    if url.startswith("/uploads/"):
        if request:
            return build_static_upload_url(request, url)
        return url
    
    # Generate Presigned URL for private S3/R2 storage
    if S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY:
        s3_key = get_s3_key_from_url(url)
        if s3_key:
            try:
                from botocore.client import Config
                s3_client = boto3.client(
                    "s3",
                    endpoint_url=S3_ENDPOINT_URL,
                    aws_access_key_id=S3_ACCESS_KEY_ID,
                    aws_secret_access_key=S3_SECRET_ACCESS_KEY,
                    region_name=S3_REGION_NAME,
                    config=Config(signature_version='s3v4')
                )
                presigned_url = s3_client.generate_presigned_url(
                    'get_object',
                    Params={'Bucket': S3_BUCKET_NAME, 'Key': s3_key},
                    ExpiresIn=3600 # 1 hour
                )
                return presigned_url
            except Exception as e:
                logger.error(f"Failed to generate presigned URL for key {s3_key}: {e}")
    return url


def _upload_file_to_storage_sync(image_bytes: bytes, filename: str, folder: str = "wfh_view") -> str:
    if S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY and boto3 is not None:
        try:
            s3_client = boto3.client(
                "s3",
                endpoint_url=S3_ENDPOINT_URL,
                aws_access_key_id=S3_ACCESS_KEY_ID,
                aws_secret_access_key=S3_SECRET_ACCESS_KEY,
                region_name=S3_REGION_NAME
            )
            try:
                s3_client.head_bucket(Bucket=S3_BUCKET_NAME)
            except Exception:
                try:
                    if S3_REGION_NAME == "us-east-1":
                        s3_client.create_bucket(Bucket=S3_BUCKET_NAME)
                    else:
                        s3_client.create_bucket(
                            Bucket=S3_BUCKET_NAME,
                            CreateBucketConfiguration={"LocationConstraint": S3_REGION_NAME}
                        )
                except Exception as bucket_err:
                    logger.warning(f"Could not verify/create bucket {S3_BUCKET_NAME}, proceeding anyway: {bucket_err}")

            s3_key = f"{folder}/{filename}"
            s3_client.put_object(
                Bucket=S3_BUCKET_NAME,
                Key=s3_key,
                Body=image_bytes,
                ContentType="image/png" if filename.endswith(".png") else "image/jpeg"
            )

            if S3_PUBLIC_URL_PREFIX:
                url = f"{S3_PUBLIC_URL_PREFIX.rstrip('/')}/{s3_key}"
            elif S3_ENDPOINT_URL:
                url = f"{S3_ENDPOINT_URL.rstrip('/')}/{S3_BUCKET_NAME}/{s3_key}"
            else:
                url = f"https://{S3_BUCKET_NAME}.s3.amazonaws.com/{s3_key}"
            logger.info(f"Successfully uploaded screenshot {filename} to S3/MinIO bucket {S3_BUCKET_NAME}")
            return url
        except Exception as e:
            logger.error(f"S3/MinIO upload failed, falling back to local storage: {e}")

    # Local filesystem fallback
    filepath = os.path.join(UPLOAD_DIR, folder, filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(image_bytes)
    return f"/uploads/{folder}/{filename}"


async def upload_file_to_storage(image_bytes: bytes, filename: str, folder: str = "wfh_view") -> str:
    """
    Enterprise hybrid storage: Uploads to S3/MinIO/R2 if configured, otherwise falls back to local disk.
    Runs in threadpool so blocking S3/disk I/O never stalls the FastAPI async event loop.
    """
    return await run_in_threadpool(_upload_file_to_storage_sync, image_bytes, filename, folder)


def _delete_file_from_storage_sync(image_url: str):
    if not image_url:
        return

    if image_url.startswith("/uploads/"):
        local_path = image_url.lstrip("/")
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
                logger.info(f"Deleted local file: {local_path}")
            except Exception as delete_err:
                logger.error(f"Failed to delete local screenshot file: {delete_err}")
        return

    if S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY and boto3 is not None:
        key_start = image_url.find("/wfh_view/")
        if key_start != -1:
            s3_key = image_url[key_start + 1:]
            try:
                s3_client = boto3.client(
                    "s3",
                    endpoint_url=S3_ENDPOINT_URL,
                    aws_access_key_id=S3_ACCESS_KEY_ID,
                    aws_secret_access_key=S3_SECRET_ACCESS_KEY,
                    region_name=S3_REGION_NAME
                )
                s3_client.delete_object(Bucket=S3_BUCKET_NAME, Key=s3_key)
                logger.info(f"Successfully deleted {s3_key} from S3/R2 bucket {S3_BUCKET_NAME}")
            except Exception as e:
                logger.error(f"Failed to delete {s3_key} from S3/R2 bucket {S3_BUCKET_NAME}: {e}")


async def delete_file_from_storage(image_url: str):
    """
    Deletes the file from S3/R2 or local filesystem in a worker thread.
    """
    if not image_url:
        return
    await run_in_threadpool(_delete_file_from_storage_sync, image_url)


async def get_effective_org_settings(org_id: Optional[str]) -> dict:
    """
    Unified settings loader that combines:
    1. Base organization settings from `settings_collection` (Office Times, Geofence, Timezone)
    2. Active Leave & Attendance policy from `leave_policies_collection` (Cutoffs, Grace, Min hours, Quotas)
    Ensures that configurations saved across any admin panel tab apply consistently across backend and apps.
    """
    if not org_id:
        return {}
    
    org_str = str(org_id).strip()
    query = {"$or": [{"organization_id": org_str}]}
    if ObjectId.is_valid(org_str):
        query["$or"].append({"organization_id": ObjectId(org_str)})
    
    merged = {}
    try:
        base_settings = await settings_collection.find_one(query)
        if base_settings:
            merged.update(base_settings)
            merged["_id"] = str(base_settings["_id"])
    except Exception as e:
        logger.warning(f"Error loading base settings for org {org_str}: {e}")

    try:
        current_year = datetime.now(timezone.utc).year
        policy = await leave_policies_collection.find_one({
            "$or": [
                {"organization_id": org_str, "year": current_year},
                {"organization_id": org_str}
            ]
        }, sort=[("year", -1), ("updated_at", -1)])
        
        if policy:
            keys_to_merge = [
                "office_start_time", "office_end_time", "late_grace_mins",
                "short_leave_cutoff_time", "late_to_short_leave_mins",
                "half_day_late_cutoff_time", "half_day_early_exit_cutoff_time",
                "early_leave_short_mins", "half_day_min_hours", "full_day_min_hours",
                "required_hours", "short_leave_max_hours", "short_leave_monthly_limit",
                "short_leave_penalty_rule", "lunch_start_time", "lunch_end_time",
                "max_lunch_duration_mins", "allocation_cycle", "carry_forward"
            ]
            for k in keys_to_merge:
                if k in policy and policy[k] is not None:
                    merged[k] = policy[k]
    except Exception as e:
        logger.warning(f"Error loading leave policy for org {org_str}: {e}")

    return merged


def get_today_start(offset_mins: int = 330):
    """
    Get the start of 'today' in UTC, given a local offset in minutes.
    Default: IST (+5:30 = 330 mins).
    """
    now_utc = datetime.now(timezone.utc)
    local_now = now_utc + timedelta(minutes=offset_mins)
    # Start of local day
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    # Convert back to UTC
    utc_start = local_start - timedelta(minutes=offset_mins)
    return utc_start


def calculate_haversine(lat1, lon1, lat2, lon2):
    dlat = math.radians(lat1 - lat2)
    dlon = math.radians(lon1 - lon2)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return 6371000 * c


def is_point_in_polygon(lat: float, lng: float, polygon: list) -> bool:
    """Ray-casting algorithm to check if a point is inside a polygon."""
    if not polygon or len(polygon) < 3:
        return False
    try:
        lat = float(lat)
        lng = float(lng)
        n = len(polygon)
        inside = False
        j = n - 1
        for i in range(n):
            pi = polygon[i]
            pj = polygon[j]
            if isinstance(pi, (list, tuple)):
                xi, yi = float(pi[0]), float(pi[1])
            else:
                xi = float(pi.get("lat", pi.get("latitude", 0)))
                yi = float(pi.get("lng", pi.get("longitude", 0)))
            if isinstance(pj, (list, tuple)):
                xj, yj = float(pj[0]), float(pj[1])
            else:
                xj = float(pj.get("lat", pj.get("latitude", 0)))
                yj = float(pj.get("lng", pj.get("longitude", 0)))
            if ((yi > lng) != (yj > lng)) and (abs(yj - yi) > 1e-9) and (lat < (xj - xi) * (lng - yi) / (yj - yi) + xi):
                inside = not inside
            j = i
        return inside
    except Exception as e:
        logger.warning(f"Polygon point test error: {e}")
        return False


def attendance_type_str(val):
    return val or "attendance"


def _is_superadmin(admin: Admin) -> bool:
    """Check if admin is a superadmin (by role or system_org binding)."""
    return admin.role == AdminRole.SUPERADMIN or admin.organization_id == "system_org"


def get_employee_filter(current_admin: Admin):
    """Returns a MongoDB filter based on admin role for multi-tenant segmenting."""
    org_id = current_admin.organization_id
    if not org_id or _is_superadmin(current_admin):
        # Superadmin / system_org sees all organizations
        return {}
    
    role = current_admin.role
    base_filter = {"organization_id": org_id}
    
    if role == "manager":
        # Managers only see their assigned team
        base_filter["manager_id"] = current_admin.email
        
    return base_filter


async def get_scoped_employee_ids(current_admin: Admin):
    base_filter = get_employee_filter(current_admin)
    emps = await employees_collection.find(base_filter, {"_id": 1}).to_list(None)
    return [str(e["_id"]) for e in emps]


async def get_scoped_employee_emails(current_admin: Admin):
    base_filter = get_employee_filter(current_admin)
    emps = await employees_collection.find(base_filter, {"email": 1}).to_list(None)
    return [e["email"] for e in emps]


async def get_scoped_employee_employee_ids(current_admin: Admin):
    base_filter = get_employee_filter(current_admin)
    emps = await employees_collection.find(base_filter, {"employee_id": 1}).to_list(None)
    return [e.get("employee_id") for e in emps if "employee_id" in e]


def check_feature_access(admin: Admin, feature: str):
    """Raises 403 if the admin doesn't have access to a specific feature."""
    if admin.role in ["owner", "superadmin"]:
        return  # Full access
    allowed = admin.allowed_features or []
    if feature not in allowed:
        raise HTTPException(status_code=403, detail=f"You do not have access to the '{feature}' feature. Contact your admin.")


async def get_current_any_user(token: str = Depends(employee_oauth2_scheme)):
    """Try to decode as employee first, then admin."""
    try:
        user = await get_current_employee(token)
        if user:
            return user, "employee"
    except Exception:
        pass
    
    try:
        admin = await get_current_admin(token)
        if admin:
            return admin, "admin"
    except Exception:
        pass
        
    raise HTTPException(status_code=401, detail="Invalid session")


def normalize_date_str(d_str: str) -> Optional[str]:
    """Normalize any date format (DD/MM/YYYY, YYYY-MM-DD, ISO) into standard YYYY-MM-DD."""
    if not d_str:
        return None
    try:
        s = str(d_str).strip()
        if "T" in s:
            s = s.split("T")[0]
        if "/" in s:
            parts = s.split("/")
            if len(parts) == 3:
                if len(parts[2]) == 4:  # DD/MM/YYYY
                    return f"{int(parts[2]):04d}-{int(parts[1]):02d}-{int(parts[0]):02d}"
                elif len(parts[0]) == 4:  # YYYY/MM/DD
                    return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
        elif "-" in s:
            parts = s.split("-")
            if len(parts) == 3:
                if len(parts[0]) == 4:  # YYYY-MM-DD
                    return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                elif len(parts[2]) == 4:  # DD-MM-YYYY
                    return f"{int(parts[2]):04d}-{int(parts[1]):02d}-{int(parts[0]):02d}"
        return s[:10]
    except Exception:
        return str(d_str)[:10]


def count_days_between(start_date_str: str, end_date_str: str) -> int:
    """Inclusive calendar days between two dates in any standard format."""
    try:
        norm_start = normalize_date_str(start_date_str)
        norm_end = normalize_date_str(end_date_str)
        d1 = datetime.strptime(norm_start, "%Y-%m-%d")
        d2 = datetime.strptime(norm_end, "%Y-%m-%d")
        if d2 < d1:
            return 1
        return (d2 - d1).days + 1
    except Exception:
        return 1


async def sync_holiday_days_off(org_id: str, payroll_month: str):
    """Synchronize days_off in holidays collection for HRMS payroll integration."""
    try:
        # Find all named holidays for this month
        month_holidays = await holidays_collection.find({
            "$or": [
                {"organization_id": org_id, "payroll_month": payroll_month, "date": {"$exists": True}},
                {"organization_id": "system_org", "payroll_month": payroll_month, "date": {"$exists": True}}
            ]
        }).to_list(length=100)

        holiday_days = set()
        holiday_names = {}
        for h in month_holidays:
            d_val = None
            if "day" in h and h["day"] is not None:
                d_val = int(h["day"])
            elif "date" in h and h["date"]:
                try:
                    d_val = int(str(h["date"]).split("-")[2])
                except Exception:
                    pass
            if d_val is not None:
                holiday_days.add(d_val)
                holiday_names[str(d_val)] = h.get("name", "Holiday")

        # Update the payroll days_off doc for this month
        payroll_doc = await holidays_collection.find_one({
            "organization_id": org_id,
            "payroll_month": payroll_month,
            "days_off": {"$exists": True}
        })

        if payroll_doc:
            current_days_off = set(payroll_doc.get("days_off", []))
            merged = sorted(list(current_days_off.union(holiday_days)))
            merged_names = payroll_doc.get("holiday_names", {})
            merged_names.update(holiday_names)
            await holidays_collection.update_one(
                {"_id": payroll_doc["_id"]},
                {"$set": {"days_off": merged, "holiday_names": merged_names, "updated_at": datetime.now(timezone.utc)}}
            )
        else:
            # Create default days_off doc with Sundays + holidays
            try:
                y, m = map(int, payroll_month.split("-"))
                num_days = calendar.monthrange(y, m)[1]
                sundays = []
                for d in range(1, num_days + 1):
                    if datetime(y, m, d).weekday() == 6:
                        sundays.append(d)
                initial_days_off = sorted(list(set(sundays).union(holiday_days)))
                await holidays_collection.insert_one({
                    "organization_id": org_id,
                    "payroll_month": payroll_month,
                    "days_off": initial_days_off,
                    "holiday_names": holiday_names,
                    "created_at": datetime.now(timezone.utc)
                })
            except Exception as cr_err:
                logger.warning(f"Could not initialize days_off doc: {cr_err}")
    except Exception as e:
        logger.error(f"sync_holiday_days_off error: {e}")


async def calculate_leave_balance(
    email: str,
    org_id: str,
    year: Optional[int] = None,
    month: Optional[str] = None,
    absent_days: float = 0.0
) -> dict:
    """Enterprise SL, CL, TL, and Loss of Pay (LOP) balance calculation engine with true month-by-month accrual, carry-forward, and half-day support."""
    now = datetime.now(timezone.utc)
    if not year:
        year = now.year
    if not month:
        month = f"{year}-{now.month:02d}"

    # 1. Fetch Leave Policy & Org Settings
    policy = await leave_policies_collection.find_one({
        "$or": [
            {"organization_id": org_id, "year": year},
            {"organization_id": "system_org", "year": year},
            {"organization_id": org_id},
            {"organization_id": "system_org"}
        ]
    })

    org_settings = None
    tz_offset = 330
    try:
        org_settings = await settings_collection.find_one({"organization_id": org_id})
        if org_settings:
            tz_offset = int(org_settings.get("timezone_offset", 330))
    except Exception: pass

    allocation_cycle = "annual"
    carry_forward = True
    start_month = f"{year}-01"
    defaults = {"sick_leave": 12.0, "casual_leave": 12.0, "total_leave": 30.0}
    paid_leave_types = ["sick", "casual", "on_duty", "duty"]
    unpaid_leave_types = ["other", "unpaid", "lop"]
    half_day_min_hours = 4.0
    full_day_min_hours = 8.0
    late_grace_mins = 15
    short_leave_monthly_limit = 2
    short_leave_max_hours = 2.0
    short_leave_penalty_rule = "half_day_per_2_excess"
    overrides = {}

    if policy:
        allocation_cycle = policy.get("allocation_cycle", "annual")
        carry_forward = policy.get("carry_forward", True)
        if policy.get("start_month"):
            start_month = policy.get("start_month")
        defaults = policy.get("defaults", defaults)
        paid_leave_types = policy.get("paid_leave_types", paid_leave_types)
        unpaid_leave_types = policy.get("unpaid_leave_types", unpaid_leave_types)
        half_day_min_hours = float(policy.get("half_day_min_hours", 4.0))
        full_day_min_hours = float(policy.get("full_day_min_hours", 8.0))
        late_grace_mins = int(policy.get("late_grace_mins", 15))
        short_leave_monthly_limit = int(policy.get("short_leave_monthly_limit", 2))
        short_leave_max_hours = float(policy.get("short_leave_max_hours", 2.0))
        short_leave_penalty_rule = policy.get("short_leave_penalty_rule", "half_day_per_2_excess")
        overrides = policy.get("overrides", {})

    clean_email = email.strip().lower()
    emp_quota = overrides.get(clean_email) or overrides.get(email) or defaults
    
    default_sl = 12.0 if allocation_cycle == "annual" else 0.0
    default_cl = 12.0 if allocation_cycle == "annual" else 0.0
    default_tl = 30.0 if allocation_cycle == "annual" else 1.5

    base_sl = float(emp_quota.get("sick_leave", default_sl))
    base_cl = float(emp_quota.get("casual_leave", default_cl))
    base_tl = float(emp_quota.get("total_leave", default_tl))

    # Identify all possible identifier variants for this employee
    emp_identifiers = [clean_email, email.strip()]
    try:
        emp_doc = await employees_collection.find_one({"email": clean_email})
        if emp_doc:
            if "_id" in emp_doc:
                emp_identifiers.append(str(emp_doc["_id"]))
            if "employee_id" in emp_doc and emp_doc["employee_id"]:
                emp_identifiers.append(str(emp_doc["employee_id"]).strip())
            # Dynamically infer start_month from joining date if policy did not set a specific start_month
            if not policy or not policy.get("start_month"):
                j_date = emp_doc.get("date_of_joining") or emp_doc.get("joining_date")
                if j_date:
                    norm_j = normalize_date_str(j_date)
                    if norm_j and norm_j.startswith(str(year)):
                        start_month = norm_j[:7]
    except Exception:
        pass

    leaves = await leave_requests_collection.find({
        "$or": [
            {"employee_id": {"$in": emp_identifiers}},
            {"email": {"$in": emp_identifiers}}
        ],
        "status": {"$regex": "^approved$", "$options": "i"}
    }).to_list(length=500)

    def eval_leave(l_doc):
        norm_start = normalize_date_str(l_doc.get("start_date", ""))
        norm_end = normalize_date_str(l_doc.get("end_date", norm_start))
        if not norm_start:
            return None, 0.0, False, "other"
            
        dur_type = str(l_doc.get("duration_type", "")).lower().strip()
        if l_doc.get("duration") is not None:
            days = float(l_doc.get("duration"))
        elif dur_type in ["first_half", "second_half", "half_day"]:
            days = 0.5
        elif dur_type == "short_leave":
            days = 0.0
        else:
            days = float(count_days_between(norm_start, norm_end or norm_start))
            
        l_type = str(l_doc.get("leave_type", "other")).lower().strip()
        
        # Check Paid Status: Explicit override takes priority, else check against paid_leave_types list
        if l_doc.get("is_paid") is not None:
            is_paid = bool(l_doc.get("is_paid"))
        else:
            is_paid = any(pt in l_type for pt in paid_leave_types)
            
        return norm_start, days, is_paid, l_type

    if allocation_cycle == "monthly":
        # Generate ordered list of months from start_month to target month
        try:
            s_yr, s_mo = map(int, start_month.split("-"))
            t_yr, t_mo = map(int, month.split("-"))
            
            # Clamp if target is before start
            if (t_yr < s_yr) or (t_yr == s_yr and t_mo < s_mo):
                start_month = month
                s_yr, s_mo = t_yr, t_mo
        except Exception:
            start_month = month
            s_yr, s_mo = int(month.split("-")[0]), int(month.split("-")[1])
            t_yr, t_mo = s_yr, s_mo

        # Build month sequence
        month_seq = []
        cur_y, cur_m = s_yr, s_mo
        while (cur_y < t_yr) or (cur_y == t_yr and cur_m <= t_mo):
            month_seq.append(f"{cur_y:04d}-{cur_m:02d}")
            cur_m += 1
            if cur_m > 12:
                cur_m = 1
                cur_y += 1

        # Month-by-month state tracking
        carried_sl = 0.0
        carried_cl = 0.0
        carried_tl = 0.0
        
        target_month_data = {}

        for m_str in month_seq:
            # Filter approved leaves in this specific month
            m_leaves = []
            for l in leaves:
                n_start, days, is_paid, l_type = eval_leave(l)
                if n_start and n_start.startswith(m_str):
                    m_leaves.append((n_start, days, is_paid, l_type))

            # Sum usage in this month
            used_sl_m = sum(d for _, d, _, lt in m_leaves if "sick" in lt)
            used_cl_m = sum(d for _, d, _, lt in m_leaves if "casual" in lt)
            used_od_m = sum(d for _, d, _, lt in m_leaves if "on_duty" in lt or "duty" in lt)
            used_other_m = sum(d for _, d, _, lt in m_leaves if not any(k in lt for k in ["sick", "casual", "on_duty", "duty"]))
            
            # Paid vs Unpaid used in this month (On-duty does not deduct from quota)
            paid_used_tl_m = sum(d for _, d, is_p, lt in m_leaves if is_p and ("on_duty" not in lt and "duty" not in lt))
            unpaid_used_tl_m = sum(d for _, d, is_p, lt in m_leaves if not is_p and ("on_duty" not in lt and "duty" not in lt))

            # Available quota in this month = monthly quota + carried forward from previous month
            avail_sl_m = base_sl + carried_sl
            avail_cl_m = base_cl + carried_cl
            avail_tl_m = base_tl + carried_tl

            # Remaining quota at end of this month
            rem_sl_m = max(0.0, avail_sl_m - used_sl_m)
            rem_cl_m = max(0.0, avail_cl_m - used_cl_m)
            rem_tl_m = max(0.0, avail_tl_m - paid_used_tl_m)

            # Excess LOP days in this month = (paid leaves taken exceeding available quota) + (unpaid leaves)
            excess_lop_m = max(0.0, paid_used_tl_m - avail_tl_m) + unpaid_used_tl_m

            # If this is our target month, record full data
            if m_str == month:
                prev_month_str = None
                prev_month_label = None
                if len(month_seq) > 1 and month_seq.index(m_str) > 0:
                    prev_idx = month_seq.index(m_str) - 1
                    prev_month_str = month_seq[prev_idx]
                    try:
                        p_dt = datetime.strptime(prev_month_str, "%Y-%m")
                        prev_month_label = p_dt.strftime("%b %Y")
                    except Exception:
                        prev_month_label = prev_month_str

                target_month_data = {
                    "year": year,
                    "month": month,
                    "allocation_cycle": "monthly",
                    "carry_forward": carry_forward,
                    "start_month": start_month,
                    "previous_month": prev_month_str,
                    "previous_month_label": prev_month_label,
                    "sick_leave": {
                        "monthly_quota": round(base_sl, 2),
                        "carried_forward": round(carried_sl, 2),
                        "available": round(avail_sl_m, 2),
                        "total": round(avail_sl_m, 2),
                        "used": round(used_sl_m, 2),
                        "remaining": round(rem_sl_m, 2)
                    },
                    "casual_leave": {
                        "monthly_quota": round(base_cl, 2),
                        "carried_forward": round(carried_cl, 2),
                        "available": round(avail_cl_m, 2),
                        "total": round(avail_cl_m, 2),
                        "used": round(used_cl_m, 2),
                        "remaining": round(rem_cl_m, 2)
                    },
                    "total_leave": {
                        "monthly_quota": round(base_tl, 2),
                        "carried_forward": round(carried_tl, 2),
                        "available": round(avail_tl_m, 2),
                        "total": round(avail_tl_m, 2),
                        "used": round(paid_used_tl_m, 2),
                        "unpaid_used": round(unpaid_used_tl_m, 2),
                        "remaining": round(rem_tl_m, 2),
                        "excess_lop": round(excess_lop_m, 2)
                    },
                    "on_duty": {
                        "used": round(used_od_m, 2)
                    },
                    "other": {
                        "used": round(used_other_m, 2)
                    },
                    "paid_leave_types": paid_leave_types,
                    "unpaid_leave_types": unpaid_leave_types,
                    "absent_days": round(float(absent_days), 2),
                    "total_lop_days": round(excess_lop_m + float(absent_days), 2)
                }

            # Update carried forward for next month in sequence
            if carry_forward:
                carried_sl = rem_sl_m
                carried_cl = rem_cl_m
                carried_tl = rem_tl_m
            else:
                carried_sl = 0.0
                carried_cl = 0.0
                carried_tl = 0.0

        # Count attendance-based and applied short leaves / half days in target month (de-duplicated by date)
        short_leave_dates = set()
        half_day_dates = set()
        att_short_leave_dates = set()
        att_half_day_dates = set()
        applied_short_leave_dates = set()
        applied_half_day_dates = set()

        try:
            local_m_start = datetime.strptime(f"{month}-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
            yr_int, mo_int = map(int, month.split("-"))
            local_next_m = datetime(yr_int + 1, 1, 1, tzinfo=timezone.utc) if mo_int == 12 else datetime(yr_int, mo_int + 1, 1, tzinfo=timezone.utc)
            m_start_utc = local_m_start - timedelta(minutes=tz_offset)
            next_m_utc = local_next_m - timedelta(minutes=tz_offset)

            month_att_logs = await attendance_logs_collection.find({
                "$or": [
                    {"user_id": {"$in": emp_identifiers}},
                    {"email": {"$in": emp_identifiers}}
                ],
                "type": {"$in": ["check-in", "IN"]},
                "timestamp": {"$gte": m_start_utc, "$lt": next_m_utc}
            }).to_list(1000)

            for l in month_att_logs:
                l_ts = l["timestamp"]
                if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
                local_l_ts = l_ts + timedelta(minutes=tz_offset)
                if local_l_ts.year != yr_int or local_l_ts.month != mo_int:
                    continue
                l_date = local_l_ts.strftime("%Y-%m-%d")
                if l.get("is_short_leave") or l.get("attendance_status") == "Short Leave":
                    att_short_leave_dates.add(l_date)
                    short_leave_dates.add(l_date)
                if l.get("is_half_day") or l.get("attendance_status") == "Half Day":
                    att_half_day_dates.add(l_date)
                    half_day_dates.add(l_date)
        except Exception as e:
            logger.warning(f"Error fetching month att logs for leave balance: {e}")

        for l in leaves:
            dur_type = str(l.get("duration_type", "")).lower().strip()
            l_start = str(l.get("start_date", ""))
            l_end = str(l.get("end_date", l_start))
            if not l_start or not (l_start.startswith(month) or l_end.startswith(month)):
                continue
            
            try:
                c_dt = max(datetime.strptime(l_start[:10], "%Y-%m-%d"), datetime.strptime(f"{month}-01", "%Y-%m-%d"))
                e_dt = min(datetime.strptime(l_end[:10], "%Y-%m-%d"), local_next_m.replace(tzinfo=None) - timedelta(days=1))
                while c_dt <= e_dt:
                    d_str = c_dt.strftime("%Y-%m-%d")
                    if dur_type == "short_leave":
                        applied_short_leave_dates.add(d_str)
                        short_leave_dates.add(d_str)
                    elif (dur_type in ["first_half", "second_half", "half_day"] or l.get("duration") == 0.5) and not l.get("is_uninformed_absence_penalty"):
                        applied_half_day_dates.add(d_str)
                        half_day_dates.add(d_str)
                    c_dt += timedelta(days=1)
            except Exception: pass

        total_short_leaves = len(short_leave_dates)
        total_half_days = len(half_day_dates)
        short_leave_monthly_limit = int((policy or {}).get("short_leave_monthly_limit", 2))
        short_leave_remaining = max(0, short_leave_monthly_limit - total_short_leaves)

        target_month_data["short_leave"] = {
            "monthly_limit": short_leave_monthly_limit,
            "used": total_short_leaves,
            "remaining": short_leave_remaining,
            "attendance_triggered": len(att_short_leave_dates - applied_short_leave_dates),
            "applied": len(applied_short_leave_dates)
        }
        target_month_data["half_day"] = {
            "used": total_half_days,
            "attendance_triggered": len(att_half_day_dates - applied_half_day_dates),
            "applied": len(applied_half_day_dates)
        }

        return target_month_data

    else:
        # Annual cycle
        y_str = str(year)
        y_leaves = []
        for l in leaves:
            n_start, days, is_paid, l_type = eval_leave(l)
            if n_start and n_start.startswith(y_str):
                y_leaves.append((n_start, days, is_paid, l_type))

        used_sick = sum(d for _, d, _, lt in y_leaves if "sick" in lt)
        used_casual = sum(d for _, d, _, lt in y_leaves if "casual" in lt)
        used_on_duty = sum(d for _, d, _, lt in y_leaves if "on_duty" in lt or "duty" in lt)
        used_other = sum(d for _, d, _, lt in y_leaves if not any(k in lt for k in ["sick", "casual", "on_duty", "duty"]))

        paid_used_tl = sum(d for _, d, is_p, lt in y_leaves if is_p and ("on_duty" not in lt and "duty" not in lt))
        unpaid_used_tl = sum(d for _, d, is_p, lt in y_leaves if not is_p and ("on_duty" not in lt and "duty" not in lt))

        rem_sl = max(0.0, base_sl - used_sick)
        rem_cl = max(0.0, base_cl - used_casual)
        rem_tl = max(0.0, base_tl - paid_used_tl)
        excess_lop = max(0.0, paid_used_tl - base_tl) + unpaid_used_tl

        # Count attendance-based and applied short leaves / half days in target month
        try:
            local_m_start = datetime.strptime(f"{month}-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
            yr_int, mo_int = map(int, month.split("-"))
            local_next_m = datetime(yr_int + 1, 1, 1, tzinfo=timezone.utc) if mo_int == 12 else datetime(yr_int, mo_int + 1, 1, tzinfo=timezone.utc)
            m_start_utc = local_m_start - timedelta(minutes=tz_offset)
            next_m_utc = local_next_m - timedelta(minutes=tz_offset)

            att_short_leaves = await attendance_logs_collection.count_documents({
                "$or": [
                    {"user_id": {"$in": emp_identifiers}},
                    {"email": {"$in": emp_identifiers}}
                ],
                "is_short_leave": True,
                "type": "check-in",
                "timestamp": {"$gte": m_start_utc, "$lt": next_m_utc}
            })
            att_half_days = await attendance_logs_collection.count_documents({
                "$or": [
                    {"user_id": {"$in": emp_identifiers}},
                    {"email": {"$in": emp_identifiers}}
                ],
                "is_half_day": True,
                "type": "check-in",
                "timestamp": {"$gte": m_start_utc, "$lt": next_m_utc}
            })
        except Exception:
            att_short_leaves = 0
            att_half_days = 0

        applied_short_leaves = sum(1 for l in leaves if str(l.get("duration_type", "")).lower().strip() == "short_leave" and str(l.get("start_date", "")).startswith(month))
        applied_half_days = sum(1 for l in leaves if str(l.get("duration_type", "")).lower().strip() in ["first_half", "second_half", "half_day"] and not l.get("is_uninformed_absence_penalty") and str(l.get("start_date", "")).startswith(month))

        total_short_leaves = att_short_leaves + applied_short_leaves
        total_half_days = att_half_days + applied_half_days
        short_leave_monthly_limit = int((policy or {}).get("short_leave_monthly_limit", 2))
        short_leave_remaining = max(0, short_leave_monthly_limit - total_short_leaves)

        return {
            "year": year,
            "month": month,
            "allocation_cycle": "annual",
            "sick_leave": {
                "total": round(base_sl, 2),
                "used": round(used_sick, 2),
                "remaining": round(rem_sl, 2)
            },
            "casual_leave": {
                "total": round(base_cl, 2),
                "used": round(used_casual, 2),
                "remaining": round(rem_cl, 2)
            },
            "total_leave": {
                "total": round(base_tl, 2),
                "available": round(base_tl, 2),
                "used": round(paid_used_tl, 2),
                "unpaid_used": round(unpaid_used_tl, 2),
                "remaining": round(rem_tl, 2),
                "excess_lop": round(excess_lop, 2)
            },
            "short_leave": {
                "monthly_limit": short_leave_monthly_limit,
                "used": total_short_leaves,
                "remaining": short_leave_remaining,
                "attendance_triggered": att_short_leaves,
                "applied": applied_short_leaves
            },
            "half_day": {
                "used": total_half_days,
                "attendance_triggered": att_half_days,
                "applied": applied_half_days
            },
            "on_duty": {
                "used": round(used_on_duty, 2)
            },
            "other": {
                "used": round(used_other, 2)
            },
            "paid_leave_types": paid_leave_types,
            "unpaid_leave_types": unpaid_leave_types,
            "absent_days": round(float(absent_days), 2),
            "total_lop_days": round(excess_lop + float(absent_days), 2)
        }


async def send_expo_push_notifications(
    tokens: List[str], 
    title: str, 
    body: str, 
    data: Optional[Dict] = None, 
    sound: str = "default", 
    channel_id: str = "nudges"
):
    """
    Sends real background push notifications via Expo Push Notification API.
    Works even when the mobile app is in the background or killed.
    """
    if not tokens:
        return
    
    valid_tokens = []
    for t in tokens:
        if isinstance(t, str) and (t.startswith("ExponentPushToken[") or t.startswith("ExpoPushToken[")):
            valid_tokens.append(t)
            
    if not valid_tokens:
        logger.info("[PUSH] No valid Expo push tokens found among provided candidates.")
        return
    
    valid_tokens = list(set(valid_tokens))

    messages = [
        {
            "to": token,
            "sound": sound,
            "title": title,
            "body": body,
            "data": data or {},
            "priority": "high",
            "channelId": channel_id,
            "_displayInForeground": True
        }
        for token in valid_tokens
    ]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                "https://exp.host/--/api/v2/push/send",
                json=messages,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip, deflate",
                    "Content-Type": "application/json"
                }
            )
            logger.info(f"[PUSH] Expo push notification dispatched to {len(valid_tokens)} device(s). Status: {resp.status_code}")
    except Exception as e:
        logger.error(f"[PUSH] Error sending Expo push notifications: {e}")


async def verify_wfh_device(employee_id: str, org_id: str, device_id: str):
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    AUTO_APPROVE = os.getenv("AUTO_APPROVE_WFH_DEVICES", "true").lower() == "true"

    device = await wfh_device_info_collection.find_one({
        "employee_id": employee_id,
        "organization_id": org_id,
        "device_id": device_id
    })

    if not device:
        from bson import ObjectId
        employee = await employees_collection.find_one({"_id": ObjectId(employee_id)})
        now = datetime.now(timezone.utc)
        initial_status = "approved" if AUTO_APPROVE else "pending"
        device_doc = {
            "employee_id": employee_id,
            "employee_email": employee.get("email") if employee else "",
            "employee_name": employee.get("full_name", "") if employee else "",
            "organization_id": org_id,
            "device_id": device_id,
            "status": initial_status,
            "registered_at": now,
            "approved_at": now if AUTO_APPROVE else None,
            "approved_by": "auto-approved" if AUTO_APPROVE else None,
            "last_seen": now,
            "updated_at": now
        }
        await wfh_device_info_collection.insert_one(device_doc)
        if not AUTO_APPROVE:
            raise HTTPException(
                status_code=403,
                detail="WFH device registered and pending admin approval."
            )
        return device_doc

    if device.get("status") == "revoked":
        raise HTTPException(
            status_code=403,
            detail="WFH device has been revoked by administrator. Please contact admin."
        )

    if device.get("status") != "approved":
        if AUTO_APPROVE:
            await wfh_device_info_collection.update_one(
                {"_id": device["_id"]},
                {"$set": {"status": "approved", "approved_at": datetime.now(timezone.utc), "approved_by": "auto-approved"}}
            )
            device["status"] = "approved"
            return device

        raise HTTPException(
            status_code=403,
            detail="WFH device not approved. Please contact admin."
        )

    return device


def _extract_screenshot_ocr_text_sync(image_path: str) -> str:
    temp_file_path = None
    try:
        if image_path.startswith("http://") or image_path.startswith("https://"):
            try:
                download_url = get_screenshot_display_url(image_path)
                fd, temp_file_path = tempfile.mkstemp(suffix=".jpg")
                os.close(fd)
                req = urllib.request.Request(
                    download_url,
                    headers={"User-Agent": "Mozilla/5.0"}
                )
                with urllib.request.urlopen(req, timeout=15) as response, open(temp_file_path, "wb") as out_file:
                    out_file.write(response.read())
                resolved_path = temp_file_path
            except Exception as download_err:
                logger.error(f"Failed to download screenshot from R2/HTTP for OCR: {download_err}")
                resolved_path = ""
        else:
            if "uploads/" in image_path:
                idx = image_path.find("uploads/")
                image_path = image_path[idx:]
            resolved_path = image_path
            if not os.path.isabs(image_path) and not os.path.exists(image_path):
                alternative_path = os.path.join("..", "wfh_desktop", "agent", image_path)
                if os.path.exists(alternative_path):
                    resolved_path = alternative_path

        if not resolved_path or not os.path.exists(resolved_path):
            logger.warning(f"Screenshot file not found for OCR: {image_path}")
            return ""

        try:
            import pytesseract  # type: ignore[import-not-found]
            from PIL import Image  # type: ignore[import-not-found]
            with Image.open(resolved_path) as img:
                return pytesseract.image_to_string(img) + "\n"
        except ImportError:
            logger.warning("pytesseract or PIL is not installed. Falling back to active window text threat check.")
            return "\n"
        except Exception as ocr_err:
            logger.warning(f"Tesseract OCR engine not fully loaded or failed: {ocr_err}")
            return "\n"
    finally:
        if temp_file_path and os.path.exists(temp_file_path):
            try:
                os.remove(temp_file_path)
            except Exception as cleanup_err:
                logger.warning(f"Failed to cleanup temp OCR screenshot file: {cleanup_err}")


async def scan_screenshot_ocr_threats(screenshot_id: str, image_path: str, employee_id: str, org_id: str, email: str, device_id: str, session_id: str):
    try:
        text = await run_in_threadpool(_extract_screenshot_ocr_text_sync, image_path)
        if not text:
            return

        # Regex threat signatures
        pattern = r'(?i)(password|passwd|api[_-]?key|secret|token)\s*[:=]\s*["\']?[a-zA-Z0-9_\-\+]{16,}'
        match = re.search(pattern, text)
        
        if match:
            leak_reason = f"Plaintext credentials/API keys visible: found '{match.group(1)}' token leak"
            logger.warning(f"WFH Security Leak Alert! screenshot {screenshot_id}: {leak_reason}")
            
            # Update screenshot record in Mongo
            await wfh_screenshots_collection.update_one(
                {"_id": ObjectId(screenshot_id)},
                {"$set": {
                    "flagged": True,
                    "flag_reason": "Plaintext credentials/API keys visible"
                }}
            )
            
            # Raise High Severity WFH Compliance Alert
            now = datetime.now(timezone.utc)
            alert_doc = {
                "employee_id": employee_id,
                "employee_email": email,
                "employee_name": "Remote Employee",
                "organization_id": org_id,
                "device_id": device_id,
                "session_id": session_id,
                "type": "Credential Leak",
                "timestamp": now,
                "image_url": image_path,
                "severity": "high",
                "status": "pending",
                "details": f"Plaintext credentials or secret tokens leaked in active window screenshot: {leak_reason}",
                "metadata": {"screenshot_id": screenshot_id},
                "created_at": now
            }
            alert_result = await wfh_alerts_collection.insert_one(alert_doc)
            
            # Mirror WFH alert into unified alerts collection
            try:
                await alerts_collection.insert_one({
                    "organization_id": org_id,
                    "employee_id": employee_id,
                    "employee_name": "Remote Employee",
                    "type": "Productivity",
                    "severity": "high",
                    "status": "pending",
                    "detail": f"WFH Security Leak Alert: Credential leak detected via background OCR scanning.",
                    "timestamp": now,
                    "metadata": {
                        "source": "wfh_desktop",
                        "wfh_alert_id": str(alert_result.inserted_id),
                        "device_id": device_id,
                        "session_id": session_id
                    }
                })
            except Exception as mirror_err:
                logger.error(f"Failed to mirror unified WFH alert: {mirror_err}")
    except Exception as e:
        logger.error(f"OCR threat scan failed: {e}")


