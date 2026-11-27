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

router = APIRouter(route_class=SecureMediaRoute)

@router.post("/api/wfh/device-info")
async def register_wfh_device(req: dict, employee=Depends(get_current_employee)):
    """
    WFH desktop app registers or updates employee device info.
    Employee must be logged in with normal employee JWT.
    """

    employee_email = employee.get("email")
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")

    if not org_id:
        raise HTTPException(status_code=400, detail="Employee organization_id missing")

    device_id = req.get("device_id")
    if not device_id:
        raise HTTPException(status_code=400, detail="device_id is required")

    existing_device = await wfh_device_info_collection.find_one({
        "employee_id": employee_id,
        "device_id": device_id,
        "organization_id": org_id
    })

    now = datetime.now(timezone.utc)

    device_data = {
        "employee_id": employee_id,
        "employee_email": employee_email,
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "mac_address": req.get("mac_address"),
        "cpu_id": req.get("cpu_id"),
        "os_info": req.get("os_info"),
        "hostname": req.get("hostname"),
        "ip_local": req.get("ip_local"),
        "ip_public": req.get("ip_public"),
        "ram_gb": req.get("ram_gb"),
        "screen_resolution": req.get("screen_resolution"),
        "monitor_count": req.get("monitor_count"),
        "last_seen": now,
        "updated_at": now
    }

    if existing_device:
        await wfh_device_info_collection.update_one(
            {"_id": existing_device["_id"]},
            {"$set": device_data}
        )

        existing_device["_id"] = str(existing_device["_id"])

        return {
            "status": "success",
            "message": "WFH device updated successfully",
            "device_status": existing_device.get("status", "pending"),
            "device_id": device_id
        }

    AUTO_APPROVE = os.getenv("AUTO_APPROVE_WFH_DEVICES", "true").lower() == "true"

    device_data["status"] = "approved" if AUTO_APPROVE else "pending"
    device_data["registered_at"] = now
    device_data["approved_by"] = "auto-approved" if AUTO_APPROVE else None
    device_data["approved_at"] = now if AUTO_APPROVE else None
    device_data["revoked_by"] = None
    device_data["revoked_at"] = None
    device_data["revoke_reason"] = None

    result = await wfh_device_info_collection.insert_one(device_data)

    return {
        "status": "success",
        "message": "WFH device approved." if AUTO_APPROVE else "WFH device registered. Waiting for admin approval.",
        "device_status": "approved" if AUTO_APPROVE else "pending",
        "device_record_id": str(result.inserted_id),
        "device_id": device_id
    }


@router.get("/api/wfh/my-device")
async def get_my_wfh_device(device_id: Optional[str] = None, employee=Depends(get_current_employee)):
    """
    Employee checks current WFH device approval status.
    """

    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")

    query = {
        "employee_id": employee_id,
        "organization_id": org_id
    }

    if device_id:
        query["device_id"] = device_id

    device = await wfh_device_info_collection.find_one(query, sort=[("registered_at", -1)])

    if not device:
        return {
            "registered": False,
            "device_status": "unregistered"
        }

    device["_id"] = str(device["_id"])

    return {
        "registered": True,
        "device": device,
        "device_status": device.get("status", "pending")
    }


@router.post("/api/wfh/checkin")
async def wfh_checkin(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    email = employee.get("email")
    device_id = req.get("device_id")
    face_image = req.get("face_image")

    if not org_id:
        raise HTTPException(status_code=400, detail="Employee organization_id missing")

    await verify_wfh_device(employee_id, org_id, device_id)

    if not face_image:
        raise HTTPException(status_code=400, detail="face_image is required")

    stored_embedding = employee.get("face_embedding")
    if not stored_embedding or is_dummy_embedding(stored_embedding):
        # Auto-enroll real DeepFace biometric if clear face image is provided
        new_emb = await run_inference(get_face_embedding, face_image)
        if new_emb is not None and not is_dummy_embedding(new_emb):
            profile_url = employee.get("profile_image")
            try:
                from upload_security import save_local_image
                profile_url = await save_local_image(face_image, employee, "onboarding", "profiles")
            except Exception as img_err:
                logger.warning(f"WFH auto-enroll profile image save skipped: {img_err}")
            await employees_collection.update_one(
                {"_id": employee["_id"]},
                {"$set": {
                    "face_embedding": new_emb,
                    "needs_face_enrollment": False,
                    "profile_image": profile_url
                }}
            )
            stored_embedding = new_emb
            logger.info(f"Auto-enrolled real DeepFace biometric during WFH checkin for {email}")
        else:
            raise HTTPException(
                status_code=400,
                detail="Face biometric enrollment missing or needs refresh. Please ensure your face is well-lit and clearly visible to complete registration."
            )

    face_ok, face_distance = await run_inference(verify_face, face_image, stored_embedding)

    if not face_ok:
        raise HTTPException(
            status_code=403,
            detail=f"Face verification failed. Distance: {face_distance}"
        )

    active_session = await wfh_sessions_collection.find_one({
        "employee_id": employee_id,
        "organization_id": org_id,
        "status": "active"
    })

    if active_session:
        return {
            "status": "already_active",
            "message": "WFH session already active",
            "session_id": str(active_session["_id"])
        }

    now = datetime.now(timezone.utc)
    # IMPORTANT: Use LOCAL date (with org timezone offset) so it matches
    # the stale-session check in /api/wfh/session/active which uses local date.
    # Using UTC date here caused instant "stale session" closure for IST users
    # between midnight and 5:30 AM local time.
    tz_offset_mins = 330  # default IST
    try:
        org_settings = await settings_collection.find_one({"organization_id": org_id}) if org_id else None
        if org_settings:
            tz_offset_mins = org_settings.get("timezone_offset", 330)
    except Exception:
        pass
    local_now = now + timedelta(minutes=tz_offset_mins)
    today = local_now.strftime("%Y-%m-%d")

    session_doc = {
        "employee_id": employee_id,
        "employee_email": email,
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "date": today,
        "check_in_time": now,
        "check_out_time": None,
        "status": "active",
        "check_in_face_verified": True,
        "face_distance": face_distance,
        "total_active_seconds": 0,
        "total_idle_seconds": 0,
        "productivity_score": 0,
        "created_at": now,
        "updated_at": now,
        "metadata": req.get("metadata", {})
    }

    result = await wfh_sessions_collection.insert_one(session_doc)
    session_id = str(result.inserted_id)

    attendance_doc = {
        "user_id": employee_id,
        "email": email,
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "timestamp": now,
        "type": "check-in",
        "status": "Present",
        "selfie_verified": True,
        "face_confidence": face_distance,
        "device_id": device_id,
        "source": "wfh_desktop",
        "wfh_session_id": session_id,
        "attendance_type": "wfh",
        "check_in_method": "wfh_desktop",
        "created_at": now
    }

    await attendance_logs_collection.insert_one(attendance_doc)
    await employees_collection.update_one(
        {"_id": employee["_id"]},
        {"$set": {"checked_in": True, "last_attendance_type": "wfh_checkin", "last_attendance_time": now}}
    )


    policy = None
    try:
        org = await organizations_collection.find_one({"_id": ObjectId(org_id)})
        if org:
            policy = org.get("wfh_policy")
    except Exception as e:
        logger.error(f"Error fetching policy in checkin: {e}")
        
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
        "message": "WFH check-in successful",
        "session_id": session_id,
        "check_in_time": now.isoformat(),
        "face_verified": True,
        "wfh_policy": policy
    }


@router.post("/api/wfh/checkout")
async def wfh_checkout(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    email = employee.get("email")
    device_id = req.get("device_id")
    session_id = req.get("session_id")

    if not org_id:
        raise HTTPException(status_code=400, detail="Employee organization_id missing")

    # Don't block checkout on device verification — the session was already
    # validated at check-in. Blocking here traps users in permanent active sessions.

    query = {
        "employee_id": employee_id,
        "organization_id": org_id,
        "status": "active"
    }

    if session_id:
        if not ObjectId.is_valid(session_id):
            raise HTTPException(status_code=400, detail="Invalid session_id")
        query["_id"] = ObjectId(session_id)

    session = await wfh_sessions_collection.find_one(query)

    if not session:
        raise HTTPException(status_code=404, detail="No active WFH session found")

    now = datetime.now(timezone.utc)
    check_in_time = session.get("check_in_time")

    total_seconds = 0
    if check_in_time:
        if check_in_time.tzinfo is None:
            check_in_time = check_in_time.replace(tzinfo=timezone.utc)
        total_seconds = int((now - check_in_time).total_seconds())

    # SERVER-SIDE PRODUCTIVITY VALIDATION
    # 1. Fetch Policy
    policy = None
    try:
        org = await organizations_collection.find_one({"_id": ObjectId(org_id)})
        if org:
            policy = org.get("wfh_policy")
    except Exception as e:
        logger.error(f"Error fetching policy in checkout validation: {e}")
        
    # 2. Activity Recomputation
    activity_ratio = 0.0
    active_ratio = 1.0
    try:
        activity_cursor = wfh_activity_collection.find({"session_id": str(session["_id"])})
        activities = await activity_cursor.to_list(length=1000)
        
        total_keystrokes = sum(a.get("keystrokes", 0) for a in activities)
        total_clicks = sum(a.get("mouse_clicks", 0) for a in activities)
        total_scrolls = sum(a.get("scroll_events", 0) for a in activities)
        total_active_sec = sum(a.get("active_seconds", 0) for a in activities)
        total_idle_sec = sum(a.get("idle_seconds", 0) for a in activities)
        
        total_rec_seconds = total_active_sec + total_idle_sec
        if total_rec_seconds > 0:
            active_ratio = float(total_active_sec) / total_rec_seconds
            
        total_events = total_keystrokes + total_clicks + total_scrolls
        expected_baseline_events = max(1.0, (total_rec_seconds / 300.0) * 250.0)
        activity_ratio = min(1.0, float(total_events) / expected_baseline_events)
    except Exception as e:
        logger.error(f"Error recomputing activity ratio: {e}")
        active_ratio = 1.0
        activity_ratio = 0.5

    # 3. App Usage Recomputation
    app_productivity_ratio = 0.5
    try:
        app_cursor = wfh_app_usage_collection.find({"session_id": str(session["_id"])})
        usages = await app_cursor.to_list(length=1000)
        
        productive_sec = 0.0
        neutral_sec = 0.0
        unproductive_sec = 0.0
        
        # Helper classification inside checkout block
        def get_local_app_category(app_name: str, org_policy: dict = None) -> str:
            if not app_name or app_name == "unknown":
                return "neutral"
            app_name_lower = app_name.lower().replace(".exe", "").strip()
            productive_keywords = ["vscode", "code", "pycharm", "idea", "figma", "notion", "slack", "teams", "excel", "winword", "word", "powerpnt", "outlook", "chrome", "firefox"]
            unproductive_keywords = ["steam", "netflix", "youtube", "instagram", "facebook", "spotify", "discord", "game", "twitter", "reddit"]
            if org_policy:
                custom_productive = org_policy.get("productive_apps", [])
                custom_unproductive = org_policy.get("unproductive_apps", [])
                if any(app.lower() in app_name_lower for app in custom_productive):
                    return "productive"
                if any(app.lower() in app_name_lower for app in custom_unproductive):
                    return "unproductive"
            if any(keyword in app_name_lower for keyword in productive_keywords):
                return "productive"
            if any(keyword in app_name_lower for keyword in unproductive_keywords):
                return "unproductive"
            return "neutral"
            
        for usage in usages:
            apps = usage.get("apps", [])
            for app in apps:
                app_name = app.get("name", "unknown")
                dur = float(app.get("duration_seconds", 0))
                category = get_local_app_category(app_name, policy)
                if category == "productive":
                    productive_sec += dur
                elif category == "neutral":
                    neutral_sec += dur
                else:
                    unproductive_sec += dur
                    
        total_app_sec = productive_sec + neutral_sec + unproductive_sec
        if total_app_sec > 0:
            app_productivity_ratio = (productive_sec * 1.0 + neutral_sec * 0.6 + unproductive_sec * 0.1) / total_app_sec
    except Exception as e:
        logger.error(f"Error recomputing app productivity ratio: {e}")

    # 4. Face Presence Recomputation
    face_ratio = 1.0
    try:
        face_cursor = wfh_face_checks_collection.find({"session_id": str(session["_id"])})
        face_checks = await face_cursor.to_list(length=1000)
        if len(face_checks) > 0:
            passed_checks = sum(1 for check in face_checks if check.get("passed", False))
            face_ratio = float(passed_checks) / len(face_checks)
    except Exception as e:
        logger.error(f"Error recomputing face ratio: {e}")

    # 5. Combined Score
    w_app = 0.45
    w_activity = 0.35
    w_face = 0.20
    app_score = active_ratio * app_productivity_ratio
    
    server_score = (app_score * w_app + activity_ratio * w_activity + face_ratio * w_face) * 100.0
    server_score = round(max(0.0, min(100.0, server_score)), 2)
    
    # 6. Flag Alert on Discrepancy (> 15 delta)
    client_reported_score = session.get("productivity_score", 0.0)
    delta = abs(client_reported_score - server_score)
    
    if delta > 15.0:
        alert_doc = {
            "employee_id": employee_id,
            "employee_email": email,
            "employee_name": employee.get("full_name", ""),
            "organization_id": org_id,
            "session_id": str(session["_id"]),
            "type": "WFH_SCORE_DISCREPANCY",
            "timestamp": now,
            "severity": "high",
            "status": "pending",
            "details": f"Productivity score tampering risk. Client reported: {client_reported_score:.1f}, Server validated: {server_score:.1f} (Delta: {delta:.1f})",
            "created_at": now
        }
        alert_res = await wfh_alerts_collection.insert_one(alert_doc)
        
        # Mirror alert to unified alerts collection
        try:
            await alerts_collection.insert_one({
                "organization_id": org_id,
                "employee_id": employee_id,
                "employee_name": employee.get("full_name", ""),
                "type": "Compliance",
                "severity": "high",
                "status": "pending",
                "detail": f"WFH Productivity discrepancy: Client ({client_reported_score:.0f}%) vs Server ({server_score:.0f}%)",
                "timestamp": now,
                "metadata": {
                    "source": "wfh_desktop",
                    "wfh_alert_id": str(alert_res.inserted_id),
                    "session_id": str(session["_id"])
                }
            })
        except Exception as err_alert:
            logger.error(f"Failed to mirror WFH discrepancy alert: {err_alert}")

    # Set completed with server-verified productivity score
    await wfh_sessions_collection.update_one(
        {"_id": session["_id"]},
        {
            "$set": {
                "status": "completed",
                "check_out_time": now,
                "total_active_seconds": total_seconds,
                "verified_productivity_score": server_score,
                "updated_at": now
            }
        }
    )

    attendance_doc = {
        "user_id": employee_id,
        "email": email,
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "timestamp": now,
        "type": "check-out",
        "status": "Present",
        "selfie_verified": True,
        "device_id": device_id,
        "source": "wfh_desktop",
        "wfh_session_id": str(session["_id"]),
        "attendance_type": "wfh",
        "check_in_method": "wfh_desktop",
        "total_seconds": total_seconds,
        "created_at": now
    }

    await attendance_logs_collection.insert_one(attendance_doc)
    await employees_collection.update_one(
        {"_id": employee["_id"]},
        {"$set": {"checked_in": False, "last_attendance_type": "wfh_checkout", "last_attendance_time": now}}
    )


    return {
        "status": "success",
        "message": "WFH check-out successful",
        "session_id": str(session["_id"]),
        "check_out_time": now.isoformat(),
        "total_seconds": total_seconds,
        "verified_productivity_score": server_score
    }


@router.get("/api/wfh/session/active")
async def get_active_wfh_session(employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")

    # Timezone-aware stale session closing
    tz_offset = 330 # default IST
    org_settings = await settings_collection.find_one({"organization_id": org_id}) if org_id else None
    if org_settings:
        tz_offset = org_settings.get("timezone_offset", 330)

    local_now = datetime.now(timezone.utc) + timedelta(minutes=tz_offset)
    today_str = local_now.strftime("%Y-%m-%d")

    session = await wfh_sessions_collection.find_one({
        "employee_id": employee_id,
        "organization_id": org_id,
        "status": "active"
    })

    if session:
        # If the session is from a previous day, auto-close it as stale!
        # But add a grace period: only close if the session is >20 hours old
        # to prevent midnight-boundary race conditions (check-in at 23:59,
        # session/active polled at 00:00 → wrongly killed as stale).
        session_date = session.get("date", "")
        stale_check_in_time = session.get("check_in_time")
        session_age_hours = 0
        if stale_check_in_time:
            if stale_check_in_time.tzinfo is None:
                stale_check_in_time = stale_check_in_time.replace(tzinfo=timezone.utc)
            session_age_hours = (datetime.now(timezone.utc) - stale_check_in_time).total_seconds() / 3600
        if session_date != today_str and session_age_hours > 20:
            now_utc = datetime.now(timezone.utc)
            check_in_time = session.get("check_in_time")
            total_seconds = 0
            if check_in_time:
                if check_in_time.tzinfo is None:
                    check_in_time = check_in_time.replace(tzinfo=timezone.utc)
                total_seconds = int((now_utc - check_in_time).total_seconds())

            await wfh_sessions_collection.update_one(
                {"_id": session["_id"]},
                {
                    "$set": {
                        "status": "completed",
                        "check_out_time": now_utc,
                        "total_active_seconds": min(total_seconds, 28800), # Cap at 8 hours if stale
                        "updated_at": now_utc
                    }
                }
            )
            session = None

    if not session:
        return {
            "active": False,
            "session": None
        }

    session["_id"] = str(session["_id"])
    
    return {
        "active": True,
        "session": session,
        "server_time": datetime.now(timezone.utc).isoformat()
    }


@router.get("/api/wfh/productivity")
async def get_my_wfh_productivity(
    date: Optional[str] = None,
    limit: int = 100,
    employee=Depends(get_current_employee)
):
    clean_email = employee.get("email").strip().lower()
    query = {"employee_email": clean_email}
    if date:
        query["date"] = date
    cursor = wfh_productivity_collection.find(query).sort("timestamp", 1).limit(limit)
    data = await cursor.to_list(length=limit)
    for item in data:
        item["_id"] = str(item["_id"])
    return data


@router.get("/api/wfh/apps")
async def get_my_wfh_apps(
    date: Optional[str] = None,
    limit: int = 100,
    employee=Depends(get_current_employee)
):
    clean_email = employee.get("email").strip().lower()
    query = {"employee_email": clean_email}
    if date:
        query["date"] = date
    cursor = wfh_app_usage_collection.find(query).sort("timestamp", -1).limit(limit)
    data = await cursor.to_list(length=limit)
    for item in data:
        item["_id"] = str(item["_id"])
    return data


@router.get("/api/wfh/screenshots")
async def get_my_screenshots(
    request: Request,
    date: Optional[str] = None,
    limit: int = 100,
    employee=Depends(get_current_employee)
):
    email = employee.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    query = {"employee_email": email}
    if date:
        query["date"] = date
        
    cursor = wfh_screenshots_collection.find(query).sort("timestamp", -1).limit(limit)
    screenshots = await cursor.to_list(length=limit)
    
    for item in screenshots:
        item["_id"] = str(item["_id"])
        item["image_url"] = get_screenshot_display_url(item.get("image_url", ""), request)
        if item.get("thumbnail_url"):
            item["thumbnail_url"] = get_screenshot_display_url(item["thumbnail_url"], request)
        
    return {"screenshots": screenshots}


@router.post("/api/wfh/screenshot")
async def wfh_submit_screenshot(req: dict, background_tasks: BackgroundTasks, request: Request, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    device_id = req.get("device_id")

    await verify_wfh_device(employee_id, org_id, device_id)

    from upload_security import decode_image_payload, owned_upload
    image_url = req.get("image_url")
    if req.get("upload_id"):
        record = await owned_upload(req["upload_id"], employee, purpose="screenshot")
        image_url = record["url"]
    elif isinstance(image_url, str) and not image_url.startswith(("http:", "https:", "/")):
        image_bytes, media_type = await run_in_threadpool(decode_image_payload, image_url)
        extension = {"image/png": ".png", "image/webp": ".webp", "image/jpeg": ".jpg"}[media_type]
        image_url = await upload_file_to_storage(image_bytes, uuid.uuid4().hex + extension, "wfh_view",
                                                 owner=employee, purpose="screenshot")
    else:
        raise HTTPException(400, "Submit an image or an owned upload_id; arbitrary URLs are not accepted")

    server_now = datetime.now(timezone.utc)
    timestamp_str = req.get("timestamp")
    if timestamp_str:
        try:
            if timestamp_str.endswith('Z'):
                timestamp_str = timestamp_str[:-1] + '+00:00'
            now = datetime.fromisoformat(timestamp_str)
        except Exception:
            now = server_now
    else:
        now = server_now

    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "session_id": req.get("session_id"),
        "date": now.strftime("%Y-%m-%d"),
        "timestamp": now,
        "image_url": image_url,
        "thumbnail_url": None,
        "active_app": req.get("active_app"),
        "active_window": req.get("active_window"),
        "flagged": req.get("flagged", False),
        "flag_reason": req.get("flag_reason"),
        "created_at": server_now
    }

    result = await wfh_screenshots_collection.insert_one(doc)
    screenshot_id = str(result.inserted_id)

    # Queue background threat scan using OCR parser
    background_tasks.add_task(
        scan_screenshot_ocr_threats,
        screenshot_id,
        image_url,
        employee_id,
        org_id,
        employee.get("email"),
        device_id,
        req.get("session_id")
    )

    return {
        "status": "success",
        "message": "WFH screenshot saved",
        "screenshot_id": screenshot_id
    }


@router.post("/api/wfh/activity")
async def wfh_submit_activity(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    device_id = req.get("device_id")

    await verify_wfh_device(employee_id, org_id, device_id)

    now = datetime.now(timezone.utc)

    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "session_id": req.get("session_id"),
        "timestamp": now,
        "period_minutes": req.get("period_minutes", 5),
        "keystrokes": req.get("keystrokes", 0),
        "mouse_clicks": req.get("mouse_clicks", 0),
        "mouse_distance_px": req.get("mouse_distance_px", 0),
        "scroll_events": req.get("scroll_events", 0),
        "idle_seconds": req.get("idle_seconds", 0),
        "active_seconds": req.get("active_seconds", 0),
        "created_at": now
    }

    result = await wfh_activity_collection.insert_one(doc)

    return {
        "status": "success",
        "message": "WFH activity saved",
        "activity_id": str(result.inserted_id)
    }


@router.post("/api/wfh/app-usage")
async def wfh_submit_app_usage(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    device_id = req.get("device_id")

    await verify_wfh_device(employee_id, org_id, device_id)

    now = datetime.now(timezone.utc)

    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "session_id": req.get("session_id"),
        "timestamp": now,
        "date": req.get("date", now.strftime("%Y-%m-%d")),
        "apps": req.get("apps", []),
        "created_at": now
    }

    result = await wfh_app_usage_collection.insert_one(doc)

    return {
        "status": "success",
        "message": "WFH app usage saved",
        "app_usage_id": str(result.inserted_id)
    }


@router.post("/api/wfh/productivity")
async def wfh_submit_productivity(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    device_id = req.get("device_id")

    await verify_wfh_device(employee_id, org_id, device_id)

    now = datetime.now(timezone.utc)

    score = float(req.get("score", 0))
    score = max(0, min(score, 100))

    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "session_id": req.get("session_id"),
        "timestamp": now,
        "date": req.get("date", now.strftime("%Y-%m-%d")),
        "score": score,
        "breakdown": req.get("breakdown", {}),
        "created_at": now
    }

    result = await wfh_productivity_collection.insert_one(doc)

    if req.get("session_id") and ObjectId.is_valid(req.get("session_id")):
        await wfh_sessions_collection.update_one(
            {"_id": ObjectId(req.get("session_id")), "employee_id": employee_id},
            {
                "$set": {
                    "productivity_score": score,
                    "updated_at": now
                }
            }
        )

    return {
        "status": "success",
        "message": "WFH productivity saved",
        "productivity_id": str(result.inserted_id),
        "score": score
    }


@router.post("/api/wfh/alert")
async def wfh_submit_alert(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    device_id = req.get("device_id")

    await verify_wfh_device(employee_id, org_id, device_id)

    now = datetime.now(timezone.utc)

    alert_type = req.get("type")
    if not alert_type:
        raise HTTPException(status_code=400, detail="alert type is required")

    severity = req.get("severity", "medium")

    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "session_id": req.get("session_id"),
        "type": alert_type,
        "timestamp": now,
        "image_url": req.get("image_url"),
        "severity": severity,
        "status": "pending",
        "details": req.get("details"),
        "metadata": req.get("metadata", {}),
        "created_at": now
    }

    result = await wfh_alerts_collection.insert_one(doc)

    # Also mirror WFH alert into existing unified alerts collection
    try:
        # Map WFH alert types to admin-friendly categories
        wfh_type_map = {
            "WFH_IDLE_EXTENDED": "Productivity",
            "WFH_IDENTITY_MISMATCH": "Identity",
            "Continuous Auth Failure": "Identity",
            "WFH_FACE_CHECK_FAILURE": "Identity",
            "WFH_FAKE_WEBCAM": "Identity",
            "WFH_SCREENSHOT_THREAT": "Compliance",
            "WFH_SUSPICIOUS_APP": "Compliance",
        }
        mapped_type = wfh_type_map.get(alert_type, "Productivity")

        # Create a clean, human-readable detail for the admin
        raw_detail = req.get('details', '')
        friendly_detail = raw_detail if raw_detail else f"{alert_type} detected for this employee."

        await alerts_collection.insert_one({
            "organization_id": org_id,
            "employee_id": employee_id,
            "employee_name": employee.get("full_name", ""),
            "type": mapped_type,
            "severity": severity,
            "status": "pending",
            "detail": friendly_detail,
            "timestamp": now,
            "metadata": {
                "source": "wfh_desktop",
                "wfh_alert_id": str(result.inserted_id),
                "wfh_alert_type": alert_type,
                "device_id": device_id,
                "session_id": req.get("session_id")
            }
        })
    except Exception as e:
        logger.error(f"Failed to mirror WFH alert into alerts collection: {e}")

    return {
        "status": "success",
        "message": "WFH alert saved",
        "alert_id": str(result.inserted_id)
    }


@router.post("/api/wfh/meeting")
async def wfh_submit_meeting(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    device_id = req.get("device_id")

    await verify_wfh_device(employee_id, org_id, device_id)

    now = datetime.now(timezone.utc)

    platform = req.get("platform")
    if not platform:
        raise HTTPException(status_code=400, detail="platform is required")

    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "device_id": device_id,
        "session_id": req.get("session_id"),
        "platform": platform,
        "start_time": req.get("start_time"),
        "end_time": req.get("end_time"),
        "duration_minutes": req.get("duration_minutes"),
        "date": req.get("date", now.strftime("%Y-%m-%d")),
        "created_at": now
    }

    result = await wfh_meetings_collection.insert_one(doc)

    return {
        "status": "success",
        "message": "WFH meeting saved",
        "meeting_id": str(result.inserted_id)
    }


@router.get("/api/wfh/team/active")
async def wfh_get_active_team(employee=Depends(get_current_employee)):
    org_id = employee.get("organization_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Employee organization_id missing")
    
    # Find active WFH sessions in the same organization, excluding current employee
    cursor = wfh_sessions_collection.find({
        "organization_id": org_id,
        "status": "active",
        "employee_email": {"$ne": employee.get("email")}
    })
    sessions = await cursor.to_list(length=100)
    
    team_members = []
    for s in sessions:
        team_members.append({
            "employee_id": s.get("employee_id"),
            "employee_email": s.get("employee_email"),
            "employee_name": s.get("employee_name", ""),
            "check_in_time": s.get("check_in_time").isoformat() if s.get("check_in_time") else None,
            "productivity_score": s.get("productivity_score", 0)
        })
    return team_members


@router.post("/api/wfh/meeting/signal")
async def wfh_post_signal(req: dict, employee=Depends(get_current_employee)):
    sender_email = employee.get("email")
    receiver_email = req.get("receiver_email")
    signal_type = req.get("type") # 'offer', 'answer', 'candidate'
    data = req.get("data")
    
    if not receiver_email or not signal_type or not data:
        raise HTTPException(status_code=400, detail="receiver_email, type, and data are required")
        
    doc = {
        "sender_email": sender_email,
        "receiver_email": receiver_email,
        "type": signal_type,
        "data": data,
        "timestamp": datetime.now(timezone.utc),
        "delivered": False
    }
    
    await wfh_signals_collection.insert_one(doc)
    return {"status": "success", "message": "Signal sent"}


@router.get("/api/wfh/meeting/signal")
async def wfh_get_signals(employee=Depends(get_current_employee)):
    email = employee.get("email")
    
    # Fetch all undelivered signals for current employee
    cursor = wfh_signals_collection.find({
        "receiver_email": email,
        "delivered": False
    }).sort("timestamp", 1)
    
    signals = await cursor.to_list(length=100)
    
    # Mark as delivered
    if signals:
        signal_ids = [s["_id"] for s in signals]
        await wfh_signals_collection.update_many(
            {"_id": {"$in": signal_ids}},
            {"$set": {"delivered": True}}
        )
        
    result = []
    for s in signals:
        result.append({
            "id": str(s["_id"]),
            "sender_email": s.get("sender_email"),
            "type": s.get("type"),
            "data": s.get("data"),
            "timestamp": s.get("timestamp").isoformat() if s.get("timestamp") else None
        })
        
    return result


@router.get("/api/wfh/policy")
async def api_get_wfh_policy(employee = Depends(get_current_employee)):
    org_id = employee.get("organization_id")
    if not org_id:
        raise HTTPException(status_code=400, detail="Employee organization_id missing")
        
    org = await organizations_collection.find_one({"_id": ObjectId(org_id)})
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
        
    policy = org.get("wfh_policy")
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
    return policy


@router.post("/api/wfh/face-check")
async def wfh_face_check(req: dict, employee=Depends(get_current_employee)):
    employee_id = str(employee.get("_id"))
    org_id = employee.get("organization_id")
    
    session_id = req.get("session_id")
    image_base64 = req.get("image_base64")
    if not image_base64 or not employee.get("face_embedding"):
        raise HTTPException(400, "A face image and enrolled face are required")
    from upload_security import decode_image_payload
    await run_in_threadpool(decode_image_payload, image_base64)
    passed, face_score = await run_inference(verify_face, image_base64, employee["face_embedding"])
    failure_reason = "" if passed else "Face verification failed"
    if session_id:
        owned_session = await wfh_sessions_collection.find_one({"_id": ObjectId(session_id),
            "employee_id": employee_id, "organization_id": org_id}) if ObjectId.is_valid(session_id) else None
        if not owned_session:
            raise HTTPException(404, "Session not found")
    
    now = datetime.now(timezone.utc)
    
    image_url = None
    if image_base64:
        import uuid
        import time
        filename = f"face_check_{int(time.time())}_{uuid.uuid4().hex[:8]}.jpg"
        import base64
        try:
            base64_data = image_base64
            if "," in image_base64:
                base64_data = image_base64.split(",", 1)[1]
            from upload_security import decode_image_payload
            image_bytes, _ = await run_in_threadpool(decode_image_payload, image_base64)
            image_url = await upload_file_to_storage(image_bytes, filename, "wfh_face_checks", owner=employee, purpose="face_check")
        except Exception as e:
            logger.error(f"Failed to save face check image: {e}")
            
    doc = {
        "employee_id": employee_id,
        "employee_email": employee.get("email"),
        "employee_name": employee.get("full_name", ""),
        "organization_id": org_id,
        "session_id": session_id,
        "timestamp": now,
        "passed": passed,
        "face_score": face_score,
        "failure_reason": failure_reason,
        "image_url": image_url,
        "created_at": now
    }
    
    result = await wfh_face_checks_collection.insert_one(doc)
    
    # Update active session counters
    if session_id and ObjectId.is_valid(session_id):
        update_op = {
            "$inc": {
                "face_check_count": 1,
                "face_check_passed": 1 if passed else 0
            },
            "$set": {
                "updated_at": now
            }
        }
        await wfh_sessions_collection.update_one(
            {"_id": ObjectId(session_id), "employee_id": employee_id},
            update_op
        )
        
    # If failed, raise alert
    if not passed:
        alert_doc = {
            "employee_id": employee_id,
            "employee_email": employee.get("email"),
            "employee_name": employee.get("full_name", ""),
            "organization_id": org_id,
            "session_id": session_id,
            "type": "WFH_FACE_CHECK_FAILED",
            "timestamp": now,
            "image_url": image_url,
            "severity": "critical" if "fake" in failure_reason.lower() else "high",
            "status": "pending",
            "details": f"WFH face verification failed: {failure_reason}",
            "created_at": now
        }
        alert_res = await wfh_alerts_collection.insert_one(alert_doc)
        
        # Mirror alert to unified alerts collection
        try:
            await alerts_collection.insert_one({
                "organization_id": org_id,
                "employee_id": employee_id,
                "employee_name": employee.get("full_name", ""),
                "type": "Identity",
                "severity": "critical" if "fake" in failure_reason.lower() else "high",
                "status": "pending",
                "detail": f"WFH face check failed: {failure_reason}",
                "timestamp": now,
                "metadata": {
                    "source": "wfh_desktop",
                    "wfh_alert_id": str(alert_res.inserted_id),
                    "session_id": session_id
                }
            })
        except Exception as e:
            logger.error(f"Failed to mirror WFH face-check failure alert: {e}")
            
    return {
        "status": "success",
        "message": "Face check recorded",
        "face_check_id": str(result.inserted_id),
        "passed": passed
    }


@router.get("/api/wfh/commands/pending")
async def get_pending_commands(employee=Depends(get_current_employee)):
    email = employee.get("email")
    if not email:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    cursor = wfh_commands_collection.find({
        "employee_email": email.strip().lower(),
        "status": "pending"
    }).sort("timestamp", 1)
    
    commands = await cursor.to_list(length=100)
    
    result = []
    for c in commands:
        result.append({
            "id": str(c["_id"]),
            "command": c.get("command"),
            "status": c.get("status"),
            "timestamp": c.get("timestamp").isoformat() if c.get("timestamp") else None
        })
        
    return {"commands": result}


@router.post("/api/wfh/commands/{command_id}/complete")
async def complete_command(command_id: str, employee=Depends(get_current_employee)):
    if not ObjectId.is_valid(command_id):
        raise HTTPException(status_code=400, detail="Invalid command ID")
        
    result = await wfh_commands_collection.update_one(
        {"_id": ObjectId(command_id), "employee_email": employee.get("email").strip().lower()},
        {"$set": {"status": "completed", "completed_at": datetime.now(timezone.utc)}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Command not found or unauthorized")
        
    return {"status": "success", "message": "Command marked as completed"}


@router.get("/api/wfh/my-alerts")
async def get_my_wfh_alerts(employee = Depends(get_current_employee)):
    clean_email = employee.get("email").strip().lower()
    cursor = wfh_alerts_collection.find({"employee_email": clean_email}).sort("timestamp", -1)
    alerts = await cursor.to_list(length=100)
    for a in alerts:
        a["_id"] = str(a["_id"])
    return alerts


@router.get("/api/tasks")
async def get_my_tasks(employee = Depends(get_current_employee)):
    email = employee.get("email").strip().lower()
    cursor = tasks_collection.find({"employee_email": email}).sort("created_at", -1)
    tasks = await cursor.to_list(length=200)
    for task in tasks:
        task["_id"] = str(task["_id"])
    return tasks


@router.post("/api/tasks")
async def create_my_task(req: dict, employee = Depends(get_current_employee)):
    email = employee.get("email").strip().lower()
    title = req.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="Task title is required")
    
    now = datetime.now(timezone.utc)
    task_doc = {
        "employee_id": str(employee["_id"]),
        "employee_email": email,
        "organization_id": employee.get("organization_id"),
        "title": title.strip(),
        "description": req.get("description", "").strip(),
        "status": req.get("status", "todo"),
        "priority": req.get("priority", "medium"),
        "worked_minutes": 0,
        "created_at": now,
        "updated_at": now
    }
    
    result = await tasks_collection.insert_one(task_doc)
    task_doc["_id"] = str(result.inserted_id)
    return task_doc


@router.put("/api/tasks/{task_id}")
async def update_my_task(task_id: str, req: dict, employee = Depends(get_current_employee)):
    email = employee.get("email").strip().lower()
    
    task = await tasks_collection.find_one({"_id": ObjectId(task_id), "employee_email": email})
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
        
    update_data = {}
    if "status" in req:
        update_data["status"] = req["status"]
    if "description" in req:
        update_data["description"] = req["description"]
    if "priority" in req:
        update_data["priority"] = req["priority"]
    if "worked_minutes" in req:
        update_data["worked_minutes"] = req["worked_minutes"]
        
    if not update_data:
        return {"status": "success", "message": "No fields to update"}
        
    update_data["updated_at"] = datetime.now(timezone.utc)
    
    await tasks_collection.update_one(
        {"_id": ObjectId(task_id)},
        {"$set": update_data}
    )
    return {"status": "success", "message": "Task updated successfully"}


