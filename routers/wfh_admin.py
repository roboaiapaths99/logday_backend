from upload_security import SecureMediaRoute
from permissions import require_feature, require_any_feature, require_admin_session
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

@router.get("/admin/wfh/devices")
async def admin_list_wfh_devices(
    status: Optional[str] = None,
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    """
    Admin lists all WFH registered devices in their organization.
    """

    org_id = current_admin.organization_id

    query = {}

    if org_id and org_id != "system_org":
        query["organization_id"] = org_id

    if status:
        query["status"] = status

    cursor = wfh_device_info_collection.find(query).sort("registered_at", -1)
    devices = await cursor.to_list(length=1000)

    for device in devices:
        device["_id"] = str(device["_id"])

    return devices


@router.post("/admin/wfh/devices/{device_record_id}/approve")
async def admin_approve_wfh_device(
    device_record_id: str,
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    """
    Admin approves a WFH device.
    """

    if not ObjectId.is_valid(device_record_id):
        raise HTTPException(status_code=400, detail="Invalid device record id")

    query = {"_id": ObjectId(device_record_id)}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    device = await wfh_device_info_collection.find_one(query)

    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    now = datetime.now(timezone.utc)

    await wfh_device_info_collection.update_one(
        {"_id": ObjectId(device_record_id)},
        {
            "$set": {
                "status": "approved",
                "approved_by": current_admin.email,
                "approved_at": now,
                "revoked_by": None,
                "revoked_at": None,
                "revoke_reason": None
            }
        }
    )

    return {
        "status": "success",
        "message": "WFH device approved successfully"
    }


@router.post("/admin/wfh/devices/{device_record_id}/revoke")
async def admin_revoke_wfh_device(
    device_record_id: str,
    req: dict,
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    """
    Admin revokes a WFH device.
    """

    if not ObjectId.is_valid(device_record_id):
        raise HTTPException(status_code=400, detail="Invalid device record id")

    query = {"_id": ObjectId(device_record_id)}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    device = await wfh_device_info_collection.find_one(query)

    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    now = datetime.now(timezone.utc)

    await wfh_device_info_collection.update_one(
        {"_id": ObjectId(device_record_id)},
        {
            "$set": {
                "status": "revoked",
                "revoked_by": current_admin.email,
                "revoked_at": now,
                "revoke_reason": req.get("reason", "Revoked by admin")
            }
        }
    )

    return {
        "status": "success",
        "message": "WFH device revoked successfully"
    }


@router.get("/admin/wfh/stats")
async def admin_wfh_stats(current_admin: Admin = Depends(require_any_feature("wfh_management", "wfh_monitoring"))):
    org_id = current_admin.organization_id
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    query = {}
    if org_id and org_id != "system_org":
        query["organization_id"] = org_id

    try:
        # Run all 4 queries in parallel to minimize latency
        today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        active_sessions_task = wfh_sessions_collection.count_documents({
            **query,
            "status": "active"
        })
        today_alerts_task = wfh_alerts_collection.count_documents({
            **query,
            "status": "pending",
            "timestamp": {"$gte": today_start}
        })
        emp_type_filter = ["wfh", "desk", "office", "hybrid", "all", None]
        wfh_employees_task = employees_collection.count_documents({
            **query,
            "$or": [
                {"employee_type": {"$in": emp_type_filter}},
                {"employee_type": {"$exists": False}}
            ]
        })
        productivity_task = wfh_productivity_collection.find({
            **query,
            "date": today
        }).to_list(length=500)

        active_sessions, today_alerts, wfh_employees, productivity_list = await asyncio.gather(
            active_sessions_task,
            today_alerts_task,
            wfh_employees_task,
            productivity_task,
            return_exceptions=True
        )

        # Handle any individual query failures gracefully
        if isinstance(active_sessions, Exception):
            logger.error(f"stats: active_sessions query failed: {active_sessions}")
            active_sessions = 0
        if isinstance(today_alerts, Exception):
            logger.error(f"stats: today_alerts query failed: {today_alerts}")
            today_alerts = 0
        if isinstance(wfh_employees, Exception):
            logger.error(f"stats: wfh_employees query failed: {wfh_employees}")
            wfh_employees = 0
        if isinstance(productivity_list, Exception):
            logger.error(f"stats: productivity query failed: {productivity_list}")
            productivity_list = []

        avg_productivity = 0
        if productivity_list and isinstance(productivity_list, list):
            scores = [item.get("score", 0) for item in productivity_list if isinstance(item, dict)]
            if scores:
                avg_productivity = round(sum(scores) / len(scores), 2)

        return {
            "active_sessions": active_sessions,
            "wfh_employees": wfh_employees,
            "pending_alerts": today_alerts,
            "avg_productivity": avg_productivity,
            "date": today
        }
    except Exception as e:
        logger.error(f"admin_wfh_stats crashed: {e}", exc_info=True)
        # Return safe fallback instead of 500 — CORS headers will still be sent
        return {
            "active_sessions": 0,
            "wfh_employees": 0,
            "pending_alerts": 0,
            "avg_productivity": 0,
            "date": today,
            "error": "Stats temporarily unavailable"
        }


@router.get("/admin/wfh/live-view")
async def admin_wfh_live_view(request: Request, current_admin: Admin = Depends(require_feature("wfh_monitoring"))):
    org_id = current_admin.organization_id

    try:
        from bson import ObjectId

        # 1. Fetch active sessions in bulk for this org
        session_query = {"status": "active"}
        if org_id and org_id != "system_org":
            session_query["organization_id"] = org_id
        active_sessions_list = await wfh_sessions_collection.find(session_query).to_list(length=500)
        active_emp_ids = [s.get("employee_id") for s in (active_sessions_list or []) if s.get("employee_id")]

        active_obj_ids = []
        for eid in active_emp_ids:
            try:
                active_obj_ids.append(ObjectId(eid))
            except Exception:
                pass

        # 2. Match employees who are WFH, desk, office, hybrid, or currently checked-in
        query_conditions = [
            {"employee_type": {"$in": ["wfh", "desk", "office", "hybrid", "all", None]}},
            {"employee_type": {"$exists": False}}
        ]
        if active_obj_ids:
            query_conditions.append({"_id": {"$in": active_obj_ids}})

        query = {"$or": query_conditions}
        if org_id and org_id != "system_org":
            query["organization_id"] = org_id

        employees = await employees_collection.find(query, {"face_embedding": 0, "hashed_password": 0}).to_list(length=500)

        if not employees:
            return []

        employee_ids = [str(emp["_id"]) for emp in employees]

        pipeline = [
            {"$match": {"employee_id": {"$in": employee_ids}}},
            {"$sort": {"timestamp": -1}},
            {"$group": {"_id": "$employee_id", "latest": {"$first": "$$ROOT"}}},
        ]
        shots, activity = await asyncio.gather(
            wfh_screenshots_collection.aggregate(pipeline).to_list(length=None),
            wfh_activity_collection.aggregate(pipeline).to_list(length=None),
        )
        screenshots_by_emp = {item["_id"]: item["latest"] for item in shots}
        activities_by_emp = {item["_id"]: item["latest"] for item in activity}

        # Build lookup dicts keyed by employee_id
        sessions_by_emp = {s["employee_id"]: s for s in (active_sessions_list or [])}

        result = []
        for employee in employees:
            employee_id = str(employee["_id"])
            employee_email = employee.get("email")

            active_session = sessions_by_emp.get(employee_id)
            latest_screenshot = screenshots_by_emp.get(employee_id)
            latest_activity = activities_by_emp.get(employee_id)

            result.append({
                "session_id": str(active_session["_id"]) if active_session else f"offline-{employee_email}",
                "employee_id": employee_id,
                "employee_email": employee_email,
                "employee_name": employee.get("full_name"),
                "check_in_time": active_session.get("check_in_time") if active_session else None,
                "productivity_score": active_session.get("productivity_score", 0) if active_session else 0,
                "device_id": active_session.get("device_id") if active_session else None,
                "latest_screenshot": {
                    "_id": str(latest_screenshot["_id"]),
                    "image_url": get_screenshot_display_url(latest_screenshot.get("image_url", ""), request),
                    "timestamp": latest_screenshot.get("timestamp"),
                    "active_app": latest_screenshot.get("active_app"),
                    "active_window": latest_screenshot.get("active_window")
                } if latest_screenshot else None,
                "latest_activity": {
                    "_id": str(latest_activity["_id"]),
                    "keystrokes": latest_activity.get("keystrokes", 0),
                    "mouse_clicks": latest_activity.get("mouse_clicks", 0),
                    "idle_seconds": latest_activity.get("idle_seconds", 0),
                    "active_seconds": latest_activity.get("active_seconds", 0),
                    "timestamp": latest_activity.get("timestamp")
                } if latest_activity else None
            })

        return result
    except Exception as e:
        logger.error(f"admin_wfh_live_view crashed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Live view temporarily unavailable: {str(e)}")


@router.post("/admin/wfh/employee/{email}/trigger-screenshot")
async def admin_trigger_screenshot(email: str, current_admin: Admin = Depends(require_feature("wfh_monitoring"))):
    clean_email = email.strip().lower()
    
    # Check if employee exists
    user = await employees_collection.find_one({"email": clean_email, **get_employee_filter(current_admin)})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    doc = {
        "employee_id": str(user["_id"]),
        "employee_email": clean_email,
        "organization_id": user.get("organization_id"),
        "command": "trigger_screenshot",
        "status": "pending",
        "timestamp": datetime.now(timezone.utc)
    }
    
    await wfh_commands_collection.insert_one(doc)
    return {"status": "success", "message": "Trigger command enqueued successfully"}


@router.post("/admin/wfh/employee/{email}/force-end")
async def admin_force_end_session(email: str, req: dict, current_admin: Admin = Depends(require_feature("wfh_monitoring"))):
    clean_email = email.strip().lower()
    reason = req.get("reason", "Force-ended by administrator").strip()
    
    user = await employees_collection.find_one({"email": clean_email, **get_employee_filter(current_admin)})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    session_query = {
        "employee_email": clean_email,
        "status": "active"
    }
    if user.get("organization_id"):
        session_query["organization_id"] = user["organization_id"]
    active_session = await wfh_sessions_collection.find_one(session_query)
    
    if not active_session:
        raise HTTPException(status_code=400, detail="No active WFH session found for this employee.")
        
    now = datetime.now(timezone.utc)
    session_id = active_session["_id"]
    
    check_in_time_val = active_session.get("check_in_time")
    total_seconds_val = 0
    if check_in_time_val:
        if check_in_time_val.tzinfo is None:
            check_in_time_val = check_in_time_val.replace(tzinfo=timezone.utc)
        total_seconds_val = int((now - check_in_time_val).total_seconds())
        
    # Update active session
    await wfh_sessions_collection.update_one(
        {"_id": session_id},
        {
            "$set": {
                "status": "force_ended",
                "check_out_time": now,
                "total_active_seconds": total_seconds_val,
                "force_ended_by": current_admin.email,
                "force_end_reason": reason,
                "updated_at": now
            }
        }
    )
    
    # Write checkout log in attendance_logs
    checkout_log = {
        "user_id": str(user["_id"]),
        "email": clean_email,
        "employee_name": user.get("full_name", ""),
        "organization_id": user.get("organization_id"),
        "timestamp": now,
        "type": "check-out",
        "attendance_type": "wfh",
        "location": None,
        "check_in_method": "wfh_desktop",
        "status": "SUCCESS",
        "location_name": "Force Ended by Admin",
        "selfie_verified": False,
        "device_id": active_session.get("device_id"),
        "wfh_session_id": str(session_id),
        "method": "admin_force_end",
        "force_ended_by": current_admin.email
    }
    await attendance_logs_collection.insert_one(checkout_log)
    
    # Enqueue logout command to agent
    cmd_doc = {
        "employee_email": clean_email,
        "command": "force_end_session",
        "status": "pending",
        "timestamp": now,
        "reason": reason
    }
    await wfh_commands_collection.insert_one(cmd_doc)
    
    # Generate WFH Alert
    alert_doc = {
        "employee_id": str(user["_id"]),
        "employee_email": clean_email,
        "employee_name": user.get("full_name", ""),
        "organization_id": user.get("organization_id"),
        "type": "WFH_SESSION_FORCE_ENDED",
        "timestamp": now,
        "severity": "info",
        "status": "pending",
        "details": f"Session force-ended by admin {current_admin.email}. Reason: {reason}",
        "created_at": now
    }
    alert_res = await wfh_alerts_collection.insert_one(alert_doc)
    
    # Mirror into unified alerts collection
    try:
        await alerts_collection.insert_one({
            "organization_id": user.get("organization_id"),
            "employee_id": str(user["_id"]),
            "employee_name": user.get("full_name", ""),
            "type": "Compliance",
            "severity": "medium",
            "status": "pending",
            "detail": f"WFH Session force-ended by admin: {reason}",
            "timestamp": now,
            "metadata": {
                "source": "wfh_desktop",
                "wfh_alert_id": str(alert_res.inserted_id),
                "session_id": str(session_id)
            }
        })
    except Exception as e:
        logger.error(f"Failed to mirror force-end alert: {e}")
        
    return {
        "status": "success",
        "message": "Session force-ended successfully",
        "session_id": str(session_id)
    }


@router.post("/admin/wfh/employee/{email}/force-logout")
async def admin_force_logout(email: str, current_admin: Admin = Depends(require_feature("wfh_monitoring"))):
    clean_email = email.strip().lower()
    
    user = await employees_collection.find_one({"email": clean_email, **get_employee_filter(current_admin)})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")
        
    now = datetime.now(timezone.utc)
    
    # Enqueue force_logout command to agent
    cmd_doc = {
        "employee_id": str(user["_id"]),
        "employee_email": clean_email,
        "organization_id": user.get("organization_id"),
        "command": "force_logout",
        "status": "pending",
        "timestamp": now,
        "reason": f"Force logged out by admin {current_admin.email}"
    }
    await wfh_commands_collection.insert_one(cmd_doc)
    
    # Also force-end active WFH session if exists
    session_query = {
        "employee_email": clean_email,
        "status": "active"
    }
    if user.get("organization_id"):
        session_query["organization_id"] = user["organization_id"]
    active_session = await wfh_sessions_collection.find_one(session_query)
    
    session_id_str = None
    if active_session:
        session_id = active_session["_id"]
        session_id_str = str(session_id)
        check_in_time_val = active_session.get("check_in_time")
        total_seconds_val = 0
        if check_in_time_val:
            if check_in_time_val.tzinfo is None:
                check_in_time_val = check_in_time_val.replace(tzinfo=timezone.utc)
            total_seconds_val = int((now - check_in_time_val).total_seconds())
            
        await wfh_sessions_collection.update_one(
            {"_id": session_id},
            {
                "$set": {
                    "status": "force_ended",
                    "check_out_time": now,
                    "total_active_seconds": total_seconds_val,
                    "force_ended_by": current_admin.email,
                    "force_end_reason": "Logged out by admin",
                    "updated_at": now
                }
            }
        )
        
        # Write checkout log in attendance_logs
        checkout_log = {
            "user_id": str(user["_id"]),
            "email": clean_email,
            "employee_name": user.get("full_name", ""),
            "organization_id": user.get("organization_id"),
            "timestamp": now,
            "type": "check-out",
            "attendance_type": "wfh",
            "location": None,
            "check_in_method": "wfh_desktop",
            "status": "SUCCESS",
            "location_name": "Logged out by Admin",
            "selfie_verified": False,
            "device_id": active_session.get("device_id"),
            "wfh_session_id": session_id_str,
            "method": "admin_force_logout",
            "force_ended_by": current_admin.email
        }
        await attendance_logs_collection.insert_one(checkout_log)

    return {
        "status": "success",
        "message": "Force-logout command queued successfully",
        "session_id": session_id_str
    }


@router.get("/admin/wfh/policy")
async def get_wfh_policy(current_admin: Admin = Depends(require_feature("wfh_monitoring"))):
    org_id = current_admin.organization_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Admin organization missing")
    
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


@router.put("/admin/wfh/policy")
async def update_wfh_policy(req: dict, current_admin: Admin = Depends(require_feature("wfh_monitoring"))):
    org_id = current_admin.organization_id
    if not org_id:
        raise HTTPException(status_code=400, detail="Admin organization missing")
        
    policy = {
        "screenshot_interval_minutes": int(req.get("screenshot_interval_minutes", 10)),
        "face_check_interval_minutes": int(req.get("face_check_interval_minutes", 30)),
        "max_idle_minutes": int(req.get("max_idle_minutes", 20)),
        "productivity_threshold_percent": int(req.get("productivity_threshold_percent", 60)),
        "working_hours_start": str(req.get("working_hours_start", "09:00")),
        "working_hours_end": str(req.get("working_hours_end", "18:00")),
        "screenshot_retention_days": int(req.get("screenshot_retention_days", 5)),
        "require_face_verification": bool(req.get("require_face_verification", True)),
        "productive_apps": list(req.get("productive_apps", [])),
        "unproductive_apps": list(req.get("unproductive_apps", []))
    }
    
    result = await organizations_collection.update_one(
        {"_id": ObjectId(org_id)},
        {"$set": {"wfh_policy": policy}}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Organization not found")
        
    return {"status": "success", "message": "WFH policy updated successfully", "policy": policy}


@router.get("/admin/wfh/employee/{email}/screenshots")
async def admin_wfh_employee_screenshots(
    request: Request,
    email: str,
    date: Optional[str] = None,
    limit: int = 100,
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    clean_email = email.strip().lower()
    user = await employees_collection.find_one({"email": clean_email, **get_employee_filter(current_admin)})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")

    query = {"employee_email": clean_email}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    if date:
        query["date"] = date

    cursor = wfh_screenshots_collection.find(query).sort("timestamp", -1).limit(limit)
    screenshots = await cursor.to_list(length=limit)
    for item in screenshots:
        item["_id"] = str(item["_id"])
        item["image_url"] = get_screenshot_display_url(item.get("image_url", ""), request)
        if item.get("thumbnail_url"):
            item["thumbnail_url"] = get_screenshot_display_url(item["thumbnail_url"], request)

    return screenshots


@router.get("/admin/wfh/employee/{email}/screenshots/download")
async def admin_wfh_employee_screenshots_download(
    request: Request,
    email: str,
    date: Optional[str] = None,
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    import zipfile
    import io
    import os
    import requests
    from PIL import Image
    from fastapi.concurrency import run_in_threadpool
    from starlette.responses import StreamingResponse

    clean_email = email.strip().lower()
    user = await employees_collection.find_one({"email": clean_email, **get_employee_filter(current_admin)})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")

    query = {"employee_email": clean_email}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    if date:
        query["date"] = date

    cursor = wfh_screenshots_collection.find(query).sort("timestamp", 1)
    screenshots = await cursor.to_list(length=1000)

    if not screenshots:
        raise HTTPException(status_code=404, detail="No screenshots found for the selected date")

    def compress_screenshot(img_bytes: bytes) -> bytes:
        img = Image.open(io.BytesIO(img_bytes))
        if img.mode != "RGB":
            img = img.convert("RGB")
        out_buf = io.BytesIO()
        img.save(out_buf, format="JPEG", quality=50)
        return out_buf.getvalue()

    import asyncio
    semaphore = asyncio.Semaphore(15)

    async def process_item(index, item):
        image_url = item.get("image_url", "")
        if not image_url:
            return None

        ts = item.get("timestamp")
        time_str = "unknown"
        if isinstance(ts, datetime):
            time_str = ts.strftime("%H-%M-%S")
        elif isinstance(ts, str):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                time_str = dt.strftime("%H-%M-%S")
            except Exception:
                time_str = "".join(c if c.isalnum() or c in "-_" else "_" for c in ts)

        active_app = item.get("active_app") or "UnknownApp"
        sanitized_app = "".join(c if c.isalnum() or c in " ._-" else "_" for c in active_app)
        sanitized_app = sanitized_app[:30].strip()

        filename = f"screenshot_{index:03d}_{time_str}_{sanitized_app}.jpg"

        async with semaphore:
            try:
                from database import uploads_collection
                from upload_security import authorize_upload, read_upload_bytes
                record = await uploads_collection.find_one({"url": image_url})
                await authorize_upload(record, current_admin.model_dump(), "admin")
                image_bytes = await run_in_threadpool(read_upload_bytes, record)
                compressed_data = await run_in_threadpool(compress_screenshot, image_bytes)
                return {"filename": filename, "data": compressed_data, "success": True}
            except Exception as e:
                err_msg = f"Failed to compress {image_url}: {str(e)}"
                return {"filename": filename, "error": err_msg, "success": False}

    tasks = [process_item(index, item) for index, item in enumerate(screenshots, 1)]
    results = await asyncio.gather(*tasks)

    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        errors = []
        for res in results:
            if not res:
                continue
            if res["success"]:
                zip_file.writestr(res["filename"], res["data"])
            else:
                errors.append(res["error"])
                err_filename = f"error_{res['filename'].replace('.jpg', '.txt')}"
                zip_file.writestr(err_filename, res["error"].encode("utf-8"))

        if errors:
            error_log = "\n".join(errors)
            zip_file.writestr("error_log.txt", error_log.encode("utf-8"))

    zip_buffer.seek(0)

    filename_date = date if date else datetime.now().strftime("%Y-%m-%d")
    zip_filename = f"screenshots_{clean_email}_{filename_date}.zip"

    headers = {
        "Content-Disposition": f'attachment; filename="{zip_filename}"',
        "Access-Control-Expose-Headers": "Content-Disposition"
    }

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers=headers
    )


@router.get("/admin/wfh/employee/{email}/activity")
async def admin_wfh_employee_activity(
    email: str,
    date: Optional[str] = None,
    limit: int = 200,
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    clean_email = email.strip().lower()
    user = await employees_collection.find_one({"email": clean_email, **get_employee_filter(current_admin)})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")

    query = {"employee_email": clean_email}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    if date:
        start = datetime.fromisoformat(date + "T00:00:00+00:00")
        end = start + timedelta(days=1)
        query["timestamp"] = {"$gte": start, "$lt": end}

    cursor = wfh_activity_collection.find(query).sort("timestamp", 1).limit(limit)
    data = await cursor.to_list(length=limit)

    for item in data:
        item["_id"] = str(item["_id"])

    return data


@router.get("/admin/wfh/employee/{email}/apps")
async def admin_wfh_employee_apps(
    email: str,
    date: Optional[str] = None,
    limit: int = 100,
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    clean_email = email.strip().lower()
    user = await employees_collection.find_one({"email": clean_email, **get_employee_filter(current_admin)})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")

    query = {"employee_email": clean_email}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    if date:
        query["date"] = date

    cursor = wfh_app_usage_collection.find(query).sort("timestamp", -1).limit(limit)
    data = await cursor.to_list(length=limit)

    for item in data:
        item["_id"] = str(item["_id"])

    return data


@router.get("/admin/wfh/employee/{email}/productivity")
async def admin_wfh_employee_productivity(
    email: str,
    date: Optional[str] = None,
    limit: int = 100,
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    clean_email = email.strip().lower()
    user = await employees_collection.find_one({"email": clean_email, **get_employee_filter(current_admin)})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")

    query = {"employee_email": clean_email}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    if date:
        query["date"] = date

    cursor = wfh_productivity_collection.find(query).sort("timestamp", 1).limit(limit)
    data = await cursor.to_list(length=limit)

    for item in data:
        item["_id"] = str(item["_id"])

    return data


@router.get("/admin/wfh/employee/{email}/timeline")
async def admin_wfh_employee_timeline(
    email: str,
    date: Optional[str] = None,
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    clean_email = email.strip().lower()
    user = await employees_collection.find_one({"email": clean_email, **get_employee_filter(current_admin)})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")

    base_query = {"employee_email": clean_email}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        base_query["organization_id"] = current_admin.organization_id

    if date:
        start = datetime.fromisoformat(date + "T00:00:00+00:00")
        end = start + timedelta(days=1)
        time_filter = {"timestamp": {"$gte": start, "$lt": end}}
    else:
        time_filter = {}

    try:
        # Run all 3 queries in parallel instead of sequentially
        screenshots_task = wfh_screenshots_collection.find({**base_query, **time_filter}).to_list(length=300)
        activities_task = wfh_activity_collection.find({**base_query, **time_filter}).to_list(length=300)
        alerts_task = wfh_alerts_collection.find({**base_query, **time_filter}).to_list(length=300)

        screenshots, activities, alerts = await asyncio.gather(
            screenshots_task, activities_task, alerts_task,
            return_exceptions=True
        )

        timeline = []

        if isinstance(screenshots, list):
            for item in screenshots:
                timeline.append({
                    "type": "screenshot",
                    "timestamp": item.get("timestamp"),
                    "data": {
                        "_id": str(item["_id"]),
                        "image_url": item.get("image_url"),
                        "active_app": item.get("active_app"),
                        "active_window": item.get("active_window")
                    }
                })
        else:
            logger.error(f"timeline: screenshots query failed: {screenshots}")

        if isinstance(activities, list):
            for item in activities:
                timeline.append({
                    "type": "activity",
                    "timestamp": item.get("timestamp"),
                    "data": {
                        "_id": str(item["_id"]),
                        "keystrokes": item.get("keystrokes", 0),
                        "mouse_clicks": item.get("mouse_clicks", 0),
                        "idle_seconds": item.get("idle_seconds", 0),
                        "active_seconds": item.get("active_seconds", 0)
                    }
                })
        else:
            logger.error(f"timeline: activities query failed: {activities}")

        if isinstance(alerts, list):
            for item in alerts:
                timeline.append({
                    "type": "alert",
                    "timestamp": item.get("timestamp"),
                    "data": {
                        "_id": str(item["_id"]),
                        "alert_type": item.get("type"),
                        "severity": item.get("severity"),
                        "details": item.get("details")
                    }
                })
        else:
            logger.error(f"timeline: alerts query failed: {alerts}")

        timeline.sort(key=lambda x: x.get("timestamp") or datetime.min, reverse=True)

        return timeline
    except Exception as e:
        logger.error(f"admin_wfh_employee_timeline crashed: {e}", exc_info=True)
        return []


@router.get("/admin/wfh/alerts")
async def admin_wfh_alerts(
    status: Optional[str] = None,
    severity: Optional[str] = None,
    limit: int = 100,
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    query = {}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    if status:
        query["status"] = status

    if severity:
        query["severity"] = severity

    cursor = wfh_alerts_collection.find(query).sort("timestamp", -1).limit(limit)
    alerts = await cursor.to_list(length=limit)

    for item in alerts:
        item["_id"] = str(item["_id"])

    return alerts


@router.post("/admin/wfh/alerts/{alert_id}/review")
async def admin_review_wfh_alert(
    alert_id: str,
    req: dict,
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    if not ObjectId.is_valid(alert_id):
        raise HTTPException(status_code=400, detail="Invalid alert id")

    query = {"_id": ObjectId(alert_id)}

    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id

    alert = await wfh_alerts_collection.find_one(query)

    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    await wfh_alerts_collection.update_one(
        {"_id": ObjectId(alert_id)},
        {
            "$set": {
                "status": req.get("status", "reviewed"),
                "reviewed_by": current_admin.email,
                "reviewed_at": datetime.now(timezone.utc),
                "review_note": req.get("note")
            }
        }
    )

    return {
        "status": "success",
        "message": "WFH alert reviewed successfully"
    }


@router.get("/admin/wfh/employee/{email}/tasks")
async def admin_get_employee_tasks(email: str, current_admin: Admin = Depends(require_feature("wfh_monitoring"))):
    clean_email = email.strip().lower()
    user = await employees_collection.find_one({"email": clean_email, **get_employee_filter(current_admin)})
    if not user:
        raise HTTPException(status_code=404, detail="Employee not found")
    query = {"employee_email": clean_email}
    
    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id
        
    cursor = tasks_collection.find(query).sort("created_at", -1)
    tasks = await cursor.to_list(length=200)
    for task in tasks:
        task["_id"] = str(task["_id"])
    return tasks


@router.post("/admin/wfh/employee/{email}/tasks")
async def admin_assign_employee_task(email: str, req: dict, current_admin: Admin = Depends(require_feature("wfh_monitoring"))):
    clean_email = email.strip().lower()
    title = req.get("title")
    if not title:
        raise HTTPException(status_code=400, detail="Task title is required")
        
    emp = await employees_collection.find_one({"email": clean_email, **get_employee_filter(current_admin)})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found or access denied")
        
    now = datetime.now(timezone.utc)
    task_doc = {
        "employee_id": str(emp["_id"]),
        "employee_email": clean_email,
        "organization_id": emp.get("organization_id"),
        "title": title.strip(),
        "description": req.get("description", "").strip(),
        "status": "todo",
        "priority": req.get("priority", "medium"),
        "worked_minutes": 0,
        "created_at": now,
        "updated_at": now
    }
    
    result = await tasks_collection.insert_one(task_doc)
    task_doc["_id"] = str(result.inserted_id)
    return task_doc


@router.get("/admin/wfh/reports/productivity")
async def get_wfh_reports_productivity(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    employee_email: Optional[str] = None,
    format: str = "json",
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    query = {}
    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id
    if employee_email:
        query["employee_email"] = employee_email.strip().lower()
    
    date_filter = {}
    if start_date:
        date_filter["$gte"] = start_date
    if end_date:
        date_filter["$lte"] = end_date
    if date_filter:
        query["date"] = date_filter
        
    cursor = wfh_sessions_collection.find(query).sort("check_in_time", -1)
    sessions = await cursor.to_list(length=1000)
    
    report_data = []
    for s in sessions:
        check_in_str = s["check_in_time"].isoformat() if s.get("check_in_time") else ""
        check_out_str = s["check_out_time"].isoformat() if s.get("check_out_time") else ""
        duration_hrs = round(s.get("total_active_seconds", 0) / 3600.0, 2)
        
        report_data.append({
            "employee_email": s.get("employee_email", ""),
            "employee_name": s.get("employee_name", ""),
            "date": s.get("date", ""),
            "check_in_time": check_in_str,
            "check_out_time": check_out_str,
            "duration_hours": duration_hrs,
            "productivity_score": s.get("productivity_score", 0),
            "verified_productivity_score": s.get("verified_productivity_score", s.get("productivity_score", 0)),
            "face_check_passed": s.get("face_check_passed", 0),
            "face_check_count": s.get("face_check_count", 0),
            "status": s.get("status", "")
        })
        
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Employee Email", "Employee Name", "Date", "Check-in Time", "Check-out Time",
            "Duration (Hours)", "Productivity Score (%)", "Verified Productivity Score (%)",
            "Face Checks Passed", "Total Face Checks", "Status"
        ])
        for row in report_data:
            writer.writerow([
                row["employee_email"], row["employee_name"], row["date"], row["check_in_time"],
                row["check_out_time"], row["duration_hours"], row["productivity_score"],
                row["verified_productivity_score"], row["face_check_passed"], row["face_check_count"],
                row["status"]
            ])
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=wfh_productivity_report.csv"}
        )
        
    return report_data


@router.get("/admin/wfh/reports/app-usage")
async def get_wfh_reports_app_usage(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    employee_email: Optional[str] = None,
    format: str = "json",
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    query = {}
    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id
    if employee_email:
        query["employee_email"] = employee_email.strip().lower()
        
    date_filter = {}
    if start_date:
        date_filter["$gte"] = start_date
    if end_date:
        date_filter["$lte"] = end_date
    if date_filter:
        query["date"] = date_filter
        
    cursor = wfh_app_usage_collection.find(query).sort("timestamp", -1)
    usages = await cursor.to_list(length=1000)
    
    # Aggregate duration by (email, name, app_name)
    app_aggregates = {}
    for u in usages:
        email = u.get("employee_email", "")
        name = u.get("employee_name", "")
        apps = u.get("apps", [])
        for app in apps:
            app_name = app.get("name", "unknown")
            duration = float(app.get("duration_seconds", 0))
            category = app.get("status", "neutral")
            
            key = (email, name, app_name, category)
            app_aggregates[key] = app_aggregates.get(key, 0.0) + duration
            
    report_data = []
    for (email, name, app_name, category), total_dur in app_aggregates.items():
        report_data.append({
            "employee_email": email,
            "employee_name": name,
            "app_name": app_name,
            "category": category,
            "total_minutes": round(total_dur / 60.0, 2)
        })
        
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Employee Email", "Employee Name", "App Name", "Classification", "Total Duration (Minutes)"])
        for row in report_data:
            writer.writerow([row["employee_email"], row["employee_name"], row["app_name"], row["category"], row["total_minutes"]])
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=wfh_app_usage_report.csv"}
        )
        
    return report_data


@router.get("/admin/wfh/reports/attendance")
async def get_wfh_reports_attendance(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    employee_email: Optional[str] = None,
    format: str = "json",
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    query = {}
    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id
    if employee_email:
        query["employee_email"] = employee_email.strip().lower()
        
    date_filter = {}
    if start_date:
        date_filter["$gte"] = start_date
    if end_date:
        date_filter["$lte"] = end_date
    if date_filter:
        query["date"] = date_filter
        
    cursor = wfh_sessions_collection.find(query).sort("check_in_time", -1)
    sessions = await cursor.to_list(length=1000)
    
    # Group by employee to calculate summary metrics
    emp_summary = {}
    for s in sessions:
        email = s.get("employee_email", "")
        name = s.get("employee_name", "")
        active_sec = s.get("total_active_seconds", 0) or 0
        score = s.get("verified_productivity_score", s.get("productivity_score", 0)) or 0
        
        # Approximate active/idle seconds if missing
        if active_sec == 0:
            if s.get("check_in_time") and s.get("check_out_time"):
                active_sec = int((s["check_out_time"] - s["check_in_time"]).total_seconds())
                
        if email not in emp_summary:
            emp_summary[email] = {
                "employee_email": email,
                "employee_name": name,
                "sessions_count": 0,
                "total_duration_hours": 0.0,
                "productivity_scores_sum": 0.0,
                "productivity_scores_count": 0
            }
            
        emp_summary[email]["sessions_count"] += 1
        emp_summary[email]["total_duration_hours"] += round(active_sec / 3600.0, 2)
        if score > 0:
            emp_summary[email]["productivity_scores_sum"] += score
            emp_summary[email]["productivity_scores_count"] += 1
            
    report_data = []
    for email, data in emp_summary.items():
        avg_score = 0.0
        if data["productivity_scores_count"] > 0:
            avg_score = round(data["productivity_scores_sum"] / data["productivity_scores_count"], 2)
            
        report_data.append({
            "employee_email": data["employee_email"],
            "employee_name": data["employee_name"],
            "total_sessions": data["sessions_count"],
            "total_duration_hours": round(data["total_duration_hours"], 2),
            "average_productivity_score": avg_score
        })
        
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Employee Email", "Employee Name", "Total Sessions", "Total Duration (Hours)", "Average Productivity Score (%)"])
        for row in report_data:
            writer.writerow([row["employee_email"], row["employee_name"], row["total_sessions"], row["total_duration_hours"], row["average_productivity_score"]])
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=wfh_attendance_report.csv"}
        )
        
    return report_data


@router.get("/admin/wfh/reports/screenshots")
async def get_wfh_reports_screenshots(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    employee_email: Optional[str] = None,
    format: str = "json",
    current_admin: Admin = Depends(require_feature("wfh_monitoring"))
):
    query = {}
    if current_admin.organization_id and current_admin.organization_id != "system_org":
        query["organization_id"] = current_admin.organization_id
        
    # Get employee ID first if email is provided
    if employee_email:
        emp = await employees_collection.find_one({"email": employee_email.strip().lower()})
        if emp:
            query["employee_id"] = str(emp["_id"])
        else:
            query["employee_id"] = "nonexistent"
            
    # Timestamp date logic
    time_filter = {}
    if start_date:
        time_filter["$gte"] = datetime.fromisoformat(start_date)
    if end_date:
        time_filter["$lte"] = datetime.fromisoformat(end_date)
    if time_filter:
        query["timestamp"] = time_filter
        
    cursor = wfh_screenshots_collection.find(query).sort("timestamp", -1)
    screenshots = await cursor.to_list(length=1000)
    
    # Batch-fetch all employee records
    from bson import ObjectId
    emp_ids = list({ObjectId(s["employee_id"]) for s in screenshots if s.get("employee_id") and ObjectId.is_valid(str(s["employee_id"]))})
    employees = await employees_collection.find({"_id": {"$in": emp_ids}}, {"_id": 1, "email": 1, "full_name": 1}).to_list(length=len(emp_ids))
    emp_map = {str(e["_id"]): e for e in employees}

    report_data = []
    for s in screenshots:
        emp_id_str = str(s.get("employee_id", ""))
        emp = emp_map.get(emp_id_str)
        emp_email = emp.get("email", "") if emp else ""
        emp_name = emp.get("full_name", "") if emp else ""
            
        report_data.append({
            "employee_email": emp_email,
            "employee_name": emp_name,
            "timestamp": s["timestamp"].isoformat() if s.get("timestamp") else "",
            "active_app": s.get("active_app", ""),
            "active_window": s.get("active_window", ""),
            "flagged": s.get("flagged", False),
            "flag_reason": s.get("flag_reason", "")
        })
        
    if format == "csv":
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Employee Email", "Employee Name", "Timestamp", "Active App", "Active Window", "Flagged Status", "Flag Reason"])
        for row in report_data:
            writer.writerow([
                row["employee_email"], row["employee_name"], row["timestamp"], row["active_app"],
                row["active_window"], "Flagged" if row["flagged"] else "Normal", row["flag_reason"]
            ])
        return Response(
            content=output.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=wfh_screenshots_report.csv"}
        )
        
    return report_data


