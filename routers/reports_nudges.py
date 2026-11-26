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

@router.get("/admin/reports/attendance")
async def attendance_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    employee_type: Optional[str] = None,
    admin=Depends(require_feature("reports"))
):
    """Generate attendance report with filters."""
    query = {}
    
    scoped_ids = await get_scoped_employee_ids(admin)
    query["user_id"] = {"$in": scoped_ids}
    
    if start_date:
        query["timestamp"] = {"$gte": datetime.fromisoformat(start_date)}
    if end_date:
        if "timestamp" in query:
            query["timestamp"]["$lte"] = datetime.fromisoformat(end_date)
        else:
            query["timestamp"] = {"$lte": datetime.fromisoformat(end_date)}
    
    logs = await attendance_logs_collection.find(query).sort("timestamp", -1).to_list(length=1000)
    
    from bson import ObjectId
    emp_ids = list({ObjectId(log["user_id"]) for log in logs if log.get("user_id") and ObjectId.is_valid(str(log["user_id"]))})
    employees = await employees_collection.find({"_id": {"$in": emp_ids}}, {"_id": 1, "full_name": 1, "employee_type": 1, "email": 1}).to_list(length=len(emp_ids))
    emp_map = {str(e["_id"]): e for e in employees}

    summary = {"total_records": len(logs), "check_ins": 0, "check_outs": 0, "unique_employees": set()}
    enriched = []
    
    for log in logs:
        emp_id_str = str(log.get("user_id", ""))
        emp = emp_map.get(emp_id_str)
        if employee_type and emp and emp.get("employee_type", "desk") != employee_type:
            continue
        
        log["_id"] = str(log["_id"])
        log["full_name"] = emp["full_name"] if emp else "Unknown"
        log["employee_type"] = emp.get("employee_type", "desk") if emp else "unknown"
        if isinstance(log.get("timestamp"), datetime):
            log["timestamp"] = log["timestamp"].isoformat()
        
        if log.get("type") == "check-in":
            summary["check_ins"] += 1
        else:
            summary["check_outs"] += 1
        summary["unique_employees"].add(emp.get("email", "") if emp else "")
        enriched.append(log)
    
    summary["unique_employees"] = len(summary["unique_employees"])
    summary["total_records"] = len(enriched)
    
    return {"summary": summary, "records": enriched}


@router.get("/admin/reports/expenses")
async def expense_report(admin=Depends(require_feature("reports"))):
    """Generate expense summary report."""
    scoped_emails = await get_scoped_employee_emails(admin)
    query = {
        "$or": [
            {"employee_id": {"$in": scoped_emails}},
            {"employee_email": {"$in": scoped_emails}}
        ]
    }
    
    claims = await expense_claims_collection.find(query).to_list(length=1000)
    
    total_amount = sum(c.get("amount", 0) for c in claims)
    approved_amount = sum(c.get("amount", 0) for c in claims if c.get("status") == "approved")
    pending_amount = sum(c.get("amount", 0) for c in claims if c.get("status") == "pending")
    rejected_amount = sum(c.get("amount", 0) for c in claims if c.get("status") == "rejected")
    
    return {
        "total_claims": len(claims),
        "total_amount": total_amount,
        "approved_amount": approved_amount,
        "pending_amount": pending_amount,
        "rejected_amount": rejected_amount,
        "by_status": {
            "pending": len([c for c in claims if c.get("status") == "pending"]),
            "approved": len([c for c in claims if c.get("status") == "approved"]),
            "rejected": len([c for c in claims if c.get("status") == "rejected"]),
        }
    }


@router.get("/admin/reports/agent-performance")
async def agent_performance_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin=Depends(require_feature("reports"))
):
    """Aggregate visits and distance per agent."""
    org_id = admin.organization_id
    
    if not start_date or not end_date:
        now = datetime.now(timezone.utc)
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_dt = now
    else:
        try:
            start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
        except ValueError:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d")
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    scoped_emp_ids = await get_scoped_employee_employee_ids(admin)
    scoped_emails = await get_scoped_employee_emails(admin)
    scoped_obj_ids = await get_scoped_employee_ids(admin)
    all_identifiers = list(set(scoped_emp_ids + scoped_emails + scoped_obj_ids))

    pipeline = [
        {"$match": {
            "organization_id": org_id,
            "$or": [
                {"employee_id": {"$in": all_identifiers}},
                {"employee_email": {"$in": scoped_emails}}
            ],
            "check_in_time": {"$gte": start_dt, "$lte": end_dt}
        }},
        {"$group": {
            "_id": {"$ifNull": ["$employee_email", "$employee_id"]},
            "total_visits": {"$sum": 1},
            "leads": {"$sum": {"$cond": ["$lead_captured", 1, 0]}},
            "orders": {"$sum": {"$cond": ["$order_captured", 1, 0]}}
        }}
    ]
    visit_stats = await visit_logs_collection.aggregate(pipeline).to_list(None)

    km_pipeline = [
        {"$match": {
            "organization_id": org_id,
            "date": {"$gte": start_dt.strftime("%Y-%m-%d"), "$lte": end_dt.strftime("%Y-%m-%d")}
        }},
        {"$group": {
            "_id": "$employee_id",
            "total_km": {"$sum": "$total_km"}
        }}
    ]
    km_stats = await km_reimbursements_collection.aggregate(km_pipeline).to_list(None)

    perf_map = {}
    for stat in visit_stats:
        eid = stat["_id"]
        emp = await employees_collection.find_one(
            {"$or": [{"employee_id": eid}, {"email": eid}]},
            {"full_name": 1, "email": 1, "employee_id": 1}
        )
        canonical_key = emp["email"] if emp and emp.get("email") else eid
        perf_map[canonical_key] = {
            "employee_id": emp.get("employee_id", eid) if emp else eid,
            "full_name": emp["full_name"] if emp else "Unknown",
            "total_visits": stat["total_visits"],
            "leads": stat["leads"],
            "orders": stat["orders"],
            "total_km": 0
        }
    
    for kstat in km_stats:
        eid = kstat["_id"]
        emp = await employees_collection.find_one(
            {"$or": [{"employee_id": eid}, {"email": eid}]},
            {"full_name": 1, "email": 1, "employee_id": 1}
        )
        canonical_key = emp["email"] if emp and emp.get("email") else eid
        if canonical_key in perf_map:
            perf_map[canonical_key]["total_km"] = round(kstat["total_km"], 2)
        else:
            perf_map[canonical_key] = {
                "employee_id": emp.get("employee_id", eid) if emp else eid,
                "full_name": emp["full_name"] if emp else "Unknown",
                "total_visits": 0,
                "leads": 0,
                "orders": 0,
                "total_km": round(kstat["total_km"], 2)
            }

    return list(perf_map.values())


@router.get("/admin/reports/leaves")
async def leave_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin=Depends(require_feature("reports"))
):
    """Generate leave analytics report."""
    scoped_emails = await get_scoped_employee_emails(admin)
    query = {
        "$or": [
            {"employee_id": {"$in": scoped_emails}},
            {"employee_email": {"$in": scoped_emails}}
        ]
    }
    
    if start_date and end_date:
        query["start_date"] = {"$gte": start_date, "$lte": end_date}
    
    cursor = leave_requests_collection.find(query)
    leaves = await cursor.to_list(length=1000)
    
    # 1. Distribution by Type
    type_counts = {}
    for l in leaves:
        lt = l.get("leave_type", "other")
        type_counts[lt] = type_counts.get(lt, 0) + 1
    
    distribution = [{"name": k.replace("_", " ").capitalize(), "value": v} for k, v in type_counts.items()]
    
    # 2. Trends (Requests per day)
    trend_map = {}
    for l in leaves:
        cd = l.get("created_at")
        if cd:
            if isinstance(cd, datetime):
                ds = cd.strftime("%Y-%m-%d")
            else:
                ds = str(cd)[:10]
            trend_map[ds] = trend_map.get(ds, 0) + 1
            
    # Sort trends
    trends = [{"date": k, "count": v} for k, v in sorted(trend_map.items())]
    
    return {
        "distribution": distribution,
        "trends": trends,
        "total_requests": len(leaves),
        "approved": len([l for l in leaves if l.get("status") == "approved"]),
        "rejected": len([l for l in leaves if l.get("status") == "rejected"]),
        "pending": len([l for l in leaves if l.get("status") == "pending"])
    }


@router.get("/admin/reports/conversion-funnel")
async def conversion_funnel_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin=Depends(require_feature("reports"))
):
    """Aggregate visit outcomes for funnel visualization."""
    org_id = admin.organization_id
    
    if not start_date or not end_date:
        now = datetime.now(timezone.utc)
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_dt = now
    else:
        start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
    scoped_emp_ids = await get_scoped_employee_employee_ids(admin)
    scoped_emails = await get_scoped_employee_emails(admin)
    scoped_obj_ids = await get_scoped_employee_ids(admin)
    all_identifiers = list(set(scoped_emp_ids + scoped_emails + scoped_obj_ids))

    pipeline = [
        {"$match": {
            "organization_id": org_id,
            "$or": [
                {"employee_id": {"$in": all_identifiers}},
                {"employee_email": {"$in": scoped_emails}}
            ],
            "check_in_time": {"$gte": start_dt, "$lte": end_dt}
        }},
        {"$group": {
            "_id": None,
            "visits": {"$sum": 1},
            "leads": {"$sum": {"$cond": ["$lead_captured", 1, 0]}},
            "orders": {"$sum": {"$cond": ["$order_captured", 1, 0]}}
        }}
    ]
    result = await visit_logs_collection.aggregate(pipeline).to_list(None)
    
    if not result:
        return {"visits": 0, "leads": 0, "orders": 0, "conversion_rate": 0}
    
    data = result[0]
    data.pop("_id", None)
    data["conversion_rate"] = round((data["orders"] / data["visits"] * 100), 2) if data["visits"] > 0 else 0
    return data


@router.get("/admin/reports/visit-frequency")
async def visit_frequency_report(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    admin=Depends(require_feature("reports"))
):
    """Daily trends analysis."""
    org_id = admin.organization_id
    
    if not start_date or not end_date:
        now = datetime.now(timezone.utc)
        start_dt = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        end_dt = now
    else:
        start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))

    scoped_emp_ids = await get_scoped_employee_employee_ids(admin)
    scoped_emails = await get_scoped_employee_emails(admin)
    scoped_obj_ids = await get_scoped_employee_ids(admin)
    all_identifiers = list(set(scoped_emp_ids + scoped_emails + scoped_obj_ids))

    pipeline_daily = [
        {"$match": {
            "organization_id": org_id,
            "$or": [
                {"employee_id": {"$in": all_identifiers}},
                {"employee_email": {"$in": scoped_emails}}
            ],
            "check_in_time": {"$gte": start_dt, "$lte": end_dt}
        }},
        {"$group": {
            "_id": {"$dateToString": {"format": "%Y-%m-%d", "date": "$check_in_time"}},
            "count": {"$sum": 1}
        }},
        {"$sort": {"_id": 1}}
    ]
    daily_stats = await visit_logs_collection.aggregate(pipeline_daily).to_list(None)

    return {
        "daily_trends": [{"date": s["_id"], "visits": s["count"]} for s in daily_stats]
    }


@router.post("/api/manager/nudge")
async def send_manager_nudge(req: dict, manager=Depends(get_current_employee)):
    """
    Manager sends a motivational nudge to one or more team members or all.
    Dispatches real background push notifications to field employees' devices.
    """
    # Verify manager status
    subordinates_count = await employees_collection.count_documents({"manager_id": manager["email"]})
    if subordinates_count == 0:
        raise HTTPException(status_code=403, detail="Only managers can send nudges.")

    employee_emails = req.get("employee_emails", [])
    recipient_type = req.get("recipient_type", "individual")  # 'individual' or 'all'
    message = req.get("message", "").strip()
    nudge_type = req.get("nudge_type", "general")

    if not message:
        raise HTTPException(status_code=400, detail="Nudge message cannot be empty.")

    # If sending to all subordinates or "all" in employee_emails
    if recipient_type == "all" or "all" in employee_emails or not employee_emails:
        recipient_type = "all"
        cursor = employees_collection.find({"manager_id": manager["email"], "is_active": {"$ne": False}})
        subordinates = await cursor.to_list(length=500)
        valid_recipients = [s["email"] for s in subordinates if s.get("email")]
    else:
        valid_recipients = []
        for email in employee_emails:
            emp = await employees_collection.find_one({"email": email, "manager_id": manager["email"]})
            if emp:
                valid_recipients.append(email)

    if not valid_recipients:
        raise HTTPException(status_code=400, detail="No valid team members found in the provided emails.")

    manager_name = manager.get("full_name") or manager.get("name") or manager.get("email")

    nudge_log = {
        "manager_id": manager["email"],
        "manager_name": manager_name,
        "organization_id": manager.get("organization_id"),
        "recipients": valid_recipients,
        "recipient_type": recipient_type,
        "message": message,
        "nudge_type": nudge_type,
        "read_by": [],
        "sent_at": datetime.now(timezone.utc),
    }
    result = await nudge_logs_collection.insert_one(nudge_log)
    nudge_id = str(result.inserted_id)

    # Collect push tokens for all recipients
    push_tokens = []
    async for emp in employees_collection.find({"email": {"$in": valid_recipients}}):
        if emp.get("push_token"):
            push_tokens.append(emp["push_token"])
        if isinstance(emp.get("push_tokens"), list):
            push_tokens.extend(emp["push_tokens"])

    nudge_titles = {
        "general": f"⚡ Message from {manager_name}",
        "target_missed": f"🎯 Target Alert from {manager_name}",
        "late_start": f"⏰ Schedule Alert from {manager_name}",
        "great_job": f"🌟 Great Job from {manager_name}!"
    }
    title = nudge_titles.get(nudge_type, f"⚡ Nudge from {manager_name}")

    push_data = {
        "type": "nudge",
        "nudge_id": nudge_id,
        "nudge_type": nudge_type,
        "sender_name": manager_name,
        "sender_role": "Manager",
        "message": message,
        "sent_at": datetime.now(timezone.utc).isoformat()
    }

    asyncio.create_task(send_expo_push_notifications(push_tokens, title, message, push_data))

    return {
        "status": "sent",
        "nudge_id": nudge_id,
        "recipients_count": len(valid_recipients),
        "recipient_type": recipient_type,
        "recipients": valid_recipients,
        "message": f"Nudge sent to {len(valid_recipients)} team member(s)."
    }


@router.get("/api/manager/nudge/history")
async def get_nudge_history(manager=Depends(get_current_employee)):
    """Fetch the nudge history sent by this manager."""
    logs = await nudge_logs_collection.find(
        {"manager_id": manager["email"]}
    ).sort("sent_at", -1).to_list(length=50)

    for log in logs:
        log["_id"] = str(log["_id"])
        if isinstance(log.get("sent_at"), datetime):
            log["sent_at"] = log["sent_at"].isoformat()

    return logs


@router.post("/admin/nudge")
async def admin_send_nudge(req: dict, admin=Depends(require_feature("nudge"))):
    """
    Admin sends a motivational nudge/message to field team members in their organization.
    Supports private 1-to-1 nudge or broadcast to all field team employees.
    Dispatches real background push notifications to field employees' devices.
    """
    org_id = admin.organization_id
    employee_emails = req.get("employee_emails", [])
    recipient_type = req.get("recipient_type", "individual")  # 'individual' or 'all'
    message = req.get("message", "").strip()
    nudge_type = req.get("nudge_type", "general")

    if not message:
        raise HTTPException(status_code=400, detail="Nudge message cannot be empty.")

    if recipient_type == "all" or "all" in employee_emails or not employee_emails:
        recipient_type = "all"
        query = {"is_active": {"$ne": False}}
        if org_id:
            query["organization_id"] = org_id
        all_emps = await employees_collection.find(query).to_list(length=1000)
        valid_recipients = [e["email"] for e in all_emps if e.get("email")]
    else:
        valid_recipients = []
        for email in employee_emails:
            query = {"email": email}
            if org_id:
                query["organization_id"] = org_id
            emp = await employees_collection.find_one(query)
            if emp:
                valid_recipients.append(email)

    if not valid_recipients:
        raise HTTPException(status_code=400, detail="No valid employees found in your organization.")

    admin_name = getattr(admin, "full_name", None) or getattr(admin, "name", None) or admin.email

    nudge_log = {
        "admin_id": admin.email,
        "admin_name": admin_name,
        "organization_id": org_id,
        "recipients": valid_recipients,
        "recipient_type": recipient_type,
        "message": message,
        "nudge_type": nudge_type,
        "read_by": [],
        "sent_at": datetime.now(timezone.utc),
    }
    result = await nudge_logs_collection.insert_one(nudge_log)
    nudge_id = str(result.inserted_id)

    # Collect push tokens
    push_tokens = []
    async for emp in employees_collection.find({"email": {"$in": valid_recipients}}):
        if emp.get("push_token"):
            push_tokens.append(emp["push_token"])
        if isinstance(emp.get("push_tokens"), list):
            push_tokens.extend(emp["push_tokens"])

    nudge_titles = {
        "general": f"📢 Announcement from {admin_name}",
        "target_missed": f"🎯 Target Alert from Admin",
        "late_start": f"⏰ Attendance Notice from Admin",
        "great_job": f"🏆 Recognition from Admin!"
    }
    title = nudge_titles.get(nudge_type, f"⚡ Message from Admin ({admin_name})")

    push_data = {
        "type": "nudge",
        "nudge_id": nudge_id,
        "nudge_type": nudge_type,
        "sender_name": admin_name,
        "sender_role": "Admin",
        "message": message,
        "sent_at": datetime.now(timezone.utc).isoformat()
    }

    asyncio.create_task(send_expo_push_notifications(push_tokens, title, message, push_data))

    return {
        "status": "sent",
        "nudge_id": nudge_id,
        "recipients_count": len(valid_recipients),
        "recipient_type": recipient_type,
        "message": f"Nudge sent to {len(valid_recipients)} member(s)."
    }


@router.get("/admin/nudge/history")
async def admin_get_nudge_history(admin=Depends(require_feature("nudge"))):
    """Fetch history of nudges sent by admins in this organization."""
    query = {}
    if admin.organization_id:
        query["organization_id"] = admin.organization_id
        
    logs = await nudge_logs_collection.find(query).sort("sent_at", -1).to_list(length=50)

    for log in logs:
        log["_id"] = str(log["_id"])
        if isinstance(log.get("sent_at"), datetime):
            log["sent_at"] = log["sent_at"].isoformat()

    return logs


@router.post("/api/employee/push-token")
async def register_push_token(req: dict, emp=Depends(get_current_employee)):
    """
    Register mobile Expo push token for the current employee.
    Enables real background notifications even when the app is closed.
    """
    token = req.get("push_token") or req.get("token")
    device_info = req.get("device_info", {})
    if not token:
        raise HTTPException(status_code=400, detail="push_token is required.")
        
    await employees_collection.update_one(
        {"email": emp["email"]},
        {
            "$set": {
                "push_token": token,
                "push_token_updated_at": datetime.now(timezone.utc),
                "device_info": device_info
            },
            "$addToSet": {
                "push_tokens": token
            }
        }
    )
    return {"status": "success", "message": "Push token registered successfully"}


@router.get("/api/employee/nudges")
async def get_employee_nudges(emp=Depends(get_current_employee)):
    """
    Fetch all nudges received by this employee (both targeted to them or broadcast to all in org).
    """
    org_id = emp.get("organization_id")
    email = emp.get("email")

    query = {
        "$or": [
            {"recipients": email},
            {"recipient_type": "all", "organization_id": org_id},
            {"recipients": "all", "organization_id": org_id}
        ]
    }
    
    logs = await nudge_logs_collection.find(query).sort("sent_at", -1).to_list(length=100)
    result = []
    for log in logs:
        log_id = str(log["_id"])
        read_by = log.get("read_by", [])
        is_read = email in read_by
        sent_at_val = log.get("sent_at")
        sent_at_str = sent_at_val.isoformat() if isinstance(sent_at_val, datetime) else str(sent_at_val or "")

        result.append({
            "id": log_id,
            "_id": log_id,
            "sender_name": log.get("admin_name") or log.get("manager_name") or "Management",
            "sender_role": "Admin" if log.get("admin_id") else "Manager",
            "message": log.get("message", ""),
            "nudge_type": log.get("nudge_type", "general"),
            "sent_at": sent_at_str,
            "is_read": is_read
        })
    return result


@router.post("/api/employee/nudges/{nudge_id}/read")
async def mark_nudge_read(nudge_id: str, emp=Depends(get_current_employee)):
    """Mark a nudge as read by this employee."""
    try:
        oid = ObjectId(nudge_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid nudge ID")

    await nudge_logs_collection.update_one(
        {"_id": oid},
        {"$addToSet": {"read_by": emp["email"]}}
    )
    return {"status": "success", "message": "Marked as read"}


@router.get("/admin/leaderboard")
async def get_admin_leaderboard(admin=Depends(require_feature("leaderboard"))):
    """
    Weekly leaderboard for Admin Portal: Top 10 agents in the organization.
    """
    org_id = admin.organization_id

    # Current week boundaries (Mon 00:00 → Sun 23:59)
    today = datetime.now(timezone.utc)
    week_start = today - timedelta(days=today.weekday())
    week_start = week_start.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)

    pipeline = [
        {
            "$match": {
                "organization_id": org_id,
                "check_in_time": {"$gte": week_start, "$lt": week_end}
            }
        },
        {
            "$group": {
                "_id": "$employee_id",
                "visits_completed": {"$sum": 1},
                "leads_captured": {"$sum": {"$cond": ["$lead_captured", 1, 0]}},
                "orders_captured": {"$sum": {"$cond": ["$order_captured", 1, 0]}},
            }
        },
        {"$sort": {"visits_completed": -1, "leads_captured": -1}},
        {"$limit": 10}
    ]

    results = await visit_logs_collection.aggregate(pipeline).to_list(length=10)

    leaderboard = []
    for idx, row in enumerate(results):
        emp = await employees_collection.find_one({"email": row["_id"]})
        emp_name = emp["full_name"] if emp else row["_id"]
        emp_designation = emp.get("designation", "Field Agent") if emp else "Field Agent"

        # Sum KM for the week from location pings
        pings = await location_pings_collection.find({
            "employee_id": row["_id"],
            "recorded_at": {"$gte": week_start, "$lt": week_end}
        }).sort("recorded_at", 1).to_list(length=5000)

        total_km = 0.0
        for i in range(1, len(pings)):
            p1, p2 = pings[i - 1], pings[i]
            dlat = math.radians(p2["lat"] - p1["lat"])
            dlng = math.radians(p2["lng"] - p1["lng"])
            a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(p1["lat"])) * math.cos(math.radians(p2["lat"])) * math.sin(dlng / 2) ** 2
            total_km += 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

        leaderboard.append({
            "rank": idx + 1,
            "employee_id": row["_id"],
            "name": emp_name,
            "designation": emp_designation,
            "visits_completed": row["visits_completed"],
            "leads_captured": row["leads_captured"],
            "orders_captured": row["orders_captured"],
            "distance_km": round(total_km, 1),
            "is_me": False, # Admins are not usually in the leaderboard
        })

    return {
        "week_start": week_start.strftime("%Y-%m-%d"),
        "week_end": (week_end - timedelta(days=1)).strftime("%Y-%m-%d"),
        "leaderboard": leaderboard
    }


@router.get("/admin/reports/employee-monthly-summary")
async def get_employee_monthly_summary(
    email: str,
    month: str,  # Format: YYYY-MM
    admin=Depends(require_feature("reports"))
):
    """
    Get a detailed monthly summary for a specific employee.
    Used for individual drill-down reports.
    """
    try:
        start_date = datetime.strptime(f"{month}-01", "%Y-%m-%d").replace(tzinfo=timezone.utc)
        # End date is first day of next month
        year, m = map(int, month.split("-"))
        if m == 12:
            next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month = datetime(year, m + 1, 1, tzinfo=timezone.utc)
            
        end_date = next_month
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid month format. Expected YYYY-MM")

    # Security: Ensure admin only sees their org's data
    org_filter = {"organization_id": admin.organization_id} if admin.organization_id else {}
    
    # 1. Fetch Employee Details
    employee = await employees_collection.find_one({"email": email, **org_filter})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    # 2. Fetch All Logs for the month
    logs = await attendance_logs_collection.find({
        "email": email,
        "timestamp": {"$gte": start_date, "$lt": end_date}
    }).sort("timestamp", 1).to_list(length=1000)

    # 3. Fetch Approved Leaves for the month (flexible identifier matching)
    clean_email = email.strip().lower()
    emp_identifiers = [clean_email, email.strip()]
    if "_id" in employee:
        emp_identifiers.append(str(employee["_id"]))
    if "employee_id" in employee and employee["employee_id"]:
        emp_identifiers.append(str(employee["employee_id"]).strip())

    raw_leaves = await leave_requests_collection.find({
        "$or": [
            {"employee_id": {"$in": emp_identifiers}},
            {"email": {"$in": emp_identifiers}}
        ],
        "status": {"$regex": "^approved$", "$options": "i"}
    }).to_list(length=100)

    leaves = []
    for rl in raw_leaves:
        norm_s = normalize_date_str(rl.get("start_date", ""))
        norm_e = normalize_date_str(rl.get("end_date", ""))
        if norm_s:
            rl["start_date"] = norm_s
            rl["end_date"] = norm_e or norm_s
            leaves.append(rl)

    raw_pending = await leave_requests_collection.find({
        "$or": [
            {"employee_id": {"$in": emp_identifiers}},
            {"email": {"$in": emp_identifiers}}
        ],
        "status": {"$regex": "^pending$", "$options": "i"}
    }).to_list(length=100)

    pending_leaves = []
    for rp in raw_pending:
        norm_s = normalize_date_str(rp.get("start_date", ""))
        norm_e = normalize_date_str(rp.get("end_date", ""))
        if norm_s:
            rp["start_date"] = norm_s
            rp["end_date"] = norm_e or norm_s
            pending_leaves.append(rp)

    # 4. Process Data Day by Day
    daily_breakdown = []
    total_working_ms = 0
    present_days = 0
    absent_days = 0
    leaves_count = 0
    
    # Fetch org settings for timezone and shift times (unified from settings + leave_policy)
    org_id = employee.get("organization_id")
    org_doc = await get_effective_org_settings(org_id)
    tz_offset = org_doc.get("timezone_offset", 330)

    if employee.get("employee_type") == "field":
        office_end_time_str = org_doc.get("field_office_end_time") or org_doc.get("office_end_time", "18:00")
        office_start_time_str = org_doc.get("field_office_start_time") or org_doc.get("office_start_time", "10:00")
        late_threshold_mins = org_doc.get("field_late_grace_mins") if org_doc.get("field_late_grace_mins") is not None else org_doc.get("field_late_threshold_mins", 30)
    else:
        office_end_time_str = org_doc.get("office_end_time", "18:00")
        office_start_time_str = org_doc.get("office_start_time", "10:00")
        late_threshold_mins = org_doc.get("late_grace_mins") if org_doc.get("late_grace_mins") is not None else org_doc.get("late_threshold_mins", 15)

    # Fetch configured days off / holidays for this organization and month
    days_off_set = set()
    holiday_names = {}
    if org_id:
        try:
            # 1. Fetch days_off doc (has composite days_off array)
            h_doc = await holidays_collection.find_one({
                "$or": [
                    {"organization_id": str(org_id), "payroll_month": month, "days_off": {"$exists": True}},
                    {"organization_id": "system_org", "payroll_month": month, "days_off": {"$exists": True}}
                ]
            })
            if h_doc:
                days_off_set.update(h_doc.get("days_off", []))
                holiday_names.update(h_doc.get("holiday_names", {}))

            # 2. Fetch all named holidays for this month (e.g. Janmashtami, festival, company)
            named_holidays = await holidays_collection.find({
                "$or": [
                    {"organization_id": str(org_id), "payroll_month": month, "date": {"$exists": True}},
                    {"organization_id": "system_org", "payroll_month": month, "date": {"$exists": True}}
                ]
            }).to_list(length=100)
            for nh in named_holidays:
                d_num = None
                if "day" in nh and nh["day"] is not None:
                    d_num = int(nh["day"])
                elif "date" in nh and nh["date"]:
                    try:
                        d_num = int(str(nh["date"]).split("-")[2])
                    except Exception:
                        pass
                if d_num is not None:
                    days_off_set.add(d_num)
                    holiday_names[str(d_num)] = nh.get("name") or "Holiday"

            # 3. Always include all Sundays in this month as weekends
            try:
                y_m = month.split("-")
                month_days_cnt = calendar.monthrange(int(y_m[0]), int(y_m[1]))[1]
                for di in range(1, month_days_cnt + 1):
                    if datetime(int(y_m[0]), int(y_m[1]), di).weekday() == 6:
                        days_off_set.add(di)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"Failed to fetch holidays in monthly summary: {e}")

    start_h, start_m = map(int, office_start_time_str.split(":"))
    end_h, end_m = map(int, office_end_time_str.split(":"))

    local_start_of_month = start_date + timedelta(minutes=tz_offset)
    local_month_end = end_date + timedelta(minutes=tz_offset)
    local_now = datetime.now(timezone.utc) + timedelta(minutes=tz_offset)
    
    # Use the earlier of (End of Month) or (Today) for the loop boundary
    local_end_boundary = local_now if local_now < local_month_end else local_month_end
    
    current_day_local = local_start_of_month
    while current_day_local < local_end_boundary:
        next_day_local = current_day_local + timedelta(days=1)
        day_str = current_day_local.strftime("%Y-%m-%d")
        
        # Filter logs for this day in localized boundary
        day_logs = []
        for l in logs:
            ts = l.get("timestamp")
            if not ts:
                continue
            
            # Ensure timestamp is UTC aware
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            else:
                ts = ts.astimezone(timezone.utc)
            
            # Localize for comparison
            local_ts = ts + timedelta(minutes=tz_offset)
            if current_day_local <= local_ts < next_day_local:
                day_logs.append(l)
        
        day_info = {
            "date": day_str,
            "status": "Absent",
            "is_late": False,
            "is_early_leave": False,
            "is_short_leave": False,
            "is_half_day": False,
            "short_leave_type": None,
            "half_day_type": None,
            "late_mins": 0,
            "early_exit_mins": 0,
            "late_off_hours": 0.0,
            "early_off_hours": 0.0,
            "off_hours": 0.0,
            "required_hours": 8.0,
            "effective_days": 0.0,
            "classification_reason": "",
            "is_applied_leave": False,
            "first_in": None,
            "last_out": None,
            "duration_hours": 0,
            "logs": []
        }

        # 1. Check for Approved Leave on this day
        matching_leave = None
        for leave in leaves:
            s_date = str(leave.get("start_date", ""))[:10]
            e_date = str(leave.get("end_date", ""))[:10]
            if s_date and e_date and (s_date <= day_str <= e_date):
                matching_leave = leave
                leaves_count += 1
                break

        # 2. Check for Pending Leave on this day
        is_pending_leave = False
        pending_label = None
        if not matching_leave:
            for pl in pending_leaves:
                s_date = str(pl.get("start_date", ""))[:10]
                e_date = str(pl.get("end_date", ""))[:10]
                if s_date and e_date and (s_date <= day_str <= e_date):
                    l_type = pl.get("leave_type", "General")
                    pending_label = f"Pending Leave ({l_type})"
                    is_pending_leave = True
                    break

        # 3. Check for Configured Holidays / Days Off / Weekend
        day_num = current_day_local.day
        is_weekend_or_holiday = False
        if day_num in days_off_set:
            is_weekend_or_holiday = True
            if current_day_local.weekday() == 6:
                day_info["status"] = "Weekend"
            elif current_day_local.weekday() == 5:
                day_info["status"] = holiday_names.get(str(day_num), "Weekend")
            else:
                day_info["status"] = holiday_names.get(str(day_num), "Holiday")
        elif current_day_local.weekday() >= 5:
            is_weekend_or_holiday = True
            day_info["status"] = "Weekend"

        if day_logs:
            day_logs.sort(key=lambda x: x["timestamp"])
            present_days += 1
            
            day_info["first_in"] = day_logs[0]["timestamp"].replace(tzinfo=timezone.utc).isoformat()
            
            day_duration_ms = 0
            last_in_time = None
            
            for log in day_logs:
                log_ts = log["timestamp"]
                if log_ts.tzinfo is None:
                    log_ts = log_ts.replace(tzinfo=timezone.utc)
                local_log_time = log_ts + timedelta(minutes=tz_offset)
                
                log_type = log.get("type", "")
                log_status = log.get("attendance_status") or "Present"

                # Basic representation for frontend
                day_info["logs"].append({
                    "time": log_ts.isoformat(),
                    "type": log_type,
                    "status": log_status,
                    "attendance_status": log.get("attendance_status", log_status),
                    "is_short_leave": log.get("is_short_leave", False),
                    "is_half_day": log.get("is_half_day", False),
                    "off_hours": log.get("off_hours", 0.0),
                    "method": log.get("check_in_method", "N/A"),
                    "location": log.get("location") or ({"lat": log.get("lat"), "long": log.get("long")} if (log.get("lat") is not None) else None),
                    "lat": (log.get("location") or {}).get("lat", log.get("lat")),
                    "long": (log.get("location") or {}).get("long", log.get("long")),
                    "address": log.get("address") or log.get("location_name"),
                    "location_name": log.get("location_name") or log.get("address"),
                    "selfie": log.get("selfie_url"),
                    "wifi_confidence": log.get("wifi_confidence", 0)
                })

                if log_type == "check-in":
                    last_in_time = log_ts
                elif log_type == "check-out":
                    day_info["last_out"] = log_ts.isoformat()
                    if last_in_time:
                        delta = log_ts - last_in_time
                        day_duration_ms += delta.total_seconds() * 1000
                        last_in_time = None

            duration_hours = round(day_duration_ms / (1000 * 3600), 2)
            day_info["duration_hours"] = duration_hours
            total_working_ms += day_duration_ms

            # Comprehensive classification via classify_attendance_day
            checkout_logs = [l for l in day_logs if l.get("type") == "check-out"]
            first_checkin = [l for l in day_logs if l.get("type") == "check-in"]

            first_in_ts = first_checkin[0]["timestamp"] if first_checkin else day_logs[0]["timestamp"]
            if first_in_ts.tzinfo is None: first_in_ts = first_in_ts.replace(tzinfo=timezone.utc)
            local_first_in = first_in_ts + timedelta(minutes=tz_offset)

            local_last_out = None
            if checkout_logs:
                last_ts = checkout_logs[-1]["timestamp"]
                if last_ts.tzinfo is None: last_ts = last_ts.replace(tzinfo=timezone.utc)
                local_last_out = last_ts + timedelta(minutes=tz_offset)

            classification = classify_attendance_day(
                first_checkin_local=local_first_in,
                last_checkout_local=local_last_out,
                worked_hours=duration_hours,
                settings=org_doc or {},
                employee_type=employee.get("employee_type", "desk"),
                approved_leave=matching_leave
            )

            day_info["status"] = classification["status"]
            day_info["is_late"] = classification["is_late"]
            day_info["is_early_leave"] = classification["is_early_leave"]
            day_info["is_short_leave"] = classification["is_short_leave"]
            day_info["is_half_day"] = classification["is_half_day"]
            day_info["short_leave_type"] = classification["short_leave_type"]
            day_info["half_day_type"] = classification["half_day_type"]
            day_info["late_mins"] = classification["late_mins"]
            day_info["early_exit_mins"] = classification["early_exit_mins"]
            day_info["late_off_hours"] = classification["late_off_hours"]
            day_info["early_off_hours"] = classification["early_off_hours"]
            day_info["off_hours"] = classification["off_hours"]
            day_info["required_hours"] = classification["required_hours"]
            day_info["effective_days"] = classification["effective_days"]
            day_info["classification_reason"] = classification["classification_reason"]
            day_info["is_applied_leave"] = classification["is_applied_leave"]

        else:
            # No attendance logs on this day
            if matching_leave:
                classification = classify_attendance_day(
                    first_checkin_local=None,
                    last_checkout_local=None,
                    worked_hours=0.0,
                    settings=org_doc or {},
                    employee_type=employee.get("employee_type", "desk"),
                    approved_leave=matching_leave
                )
                day_info["status"] = classification["status"]
                day_info["is_short_leave"] = classification["is_short_leave"]
                day_info["is_half_day"] = classification["is_half_day"]
                day_info["short_leave_type"] = classification["short_leave_type"]
                day_info["half_day_type"] = classification["half_day_type"]
                day_info["off_hours"] = classification["off_hours"]
                day_info["effective_days"] = classification["effective_days"]
                day_info["classification_reason"] = classification["classification_reason"]
                day_info["is_applied_leave"] = True
            elif is_pending_leave:
                day_info["status"] = pending_label
                day_info["classification_reason"] = "Pending Leave Approval"
            elif is_weekend_or_holiday:
                day_info["classification_reason"] = day_info["status"]
                day_info["off_hours"] = 0.0
                day_info["effective_days"] = 1.0 if day_info["status"] not in ["Weekend"] else 0.0
            else:
                day_info["status"] = "Absent"
                day_info["classification_reason"] = "Unexcused Absence"
                absent_days += 1
                req_h = float((org_doc or {}).get("required_hours", 8.0))
                day_info["off_hours"] = req_h
                day_info["required_hours"] = req_h

        daily_breakdown.append(day_info)
        current_day_local = next_day_local

    # Calculate holiday count from breakdown
    holiday_days_count = sum(1 for d in daily_breakdown if d.get("status") not in ["Present", "Late", "Early Leave", "Short Leave", "Half Day", "Absent", "Weekend"] and not str(d.get("status", "")).startswith("Leave") and not str(d.get("status", "")).startswith("Pending Leave"))
    
    # Calculate Short Leave & Half Day metrics
    short_leave_days = sum(1 for d in daily_breakdown if d.get("is_short_leave"))
    half_day_days = sum(1 for d in daily_breakdown if d.get("is_half_day"))
    total_off_hours = round(sum(d.get("off_hours", 0.0) for d in daily_breakdown if d.get("status") not in ["Weekend", "Holiday"] and not str(d.get("status", "")).startswith("Leave")), 2)
    total_late_off_hours = round(sum(d.get("late_off_hours", 0.0) for d in daily_breakdown), 2)
    total_early_off_hours = round(sum(d.get("early_off_hours", 0.0) for d in daily_breakdown), 2)

    # Live leave balance for this year and month (including unexcused absent days in total deduction)
    leave_balance = await calculate_leave_balance(email, org_id or "system_org", year, month, absent_days=absent_days)

    total_hours = round(total_working_ms / (1000 * 3600), 2)
    avg_hours = round(total_hours / present_days, 2) if present_days > 0 else 0

    return {
        "employee": {
            "full_name": employee.get("full_name"),
            "email": employee.get("email"),
            "designation": employee.get("designation"),
            "department": employee.get("department"),
            "employee_type": employee.get("employee_type")
        },
        "summary": {
            "total_working_hours": total_hours,
            "average_daily_hours": avg_hours,
            "present_days": present_days,
            "leaves_taken": leaves_count,
            "absent_days": absent_days,
            "short_leave_days": short_leave_days,
            "half_day_days": half_day_days,
            "total_off_hours": total_off_hours,
            "total_late_off_hours": total_late_off_hours,
            "total_early_off_hours": total_early_off_hours,
            "holiday_days": holiday_days_count,
            "total_days_evaluated": len(daily_breakdown),
            "leave_balance": leave_balance
        },
        "daily_breakdown": daily_breakdown
    }


