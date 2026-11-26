from auth import resolve_access_token
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
from services.common_helpers import (
    BASE_DIR, LOG_DIR, GEOCODE_CACHE, MAX_GEOCODE_CACHE_SIZE,
    UPLOAD_DIR, PUBLIC_UPLOAD_FOLDERS,
    S3_ENDPOINT_URL, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY,
    S3_BUCKET_NAME, S3_REGION_NAME, S3_PUBLIC_URL_PREFIX,
    build_static_upload_url,
    send_security_alert_notification, trigger_alert, get_s3_key_from_url,
    get_screenshot_display_url, upload_file_to_storage, delete_file_from_storage,
    get_effective_org_settings, get_today_start, calculate_haversine,
    is_point_in_polygon, attendance_type_str, _is_superadmin,
    get_employee_filter, get_scoped_employee_ids, get_scoped_employee_emails,
    get_scoped_employee_employee_ids, check_feature_access, get_current_any_user,
    normalize_date_str, count_days_between, sync_holiday_days_off,
    calculate_leave_balance, send_expo_push_notifications, verify_wfh_device,
    scan_screenshot_ocr_threats
)
from services.scheduler_jobs import check_missed_visits, purge_old_screenshots

router = APIRouter()

@router.get("/api/geocoding/reverse")
async def reverse_geocode(lat: float, lon: float):
    """
    Proxy for Nominatim reverse geocoding to bypass CORS and 429 errors.
    Uses a server-side User-Agent to comply with Nominatim's policy.
    Uses an in-memory cache and runs in threadpool to prevent event-loop blockages.
    """
    cache_key = (round(lat, 4), round(lon, 4))
    if cache_key in GEOCODE_CACHE:
        return GEOCODE_CACHE[cache_key]

    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat={lat}&lon={lon}"
        headers = {
            "User-Agent": "LogDayAttendanceApp/1.0 (contact: roboaiapaths99@gmail.com)"
        }
        # Run blocking requests.get in threadpool to keep event loop unblocked
        response = await run_in_threadpool(
            requests.get, url, headers=headers, timeout=5
        )
        response.raise_for_status()
        data = response.json()
        if len(GEOCODE_CACHE) >= MAX_GEOCODE_CACHE_SIZE:
            for k in list(GEOCODE_CACHE.keys())[:200]:
                del GEOCODE_CACHE[k]
        GEOCODE_CACHE[cache_key] = data
        return data
    except Exception as e:
        logger.error(f"Geocoding proxy error: {str(e)}")
        # Fail gracefully with coordinates if geocoding fails
        return {"display_name": f"{lat:.4f}, {lon:.4f}", "address": {}}


@router.get("/uploads/{file_path:path}")
async def serve_upload_file(
    file_path: str,
    request: Request
):
    from upload_security import safe_local_path, authorize_upload
    from database import uploads_collection
    url = "/uploads/" + file_path
    target_file = safe_local_path(url)
    if file_path.startswith("logos/"):
        if not target_file.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(target_file)
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, "Authentication required to view this file")
    account, kind = await resolve_access_token(auth_header.split(" ", 1)[1])
    record = await uploads_collection.find_one({"url": url})
    await authorize_upload(record, account, kind)
    if not target_file.is_file():
        raise HTTPException(404, "File not found")
    return FileResponse(target_file, media_type=record["media_type"], headers={
        "Cache-Control": "private, no-store", "Content-Security-Policy": "sandbox",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
    })



@router.get("/")
async def root():
    return {"message": "LogDay AI Attendance API is active", "status": "online"}


@router.get("/health", tags=["System"])
async def health_check():
    """Health check endpoint to verify API and DB are connected."""
    try:
        from database import client
        await client.admin.command("ping")
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"
    return {
        "api": "LogDay AI Attendance API",
        "status": "healthy",
        "env": APP_ENV,
        "database": db_status,
        "version": "1.0.0"
    }


