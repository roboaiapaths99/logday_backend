from services.inference import run_inference
from upload_security import SecureMediaRoute
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
from attendance_service import classify_attendance_day
from face_utils import get_face_embedding, verify_face, compare_faces, is_dummy_embedding
from sheets_sync import sync_to_google_sheets, sync_visit_to_google_sheets

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

router = APIRouter(route_class=SecureMediaRoute)

@router.post("/verify-presence")
async def verify_presence(req: VerifyPresenceRequest, request: Request, employee=Depends(get_current_employee)):
    """Verify user presence using face + GPS + WiFi telemetry."""
    user = employee
    clean_email = user["email"]
    if req.email and req.email.strip().lower() != clean_email:
        raise HTTPException(403, "Cannot verify another employee")

    # 2. Face Verification
    is_match, distance = await run_inference(verify_face, req.image, user["face_embedding"])
    if distance == 1.0:
        raise HTTPException(status_code=400, detail="Biometric data mismatch. Please re-enroll your face in Profile.")
    
    if not is_match:
        raise HTTPException(
            status_code=400,
            detail=f"Face verification failed (distance: {distance:.4f}). Please try again."
        )
    # 3. Geofencing & WiFi Validation (Split Field/Desk)
    org_id = user.get("organization_id")
    org_settings = await settings_collection.find_one({"organization_id": org_id}) if org_id else None
    settings = org_settings or {}
    emp_type = user.get("employee_type", "desk")
    
    # Coordinates fallback (Dynamic Database with Env Fallback)
    db_lat = settings.get("office_lat")
    db_long = settings.get("office_long")
    office_lat = float(db_lat) if (db_lat is not None and abs(float(db_lat)) > 0.0001) else float(os.getenv("OFFICE_LAT", 0))
    office_long = float(db_long) if (db_long is not None and abs(float(db_long)) > 0.0001) else float(os.getenv("OFFICE_LONG", 0))
    
    # Radius fallback
    db_radius = settings.get("geofence_radius")
    if db_radius is not None and float(db_radius) > 0.0001:
        radius = float(db_radius)
    else:
        radius = float(os.getenv("GEOFENCE_RADIUS_METERS", 40 if emp_type == "desk" else 150))
        
    # WiFi SSID fallback
    db_wifi_ssid = settings.get("office_wifi_ssid")
    target_ssid = str(db_wifi_ssid).strip() if (db_wifi_ssid and str(db_wifi_ssid).strip() != "") else os.getenv("OFFICE_WIFI_SSID", "")
    
    # WiFi BSSID fallback
    db_wifi_bssid = settings.get("office_wifi_bssid")
    target_bssid = str(db_wifi_bssid).strip() if (db_wifi_bssid and str(db_wifi_bssid).strip() != "") else os.getenv("OFFICE_WIFI_BSSID", "")
    
    # Timezone offset fallback
    tz_offset = int(settings.get("timezone_offset", 330))

    # Haversine distance
    import math
    dlat = math.radians(req.lat - office_lat)
    dlon = math.radians(req.long - office_long)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(office_lat)) * math.cos(math.radians(req.lat)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    dist_meters = 6371000 * c

    if dist_meters > radius:
        raise HTTPException(
            status_code=403,
            detail=f"You are {dist_meters:.1f}m away from office. Must be within {radius:.1f}m."
        )

    if emp_type != "field":
        clean_req_ssid = req.wifi_ssid.strip().strip('"').strip("'").lower() if req.wifi_ssid else ""
        clean_target_ssid = target_ssid.strip().strip('"').strip("'").lower() if target_ssid else ""
        if clean_target_ssid and clean_req_ssid and clean_req_ssid != clean_target_ssid:
             raise HTTPException(status_code=403, detail=f"Must be connected to Office WiFi: {target_ssid}")
        if target_bssid and req.wifi_bssid and req.wifi_bssid.strip().lower() != target_bssid.strip().lower():
             raise HTTPException(status_code=403, detail="Must be connected to Office WiFi Access Point (BSSID mismatch).")

    # 5. Determine check-in or check-out (Localized)
    today_start_utc = get_today_start(tz_offset)
    user_id_str = str(user["_id"])
    emp_email = user.get("email", req.email)
    last_log = await attendance_logs_collection.find_one(
        {
            "$or": [
                {"user_id": user_id_str},
                {"user_id": emp_email},
                {"email": emp_email}
            ],
            "timestamp": {"$gte": today_start_utc}
        },
        sort=[("timestamp", -1)]
    )
    attendance_type = "check-out" if (last_log and last_log.get("type") == "check-in") else "check-in"

    # 6. Log Attendance
    log = {
        "user_id": user_id_str,
        "email": emp_email,
        "timestamp": datetime.now(timezone.utc),
        "type": attendance_type,
        "status": "SUCCESS",
        "location": {"lat": req.get_lat(), "long": req.get_long()},
        "location_name": req.address or "Verified Zone",
        "distance_meters": dist_meters,
        "wifi_info": {"bssid": req.wifi_bssid, "strength": req.wifi_strength},
        "face_confidence": float(distance),
        "device_id": req.device_id,
        "organization_id": str(user.get("organization_id")) if user.get("organization_id") else None,
    }
    await attendance_logs_collection.insert_one(log)
    await employees_collection.update_one(
        {"_id": user["_id"]},
        {"$set": {
            "checked_in": (attendance_type == "check-in"),
            "last_attendance_type": attendance_type,
            "last_attendance_time": log["timestamp"]
        }}
    )

    return {
        "status": "success",
        "type": attendance_type,
        "current_status": attendance_type, # Provide immediate state for UI sync
        "message": f"{attendance_type.replace('-', ' ').title()} recorded at {log['timestamp'].strftime('%I:%M %p')}",
        "time": str(log["timestamp"]),
        "distance_from_office": f"{dist_meters:.1f}m",
    }


@router.post("/smart-attendance")
async def smart_attendance(req: VerifyPresenceRequest, request: Request, background_tasks: BackgroundTasks, employee=Depends(get_current_employee)):
    """
    Unified endpoint for Enterprise Smart Attendance:
    - DESK: Strict WiFi (80%) + Office Geofence (4m).
    - FIELD: Bypass WiFi. Validate Territory (500m default) OR OTP.
    - Both: Face Liveness/Match + Mock Detection.
    - SUPERADMIN: Bypass all security restrictions for testing/management.
    """
    try:
        # 0. Global Telemetry Defaults (Fetched later per org)
        office_lat = 0
        office_long = 0
        radius = 40
        wifi_pct = 0
        office_wifi_ssid = None
        
        user = employee
        clean_email = employee["email"]
        requested_email = (req.email or "").strip().lower()
        if requested_email not in {"", "unknown", "auto", "smart@auto.com", clean_email}:
            raise HTTPException(403, "Cannot mark attendance for another employee")
        if req.organization_id and req.organization_id != employee.get("organization_id"):
            raise HTTPException(403, "Organization mismatch")
        is_admin_user = False
        is_reviewer = False
        is_superadmin = False

        # Fetch settings.
        org_id = user.get("organization_id")
        settings = await get_effective_org_settings(org_id)
        org_settings = settings
        
        emp_type = user.get("employee_type", "desk")
        
        # Coordinates fallback (Dynamic Database with Env Fallback)
        db_lat = settings.get("office_lat")
        db_long = settings.get("office_long")
        office_lat = float(db_lat) if (db_lat is not None and abs(float(db_lat)) > 0.0001) else float(os.getenv("OFFICE_LAT", 0))
        office_long = float(db_long) if (db_long is not None and abs(float(db_long)) > 0.0001) else float(os.getenv("OFFICE_LONG", 0))
        
        # Radius fallback
        db_radius = settings.get("geofence_radius")
        if db_radius is not None and float(db_radius) > 0.0001:
            radius = float(db_radius)
        else:
            radius = float(os.getenv("GEOFENCE_RADIUS_METERS", 40 if emp_type == "desk" else 150))
            
        # WiFi SSID fallback
        db_wifi_ssid = settings.get("office_wifi_ssid")
        office_wifi_ssid = str(db_wifi_ssid).strip() if (db_wifi_ssid and str(db_wifi_ssid).strip() != "") else os.getenv("OFFICE_WIFI_SSID", "")
        
        # WiFi BSSID fallback
        db_wifi_bssid = settings.get("office_wifi_bssid")
        office_wifi_bssid = str(db_wifi_bssid).strip() if (db_wifi_bssid and str(db_wifi_bssid).strip() != "") else os.getenv("OFFICE_WIFI_BSSID", "")

        role = str(user.get("employee_type") or user.get("role") or "desk").lower()
        attendance_type = "check-in"
        if req.punch_type in ["out", "check-out", "checkout"] or req.intended_type in ["check-out", "out", "checkout"]:
            attendance_type = "check-out"
        elif req.intended_type:
            attendance_type = req.intended_type
        elif req.punch_type:
            attendance_type = req.punch_type
        logger.info(f"Processing {attendance_type.replace('-', ' ').title()} for {user['email']} (Role: {role}, Superadmin: {is_superadmin})")

        # Session Validation
        tz_offset = 330 # Default IST
        if org_settings:
            tz_offset = org_settings.get("timezone_offset", 330)
        today_start_utc = get_today_start(tz_offset)
        user_id_str = str(user["_id"])
        emp_email = user.get("email", clean_email)

        # --- Query 1: All-time last log (used ONLY for auto-closing stale sessions) ---
        alltime_last_log = await attendance_logs_collection.find_one(
            {
                "$or": [
                    {"user_id": user_id_str},
                    {"user_id": emp_email},
                    {"email": emp_email}
                ]
            },
            sort=[("timestamp", -1)]
        )

        # AUTO-CLOSE STALE SESSIONS (uses all-time log to detect yesterday's open session)
        if alltime_last_log and alltime_last_log.get("type") == "check-in":
            last_ts = alltime_last_log.get("timestamp")
            if last_ts and last_ts.tzinfo is None:
                last_ts = last_ts.replace(tzinfo=timezone.utc)
            if last_ts and last_ts < today_start_utc:
                local_ts = last_ts + timedelta(minutes=tz_offset)
                if local_ts.hour < 23:
                    local_end = local_ts.replace(hour=23, minute=59, second=59, microsecond=0)
                    auto_checkout_time = local_end - timedelta(minutes=tz_offset)
                else:
                    auto_checkout_time = last_ts + timedelta(seconds=1)
                auto_checkout_log = {
                    "user_id": user_id_str,
                    "email": emp_email,
                    "type": "check-out",
                    "timestamp": auto_checkout_time,
                    "lat": alltime_last_log.get("lat", 0),
                    "long": alltime_last_log.get("long", 0),
                    "location_name": "Auto-closed (missed checkout)",
                    "wifi_bssid": "",
                    "wifi_ssid": "",
                    "method": "system_auto_close",
                    "organization_id": str(org_id) if org_id else None,
                }
                await attendance_logs_collection.insert_one(auto_checkout_log)
                logger.info(f"[AutoClose] Stale session closed for {user['email']}")

        # --- Query 2: Today-only last log (used for state machine validation) ---
        # This is SEPARATE from the all-time query above so that yesterday's logs
        # never cause a false "already checked in" error today.
        last_log = await attendance_logs_collection.find_one(
            {
                "$or": [
                    {"user_id": user_id_str},
                    {"user_id": emp_email},
                    {"email": emp_email}
                ],
                "timestamp": {"$gte": today_start_utc}
            },
            sort=[("timestamp", -1)]
        )

        # State Machine Validation (only uses today's logs)
        if attendance_type == "check-in":
            if last_log and last_log.get("type") == "check-in":
                raise HTTPException(status_code=400, detail="You are already checked in. Please check out first.")
        elif attendance_type == "check-out":
            if not last_log or last_log.get("type") == "check-out":
                raise HTTPException(status_code=400, detail="You haven't checked in today. Please check in first.")

        # 2. Universal Security
        if req.mock_detected and not is_superadmin:
            background_tasks.add_task(
                trigger_alert, "Territory", user.get("email"), user.get("organization_id"), 
                "Mock Location detected during attendance.", "high", {"lat": req.lat, "long": req.long}
            )
            raise HTTPException(status_code=403, detail="Security violation: Location spoofing/virtualization detected. Attendance rejected.")

        # Device Binding Check (Bypass for Superadmin)
        if user.get("device_id") and req.device_id and user["device_id"] != req.device_id:
            if not is_superadmin:
                background_tasks.add_task(
                    trigger_alert, "Identity", user.get("email"), user.get("organization_id"), 
                    f"Device mismatch. Registered: {user.get('device_id')}, Current: {req.device_id}", "medium"
                )
                raise HTTPException(status_code=403, detail="Security Violation: This account is bound to another device. Contact admin.")
            else:
                logger.info(f"Superadmin bypass: Device mismatch ignored.")

        has_valid_face = bool(user.get("face_embedding")) and not is_dummy_embedding(user.get("face_embedding"))
        if not has_valid_face and not is_superadmin and req.image:
            try:
                new_emb = await run_inference(get_face_embedding, req.image)
                if new_emb is not None and not is_dummy_embedding(new_emb):
                    await employees_collection.update_one(
                        {"_id": user["_id"]},
                        {"$set": {
                            "face_embedding": new_emb,
                            "needs_face_enrollment": False
                        }}
                    )
                    user["face_embedding"] = new_emb
                    has_valid_face = True
                    logger.info(f"Auto-enrolled face biometric for {user.get('email')}")
            except Exception as emb_err:
                logger.warning(f"Face auto-enrollment attempt failed: {emb_err}")
            
            if not has_valid_face and not is_superadmin and req.client_type != "flyfield":
                raise HTTPException(status_code=400, detail="Face biometric not enrolled for this user.")
        
        # Face Verification (Bypass for Superadmin, checkout, or if no image provided)
        distance = 0.0
        if req.image and user.get("face_embedding") and attendance_type != "check-out":
            is_match, distance = await run_inference(verify_face, req.image, user["face_embedding"])
            if not is_match and not is_superadmin:
                background_tasks.add_task(
                    trigger_alert, "Identity", user.get("email"), user.get("organization_id"), 
                    f"Face verification failed with confidence distance {distance:.3f}", "medium"
                )
                raise HTTPException(status_code=400, detail="Face verification failed. Please ensure your face is clearly visible.")
        # 3. Geofence/Territory Logic
        check_in_method = CheckInMethod.GPS_TERRITORY
        is_at_office = False
        office_dist = 9999999
        req_lat = req.get_lat()
        req_long = req.get_long()
        if abs(office_lat) > 0.01 or abs(office_long) > 0.01:
            office_dist = calculate_haversine(req_lat, req_long, float(office_lat), float(office_long))
            if office_dist <= float(radius):
                is_at_office = True

        # Check if employee has approved WFH for today
        now_utc = datetime.now(timezone.utc)
        local_now = now_utc + timedelta(minutes=tz_offset)
        today_str = local_now.strftime("%Y-%m-%d")
        today_date_str = today_str

        wfh_approved = False
        wfh_req = await wfh_requests_collection.find_one({
            "employee_email": clean_email,
            "date": today_str,
            "status": "approved"
        })
        if wfh_req:
            wfh_approved = True

        is_wfh_or_hybrid = role in ["wfh", "hybrid", EmployeeType.WFH, EmployeeType.HYBRID]

        # GEOFENCE BYPASS for Superadmin or approved WFH or permanent WFH/hybrid
        if is_superadmin or wfh_approved or is_wfh_or_hybrid:
            logger.info(f"Geofence bypass: superadmin={is_superadmin}, wfh_approved={wfh_approved}, is_wfh_or_hybrid={is_wfh_or_hybrid}")
            wifi_pct = 100
            check_in_method = CheckInMethod.GPS_TERRITORY
            # Check WFH device status if permanent WFH or hybrid and device_id is provided
            if (role in ["wfh", "hybrid", EmployeeType.WFH, EmployeeType.HYBRID]) and req.device_id and not is_superadmin:
                try:
                    await verify_wfh_device(str(user["_id"]), str(org_id), req.device_id)
                except Exception as e:
                    logger.warning(f"WFH device verification failed: {e}")
        else:
            if role == "desk" or role == EmployeeType.DESK:
                if not is_at_office:
                     raise HTTPException(
                          status_code=403, 
                          detail=f"Location error. You are {office_dist:.1f}m away from office (Limit: {radius}m)."
                     )
                
                is_wifi_ok = True
                clean_req_ssid = req.wifi_ssid.strip().strip('"').strip("'").lower() if req.wifi_ssid else ""
                clean_office_ssid = office_wifi_ssid.strip().strip('"').strip("'").lower() if office_wifi_ssid else ""
                if clean_office_ssid and (not clean_req_ssid or clean_req_ssid != clean_office_ssid):
                    is_wifi_ok = False
                if office_wifi_bssid and (not req.wifi_bssid or req.wifi_bssid.strip().lower() != office_wifi_bssid.strip().lower()):
                    is_wifi_ok = False
                
                wifi_pct = 100 if is_wifi_ok else 0
                check_in_method = CheckInMethod.WIFI_GEOFENCE
            else:
                # Field Logic
                if is_at_office:
                    check_in_method = CheckInMethod.WIFI_GEOFENCE
                    wifi_pct = 100
                elif req.otp_used:
                    # OTP verification
                    stored_otp = user.get("gps_otp")
                    otp_expiry = user.get("gps_otp_expiry")
                    if not stored_otp or str(stored_otp) != str(req.otp_code):
                        raise HTTPException(status_code=403, detail="Invalid OTP code.")
                    if otp_expiry and datetime.now(timezone.utc) > (otp_expiry.replace(tzinfo=timezone.utc) if otp_expiry.tzinfo is None else otp_expiry):
                         raise HTTPException(status_code=403, detail="OTP code has expired.")
                    await employees_collection.update_one({"_id": user["_id"]}, {"$set": {"gps_otp": None, "gps_otp_expiry": None}})
                    check_in_method = CheckInMethod.OTP_FALLBACK
                else:
                    # Multi-Tier Territory & Plan verification for Field Agents
                    territory_verified = False
                    verification_reason = ""
                    min_distance = 9999999

                    # Check 0: FlyField Mode (Dynamic on-the-fly field staff bypass fixed plan/territory)
                    if user.get("field_mode") == "flyfield" or req.client_type == "flyfield":
                        territory_verified = True
                        verification_reason = "FlyField dynamic agent — location recorded"

                    # Check 1: Polygon Territory (if configured)
                    t_poly = user.get("territory_polygon")
                    if user.get("territory_type") == "polygon" and t_poly and len(t_poly) >= 3:
                        if is_point_in_polygon(req.lat, req.long, t_poly):
                            territory_verified = True
                            verification_reason = "Within assigned polygon zone"

                    # Check 2: Circular Radius Territory (if configured)
                    t_lat_val = user.get("territory_center_lat")
                    t_lng_val = user.get("territory_center_lng")
                    if not territory_verified and t_lat_val is not None and t_lng_val is not None:
                        t_lat = float(t_lat_val)
                        t_lng = float(t_lng_val)
                        if abs(t_lat) > 0.001 or abs(t_lng) > 0.001:
                            t_radius = float(user.get("territory_radius_meters") or 5000.0)
                            dist = calculate_haversine(req.lat, req.long, t_lat, t_lng)
                            min_distance = min(min_distance, dist)
                            if dist <= t_radius:
                                territory_verified = True
                                verification_reason = f"Within territory radius ({dist:.0f}m from center)"

                    # Check 3: Today's Approved Visit Plan Stops
                    if not territory_verified:
                        plan = await visit_plans_collection.find_one({
                            "$or": [
                                {"employee_id": clean_email},
                                {"employee_id": user.get("employee_id")}
                            ],
                            "date": today_date_str,
                            "status": {"$in": ["approved", "submitted"]}
                        })

                        if plan and plan.get("stops"):
                            for stop in plan["stops"]:
                                s_lat = float(stop.get("place_lat", 0) or 0)
                                s_lng = float(stop.get("place_lng", 0) or 0)
                                if abs(s_lat) > 0.001:
                                    s_dist = calculate_haversine(req.lat, req.long, s_lat, s_lng)
                                    min_distance = min(min_distance, s_dist)
                                    # Allow within 1000m of any approved stop for Start of Day
                                    if s_dist <= 1000:
                                        territory_verified = True
                                        verification_reason = f"At scheduled visit stop '{stop.get('place_name')}' ({s_dist:.0f}m away)"
                                        break

                    # Check 4: Office Fallback (if employee territory is not set up at all)
                    if not territory_verified and (t_lat_val is None or abs(float(t_lat_val)) < 0.001):
                        if abs(office_lat) > 0.001 or abs(office_long) > 0.001:
                            dist = calculate_haversine(req.lat, req.long, office_lat, office_long)
                            min_distance = min(min_distance, dist)
                            if dist <= float(radius):
                                territory_verified = True
                                verification_reason = f"At registered office location ({dist:.0f}m away)"

                    if not territory_verified:
                        dist_msg = f"{min_distance:.0f}m away" if min_distance < 9000000 else "Outside assigned area"
                        raise HTTPException(
                            status_code=403,
                            detail=f"Territory Breach. You are {dist_msg} from your assigned zone or visit stops. Contact admin for a GPS OTP to bypass."
                        )

                    logger.info(f"Field agent {clean_email} attendance verified: {verification_reason}")
                    check_in_method = CheckInMethod.GPS_TERRITORY



        # 4. Log Attendance (Consolidated Enterprise Classification)
        attendance_type = req.intended_type or "check-in" # Default or provided
        settings = org_settings or SystemSettings().model_dump()
        tz_offset = int(settings.get("timezone_offset", 330))
        log_time_utc = datetime.now(timezone.utc)
        current_time_local = log_time_utc + timedelta(minutes=tz_offset)
        today_date_str = current_time_local.strftime("%Y-%m-%d")

        # Check for approved leave for today (applied short leave, half day, etc.)
        approved_leave = None
        try:
            approved_leave = await leave_requests_collection.find_one({
                "$or": [
                    {"employee_id": clean_email},
                    {"email": clean_email},
                    {"employee_id": str(user["_id"])}
                ],
                "status": {"$regex": "^approved$", "$options": "i"},
                "start_date": {"$lte": today_date_str},
                "end_date": {"$gte": today_date_str}
            })
        except Exception as e:
            logger.warning(f"Leave lookup error in attendance: {e}")

        today_start_utc = get_today_start(tz_offset)

        if attendance_type == "check-in":
            classification = classify_attendance_day(
                first_checkin_local=current_time_local,
                last_checkout_local=None,
                worked_hours=0.0,
                settings=settings,
                employee_type=role,
                approved_leave=approved_leave
            )
        else: # check-out
            # Fetch all attendance logs for this employee today (in chronological order)
            today_logs = await attendance_logs_collection.find(
                {
                    "$or": [
                        {"user_id": user_id_str},
                        {"user_id": emp_email},
                        {"email": emp_email}
                    ],
                    "timestamp": {"$gte": today_start_utc}
                }
            ).sort("timestamp", 1).to_list(100)

            checkin_logs = [l for l in today_logs if l.get("type") in ["check-in", "IN"]]
            today_first_checkin = checkin_logs[0] if checkin_logs else None

            # Calculate paired worked hours across all sessions today plus this checkout
            worked_sec = 0.0
            last_in = None
            for l in today_logs:
                l_ts = l["timestamp"]
                if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
                l_type = l.get("type")
                if l_type in ["check-in", "IN"]:
                    last_in = l_ts
                elif l_type in ["check-out", "OUT"] and last_in:
                    worked_sec += max(0.0, (l_ts - last_in).total_seconds())
                    last_in = None

            # Add current checkout session
            if last_in:
                worked_sec += max(0.0, (log_time_utc - last_in).total_seconds())
            elif today_first_checkin:
                # Fallback if intermediate in/out was missing
                f_ts = today_first_checkin["timestamp"]
                if f_ts.tzinfo is None: f_ts = f_ts.replace(tzinfo=timezone.utc)
                worked_sec = max(0.0, (log_time_utc - f_ts).total_seconds())

            if today_first_checkin:
                f_ts = today_first_checkin["timestamp"]
                if f_ts.tzinfo is None: f_ts = f_ts.replace(tzinfo=timezone.utc)
                first_checkin_local = f_ts + timedelta(minutes=tz_offset)
            else:
                first_checkin_local = current_time_local

            worked_hours = round(worked_sec / 3600.0, 2)

            classification = classify_attendance_day(
                first_checkin_local=first_checkin_local,
                last_checkout_local=current_time_local,
                worked_hours=worked_hours,
                settings=settings,
                employee_type=role,
                approved_leave=approved_leave
            )

            # Retroactively update today's first check-in log with final status metadata without corrupting check-in reason
            if today_first_checkin:
                checkin_upd = {
                    "is_half_day": classification["is_half_day"],
                    "half_day_type": classification["half_day_type"],
                    "off_hours": 0.0,
                    "late_off_hours": 0.0,
                    "early_off_hours": 0.0,
                    "is_short_leave": classification["is_short_leave"],
                    "short_leave_type": classification["short_leave_type"],
                    "effective_days": classification["effective_days"]
                }
                if classification.get("is_applied_leave"):
                    checkin_upd["attendance_status"] = classification["status"]
                    checkin_upd["is_applied_leave"] = True
                await attendance_logs_collection.update_one(
                    {"_id": today_first_checkin["_id"]},
                    {"$set": checkin_upd}
                )

        # Determine unified attendance type
        att_type_val = AttendanceType.REMOTE_FIELD
        if wfh_approved or role == EmployeeType.WFH or role == "wfh":
            att_type_val = AttendanceType.WFH
        elif role == EmployeeType.DESK or role == "desk":
            att_type_val = AttendanceType.OFFICE

        # Location name & real physical address resolution
        loc_name_val = req.address
        if not loc_name_val or not any(c.isalpha() for c in str(loc_name_val)):
            if role == EmployeeType.DESK and not wfh_approved:
                loc_name_val = "Office Zone"
            elif wfh_approved or role in [EmployeeType.WFH, "wfh"]:
                loc_name_val = "Remote Workspace"
            else:
                from flyfield import resolve_real_address
                loc_name_val = await resolve_real_address(req.lat, req.long, req.address)

        wfh_session_id = None
        if wfh_approved or role == EmployeeType.WFH or role == "wfh":
            if attendance_type == "check-in":
                active_session = await wfh_sessions_collection.find_one({
                    "employee_id": str(user["_id"]),
                    "organization_id": org_id,
                    "status": "active"
                })
                if not active_session:
                    now_val = datetime.now(timezone.utc)
                    today_val = today_date_str
                    session_doc = {
                        "employee_id": str(user["_id"]),
                        "employee_email": clean_email,
                        "employee_name": user.get("full_name", ""),
                        "organization_id": org_id,
                        "device_id": req.device_id,
                        "date": today_val,
                        "check_in_time": now_val,
                        "check_out_time": None,
                        "status": "active",
                        "check_in_face_verified": True,
                        "face_distance": float(distance),
                        "total_active_seconds": 0,
                        "total_idle_seconds": 0,
                        "productivity_score": 0,
                        "created_at": now_val,
                        "updated_at": now_val,
                        "metadata": {}
                    }
                    result = await wfh_sessions_collection.insert_one(session_doc)
                    wfh_session_id = str(result.inserted_id)
                else:
                    wfh_session_id = str(active_session["_id"])
            elif attendance_type == "check-out":
                session = await wfh_sessions_collection.find_one({
                    "employee_id": str(user["_id"]),
                    "organization_id": org_id,
                    "status": "active"
                })
                if session:
                    now_val = datetime.now(timezone.utc)
                    check_in_time_val = session.get("check_in_time")
                    total_seconds_val = 0
                    if check_in_time_val:
                        if check_in_time_val.tzinfo is None:
                            check_in_time_val = check_in_time_val.replace(tzinfo=timezone.utc)
                        total_seconds_val = int((now_val - check_in_time_val).total_seconds())
                    await wfh_sessions_collection.update_one(
                        {"_id": session["_id"]},
                        {
                            "$set": {
                                "status": "completed",
                                "check_out_time": now_val,
                                "total_active_seconds": total_seconds_val,
                                "updated_at": now_val
                            }
                        }
                    )
                    wfh_session_id = str(session["_id"])

        log = {
            "user_id": str(user["_id"]),
            "email": user["email"],
            "organization_id": user.get("organization_id"),
            "timestamp": log_time_utc,
            "type": attendance_type,
            "attendance_type": att_type_val,
            "attendance_status": classification["status"],
            "is_late": classification["is_late"],
            "is_early_leave": classification["is_early_leave"],
            "is_short_leave": classification["is_short_leave"],
            "is_half_day": classification["is_half_day"],
            "short_leave_type": classification["short_leave_type"],
            "half_day_type": classification["half_day_type"],
            "late_mins": classification["late_mins"],
            "early_exit_mins": classification["early_exit_mins"],
            "late_off_hours": classification["late_off_hours"],
            "early_off_hours": classification["early_off_hours"],
            "off_hours": classification["off_hours"],
            "worked_hours": classification["worked_hours"],
            "required_hours": classification["required_hours"],
            "effective_days": classification["effective_days"],
            "classification_reason": classification["classification_reason"],
            "is_applied_leave": classification["is_applied_leave"],
            "location": {"lat": req.lat, "long": req.long},
            "check_in_method": check_in_method,
            "wifi_confidence": wifi_pct if role == EmployeeType.DESK else 0,
            "confidence_score": float(distance),
            "status": "SUCCESS",
            "location_name": loc_name_val,
            "address": loc_name_val,
            "field_mode": user.get("field_mode"),
            "selfie_verified": True,
            "device_id": req.device_id,
            "mock_location_detected": req.mock_detected
        }
        if wfh_session_id:
            log["wfh_session_id"] = wfh_session_id
        
        await attendance_logs_collection.insert_one(log)
        await employees_collection.update_one(
            {"_id": user["_id"]},
            {"$set": {
                "checked_in": (attendance_type == "check-in"),
                "last_attendance_type": attendance_type,
                "last_attendance_time": log["timestamp"],
                "last_attendance_status": classification["status"]
            }}
        )

        # Trigger Background Sync to Google Sheets
        background_tasks.add_task(sync_to_google_sheets, log)
        
        policy = None
        if (role == EmployeeType.WFH or role == "wfh") and attendance_type == "check-in":
            try:
                org = await organizations_collection.find_one({"_id": ObjectId(org_id)})
                if org:
                    policy = org.get("wfh_policy")
            except Exception as e:
                logger.error(f"Error fetching policy in smart checkin: {e}")
                
            if not policy:
                policy = {
                    "screenshot_interval_minutes": 10,
                    "face_check_interval_minutes": 30,
                    "max_idle_minutes": 20,
                    "productivity_threshold_percent": 60,
                    "working_hours_start": "09:00",
                    "working_hours_end": "18:00",
                    "screenshot_retention_days": 5,
                    "require_face_verification": True,
                    "productive_apps": [],
                    "unproductive_apps": []
                }
                
        return {
            "status": "success",
            "type": attendance_type,
            "current_status": attendance_type, # Provide immediate state for UI sync
            "attendance_status": classification["status"],
            "is_late": classification["is_late"],
            "is_early_leave": classification["is_early_leave"],
            "is_short_leave": classification["is_short_leave"],
            "is_half_day": classification["is_half_day"],
            "short_leave_type": classification["short_leave_type"],
            "half_day_type": classification["half_day_type"],
            "late_mins": classification["late_mins"],
            "early_exit_mins": classification["early_exit_mins"],
            "late_off_hours": classification["late_off_hours"],
            "early_off_hours": classification["early_off_hours"],
            "off_hours": classification["off_hours"],
            "worked_hours": classification["worked_hours"],
            "required_hours": classification["required_hours"],
            "effective_days": classification["effective_days"],
            "classification_reason": classification["classification_reason"],
            "is_applied_leave": classification["is_applied_leave"],
            "message": f"Attendance {attendance_type.replace('-', ' ').title()} Recorded: {classification['status']} ({classification['classification_reason']})",
            "time": log["timestamp"].isoformat(),
            "wifi_confidence": wifi_pct,
            "wfh_session_id": wfh_session_id,
            "wfh_policy": policy
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback
        error_trace = traceback.format_exc()
        logger.error(f"Execution Error in smart_attendance: {e}\n{error_trace}")
        raise HTTPException(status_code=500, detail=f"Internal processing error in attendance module: {type(e).__name__} - {str(e)}")


@router.get("/api/logs/{email}")
async def get_logs(email: str, page: int = 1, limit: int = 50, start_date: Optional[str] = None, end_date: Optional[str] = None, current_user: dict = Depends(get_current_employee)):
    """Get attendance logs for a user with date filters and pagination (JWT Protected)."""
    if current_user["email"].strip().lower() != email.strip().lower():
        raise HTTPException(status_code=403, detail="Forbidden: You can only access your own logs.")

    user = await employees_collection.find_one({
        "$or": [
            {"email": email.strip().lower()},
            {"email": email.strip()}
        ]
    })
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user_id_str = str(user["_id"])
    query = {
        "$or": [
            {"user_id": user_id_str},
            {"user_id": email},
            {"email": email}
        ]
    }
    
    if start_date or end_date:
        time_filter = {}
        if start_date:
            try:
                time_filter["$gte"] = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            except: pass
        if end_date:
            try:
                time_filter["$lte"] = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
            except: pass
        if time_filter:
            query["timestamp"] = time_filter

    total_count = await attendance_logs_collection.count_documents(query)
    skip = (page - 1) * limit
    
    cursor = attendance_logs_collection.find(query).sort("timestamp", -1).skip(skip).limit(limit)
    logs = await cursor.to_list(length=limit)

    # Convert ObjectIDs to strings for JSON serialization
    for log in logs:
        log["_id"] = str(log["_id"])
        if "timestamp" in log and isinstance(log["timestamp"], datetime):
            ts = log["timestamp"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            log["timestamp"] = ts.isoformat().replace("+00:00", "Z")
        elif "timestamp" in log:
            log["timestamp"] = str(log["timestamp"])

    return {
        "logs": logs, 
        "total_count": total_count,
        "page": page,
        "limit": limit,
        "has_more": total_count > (skip + len(logs))
    }


@router.get("/api/analytics/me")
async def get_my_analytics(current_user: dict = Depends(get_current_employee)):
    """Get summarized work hours and stats for the logged-in employee."""
    user_email = current_user.get("email")
    current_status = "check-out" # Default safety
    
    try:
        user = await employees_collection.find_one({"email": user_email})
        if not user:
            return {"email": user_email, "current_status": "check-out", "today_hours": "00:00"}

        user_id_str = str(user["_id"])
        org_id = user.get("organization_id")
        tz_offset = 330
        if org_id:
            org_settings = await settings_collection.find_one({"organization_id": str(org_id) if ObjectId.is_valid(str(org_id)) else org_id})
            if org_settings:
                tz_offset = org_settings.get("timezone_offset", 330)

        today_start = get_today_start(tz_offset)

        # 1. Determine Status FIRST (Highest Priority for UI Sync)
        latest_log = await attendance_logs_collection.find_one(
            {
                "$or": [
                    {"user_id": user_id_str},
                    {"user_id": user_email},
                    {"email": user_email}
                ]
            },
            sort=[("timestamp", -1)]
        )
        
        if latest_log:
            log_type = latest_log.get("type", "check-out")
            if log_type == "check-in":
                l_ts = latest_log["timestamp"]
                if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
                if l_ts < today_start:
                    current_status = "check-out" # Stale
                else:
                    current_status = "check-in"
            else:
                current_status = "check-out"

        week_start = today_start - timedelta(days=today_start.weekday())
        month_start = today_start.replace(day=1)

        # Get logs for calculation
        all_logs = await attendance_logs_collection.find({
            "$or": [
                {"user_id": user_id_str},
                {"user_id": user_email},
                {"email": user_email}
            ],
            "timestamp": {"$gte": week_start}
        }).sort("timestamp", 1).to_list(length=1000)

        async def calculate_hours(logs_subset):
            total_seconds = 0
            current_check_in = None
            for log in logs_subset:
                l_ts = log["timestamp"]
                if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
                
                ltype = log.get("type", "").lower()
                if "in" in ltype:
                    current_check_in = l_ts
                elif "out" in ltype and current_check_in:
                    duration = (l_ts - current_check_in).total_seconds()
                    total_seconds += max(0, duration)
                    current_check_in = None
            
            if current_check_in:
                now_utc = datetime.now(timezone.utc)
                duration = (now_utc - current_check_in).total_seconds()
                total_seconds += max(0, duration)
            return round(total_seconds / 3600.0, 1)

        # Create aware logs for filtering
        aware_logs = []
        for l in all_logs:
            l_ts = l["timestamp"]
            if l_ts.tzinfo is None: l_ts = l_ts.replace(tzinfo=timezone.utc)
            l["aware_ts"] = l_ts
            aware_logs.append(l)

        today_logs = [l for l in aware_logs if l["aware_ts"] >= today_start]
        
        today_hours = await calculate_hours(today_logs)
        week_hours = await calculate_hours(aware_logs)
        
        # Month hours (Separate query for performance)
        month_logs = await attendance_logs_collection.find({
            "$or": [
                {"user_id": user_id_str},
                {"user_id": user_email},
                {"email": user_email}
            ],
            "timestamp": {"$gte": month_start}
        }).to_list(length=2000)
        month_hours = await calculate_hours(month_logs)

        # On-time count
        on_time_count = 0
        user_type = user.get("employee_type", "desk")
        org_doc = None
        if org_id:
            try:
                org_doc = await settings_collection.find_one({"organization_id": str(org_id) if ObjectId.is_valid(str(org_id)) else org_id})
            except Exception: pass

        if user_type == "field":
            start_time_str = (org_doc or {}).get("field_office_start_time", "10:00")
            threshold_mins = int((org_doc or {}).get("field_late_grace_mins") or (org_doc or {}).get("field_late_threshold_mins", 30))
        else:
            start_time_str = (org_doc or {}).get("office_start_time", "09:00")
            threshold_mins = int((org_doc or {}).get("late_grace_mins") or (org_doc or {}).get("late_threshold_mins", 15))

        for log in aware_logs:
            if "in" in log.get("type", "").lower():
                try:
                    start_h, start_m = map(int, start_time_str.split(":"))
                    local_log_time = log["aware_ts"] + timedelta(minutes=tz_offset)
                    limit_time = local_log_time.replace(hour=start_h, minute=start_m, second=0, microsecond=0) + timedelta(minutes=threshold_mins)
                    if local_log_time <= limit_time:
                        on_time_count += 1
                except: pass

        # Latest history
        history_cursor = attendance_logs_collection.find(
            {
                "$or": [
                    {"user_id": user_id_str},
                    {"user_id": user_email},
                    {"email": user_email}
                ]
            }
        ).sort("timestamp", -1).limit(5)
        history = await history_cursor.to_list(length=5)
        for h in history:
            h["_id"] = str(h["_id"])
            if "timestamp" in h and isinstance(h["timestamp"], datetime):
                h["timestamp"] = h["timestamp"].isoformat()


        # NOTE: current_status was already determined by the first latest_log query above.
        # The previous "Absolute Status Sync" block here was a redundant duplicate DB query
        # that re-fetched latest_log and overwrote current_status — removed to save one DB round-trip.
        

        # Resolve per-org WiFi SSID from DB (fallback to global env var)
        # Bug fix: was always returning global OFFICE_WIFI_SSID env var, ignoring per-org DB value
        org_wifi_ssid = os.getenv("OFFICE_WIFI_SSID", "")
        if org_id:
            try:
                _wifi_settings = await settings_collection.find_one(
                    {"organization_id": str(org_id) if ObjectId.is_valid(str(org_id)) else org_id},
                    {"office_wifi_ssid": 1}
                )
                if _wifi_settings and _wifi_settings.get("office_wifi_ssid"):
                    org_wifi_ssid = str(_wifi_settings["office_wifi_ssid"]).strip()
            except Exception:
                pass  # Fallback to env var is fine

        # Upcoming Holidays
        today_str = (datetime.now(timezone.utc) + timedelta(minutes=tz_offset)).strftime("%Y-%m-%d")
        holiday_cursor = holidays_collection.find({
            "$or": [
                {"organization_id": org_id, "date": {"$gte": today_str}},
                {"organization_id": "system_org", "date": {"$gte": today_str}}
            ]
        }).sort("date", 1).limit(5)
        upcoming_holidays_raw = await holiday_cursor.to_list(length=5)
        upcoming_holidays = [
            {"date": h.get("date"), "name": h.get("name"), "type": h.get("holiday_type", "festival")}
            for h in upcoming_holidays_raw
        ]

        # Leave balance
        leave_balance = await calculate_leave_balance(user_email, org_id or "system_org")

        # Today's attendance classification
        today_first_checkin = next((l for l in today_logs if l.get("type") == "check-in"), None)
        today_last_checkout = next((l for l in reversed(today_logs) if l.get("type") == "check-out"), None)
        
        first_in_local = (today_first_checkin["aware_ts"] + timedelta(minutes=tz_offset)) if today_first_checkin else None
        last_out_local = (today_last_checkout["aware_ts"] + timedelta(minutes=tz_offset)) if today_last_checkout else None

        # Check if user has approved leave for today
        today_approved_leave = None
        try:
            today_approved_leave = await leave_requests_collection.find_one({
                "$or": [
                    {"employee_id": user_email.lower().strip()},
                    {"email": user_email.lower().strip()},
                    {"employee_id": str(user["_id"])}
                ],
                "status": {"$regex": "^approved$", "$options": "i"},
                "start_date": {"$lte": today_str},
                "end_date": {"$gte": today_str}
            })
        except Exception:
            pass

        org_settings_doc = await get_effective_org_settings(org_id) if org_id else {}

        today_classification = classify_attendance_day(
            first_checkin_local=first_in_local,
            last_checkout_local=last_out_local,
            worked_hours=today_hours,
            settings=org_settings_doc or {},
            employee_type=user.get("employee_type", "desk"),
            approved_leave=today_approved_leave
        )

        return {
            "email": user["email"],
            "today_hours": today_hours,
            "week_total": week_hours,
            "month_total": month_hours,
            "on_time_count": on_time_count,
            "current_status": current_status,
            "today_attendance_status": today_classification["status"],
            "is_short_leave": today_classification["is_short_leave"],
            "is_half_day": today_classification["is_half_day"],
            "is_late": today_classification["is_late"],
            "is_early_leave": today_classification["is_early_leave"],
            "late_mins": today_classification["late_mins"],
            "early_exit_mins": today_classification["early_exit_mins"],
            "late_off_hours": today_classification["late_off_hours"],
            "early_off_hours": today_classification["early_off_hours"],
            "off_hours": today_classification["off_hours"],
            "classification_reason": today_classification["classification_reason"],
            "is_applied_leave": today_classification["is_applied_leave"],
            "history": history,
            "office_wifi_ssid": org_wifi_ssid,
            "upcoming_holidays": upcoming_holidays,
            "leave_balance": leave_balance
        }
    except Exception as e:
        logger.error(f"Analytics Error: {str(e)}", exc_info=True)
        return {
            "today_hours": "00:00",
            "week_total": "00:00",
            "current_status": current_status,
            "office_wifi_ssid": os.getenv("OFFICE_WIFI_SSID", ""),
            "error": "Internal analytics error"
        }


@router.get("/api/me/sync-status")
async def get_sync_status(last_sync: Optional[str] = None, employee=Depends(get_current_employee)):
    """Check for new data to sync to the mobile app."""
    # Simplified sync payload for pulling latest configurations
    profile = {
        "employee_id": employee.get("employee_id"),
        "full_name": employee.get("full_name"),
        "manager_id": employee.get("manager_id"),
        "territory": employee.get("territory"),
        "employee_type": employee.get("employee_type")
    }
    
    now = datetime.now(timezone.utc)
    
    return {
        "status": "success",
        "latest_profile": profile,
        "server_time": now.isoformat(),
        "requires_full_sync": True
    }


